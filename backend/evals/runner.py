from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
import time
import threading
import tempfile
from types import SimpleNamespace
from pathlib import Path
from typing import Any, Callable, Literal
from uuid import uuid4
from unittest.mock import patch

from backend.agent_runtime.loop import run_agent

from .models import EvalCase, EvalResult, load_cases
from .reporting import compare_baseline, write_report
from .scorers import score_run

TRAJECTORY_PROTOCOL_VERSION = 2


def _load_trajectory(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("version") != TRAJECTORY_PROTOCOL_VERSION:
        raise ValueError("trajectory protocol version must be 2 (explicit plan / answer schema v2)")
    if value.get("protocol") != {"task_plan": 1, "answer_schema": 2}:
        raise ValueError("trajectory protocol metadata does not match the runtime contract")
    responses = value.get("responses")
    if not isinstance(responses, list) or any(not isinstance(item, dict) for item in responses):
        raise ValueError("trajectory responses must be an array of objects")
    return responses


def _fixture(case: EvalCase) -> dict[str, Any] | None:
    path = Path("evals/replays/v1") / f"{case.id}.json"
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else None


def _trajectory_path(filename: str) -> Path | None:
    """Resolve only repository-owned trajectory directories by basename."""
    name = Path(filename).name
    if name != filename or not name.endswith(".json"):
        return None
    for directory in ("v1", "reliability"):
        path = Path("evals/trajectories") / directory / name
        if path.is_file():
            return path
    return None


class _TrajectoryProvider:
    """Offline OpenAI-shaped provider that supplies scripted model decisions."""

    def __init__(self, responses: list[dict[str, Any]]):
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.response_round = 0
        self.available = True
        self.reason = "offline trajectory provider"

    def is_available(self) -> bool:
        return True

    @staticmethod
    def _evidence_from_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for message in reversed(messages):
            if message.get("role") != "tool":
                continue
            try:
                content = json.loads(str(message.get("content") or ""))
            except (TypeError, ValueError):
                continue
            evidence = content.get("evidence") if isinstance(content, dict) else None
            if isinstance(evidence, list):
                records.extend(item for item in evidence if isinstance(item, dict))
        return records

    @classmethod
    def _resolve_evidence_templates(cls, value: Any, evidence: list[dict[str, Any]]) -> Any:
        if isinstance(value, list):
            return [cls._resolve_evidence_templates(item, evidence) for item in value]
        if not isinstance(value, dict):
            return value
        evidence_ref = value.get("$evidence_ref")
        if isinstance(evidence_ref, dict):
            metric = str(evidence_ref.get("metric") or "first")
            entity_index = evidence_ref.get("entity_index")
            record = next(
                (
                    item for item in evidence
                    if (metric == "first" or str(item.get("metric")) == metric)
                    and (entity_index is None or (item.get("entity_group_id") is not None
                         and str(item.get("metric", "")).startswith(f"recommendations.{entity_index}.")))
                ),
                None,
            )
            if record is None:
                raise RuntimeError(f"trajectory evidence ref has no matching record: {metric}")
            return record.get("evidence_id")
        entity_group_refs = value.get("$entity_group_refs")
        if isinstance(entity_group_refs, list):
            refs: list[str] = []
            for selector in entity_group_refs:
                if isinstance(selector, int):
                    prefix = f"recommendations.{selector}."
                    record = next((item for item in evidence if str(item.get("metric", "")).startswith(prefix)
                                   and item.get("entity_group_id")), None)
                else:
                    metric = str(selector)
                    record = next((item for item in evidence if str(item.get("metric")) == metric
                                   and item.get("entity_group_id")), None)
                if record is None:
                    raise RuntimeError(f"trajectory entity ref has no matching record: {selector}")
                entity_id = str(record["entity_group_id"])
                if entity_id not in refs:
                    refs.append(entity_id)
            return refs
        marker = value.get("$evidence_claim")
        if isinstance(marker, dict):
            metric = str(marker.get("metric") or "first")
            record = next(
                (item for item in evidence if metric == "first" or str(item.get("metric")) == metric),
                None,
            )
            if record is None:
                raise RuntimeError(f"trajectory evidence claim has no matching record: {metric}")
            claim = {
                "claim_id": str(marker.get("claim_id") or "result"),
                "evidence_id": record.get("evidence_id"),
                "metric": record.get("metric"),
                "value": record.get("value"),
                "unit": record.get("unit"),
                "label": str(marker.get("label") or "查询指标"),
            }
            if record.get("scope") is not None:
                claim["scope"] = record["scope"]
            if isinstance(record.get("filters"), list):
                claim["filters"] = record["filters"]
            return claim
        return {
            key: cls._resolve_evidence_templates(item, evidence)
            for key, item in value.items()
        }

    def complete(self, **request: Any) -> Any:
        self.requests.append(request)
        if not self.responses:
            raise RuntimeError("trajectory exhausted before Agent finished")
        item = self.responses.pop(0)
        response_round = self.response_round
        self.response_round += 1
        if item.get("failure"):
            raise RuntimeError(str(item["failure"]))
        evidence = self._evidence_from_messages(request.get("messages") or [])
        calls = [SimpleNamespace(
            # Model tool-call IDs are unique within a response, not across
            # responses. Prefix fixtures so repeated placeholder IDs cannot
            # make a later, legitimate call look like a replay.
            id=f"trajectory-{response_round}-{call.get('id') or index}",
            type="function",
            function=SimpleNamespace(
                name=call["name"],
                arguments=json.dumps(
                    self._resolve_evidence_templates(call.get("arguments") or {}, evidence),
                    ensure_ascii=False,
                ) if isinstance(call.get("arguments"), dict) else str(call.get("arguments") or "{}"),
            ),
        ) for index, call in enumerate(item.get("tool_calls") or [])]
        message = SimpleNamespace(content=item.get("content"), tool_calls=calls, reasoning_content=None)
        usage = item.get("usage") or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=SimpleNamespace(**usage))


