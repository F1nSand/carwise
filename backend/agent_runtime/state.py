"""原生 OpenAI 消息流状态：system/user/assistant/tool。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

CHECKPOINT_VERSION = 4

_ALLOWED_ROLES = {"user", "assistant", "tool", "system"}


class CheckpointMigrationRequired(ValueError):
    """Raised when an older or newer checkpoint cannot be resumed safely."""

    failure_code = "migration_required"


def _safe_value(value: Any) -> Any:
    """把状态转换成可 JSON 序列化的值，不让日志序列化失败。"""
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        if isinstance(value, dict):
            return {str(k): _safe_value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [_safe_value(item) for item in value]
        return repr(value)


@dataclass
class AgentState:
    user_text: str
    trace_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    run_logs: list[dict[str, Any]] = field(default_factory=list)
    flags: dict[str, Any] = field(
        default_factory=lambda: {"status": "initialized", "steps": 0}
    )
    final_message: dict[str, Any] = field(default_factory=dict)
    plan: list[dict[str, Any]] = field(default_factory=list)
    pending_input: dict[str, Any] | None = None
    answered_inputs: list[dict[str, Any]] = field(default_factory=list)
    runtime_status: str = "initialized"
    pending_tool_batch: dict[str, Any] | None = None
    run_budget: dict[str, Any] = field(default_factory=dict)
    task_context: dict[str, Any] = field(default_factory=dict)
    task_intent: dict[str, Any] = field(default_factory=dict)
    evidence_records: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def create(
        cls,
        user_text: str,
        system_prompt: str,
        history: list[dict[str, Any]] | None = None,
        append_user: bool = True,
    ) -> "AgentState":
        text = str(user_text or "").strip()
        state = cls(user_text=text, trace_id=uuid4().hex)
        state.append_message({"role": "system", "content": str(system_prompt or "")})
        for message in history or []:
            if message.get("role") == "system":
                raise ValueError("动态历史不能包含 system 消息")
            state.append_message(message)
        if append_user:
            state.append_message({"role": "user", "content": text})
            state.flags["current_run_start"] = len(state.messages) - 1
        else:
            current_user = next(
                (index for index in range(len(state.messages) - 1, 0, -1) if state.messages[index].get("role") == "user"),
                None,
            )
            if current_user is not None:
                state.flags["current_run_start"] = current_user
        return state

    @classmethod
    def from_checkpoint(cls, data: dict[str, Any]) -> "AgentState":
        """从服务层保存的 JSON checkpoint 恢复消息顺序和运行元数据。"""
        if not isinstance(data, dict):
            raise ValueError("Agent checkpoint 必须是对象")
        version = int(data.get("checkpoint_version") or 1)
        if version > CHECKPOINT_VERSION:
            raise CheckpointMigrationRequired(f"不支持的 checkpoint 版本：{version}")
        user_text = str(data.get("user_text") or "").strip()
        trace_id = str(data.get("trace_id") or "").strip()
        if not trace_id:
            raise ValueError("Agent checkpoint 缺少 trace_id")
        messages = data.get("messages") or []
        if not isinstance(messages, list) or not messages:
            raise ValueError("Agent checkpoint 缺少 messages")
        state = cls(
            user_text=user_text,
            trace_id=trace_id,
            flags=dict(data.get("flags") or {"status": "initialized", "steps": 0}),
            final_message=dict(data.get("final_message") or {}),
            plan=list(data.get("plan") or []),
            pending_input=(dict(data["pending_input"]) if isinstance(data.get("pending_input"), dict) else None),
            answered_inputs=list(data.get("answered_inputs") or []),
            runtime_status=str(data.get("runtime_status") or "initialized"),
            pending_tool_batch=(dict(data["pending_tool_batch"]) if isinstance(data.get("pending_tool_batch"), dict) else None),
            run_budget=dict(data.get("run_budget") or {}),
            task_context=dict(data.get("task_context") or {}),
            task_intent=dict(data.get("task_intent") or {}),
            evidence_records=list(data.get("evidence_records") or []),
        )
        for message in messages:
            state.append_message(message)
        state.tool_results = list(data.get("tool_results") or [])
        state.run_logs = list(data.get("run_logs") or [])
        if not state.task_intent:
            from backend.agent_runtime.task_context import legacy_task_intent

            state.task_intent = legacy_task_intent(state.user_text, state.plan)
        if version < CHECKPOINT_VERSION and state.plan:
            state._migrate_legacy_plan()
        if version < 2 and state.pending_tool_batch is None:
            try:
                state.pending_tool_batch = cls._derive_pending_batch(state)
            except ValueError as exc:
                raise CheckpointMigrationRequired(
                    f"旧 checkpoint 无法安全迁移：{exc}"
                ) from exc
        try:
            state._validate_pending_batch()
        except ValueError as exc:
            if version < CHECKPOINT_VERSION:
                raise CheckpointMigrationRequired(
                    f"旧 checkpoint 无法安全迁移：{exc}"
                ) from exc
            raise
        return state

    def _migrate_legacy_plan(self) -> None:
        """Upgrade a resumable legacy plan without erasing completed work."""
        from backend.agent_runtime.control_tools import normalize_plan
        from backend.agent_runtime.task_context import validate_plan_goal_coverage

        if not isinstance(self.plan, list) or len(self.plan) > 8:
            raise CheckpointMigrationRequired("旧 checkpoint 计划无法安全迁移：步骤数量无效")
        goal_ids = [
            str(goal.get("id"))
            for goal in self.task_intent.get("goals", [])
            if isinstance(goal, dict) and goal.get("id")
        ]
        if not goal_ids:
            raise CheckpointMigrationRequired("旧 checkpoint 计划缺少可恢复的任务目标")

        definitions: list[dict[str, Any]] = []
        old_steps: list[dict[str, Any]] = []
        for index, raw in enumerate(self.plan):
            if not isinstance(raw, dict):
                raise CheckpointMigrationRequired("旧 checkpoint 计划包含非法步骤")
            old_steps.append(raw)
            definitions.append({
                "id": raw.get("id") or f"legacy_step_{index + 1}",
                "title": raw.get("title") or f"继续第 {index + 1} 步",
                "description": raw.get("description") or "根据已保存的任务状态继续执行",
                "tool_names": raw.get("tool_names") if isinstance(raw.get("tool_names"), list) else [],
                "goal_ids": raw.get("goal_ids") if isinstance(raw.get("goal_ids"), list) and raw.get("goal_ids") else goal_ids,
                **({"kind": raw["kind"]} if raw.get("kind") else {}),
                **({"completion_criteria": raw["completion_criteria"]} if raw.get("completion_criteria") else {}),
            })

        has_finalize = any(
            "finalize_answer" in (step.get("tool_names") or []) or step.get("kind") == "finalize"
            for step in definitions
        )
        if not has_finalize:
            if len(definitions) >= 8:
                raise CheckpointMigrationRequired("旧 checkpoint 计划已达上限，无法安全追加 finalize 阶段")
            finalize_id = "runtime_migration_finalize"
            existing_ids = {str(step.get("id")) for step in definitions}
            suffix = 1
            while finalize_id in existing_ids:
                finalize_id = f"runtime_migration_finalize_{suffix}"
                suffix += 1
            definitions.append({
                "id": finalize_id,
                "title": "整理并核验回答",
                "description": "保留已完成步骤，根据当前有效证据提交最终回答",
                "tool_names": ["finalize_answer"],
                "goal_ids": goal_ids,
                "kind": "finalize",
                "completion_criteria": "validated_answer",
            })

        try:
            migrated = normalize_plan(definitions)
            for index, old in enumerate(old_steps):
                status = str(old.get("status") or "queued")
                if status not in {"queued", "running", "complete", "skipped"}:
                    raise ValueError(f"旧步骤状态无效：{status}")
                migrated[index]["status"] = status
                if old.get("reason"):
                    migrated[index]["reason"] = str(old["reason"])[:500]
            validate_plan_goal_coverage(migrated, self.task_intent)
        except (TypeError, ValueError) as exc:
            raise CheckpointMigrationRequired(f"旧 checkpoint 计划无法安全迁移：{exc}") from exc

        finalize = migrated[-1]
        prior_settled = all(step["status"] in {"complete", "skipped"} for step in migrated[:-1])
        answer_saved = bool(self.flags.get("answer_finalized") and str(self.flags.get("validated_answer") or "").strip())
        if prior_settled and not self.pending_input and self.pending_tool_batch is None:
            finalize["status"] = "running"
        else:
            finalize["status"] = "queued"
        if answer_saved:
            finalize["status"] = "running"
        self.plan = migrated
        revision = self.flags.get("plan_revision")
        self.flags["plan_revision"] = (max(0, revision) if isinstance(revision, int) and not isinstance(revision, bool) else 0) + 1
        self.flags["plan_change_reason"] = "runtime_migration"

    def _validate_pending_batch(self) -> None:
        batch = self.pending_tool_batch
        if batch is None:
            if self.pending_input is not None:
                raise ValueError("checkpoint 存在待回答问题但缺少工具批次")
            return
        if not isinstance(batch, dict):
            raise ValueError("pending_tool_batch 必须是对象")
        assistant_index = batch.get("assistant_message_index")
        calls = batch.get("calls")
        next_index = batch.get("next_index")
        completed = batch.get("completed_call_ids")
        if (
            isinstance(assistant_index, bool)
            or not isinstance(assistant_index, int)
            or assistant_index < 0
            or assistant_index >= len(self.messages)
            or self.messages[assistant_index].get("role") != "assistant"
            or not isinstance(calls, list)
            or not calls
            or isinstance(next_index, bool)
            or not isinstance(next_index, int)
            or not 0 <= next_index <= len(calls)
            or not isinstance(completed, list)
        ):
            raise ValueError("pending_tool_batch 结构无效")
        call_ids = [str(call.get("id") or "") if isinstance(call, dict) else "" for call in calls]
        if any(not call_id for call_id in call_ids) or len(set(call_ids)) != len(call_ids):
            raise ValueError("pending_tool_batch 调用 ID 必须非空且唯一")
        assistant_calls = self.messages[assistant_index].get("tool_calls") or []
        message_ids = [str(call.get("id") or "") for call in assistant_calls if isinstance(call, dict)]
        if message_ids != call_ids:
            raise ValueError("pending_tool_batch 与 assistant 工具调用不一致")
        if [str(call_id) for call_id in completed] != call_ids[:next_index]:
            raise ValueError("pending_tool_batch 完成列表与游标不一致")
        following_tool_ids = [
            str(message.get("tool_call_id") or "")
            for message in self.messages[assistant_index + 1:]
            if message.get("role") == "tool"
        ]
        if following_tool_ids != call_ids[:next_index]:
            raise ValueError("pending_tool_batch 游标与 tool 消息闭合状态不一致")
        if self.pending_input is not None:
            pending_id = str(self.pending_input.get("tool_call_id") or "")
            pending_index = self.pending_input.get("batch_index")
            if pending_index is None and pending_id in call_ids:
                pending_index = call_ids.index(pending_id)
                self.pending_input["batch_index"] = pending_index
            if (
                not pending_id
                or isinstance(pending_index, bool)
                or not isinstance(pending_index, int)
                or pending_index != next_index
                or pending_index >= len(call_ids)
                or call_ids[pending_index] != pending_id
            ):
                raise ValueError("pending_input 与工具批次游标不一致")

    @staticmethod
    def _derive_pending_batch(state: "AgentState") -> dict[str, Any] | None:
        assistant_index = next(
            (index for index in range(len(state.messages) - 1, -1, -1)
             if state.messages[index].get("role") == "assistant" and state.messages[index].get("tool_calls")),
            None,
        )
        if assistant_index is None:
            if state.pending_input:
                raise ValueError("旧 checkpoint 缺少 ask_user 工具调用，无法安全恢复")
            return None
        calls = state.messages[assistant_index].get("tool_calls") or []
        call_ids = [str(call.get("id") or "") for call in calls]
        if any(not value for value in call_ids) or len(set(call_ids)) != len(call_ids):
            raise ValueError("旧 checkpoint 工具调用 ID 无法唯一推导，拒绝恢复")
        completed: set[str] = set()
        for message in state.messages[assistant_index + 1:]:
            if message.get("role") == "tool":
                call_id = str(message.get("tool_call_id") or "")
                if call_id not in call_ids or call_id in completed:
                    raise ValueError("旧 checkpoint 工具结果无法唯一匹配，拒绝恢复")
                completed.add(call_id)
            elif len(completed) < len(call_ids):
                raise ValueError("旧 checkpoint 的工具消息链不完整，拒绝恢复")
        pending_ids = [value for value in call_ids if value not in completed]
        if not pending_ids:
            if state.pending_input:
                raise ValueError("旧 checkpoint 存在待回答问题但 ask_user 调用已关闭")
            return None
        next_index = next(index for index, value in enumerate(call_ids) if value not in completed)
        if state.pending_input:
            pending_id = str(state.pending_input.get("tool_call_id") or "")
            if pending_id not in pending_ids:
                raise ValueError("旧 checkpoint 待回答问题不能匹配未完成批次")
            state.pending_input.setdefault("batch_index", call_ids.index(pending_id))
        return {
            "batch_id": f"legacy-{assistant_index}",
            "assistant_message_index": assistant_index,
            "calls": calls,
            "next_index": next_index,
            "completed_call_ids": [value for value in call_ids if value in completed],
            "step": int(state.flags.get("steps") or 1),
        }

    def dynamic_messages(self) -> list[dict[str, Any]]:
        """返回不含静态 system prompt 的动态消息快照。"""
        return list(self.messages[1:])

    def append_message(self, message: dict[str, Any]) -> None:
        if not isinstance(message, dict):
            raise ValueError("消息必须是对象")
        role = message.get("role")
        if role not in _ALLOWED_ROLES:
            raise ValueError(f"不支持的消息角色：{role}")

        normalized = {"role": role, "content": _safe_value(message.get("content", ""))}
        if role == "assistant" and "reasoning_content" in message:
            normalized["reasoning_content"] = _safe_value(message["reasoning_content"])
        if role == "assistant" and message.get("tool_calls"):
            normalized["tool_calls"] = _safe_value(message["tool_calls"])
        if role == "tool":
            if not message.get("tool_call_id"):
                raise ValueError("tool 消息必须包含 tool_call_id")
            normalized["tool_call_id"] = str(message["tool_call_id"])
            normalized["name"] = str(message.get("name") or "")
        self.messages.append(normalized)

    def add_tool_result(
        self,
        tool_call_id: str,
        tool_name: str,
        args: dict[str, Any],
        output: dict[str, Any],
        status: str,
        duration_ms: int,
    ) -> None:
        result = {
            "tool_call_id": str(tool_call_id),
            "tool_name": str(tool_name),
            "input": _safe_value(args or {}),
            "output": _safe_value(output),
            "status": str(status),
            "duration_ms": max(0, int(duration_ms)),
        }
        self.tool_results.append(result)
        self.append_message(
            {
                "role": "tool",
                "tool_call_id": str(tool_call_id),
                "name": result["tool_name"],
                "content": json.dumps(result["output"], ensure_ascii=False, default=str),
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return self.to_checkpoint()

    def to_checkpoint(self) -> dict[str, Any]:
        return _safe_value(
            {
                "user_text": self.user_text,
                "trace_id": self.trace_id,
                "messages": self.messages,
                "tool_results": self.tool_results,
                "run_logs": self.run_logs,
                "flags": self.flags,
                "final_message": self.final_message,
                "plan": self.plan,
                "pending_input": self.pending_input,
                "answered_inputs": self.answered_inputs,
                "runtime_status": self.runtime_status,
                "checkpoint_version": CHECKPOINT_VERSION,
                "pending_tool_batch": self.pending_tool_batch,
                "run_budget": self.run_budget,
                "task_context": self.task_context,
                "task_intent": self.task_intent,
                "evidence_records": self.evidence_records,
            }
        )
