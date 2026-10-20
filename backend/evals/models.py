from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SUPPORTED_CATEGORIES = {
    "price_comparison", "recommendation", "regional_gap", "price_decay",
    "representative_listings", "unsupported_filters", "clarification", "out_of_domain",
}
SUPPORTED_ROLES = {"system", "user", "assistant", "tool"}
SUPPORTED_ACTIONS = {"user_input", "answer", "continue", "cancel", "fail"}


@dataclass(frozen=True)
class EvalTurn:
    role: str
    content: Any
    answers: list[dict[str, Any]] = field(default_factory=list)
    action: str = "user_input"


@dataclass(frozen=True)
class EvalCase:
    id: str
    category: str
    turns: tuple[EvalTurn, ...]
    expect: dict[str, Any]
    tags: frozenset[str] = frozenset()
    trajectory: tuple[dict[str, Any], ...] = ()
    trajectory_fixture: str | None = None


@dataclass(frozen=True)
class EvalResult:
    case_id: str
    passed: bool
    scores: dict[str, float]
    violations: tuple[str, ...]
    latency_ms: float | None = None
    model_rounds: int = 0
    token_usage: dict[str, int] = field(default_factory=dict)
    run: dict[str, Any] = field(default_factory=dict)
    category: str = "unknown"
    mode: str = "fixture"


def _turn(raw: Any, index: int) -> EvalTurn:
    if not isinstance(raw, dict):
        raise ValueError(f"turn[{index}] 必须是对象")
    role = str(raw.get("role") or "")
    if role not in SUPPORTED_ROLES:
        raise ValueError(f"turn[{index}] role 不受支持：{role}")
    if "content" not in raw:
        raise ValueError(f"turn[{index}] 缺少 content")
    answers = raw.get("answers") or []
    if not isinstance(answers, list) or any(not isinstance(item, dict) for item in answers):
        raise ValueError(f"turn[{index}] answers 必须是对象数组")
    action = str(raw.get("action") or ("user_input" if role == "user" else "continue"))
    if action not in SUPPORTED_ACTIONS:
        raise ValueError(f"turn[{index}] action 不受支持：{action}")
    if action == "answer" and not answers:
        raise ValueError(f"turn[{index}] answer 必须包含 answers")
    return EvalTurn(role, raw["content"], list(answers), action)


def parse_case(raw: Any, line_number: int = 0) -> EvalCase:
    if not isinstance(raw, dict):
        raise ValueError(f"case line {line_number} 必须是对象")
    case_id = str(raw.get("id") or "").strip()
    category = str(raw.get("category") or "").strip()
    if not case_id or not case_id.replace("-", "").replace("_", "").isalnum():
        raise ValueError(f"case line {line_number} id 非法")
    if category not in SUPPORTED_CATEGORIES:
        raise ValueError(f"case {case_id} category 不受支持：{category}")
    turns_raw = raw.get("turns")
    if not isinstance(turns_raw, list) or not turns_raw:
        raise ValueError(f"case {case_id} 至少需要一个 turn")
    turns = tuple(_turn(item, index) for index, item in enumerate(turns_raw))
    if not any(turn.role == "user" for turn in turns):
        raise ValueError(f"case {case_id} 至少需要一个 user turn")
    expect = raw.get("expect")
    if not isinstance(expect, dict) or not expect:
        raise ValueError(f"case {case_id} 缺少 expect")
    tags = raw.get("tags") or []
    if not isinstance(tags, list) or any(not isinstance(tag, str) or not tag.strip() for tag in tags):
        raise ValueError(f"case {case_id} tags 必须是非空字符串数组")
    trajectory = raw.get("trajectory") or []
    if not isinstance(trajectory, list) or any(not isinstance(item, dict) for item in trajectory):
        raise ValueError(f"case {case_id} trajectory 必须是对象数组")
    for index, item in enumerate(trajectory):
        if "content" not in item and not isinstance(item.get("tool_calls"), list):
            raise ValueError(f"case {case_id} trajectory[{index}] 必须包含 content 或 tool_calls")
    fixture = raw.get("trajectory_fixture")
    if fixture is not None and (not isinstance(fixture, str) or not fixture.endswith(".json") or "/" in fixture or "\\" in fixture):
        raise ValueError(f"case {case_id} trajectory_fixture 必须是同目录 JSON 文件名")
    return EvalCase(case_id, category, turns, dict(expect), frozenset(tags), tuple(trajectory), fixture)


def load_cases(path: Path, tags: set[str] | None = None) -> list[EvalCase]:
    cases: list[EvalCase] = []
    seen: set[str] = set()
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            case = parse_case(json.loads(line), line_number)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"无法加载评估 case 第 {line_number} 行：{exc}") from exc
        if case.id in seen:
            raise ValueError(f"重复评估 case id：{case.id}")
        seen.add(case.id)
        if tags is None or tags.intersection(case.tags):
            cases.append(case)
    return cases