def _drive_case(case: EvalCase, run_id: str, *, offline: bool) -> dict[str, Any]:
    provider: _TrajectoryProvider | None = None
    if offline:
        responses = list(case.trajectory)
        if case.trajectory_fixture:
            path = _trajectory_path(case.trajectory_fixture)
            try:
                if path is None:
                    raise ValueError("trajectory fixture was not found")
                responses = _load_trajectory(path)
            except (OSError, ValueError) as exc:
                return {"status": "error", "summary": f"trajectory fixture invalid: {exc}"}
        if not responses:
            return {"status": "error", "summary": "trajectory case lacks scripted model responses"}
        provider = _TrajectoryProvider(responses)
    from backend.agent_runtime.session_store import SessionStore
    from backend.agent_runtime import loop

    store_dir = tempfile.TemporaryDirectory(prefix="agent-eval-")
    store = SessionStore(root=Path(store_dir.name))
    result: dict[str, Any] | None = None
    all_events: list[dict[str, Any]] = []
    session_id = f"eval-session-{uuid4().hex}"
    turns = list(case.turns)
    actions = [turn for turn in turns if turn.role == "user"]
    if not actions:
        return {"status": "error", "summary": "trajectory case has no user action"}
    patch_context = patch.object(loop, "get_client", lambda: provider) if provider is not None else nullcontext()
    previous_results: list[dict[str, Any]] = []
    with patch_context:
        for index, turn in enumerate(actions):
            action = turn.action
            text = str(turn.content or "")
            if action in {"user_input", "continue"}:
                cancel = threading.Event()
                if action == "cancel":
                    cancel.set()
                result = loop.run_agent(text, run_id=f"{run_id}-{index}", session_id=session_id, session_store=store, cancellation=cancel)
            elif action == "answer":
                if not result or result.get("status") != "waiting_user":
                    return {"status": "error", "summary": "unexpected answer action: Agent is not waiting_user"}
                result = loop.run_agent(text, run_id=f"{run_id}-{index}", session_id=session_id, session_store=store, resume_checkpoint=result["checkpoint"], input_answers=turn.answers)
            elif action == "cancel":
                cancel = threading.Event()
                cancel.set()
                result = loop.run_agent(text, run_id=f"{run_id}-{index}", session_id=session_id, session_store=store, cancellation=cancel)
            elif action == "fail":
                # The corresponding trajectory response injects a provider failure.
                result = loop.run_agent(text, run_id=f"{run_id}-{index}", session_id=session_id, session_store=store)
            else:
                return {"status": "error", "summary": f"unsupported trajectory action: {action}"}
            trace = result.get("trace") if isinstance(result, dict) else None
            if isinstance(trace, dict):
                all_events.extend(item for item in trace.get("events", []) if isinstance(item, dict))
            previous_results.append(result)
            if result.get("status") == "waiting_user" and not any(item.action == "answer" for item in actions[index + 1:]):
                break
            if result.get("status") in {"done", "error"} and index + 1 < len(actions):
                continue
        if result is None:
            return {"status": "error", "summary": "trajectory produced no run"}
    result = dict(result)
    result["trace"] = {**(result.get("trace") or {}), "events": all_events}
    result["model_rounds"] = len(provider.requests) if provider is not None else sum(event.get("event_type") == "model_round" for event in all_events)
    if provider is not None:
        result["token_usage"] = result.get("state", {}).get("flags", {}).get("token_usage", {})
        result["trajectory_remaining"] = len(provider.responses)
        result["model_request_messages"] = [[{"role": message.get("role"), "content": str(message.get("content") or ""), "tool_calls": message.get("tool_calls")} for message in request.get("messages", [])] for request in provider.requests]
    else:
        result["token_usage"] = {key: sum(int((item.get("state", {}).get("flags", {}).get("token_usage", {}) or {}).get(key) or 0) for item in previous_results) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
    store_dir.cleanup()
    return result


def _trajectory_run(case: EvalCase, run_id: str) -> dict[str, Any]:
    return _drive_case(case, run_id, offline=True)


def evaluate_case(
    case: EvalCase,
    *,
    mode: Literal["replay", "fixture", "trajectory", "live"],
    repetitions: int = 1,
    runner: Callable[..., dict[str, Any]] | None = None,
) -> list[EvalResult]:
    if mode not in {"replay", "fixture", "trajectory", "live"}:
        raise ValueError("mode 必须是 fixture、trajectory 或 live")
    if repetitions <= 0:
        raise ValueError("repetitions 必须为正数")
    results: list[EvalResult] = []
    for _ in range(repetitions):
        run_id = f"eval-{case.id}-{uuid4().hex}"
        started = time.perf_counter()
        if mode in {"replay", "fixture"}:
            run = _fixture(case)
            if run is None:
                if mode == "fixture" or runner is None:
                    run = {"status": "error", "summary": "缺少 replay fixture"}
                else:
                    run = runner(case.turns[0].content, run_id=run_id, session_id=None)
        elif mode == "trajectory":
            run = _trajectory_run(case, run_id)
        else:
            run = runner(case.turns[0].content, run_id=run_id, session_id=None) if runner else _drive_case(case, run_id, offline=False)
        if not isinstance(run, dict):
            run = {"status": "error", "summary": "runner 返回格式非法"}
        run["evaluation_mode"] = ("fixture" if mode == "replay" else mode)
        run = {**run, "latency_ms": (time.perf_counter() - started) * 1000}
        results.append(score_run(case, run))
    return results


def _main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic Agent evaluations")
    parser.add_argument("--suite", choices=["smoke", "full", "reliability"], default="smoke")
    parser.add_argument("--mode", choices=["fixture", "trajectory", "live", "replay"], default="trajectory")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--output", type=Path, default=Path("artifacts/evals"))
    parser.add_argument("--compare", type=Path)
    args = parser.parse_args()
    if args.compare:
        baseline = json.loads(args.compare.read_text(encoding="utf-8"))
        candidate = json.loads((args.output / "summary.json").read_text(encoding="utf-8"))
        violations = compare_baseline(baseline, candidate)
        print(json.dumps({"violations": violations}, ensure_ascii=False))
        return 1 if violations else 0
    tags = {args.suite} if args.suite in {"smoke", "reliability"} else None
    cases = load_cases(Path("evals/cases/v1.jsonl"), tags=tags)
    selected_mode = "fixture" if args.mode == "replay" else args.mode
    results = [result for case in cases for result in evaluate_case(case, mode=selected_mode, repetitions=args.repetitions)]
    summary = write_report(results, args.output)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["pass_rate"] >= 0.95 else 1


if __name__ == "__main__":
    raise SystemExit(_main())
