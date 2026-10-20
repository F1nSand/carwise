"""Native OpenAI-compatible tool-call loop tests."""

import json
import copy
import re
import threading
import time
from types import SimpleNamespace

import pytest

import backend.agent_runtime.loop as loop
import backend.agent_runtime.nodes as nodes
from backend.agent_runtime.tool_registry import tool_schemas
from backend.agent_runtime.session_store import SessionStore
from backend.agent_runtime.state import AgentState
from backend.agent_runtime.sqlite_repository import SQLiteRuntimeRepository, SQLiteSessionStore
from backend.agent_runtime.mcp import MCPManager, MCPToolDefinition
from backend.agent_runtime.control_tools import normalize_questions, question_fingerprint
from backend.agent_runtime.task_context import initial_task_context, normalize_task_intent
from backend.agent_runtime.trace import RunTrace
from backend.agent_runtime.execution import ExecutionContext
from backend.agent_runtime.prefix import PrefixContext


def _message(content="", tool_calls=None, reasoning_content=None):
    calls = []
    for call in tool_calls or []:
        calls.append(
            SimpleNamespace(
                id=call["id"],
                type="function",
                function=SimpleNamespace(
                    name=call["name"],
                    arguments=call.get("arguments", "{}"),
                ),
            )
        )
    return SimpleNamespace(
        content=content,
        tool_calls=calls,
        reasoning_content=reasoning_content,
    )


def _response(message, usage=None, finish_reason=None, response_id=None):
    return SimpleNamespace(
        id=response_id,
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=SimpleNamespace(**(usage or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})),
    )


class FakeClient:
    def __init__(self, responses, available=True, auto_declare=True, auto_data=True):
        self.responses = list(responses)
        self.available = available
        self.reason = "fake offline"
        self.requests = []
        self.auto_declare = auto_declare
        self.auto_data = auto_data
        self.default_data_pending = False
        self.plan_step_ids = []
        self.plan_tool_names = []

    def is_available(self):
        return self.available

    def complete(self, **kwargs):
        self.requests.append(kwargs)
        response = self.responses.pop(0)
        messages = kwargs.get("messages") or []
        has_runtime_task_summary = any(
            message.get("role") == "system"
            and "[TASK_CONTEXT_SUMMARY]" in str(message.get("content") or "")
            for message in messages
        )
        if not self.auto_declare or not has_runtime_task_summary:
            return response
        has_declared_task_contract = any(
            message.get("role") == "system"
            and "[TASK_CONTEXT_SUMMARY]" in str(message.get("content") or "")
            and "已确认的任务合同" in str(message.get("content") or "")
            for message in messages
        )
        summary = next((str(message.get("content") or "") for message in messages
                        if message.get("role") == "system"
                        and "[TASK_CONTEXT_SUMMARY]" in str(message.get("content") or "")), "")
        if self.default_data_pending and any(
            line.strip().startswith("- [running]") and "澄清" not in line
            for line in summary.splitlines()
        ):
            self.default_data_pending = False
            self.responses.insert(0, response)
            return _response(_message(tool_calls=[{"id": f"test-default-data-{len(self.requests)}",
                "name": "csv_query", "arguments": "{}"}]))
        if has_declared_task_contract:
            if "尚未发布计划" in summary:
                if self._calls_named(response, "set_plan"):
                    self._modernize_explicit_plan(response)
                    return self._prepend_declaration(response, len(self.requests))
                self.responses.insert(0, response)
                return self._set_plan_response(len(self.requests), response)
            return self._modernize_final_response(response, summary)
        # Older loop fixtures predate the explicit task declaration tool. Once
        # a conversation already contains that tool call, preserve its scripted
        # provider response instead of injecting another declaration forever.
        declared_in_history = any(
            getattr(getattr(call, "function", None), "name", "") == "declare_task"
            for message in messages
            if isinstance(message, dict) and message.get("role") == "assistant"
            for call in (message.get("tool_calls") or [])
        )
        if declared_in_history:
            return response
        choices = getattr(response, "choices", []) or []
        if not choices:
            return response
        if str(getattr(choices[0], "finish_reason", "") or "") == "length":
            return response
        message = getattr(choices[0], "message", None)
        tool_calls = list(getattr(message, "tool_calls", []) or [])
        if any(getattr(getattr(call, "function", None), "name", "") == "declare_task" for call in tool_calls):
            return response

        goal_ids = []
        for call in tool_calls:
            function = getattr(call, "function", None)
            if getattr(function, "name", "") != "set_plan":
                continue
            try:
                arguments = json.loads(str(getattr(function, "arguments", "{}") or "{}"))
            except ValueError:
                continue
            steps = arguments.get("steps") if isinstance(arguments, dict) else []
            for plan_step in steps or []:
                if isinstance(plan_step, dict):
                    raw_ids = plan_step.get("goal_ids")
                    if isinstance(raw_ids, list):
                        goal_ids.extend(str(value) for value in raw_ids if str(value) not in goal_ids)
            if not goal_ids:
                goal_ids = ["test_goal"]
                for plan_step in steps or []:
                    if not isinstance(plan_step, dict):
                        continue
                    plan_step["goal_ids"] = ["test_goal"]
                function.arguments = json.dumps(arguments, ensure_ascii=False)
        if not goal_ids:
            goal_ids = ["test_goal"]
        declaration = SimpleNamespace(
            id=f"test-declare-{len(self.requests)}",
            type="function",
            function=SimpleNamespace(
                name="declare_task",
                arguments=json.dumps({
                    "goals": [{
                        "id": goal_id,
                        "description": "测试任务中声明的交付目标",
                        "deliverable": "可验证的测试结果",
                    } for goal_id in goal_ids],
                    "requires_plan": len(goal_ids) > 1,
                }, ensure_ascii=False),
            ),
        )
        if tool_calls:
            if self._calls_named(response, "set_plan"):
                self._modernize_explicit_plan(response)
                message.tool_calls = [declaration, *tool_calls]
                return response
            plan_response = self._set_plan_response(len(self.requests), response)
            plan_calls = plan_response.choices[0].message.tool_calls
            advanced_calls = self._insert_plan_advances(tool_calls)
            message.tool_calls = [_message(tool_calls=[{
                "id": declaration.id, "name": "declare_task",
                "arguments": declaration.function.arguments,
            }]).tool_calls[0], *plan_calls, *advanced_calls]
            return response

        self.responses.insert(0, response)
        return _response(_message(tool_calls=[{
            "id": declaration.id,
            "name": "declare_task",
            "arguments": declaration.function.arguments,
        }]))

    @staticmethod
    def _calls_named(response, name):
        choices = getattr(response, "choices", []) or []
        message = getattr(choices[0], "message", None) if choices else None
        return any(getattr(getattr(call, "function", None), "name", "") == name
                   for call in (getattr(message, "tool_calls", []) or []))

    @staticmethod
    def _prepend_declaration(response, ordinal):
        choices = getattr(response, "choices", []) or []
        if not choices:
            return response
        message = choices[0].message
        calls = list(getattr(message, "tool_calls", []) or [])
        goal_ids = []
        for call in calls:
            function = getattr(call, "function", None)
            if getattr(function, "name", "") != "set_plan":
                continue
            try:
                plan = json.loads(function.arguments or "{}")
            except (ValueError, TypeError):
                continue
            for step in plan.get("steps", []) if isinstance(plan, dict) else []:
                for goal in step.get("goal_ids", []) if isinstance(step, dict) else []:
                    if goal not in goal_ids:
                        goal_ids.append(goal)
        declaration = {
            "id": f"test-declare-{ordinal}", "name": "declare_task",
            "arguments": json.dumps({"goals": [{"id": goal or "test_goal",
                "description": "测试任务中声明的交付目标", "deliverable": "可验证的测试结果"}
                for goal in (goal_ids or ["test_goal"])], "requires_plan": True}, ensure_ascii=False),
        }
        # Replace the prior declaration if this response was already decorated.
        calls = [call for call in calls
                 if getattr(getattr(call, "function", None), "name", "") != "declare_task"]
        message.tool_calls = [
            _message(tool_calls=[declaration]).tool_calls[0], *calls,
        ]
        return response

    def _planned_tools(self, current_response=None):
        names = []
        known_tools = {item["function"]["name"] for item in tool_schemas()}
        responses = ([current_response] if current_response is not None else []) + self.responses
        for response in responses:
            choices = getattr(response, "choices", []) or []
            message = getattr(choices[0], "message", None) if choices else None
            for call in getattr(message, "tool_calls", []) or []:
                name = getattr(getattr(call, "function", None), "name", "")
                if (name and name not in {"declare_task", "set_plan", "update_plan", "finalize_answer"}
                        and (name in known_tools or name.startswith("mcp__"))):
                    names.append(name)
        return names

    def _set_plan_response(self, ordinal, current_response=None):
        names = self._planned_tools(current_response)
        if any(name.startswith("mcp__") for name in names) and "csv_query" not in names:
            names.append("csv_query")
        if not names and self.auto_data:
            names = ["csv_query"]
        steps = []
        index = 0
        while index < len(names):
            name = names[index]
            if (name == "ask_user" and steps and steps[-1]["kind"] == "data"
                    and index + 1 < len(names) and names[index + 1] == steps[-1]["tool_names"][0]):
                steps[-1]["tool_names"].append("ask_user")
                # The following repeated data tool is the bounded retry after
                # the user decision, still within the same data stage.
                index += 3
                continue
            shared_analysis_tools = {"price_distribution", "mileage_price_correlation", "model_comparison", "brand_profile", "age_price_relation"}
            data_tool = name in {"csv_query", "recommend", "price_compare", "price_decay", "region_gap"} | shared_analysis_tools or name.startswith("mcp__")
            if data_tool:
                data_names = [name]
                index += 1
                while index < len(names):
                    next_name = names[index]
                    next_is_data = next_name in {"csv_query", "recommend", "price_compare", "price_decay", "region_gap"} | shared_analysis_tools or next_name.startswith("mcp__")
                    if not next_is_data:
                        break
                    if next_name not in data_names:
                        data_names.append(next_name)
                    index += 1
                steps.append({"id": f"test-step-{len(steps) + 1}",
                    "title": "执行工具并收集结果", "description": "按测试脚本完成数据查询阶段",
                    "tool_names": data_names, "goal_ids": ["test_goal"], "kind": "data",
                    "completion_criteria": "tool_result"})
                continue
            clarification = name == "ask_user"
            preparation = name == "load_skill"
            steps.append({"id": f"test-step-{len(steps) + 1}",
                "title": "澄清用户条件" if clarification else ("准备测试上下文" if preparation else "执行工具并收集结果"),
                "description": "按测试脚本完成对应阶段",
                "tool_names": [name], "goal_ids": ["test_goal"],
                "kind": "clarification" if clarification else ("data" if data_tool else "preparation"),
                "completion_criteria": "user_answer" if clarification else "tool_result"})
            index += 1
        steps.append({"id": "test-finalize", "title": "核验并整理答案",
            "description": "提交结构化最终答案", "tool_names": ["finalize_answer"],
            "goal_ids": ["test_goal"], "kind": "finalize",
            "completion_criteria": "validated_answer"})
        self.plan_step_ids = [step["id"] for step in steps]
        self.plan_tool_names = [list(step["tool_names"]) for step in steps]
        calls = [{"id": f"test-plan-{ordinal}", "name": "set_plan",
            "arguments": json.dumps({"steps": steps}, ensure_ascii=False)}]
        if names == ["csv_query"] and not self._planned_tools(current_response):
            self.default_data_pending = True
        return _response(_message(tool_calls=calls))

    def _modernize_explicit_plan(self, response):
        choices = getattr(response, "choices", []) or []
        message = getattr(choices[0], "message", None) if choices else None
        for call in getattr(message, "tool_calls", []) or []:
            function = getattr(call, "function", None)
            if getattr(function, "name", "") != "set_plan":
                continue
            try:
                args = json.loads(function.arguments or "{}")
            except (ValueError, TypeError):
                continue
            steps = args.get("steps") if isinstance(args, dict) else None
            if not isinstance(steps, list):
                continue
            goal_ids = []
            for step in steps:
                if not isinstance(step, dict):
                    continue
                if not step.get("goal_ids"):
                    step["goal_ids"] = ["test_goal"]
                goal_ids.extend(goal for goal in step["goal_ids"] if goal not in goal_ids)
                names = step.get("tool_names") or []
                if not step.get("kind"):
                    step["kind"] = "clarification" if names == ["ask_user"] else ("data" if names else "preparation")
                if not step.get("completion_criteria"):
                    step["completion_criteria"] = {"clarification": "user_answer", "finalize": "validated_answer"}.get(step["kind"], "tool_result")
            if not any(step.get("kind") == "finalize" for step in steps if isinstance(step, dict)):
                steps.append({"id": "test-finalize", "title": "整理并核验最终答案",
                    "description": "提交结构化最终答案", "tool_names": ["finalize_answer"],
                    "goal_ids": goal_ids or ["test_goal"], "kind": "finalize",
                    "completion_criteria": "validated_answer"})
            self.plan_step_ids = [str(step.get("id") or "") for step in steps if isinstance(step, dict)]
            self.plan_tool_names = [list(step.get("tool_names") or []) for step in steps if isinstance(step, dict)]
            function.arguments = json.dumps(args, ensure_ascii=False)

    def _modernize_final_response(self, response, summary):
        choices = getattr(response, "choices", []) or []
        if not choices:
            return response
        message = choices[0].message
        calls = list(getattr(message, "tool_calls", []) or [])
        if calls:
            has_explicit_v2_final = any(
                getattr(getattr(call, "function", None), "name", "") == "finalize_answer"
                and (lambda args: isinstance(args, dict) and args.get("schema_version") == 2)(
                    json.loads(getattr(getattr(call, "function", None), "arguments", "{}") or "{}")
                )
                for call in calls
            )
            if not has_explicit_v2_final:
                calls = self._insert_plan_advances(calls)
            for call in calls:
                function = getattr(call, "function", None)
                if getattr(function, "name", "") != "finalize_answer":
                    continue
                try:
                    draft = json.loads(function.arguments or "{}")
                except (ValueError, TypeError):
                    continue
                if draft.get("schema_version") == 2:
                    continue
                narrative = "。".join(str(item) for item in draft.get("narrative", []) if item)
                if narrative:
                    function.arguments = json.dumps({"schema_version": 2, "result_kind": "explanation",
                        "goal_ids": ["test_goal"], "blocks": [{"kind": "text", "text": narrative}],
                        "limitation_codes": []}, ensure_ascii=False)
            message.tool_calls = calls
            return response
        content = str(getattr(message, "content", "") or "").strip()
        if not content:
            return response
        message.content = None
        goal_ids = re.findall(r"goal_id=([A-Za-z0-9_-]+):", summary)
        refs_by_goal = {goal_id: [] for goal_id in goal_ids}
        for line in summary.splitlines():
            fields = [item.strip() for item in line.split("|")]
            if len(fields) < 8 or not fields[0].startswith("- ev_"):
                continue
            evidence_id = fields[0][2:].strip()
            for goal_id in fields[6].removeprefix("goal_ids=").split(","):
                if goal_id in refs_by_goal and evidence_id not in refs_by_goal[goal_id]:
                    refs_by_goal[goal_id].append(evidence_id)
        from backend.agent_runtime.answer_validation import _unverified_number
        if goal_ids and all(refs_by_goal.values()):
            blocks = []
            if not _unverified_number(content):
                blocks.append({"kind": "text", "text": content})
            blocks.extend({"kind": "fact_ref", "evidence_id": refs[0], "format": "sentence"}
                          for refs in refs_by_goal.values())
            draft = {"schema_version": 2, "result_kind": "data_answer", "goal_ids": goal_ids,
                "blocks": blocks, "limitation_codes": []}
        else:
            draft = {"schema_version": 2, "result_kind": "explanation", "goal_ids": ["test_goal"],
                "blocks": [{"kind": "text", "text": content}], "limitation_codes": []}
        finalize = {"id": f"test-finalize-{len(self.requests)}",
            "name": "finalize_answer", "arguments": json.dumps(draft, ensure_ascii=False)}
        plan_lines = [line.strip() for line in summary.splitlines() if line.strip().startswith("- [")]
        running_ids, queued_finalize_ids = [], []
        for line_index, line in enumerate(plan_lines):
            step_id = self.plan_step_ids[line_index] if line_index < len(self.plan_step_ids) else ""
            if "[running]" in line:
                running_ids.append(step_id)
            elif "[queued]" in line and "finalize" in line:
                queued_finalize_ids.append(step_id)
        calls = []
        if queued_finalize_ids:
            if running_ids:
                calls.append({"id": f"test-close-data-{len(self.requests)}", "name": "update_plan",
                    "arguments": json.dumps({"updates": [
                        {"id": step_id, "status": "complete"} for step_id in running_ids
                    ]}, ensure_ascii=False)})
            calls.append({"id": f"test-open-finalize-{len(self.requests)}", "name": "update_plan",
                "arguments": json.dumps({"updates": [
                    {"id": step_id, "status": "running"} for step_id in queued_finalize_ids
                ]}, ensure_ascii=False)})
        calls.append(finalize)
        message.tool_calls = _message(tool_calls=calls).tool_calls
        return response

    def _insert_plan_advances(self, calls):
        """Add plan transitions omitted by older one-batch test scripts."""
        if not self.plan_step_ids or not self.plan_tool_names:
            return calls
        expanded_calls = []
        planned_names = {tool for group in self.plan_tool_names for tool in group}
        for call in calls:
            expanded_calls.append(call)
            name = getattr(getattr(call, "function", None), "name", "")
            if name.startswith("mcp__") and "csv_query" in planned_names:
                expanded_calls.append(_message(tool_calls=[{
                    "id": f"test-mcp-data-query-{len(self.requests)}",
                    "name": "csv_query", "arguments": "{}",
                }]).tool_calls[0])
        output = []
        cursor = 0
        running = 0
        for call in expanded_calls:
            name = getattr(getattr(call, "function", None), "name", "")
            target = next((index for index, planned_names in enumerate(self.plan_tool_names)
                           if name in planned_names), None)
            if target is not None and target > running:
                updates = []
                if running < len(self.plan_step_ids) - 1 and "finalize_answer" not in self.plan_tool_names[running]:
                    updates.append({"id": self.plan_step_ids[running], "status": "complete"})
                updates.append({"id": self.plan_step_ids[target], "status": "running"})
                output.append(_message(tool_calls=[{"id": f"test-plan-advance-{len(self.requests)}-{target}",
                    "name": "update_plan", "arguments": json.dumps({"updates": updates}, ensure_ascii=False)}]).tool_calls[0])
                running = target
                cursor = target + 1
            output.append(call)
        return output


def _fake_success_output(name, args):
    result = {"ok": True, "tool": name, "input": args,
        "recommendations": [{"series": "测试车系", "brand": "测试品牌", "median_wan": 10.99, "sample_size": 12}],
        "match_count": 1,
        "rows": [{"series": "测试车系", "price_yuan": 109900, "price_wan": 10.99,
            "production_year": 2022, "mileage_km": 30000}],
        "median_yuan": 109900, "median_wan": 10.99, "raw_count": 12}
    if name == "price_compare":
        result.update({"p25_yuan": 99000, "median_yuan": 109900, "p75_yuan": 119900,
            "mean_yuan": 110000, "min_yuan": 90000, "max_yuan": 130000, "sample_size": 12})
    return result


def _patch_client(monkeypatch, client):
    monkeypatch.setattr(loop, "get_client", lambda: client)
    from backend.agent_runtime.tool_registry import execute_registered_tool as real_execute

    def execute(name, args):
        if name in {"declare_task", "set_plan", "update_plan", "ask_user", "finalize_answer"}:
            return real_execute(name, args)
        if name == "csv_query":
            return _fake_success_output(name, args)
        return _fake_success_output(name, args)

    monkeypatch.setattr(
        nodes,
        "execute_registered_tool",
        execute,
    )


def _request_with_user_content(client, content):
    return next(request for request in client.requests
                if any(message.get("role") == "user" and message.get("content") == content
                       for message in request.get("messages", [])))


def test_model_can_return_final_text_without_tools(monkeypatch):
    client = FakeClient([_response(_message("可以从合适车系开始比较。"))])
    _patch_client(monkeypatch, client)

    result = loop.run_agent("15万以内帮我选3款车")

    assert result["summary"].startswith("可以从合适车系开始比较。")
    assert result["report"]["intent"] == "evidence"
    assert result["report"]["answer"] == result["summary"]
    assert len(client.requests) == 4  # declare_task, set_plan, tool result, finalize_answer
    assert [m["role"] for m in client.requests[0]["messages"][:2]] == ["system", "user"]
    assert client.requests[0]["tools"] == tool_schemas()
    assert client.requests[0]["tool_choice"] == "auto"
    assert client.requests[0]["stream"] is False
    assert client.requests[0]["temperature"] == 0.1
    assert client.requests[0]["max_tokens"] == 65_536


def test_success_publishes_the_final_completed_plan(monkeypatch):
    client = FakeClient([_response(_message("可以从合适车系开始比较。"))])
    _patch_client(monkeypatch, client)
    events = []
    result = loop.run_agent("15万以内帮我选3款车", event_sink=lambda event: events.append(copy.deepcopy(event)))
    assert result["status"] == "done", result
    plan_events = [event for event in events if event["event_type"] == "plan_update"]
    assert plan_events[-1]["output"]["steps"][-1]["status"] == "complete"
    assert plan_events[-1]["output"]["revision"] == result["plan_revision"]
    assert plan_events[-1]["output"]["steps"] == result["runtime"]["plan"]


def test_model_usage_is_accumulated_across_rounds(monkeypatch):
    client = FakeClient(
        [
            _response(
                _message(tool_calls=[{"id": "usage-call", "name": "recommend", "arguments": "{}"}]),
                {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            ),
            _response(
                _message("完成。"),
                {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            ),
        ]
    )
    _patch_client(monkeypatch, client)

    result = loop.run_agent("15 万以内帮我选车")
    usage = result["state"]["flags"]["token_usage"]

    assert usage["prompt_tokens"] == 20
    assert usage["completion_tokens"] == 10
    assert usage["total_tokens"] == 30
    assert usage["model_calls"] == 2


def test_context_budget_sets_output_reservation_and_records_counters(monkeypatch):
    from backend.agent_runtime.context_budget import ContextBudget

    client = FakeClient([_response(_message("完成。"))])
    _patch_client(monkeypatch, client)

    result = loop.run_agent(
        "短问题",
        context_budget=ContextBudget(100_000, 777, 1_000, 2, 6_000),
    )

    assert client.requests[0]["max_tokens"] == 777
    counters = result["state"]["flags"]["context_budget"]
    assert counters["estimated_input_tokens"] > 0
    assert counters["input_budget_tokens"] == 98_223


def test_oversized_context_fails_stably_without_calling_model(monkeypatch):
    from backend.agent_runtime.context_budget import ContextBudget

    client = FakeClient([_response(_message("不应到达模型"))])
    _patch_client(monkeypatch, client)

    result = loop.run_agent(
        "短问题",
        context_budget=ContextBudget(128, 32, 16, 2, 64),
    )

    assert result["status"] == "error"
    assert result["summary"] == "context_input_too_large"
    assert result["state"]["flags"]["error_code"] == "context_input_too_large"
    assert client.requests == []


def test_thinking_model_pending_decision_uses_auto_with_only_ask_user(monkeypatch):
    model = "deepseek-v4-flash"
    monkeypatch.setattr(loop, "DEEPSEEK_MODEL", model)
    monkeypatch.setattr(nodes, "DEEPSEEK_MODEL", model)
    client = FakeClient([_response(_message("请先确认下一步。"))])
    state = nodes.AgentState.create("15万以内的SUV", nodes.SYSTEM_PROMPT)
    state.flags["pending_user_decision"] = {"reason_code": "unsupported_filter"}
    prefix = loop.build_prefix_context(model, nodes.SYSTEM_PROMPT, tool_schemas())

    nodes.agent_execute_node(
        state,
        nodes.RunTrace.create(state.trace_id),
        client,
        1,
        prefix=prefix,
    )

    request = client.requests[0]
    assert request["tool_choice"] == "auto"
    assert [item["function"]["name"] for item in request["tools"]] == ["ask_user"]


def test_report_followup_context_is_sent_to_model_without_becoming_transcript():
    client = FakeClient([_response(_message("报告 v2 的结果表示……"))])
    state = nodes.AgentState.create("请解释报告 v2", nodes.SYSTEM_PROMPT)
    original_messages = list(state.messages)
    report_context = "[HISTORICAL_REPORT_CONTEXT_V1] report_id=r2; version=v2; dataset_version=d7"

    nodes.agent_execute_node(
        state,
        nodes.RunTrace.create(state.trace_id),
        client,
        1,
        context_messages=[{"role": "system", "content": report_context}],
    )

    request_messages = client.requests[0]["messages"]
    user_index = next(i for i, message in enumerate(request_messages) if message.get("role") == "user")
    assert request_messages[user_index - 1]["content"] == report_context
    assert state.messages[:len(original_messages)] == original_messages
    assert all(report_context not in str(message.get("content") or "") for message in state.messages)


def test_report_selected_on_midrun_steering_is_loaded_into_next_model_context(tmp_path, monkeypatch):
    repository = SQLiteRuntimeRepository(tmp_path / "steering-report-context.sqlite3")
    conversation_id = repository.create_conversation("owner-a", "create-report-context") ["conversation_id"]
    repository.create_run("saved-report", "browser-report", "原问题", context_id=conversation_id, owner_id="owner-a")
    lease = repository.acquire_run_lease("saved-report", "report-worker", lease_ttl_s=10)
    repository.complete_run(
        "saved-report",
        {"status": "done", "summary": "原答案", "final_message": {"role": "assistant", "content": "原答案"},
         "report": {"id": "saved-report", "question": "原问题", "summaryTitle": "原报告",
                    "summaryText": "原答案", "answer": "原答案"}},
        session_messages=[{"role": "user", "content": "原问题"}],
        session_run_id="saved-report", lease_token=lease["token"],
    )

    captured_contexts = []

    def capture_context(_state, _trace, _client, _step, **kwargs):
        captured_contexts.extend(kwargs.get("context_messages") or [])
        return {"error": "controlled stop", "error_code": "test_stop", "assistant": {}}

    monkeypatch.setattr(loop, "get_client", lambda: SimpleNamespace(is_available=lambda: True))
    monkeypatch.setattr(loop, "agent_execute_node", capture_context)
    pending = [{"client_message_id": "steer-with-report", "content": "改成 18 万预算", "revision": 1,
                "source_report_id": "saved-report"}]
    result = loop.run_agent(
        "继续调整推荐", max_steps=1, session_id="browser-follow-up", context_id=conversation_id,
        run_id="run-report-follow-up", session_store=SQLiteSessionStore(repository), owner_id="owner-a",
        message_source=lambda: pending,
        message_ack=lambda _messages, _checkpoint: True,
    )

    assert any("report_id=saved-report; version=v1" in item["content"] for item in captured_contexts)
    assert any("必须调用对应数据工具重新取数" in item["content"] for item in captured_contexts)
    assert all("HISTORICAL_REPORT_CONTEXT_V1" not in str(message.get("content") or "")
               for message in result["state"]["messages"])


def test_model_calls_recommend_then_reads_tool_result(monkeypatch, tmp_path):
    client = FakeClient(
        [
            _response(
                _message(
                    tool_calls=[
                        {
                            "id": "call-recommend",
                            "name": "recommend",
                            "arguments": json.dumps({"budget_max_wan": 15, "top_k": 3}),
                        }
                    ]
                )
            ),
            _response(_message("根据工具结果，推荐宝马3系。")),
        ]
    )
    _patch_client(monkeypatch, client)
    repository = SQLiteRuntimeRepository(tmp_path / "validated-final-session.sqlite3")
    repository.create_run("run-validated-final", "browser-group", "15万以内帮我选3款车",
                          context_id="context-validated-final", owner_id="owner-final")

    result = loop.run_agent(
        "15万以内帮我选3款车", session_store=SQLiteSessionStore(repository),
        session_id="browser-group", context_id="context-validated-final", run_id="run-validated-final",
        owner_id="owner-final", defer_session_commit=True,
    )

    assert result["summary_source"] == "validated"
    transcript_delta = result["_session_commit"]["messages"]
    assert transcript_delta[-1] == {"role": "assistant", "content": result["summary"]}
    assert len(client.requests) == 2
    second_messages = next(
        request["messages"] for request in client.requests
        if any(message.get("role") == "tool" and message.get("tool_call_id") == "call-recommend"
               for message in request.get("messages", []))
    )
    assistant_message = next(message for message in second_messages if message.get("role") == "assistant" and message.get("tool_calls") and any(call["id"] == "call-recommend" for call in message["tool_calls"]))
    tool_message = next(message for message in second_messages if message.get("role") == "tool" and message.get("tool_call_id") == "call-recommend")
    assert any(call["id"] == "call-recommend" for call in assistant_message["tool_calls"])
    tool_output = json.loads(tool_message["content"])
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "call-recommend"
    assert tool_output["status"] == "success"
    assert tool_output["evidence"][0]["value"] == 10.99
    assert tool_output["recommendations"][0]["series"] == "测试车系"


def test_model_can_call_two_tools_across_two_rounds(monkeypatch):
    client = FakeClient(
        [
            _response(
                _message(
                    tool_calls=[
                        {"id": "call-1", "name": "recommend", "arguments": '{"budget_max_wan": 15}'},
                    ]
                )
            ),
            _response(
                _message(
                    tool_calls=[
                        {"id": "call-2", "name": "price_compare", "arguments": '{"series": "宝马3系"}'},
                    ]
                )
            ),
            _response(_message("已完成推荐和行情分析。")),
            _response(_message("")),
        ]
    )
    _patch_client(monkeypatch, client)

    result = loop.run_agent("15万以内选车并查看价格")

    assert result["summary_source"] == "validated"
    assert result["state"]["flags"]["steps"] == 3
    assert [item["tool_name"] for item in result["state"]["tool_results"] if item["tool_name"] in {"recommend", "price_compare"}] == [
        "recommend",
        "price_compare",
    ]


def test_model_can_return_multiple_tool_calls_in_one_response(monkeypatch):
    client = FakeClient(
        [
            _response(
                _message(
                    tool_calls=[
                        {"id": "call-1", "name": "recommend", "arguments": '{"budget_max_wan": 15}'},
                        {"id": "call-2", "name": "csv_query", "arguments": '{"limit": 2}'},
                    ]
                )
            ),
            _response(_message("已返回推荐和车源样本。")),
            _response(_message("")),
        ]
    )
    _patch_client(monkeypatch, client)

    result = loop.run_agent("15万以内推荐并给我车源")

    assert result["summary_source"] == "validated"
    assert [item["tool_name"] for item in result["state"]["tool_results"] if item["tool_name"] in {"recommend", "csv_query"}] == [
        "recommend",
        "csv_query",
    ]
    assert any(
        {call["id"] for call in message.get("tool_calls") or []}.issuperset({"call-1", "call-2"})
        for message in client.requests[1]["messages"]
    )


def test_tool_batch_resumes_after_ask_user_without_replaying_completed_calls(monkeypatch):
    client = FakeClient([
        _response(_message(tool_calls=[
            {"id": "batch-query", "name": "csv_query", "arguments": '{"limit": 1}'},
            {"id": "batch-ask", "name": "ask_user", "arguments": '{"questions":[{"id":"budget","prompt":"预算？","options":[],"allow_custom":true,"required":true}]}'},
            {"id": "batch-tail", "name": "price_compare", "arguments": '{"series":"宝马3系"}'},
        ])),
        _response(_message("已按预算继续完成分析。")),
    ])
    monkeypatch.setattr(loop, "get_client", lambda: client)
    from backend.agent_runtime.tool_registry import execute_registered_tool as real_execute
    calls = []

    def execute(name, args):
        if name in {"declare_task", "set_plan", "update_plan", "ask_user", "finalize_answer"}:
            return real_execute(name, args)
        calls.append(name)
        return _fake_success_output(name, args)

    monkeypatch.setattr(nodes, "execute_registered_tool", execute)
    waiting = loop.run_agent("帮我查车并确认预算")

    assert waiting["status"] == "waiting_user"
    assert waiting["checkpoint"]["pending_tool_batch"]["next_index"] == 4
    assert calls == ["csv_query"]
    batch_names = [item["function"]["name"] for item in waiting["checkpoint"]["pending_tool_batch"]["calls"]]
    assert "ask_user" in batch_names
    assert batch_names.index("csv_query") < batch_names.index("ask_user")

    done = loop.run_agent(
        "帮我查车并确认预算", resume_checkpoint=waiting["checkpoint"],
        input_answers=[{"question_id": "budget", "value": "15"}],
    )

    assert done["status"] == "done", done
    assert calls == ["csv_query", "price_compare"]
    assert done["checkpoint"]["pending_tool_batch"] is None
    model_messages = next(request["messages"] for request in client.requests
                          if any(message.get("role") == "tool" and message.get("tool_call_id") == "batch-tail"
                                 for message in request["messages"]))
    tool_call_ids = [message.get("tool_call_id") for message in model_messages if message["role"] == "tool"]
    assert tool_call_ids.index("batch-query") < tool_call_ids.index("batch-ask") < tool_call_ids.index("batch-tail")
    assert "test-declare-1" in tool_call_ids


def test_reused_ask_user_answer_commits_tool_result_and_batch_cursor(monkeypatch):
    question = normalize_questions([
        {"id": "budget", "prompt": "预算？", "options": [], "allow_custom": True, "required": True}
    ])[0]
    calls = [
        {"id": "reuse-ask", "type": "function", "function": {"name": "ask_user", "arguments": json.dumps({"questions": [question]})}},
        {"id": "reuse-tail", "type": "function", "function": {"name": "price_compare", "arguments": '{"series":"宝马3系"}'}},
    ]
    state = AgentState.create("继续", "system")
    state.answered_inputs.append({
        "question_id": "budget", "prompt": "预算？", "fingerprint": question_fingerprint(question),
        "values": ["15"], "value": "15", "tool_call_id": "old-ask", "source": "user",
    })
    state.append_message({"role": "assistant", "content": "", "tool_calls": calls})
    state.pending_tool_batch = {
        "batch_id": "reuse-batch", "assistant_message_index": len(state.messages) - 1,
        "calls": calls, "next_index": 0, "completed_call_ids": [], "step": 1,
    }
    from backend.agent_runtime.tool_registry import execute_registered_tool as real_execute
    monkeypatch.setattr(
        nodes, "execute_registered_tool",
        lambda name, args: real_execute(name, args) if name in {"declare_task", "set_plan", "update_plan", "ask_user", "finalize_answer"} else {"ok": True, "tool": name},
    )
    checkpoints = []

    result = nodes.tool_execute_node(
        state, RunTrace.create(run_id="reuse-run"), calls, step=1,
        checkpoint_sink=lambda value: checkpoints.append(copy.deepcopy(value)),
    )

    assert result is None
    assert [item["tool_call_id"] for item in state.tool_results] == ["reuse-ask", "reuse-tail"]
    assert [item["tool_call_id"] for item in state.messages if item["role"] == "tool"] == ["reuse-ask", "reuse-tail"]
    assert checkpoints[0]["pending_tool_batch"]["next_index"] == 1
    assert checkpoints[0]["pending_tool_batch"]["completed_call_ids"] == ["reuse-ask"]
    assert checkpoints[1]["pending_tool_batch"]["next_index"] == 2


def test_truncated_model_response_is_rejected_before_entering_message_history():
    state = AgentState.create("请分析", "system")
    trace = RunTrace.create(run_id="truncated-run")
    client = FakeClient([
        _response(
            _message("被截断的回答"),
            {"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50, "completion_tokens_details": {"reasoning_tokens": 12}},
            finish_reason="length",
            response_id="provider-123",
        ),
        _response(
            _message("仍被截断的回答"),
            {"prompt_tokens": 22, "completion_tokens": 31, "total_tokens": 53, "completion_tokens_details": {"reasoning_tokens": 14}},
            finish_reason="length",
            response_id="provider-456",
        ),
    ])

    result = nodes.agent_execute_node(state, trace, client, step=1)

    assert result["error_code"] == "model_truncated"
    assert not any(message["role"] == "assistant" for message in state.messages)
    rejected = next(event for event in trace.events if event["event_type"] == "model_response_rejected")
    assert rejected["output"]["finish_reason"] == "length"
    assert rejected["output"]["provider_request_id"] == "provider-456"
    assert rejected["output"]["request_max_tokens"] == 65_536
    assert rejected["output"]["usage"]["completion_tokens_details"]["reasoning_tokens"] == 14
    assert state.flags["token_usage"]["prompt_tokens"] == 42
    assert state.flags["token_usage"]["completion_tokens"] == 61
    assert state.flags["token_usage"]["model_calls"] == 2
    assert len(client.requests) == 2
    assert [request["max_tokens"] for request in client.requests] == [65_536, 65_536]


def test_truncated_tool_call_is_not_executed_and_compact_retry_can_finish(monkeypatch):
    partial_call = {"id": "cut-call", "name": "recommend", "arguments": '{"budget_max_wan":'}
    client = FakeClient([
        _response(_message(tool_calls=[partial_call]), finish_reason="length"),
        _response(_message("已完成分析说明。"), finish_reason="stop"),
    ])
    _patch_client(monkeypatch, client)
    executed = []
    monkeypatch.setattr(nodes, "execute_registered_tool", lambda name, args: executed.append((name, args)) or {"ok": True})

    result = loop.run_agent("简单说明", max_steps=1)

    assert len(client.requests) == 2
    assert executed == []
    assert [item["tool_name"] for item in result["state"]["tool_results"]] == ["declare_task"]
    assert result["state"]["flags"]["token_usage"]["model_calls"] == 2


def test_provider_timeout_is_clamped_to_remaining_agent_deadline():
    from backend.agent_runtime.execution import ExecutionContext

    state = AgentState.create("短问题", "system")
    trace = RunTrace.create(run_id="bounded-timeout")
    client = FakeClient([_response(_message("完成。"))])
    execution = ExecutionContext(
        run_id="bounded-timeout",
        segment_id="segment-1",
        cancellation=threading.Event(),
        deadline_monotonic=time.monotonic() + 20,
    )

    nodes.agent_execute_node(state, trace, client, step=1, execution_context=execution)

    assert 19.0 < client.requests[0]["timeout"] <= 20


def test_event_sink_failure_is_kept_as_sanitized_trace_diagnostic():
    trace = RunTrace.create(run_id="telemetry-run")
    trace.record("agent", "model_round", {"event_type": "model_round"}, {}, "ok")

    cursor = loop._emit_new_events(trace, lambda _event: (_ for _ in ()).throw(OSError("private path detail")), 0)

    assert cursor == 2
    diagnostic = trace.events[-1]
    assert diagnostic["event_type"] == "event_sink_error"
    assert diagnostic["output"] == {"error_type": "OSError"}
    assert "private path detail" not in str(trace.to_dict())


def test_cancellation_during_tool_batch_prevents_starting_next_tool(monkeypatch):
    client = FakeClient([_response(_message(tool_calls=[
        {"id": "cancel-first", "name": "csv_query", "arguments": "{}"},
        {"id": "cancel-second", "name": "price_compare", "arguments": '{"series":"宝马3系"}'},
    ]))])
    monkeypatch.setattr(loop, "get_client", lambda: client)
    cancellation = __import__("threading").Event()
    calls = []
    from backend.agent_runtime.tool_registry import execute_registered_tool as real_execute

    def execute(name, args):
        if name in {"declare_task", "set_plan", "update_plan", "ask_user", "finalize_answer"}:
            return real_execute(name, args)
        calls.append(name)
        cancellation.set()
        return {"ok": True, "tool": name, "input": args}

    monkeypatch.setattr(nodes, "execute_registered_tool", execute)
    result = loop.run_agent("查询", cancellation=cancellation)
    assert result["status"] == "cancelled"
    assert calls == ["csv_query"]
    assert len(client.requests) == 1


def test_invalid_json_unknown_tool_and_unknown_fields_return_tool_errors(monkeypatch):
    from backend.agent_runtime.evidence import evidence_from_tool_result
    query_call_id = "valid-query-retry"
    query_evidence_id = evidence_from_tool_result(
        "csv_query", _fake_success_output("csv_query", {}), query_call_id,
        constraint_revision=0,
    )[0]["evidence_id"]
    validated_draft = {
        "schema_version": 2, "result_kind": "data_answer", "goal_ids": ["test_goal"],
        "blocks": [
            {"kind": "text", "text": "工具参数存在问题，无法完成查询。"},
            {"kind": "fact_ref", "evidence_id": query_evidence_id, "format": "sentence"},
        ], "limitation_codes": [],
    }
    client = FakeClient(
        [
            _response(
                _message(
                    tool_calls=[
                        {"id": "bad-json", "name": "recommend", "arguments": "not-json"},
                        {"id": "unknown", "name": "not_registered", "arguments": "{}"},
                        {"id": "bad-field", "name": "csv_query", "arguments": '{"path": "x"}'},
                    ]
                )
            ),
            _response(_message(tool_calls=[{"id": "valid-query-retry", "name": "csv_query", "arguments": "{}"}])),
            _response(_message(tool_calls=[
                {"id": "advance-final", "name": "update_plan", "arguments":
                 '{"updates":[{"id":"test-step-1","status":"complete"},{"id":"test-finalize","status":"running"}]}'},
                {"id": "validated-final", "name": "finalize_answer", "arguments": json.dumps(validated_draft)},
            ])),
        ]
    )
    monkeypatch.setattr(loop, "get_client", lambda: client)

    from backend.agent_runtime.tool_registry import execute_registered_tool as real_execute

    def fake_or_real_execute(name, args):
        if name == "recommend":
            return _fake_success_output(name, args)
        return real_execute(name, args)

    monkeypatch.setattr(nodes, "execute_registered_tool", fake_or_real_execute)

    result = loop.run_agent("查询车源")

    business_errors = {
        message.get("tool_call_id"): json.loads(message["content"])
        for request in client.requests for message in request["messages"]
        if message.get("role") == "tool" and message.get("tool_call_id") in {"bad-json", "unknown", "bad-field"}
    }
    assert set(business_errors) == {"bad-json", "unknown", "bad-field"}
    assert {item.get("status") for item in business_errors.values()} <= {"error", "invalid_arguments"}
    assert all(item.get("status") in {"error", "invalid_arguments"} for item in business_errors.values())
    assert "工具参数存在问题，无法完成查询。" in result["summary"]


def test_tool_exception_is_returned_to_model(monkeypatch):
    from backend.agent_runtime.evidence import evidence_from_tool_result
    query_evidence_id = evidence_from_tool_result(
        "csv_query", _fake_success_output("csv_query", {}), "call-query", constraint_revision=0,
    )[0]["evidence_id"]
    validated_draft = {
        "schema_version": 2, "result_kind": "data_answer", "goal_ids": ["test_goal"],
        "blocks": [
            {"kind": "text", "text": "工具执行失败，我暂时无法完成分析。"},
            {"kind": "fact_ref", "evidence_id": query_evidence_id, "format": "sentence"},
        ], "limitation_codes": [],
    }
    client = FakeClient([
        _response(_message(tool_calls=[
            {"id": "call-error", "name": "recommend", "arguments": '{"budget_max_wan":15}'},
            {"id": "call-query", "name": "csv_query", "arguments": "{}"},
        ])),
        _response(_message(tool_calls=[
            {"id": "advance-final", "name": "update_plan", "arguments":
             '{"updates":[{"id":"test-step-1","status":"complete"},{"id":"test-finalize","status":"running"}]}'},
            {"id": "validated-final", "name": "finalize_answer", "arguments": json.dumps(validated_draft)},
        ])),
    ])
    monkeypatch.setattr(loop, "get_client", lambda: client)
    from backend.agent_runtime.tool_registry import execute_registered_tool as real_execute

    def explode(name, args):
        if name in {"declare_task", "set_plan", "update_plan", "ask_user", "finalize_answer"}:
            return real_execute(name, args)
        if name == "recommend":
            raise RuntimeError("database unavailable")
        return _fake_success_output(name, args)

    monkeypatch.setattr(nodes, "execute_registered_tool", explode)

    result = loop.run_agent("15万以内帮我选车")

    tool_message = next(message for request in client.requests for message in request["messages"] if message.get("role") == "tool" and message.get("tool_call_id") == "call-error")
    assert tool_message["role"] == "tool"
    assert json.loads(tool_message["content"])["status"] == "error"
    assert json.loads(tool_message["content"])["tool_name"] == "recommend"
    assert tool_message["tool_call_id"] == "call-error"
    assert "工具执行失败，我暂时无法完成分析。" in result["summary"]


def test_fake_model_shared_analysis_tool_evidence_finalize_and_report_chain(monkeypatch):
    from backend.agent_runtime.evidence import evidence_from_tool_result
    from backend.tools import shared_analysis
    from backend.data_loader import get_dataset_version

    actual_version = get_dataset_version()
    analysis = {
        "schema_version": "1.0", "analysis_type": "price_distribution", "status": "success",
        "reason_code": None, "dataset_version": actual_version,
        "normalized_filters": {}, "filters_applied": [],
        "counts": {"dataset_rows": 20, "scope_rows": 20, "valid_rows": 20, "excluded_rows": 0},
        "data": {"n": 20, "median_yuan": 123000, "p25_yuan": 100000, "p75_yuan": 150000,
                 "min_yuan": 80000, "max_yuan": 180000, "mean_yuan": 125000, "bins": []},
        "limitations": ["价格为挂牌价"], "methodology": {"price_basis": "listing"},
    }
    monkeypatch.setattr(shared_analysis, "run_analysis", lambda *args, **kwargs: analysis)
    evidence_id = next(record["evidence_id"] for record in evidence_from_tool_result(
        "price_distribution",
        {"ok": True, "result_status": "success", "analysis_type": "price_distribution",
         "dataset_version": analysis["dataset_version"], "filters_applied": [], "data": analysis},
        "call-analysis", constraint_revision=0,
    ) if record["metric"] == "data.data.median_yuan")
    final = {
        "schema_version": 2, "result_kind": "data_answer", "goal_ids": ["test_goal"],
        "blocks": [
            {"kind": "text", "text": "统计结果已核验。"},
            {"kind": "fact_ref", "evidence_id": evidence_id, "format": "sentence"},
        ], "limitation_codes": ["listing_price_not_transaction"],
    }
    client = FakeClient([
        _response(_message(tool_calls=[{"id": "call-analysis", "name": "price_distribution", "arguments": "{}"}])),
        _response(_message(tool_calls=[
            {"id": "advance-final", "name": "update_plan", "arguments":
             '{"updates":[{"id":"test-step-1","status":"complete"},{"id":"test-finalize","status":"running"}]}'},
            {"id": "validated-final", "name": "finalize_answer", "arguments": json.dumps(final)},
        ])),
    ])
    monkeypatch.setattr(loop, "get_client", lambda: client)

    result = loop.run_agent("查看价格分布")

    assert result["status"] == "done", result
    assert any(item.get("metric") == "data.data.median_yuan" and item.get("value") == 123000
               and item.get("dataset_version") == actual_version for item in result["state"]["evidence_records"])
    assert result["report"]["analysisBlocks"][0]["dataset_version"] == actual_version
    assert result["report"]["answerEvidence"]["validated"] is True


def test_fake_model_can_finalize_correlation_and_brand_share_evidence(monkeypatch):
    from backend.agent_runtime.evidence import evidence_from_tool_result
    from backend.data_loader import get_dataset_version
    from backend.tools import shared_analysis

    actual_version = get_dataset_version()
    analyses = {
        "mileage_price_correlation": {
            "schema_version": "1.0", "analysis_type": "mileage_price_correlation", "status": "success",
            "reason_code": None, "dataset_version": actual_version, "normalized_filters": {},
            "filters_applied": [], "counts": {"dataset_rows": 12, "scope_rows": 12, "valid_rows": 12,
            "excluded_rows": 0}, "data": {"n": 12, "pearson_r": 0.6, "spearman_rho": 0.5,
            "correlation_reason": None, "mileage_bins": [], "scatter": {"total_points": 12,
            "display_points": 12, "sampled": False, "points": []}}, "limitations": [], "methodology": {},
        },
        "brand_profile": {
            "schema_version": "1.0", "analysis_type": "brand_profile", "status": "success",
            "reason_code": None, "dataset_version": actual_version, "normalized_filters": {},
            "filters_applied": [], "counts": {"dataset_rows": 10, "scope_rows": 10, "valid_rows": 10,
            "excluded_rows": 0}, "data": {"denominator_n": 10, "groups": [{"brand_name": "甲牌", "n": 2,
            "share": 0.2, "price": {"median": 80000}, "category": "brand"}]},
            "limitations": [], "methodology": {},
        },
    }
    monkeypatch.setattr(shared_analysis, "run_analysis", lambda name, **kwargs: analyses[name])
    def evidence_ref(name, call_id, metric):
        output = {"ok": True, "result_status": "success", "analysis_type": name,
                  "dataset_version": actual_version, "filters_applied": [], "data": analyses[name]}
        return next(item["evidence_id"] for item in evidence_from_tool_result(name, output, call_id)
                    if item["metric"] == metric)
    correlation_ref = evidence_ref("mileage_price_correlation", "call-correlation", "data.data.pearson_r")
    share_ref = evidence_ref("brand_profile", "call-brand", "data.data.groups.0.share")
    final = {"schema_version": 2, "result_kind": "data_answer", "goal_ids": ["test_goal"],
             "blocks": [{"kind": "fact_ref", "evidence_id": correlation_ref, "format": "sentence"},
                        {"kind": "fact_ref", "evidence_id": share_ref, "format": "sentence"}],
             "limitation_codes": ["sample_scope"]}
    client = FakeClient([
        _response(_message(tool_calls=[
            {"id": "call-correlation", "name": "mileage_price_correlation", "arguments": "{}"},
            {"id": "call-brand", "name": "brand_profile", "arguments": "{}"},
        ])),
        _response(_message(tool_calls=[
            {"id": "advance-final", "name": "update_plan", "arguments":
             '{"updates":[{"id":"test-step-1","status":"complete"},{"id":"test-finalize","status":"running"}]}'},
            {"id": "validated-final", "name": "finalize_answer", "arguments": json.dumps(final)},
        ])),
    ])
    monkeypatch.setattr(loop, "get_client", lambda: client)

    result = loop.run_agent("分析里程相关性并看品牌画像")

    assert result["status"] == "done", result
    assert "0.600相关系数" in result["summary"]
    assert "20%" in result["summary"]
    assert len(result["report"]["analysisBlocks"]) == 2


def test_unknown_or_unavailable_model_does_not_reactivate_old_planner(monkeypatch):
    client = FakeClient([], available=False)
    monkeypatch.setattr(loop, "get_client", lambda: client)

    result = loop.run_agent("15万以内帮我选车")

    assert "DeepSeek" in result["summary"] or "模型" in result["summary"]
    assert client.requests == []
    assert result["state"]["tool_results"] == []


def test_max_steps_stops_without_an_extra_model_call(monkeypatch):
    client = FakeClient(
        [
            _response(_message(tool_calls=[{"id": "call-1", "name": "recommend", "arguments": '{"budget_max_wan": 15}'}])),
            _response(_message(tool_calls=[{"id": "call-2", "name": "recommend", "arguments": '{"budget_max_wan": 15}'}])),
        ]
    )
    _patch_client(monkeypatch, client)

    result = loop.run_agent("15万以内帮我选车", max_steps=2)

    assert result["state"]["flags"]["max_steps_exceeded"] is True
    assert result["status"] == "error"
    assert "最大执行步数" in result["summary"]
    assert len(client.requests) == 2


def test_model_can_publish_its_own_plan_without_fixed_frontend_phases(monkeypatch):
    client = FakeClient(
        [
            _response(
                _message(
                    tool_calls=[
                        {
                            "id": "call-plan",
                            "name": "set_plan",
                            "arguments": json.dumps(
                                {
                                    "steps": [
                                        {
                                            "id": "query",
                                            "title": "查询可比样本",
                                            "description": "根据用户条件查询数据",
                                            "tool_names": ["recommend"],
                                        }
                                    ]
                                }
                            ),
                        },
                        {
                            "id": "call-plan-query",
                            "name": "recommend",
                            "arguments": '{"budget_max_wan": 15}',
                        },
                    ]
                )
            ),
            _response(
                _message(
                    tool_calls=[
                        {
                            "id": "call-plan-complete",
                            "name": "update_plan",
                            "arguments": '{"updates":[{"id":"query","status":"complete"},{"id":"finalize","status":"running"}]}',
                        }
                    ]
                )
            ),
            _response(_message("我会按这个计划继续分析。")),
        ]
    )
    _patch_client(monkeypatch, client)
    from backend.agent_runtime.tool_registry import execute_registered_tool as real_execute
    monkeypatch.setattr(
        nodes,
        "execute_registered_tool",
        lambda name, args: real_execute(name, args) if name in {"declare_task", "set_plan", "update_plan", "ask_user", "finalize_answer"} else {
            "ok": True,
            "tool": name,
            "input": args,
            "recommendations": [{"series": "测试车系", "median_wan": 10.99, "sample_size": 12}],
        },
    )
    events = []

    result = loop.run_agent("15 万以内选车", event_sink=events.append)

    assert result["summary"].startswith("我会按这个计划继续分析。")
    assert result["state"]["plan"][0]["title"] == "查询可比样本"
    assert result["state"]["plan"][0]["status"] == "complete"
    assert any(event["event_type"] == "plan_update" for event in events)


def test_set_plan_is_initialized_once_and_update_plan_emits_authoritative_statuses(monkeypatch):
    state = nodes.AgentState.create("帮我选车", nodes.SYSTEM_PROMPT)
    state.task_intent = normalize_task_intent([
        {"id": "goal", "description": "整理分析结果", "deliverable": "分析结果"},
    ])
    trace = nodes.RunTrace.create(state.trace_id)

    nodes.tool_execute_node(
        state,
        trace,
        [
            {
                "id": "call-plan-1",
                "function": {
                    "name": "set_plan",
                    "arguments": json.dumps(
                        {
                            "steps": [
                                {"id": "inspect", "title": "查询数据", "description": "检查数据样本", "tool_names": ["csv_query"], "goal_ids": ["goal"], "kind": "data", "completion_criteria": "tool_result"},
                                {"id": "report", "title": "输出", "description": "输出结果", "tool_names": ["finalize_answer"], "goal_ids": ["goal"], "kind": "finalize", "completion_criteria": "validated_answer"},
                            ]
                        }
                    ),
                },
            }
        ],
        1,
    )
    original_plan = [dict(step) for step in state.plan]
    nodes.tool_execute_node(
        state,
        trace,
        [
            {
                "id": "call-plan-2",
                "function": {
                    "name": "set_plan",
                    "arguments": json.dumps({"steps": []}),
                },
            }
        ],
        2,
    )

    assert state.plan == original_plan
    assert state.tool_results[-1]["status"] == "error"

    state.tool_results.append({"tool_call_id": "inspect-result", "tool_name": "csv_query",
        "plan_step_id": "inspect", "goal_ids": ["goal"], "constraint_revision": 0,
        "status": "done", "output": {"ok": True}})

    nodes.tool_execute_node(
        state,
        trace,
        [
            {
                "id": "call-update-1",
                "function": {
                    "name": "update_plan",
                    "arguments": json.dumps({"updates": [{"id": "inspect", "status": "complete"}]}),
                },
            }
        ],
        3,
    )
    assert state.plan[0]["status"] == "complete"
    assert state.plan[1]["status"] == "queued"
    assert any(
        event["event_type"] == "plan_update"
        and event["output"]["steps"][0]["status"] == "complete"
        for event in trace.events
    )


def test_declare_task_set_plan_and_first_data_tool_can_share_one_model_batch(monkeypatch):
    state = AgentState.create("帮我查车源", nodes.SYSTEM_PROMPT)
    trace = RunTrace.create(state.trace_id)
    from backend.agent_runtime.tool_registry import execute_registered_tool as real_execute
    monkeypatch.setattr(
        nodes,
        "execute_registered_tool",
        lambda name, args: real_execute(name, args) if name in {"declare_task", "set_plan", "update_plan", "ask_user", "finalize_answer"} else {
            "ok": True, "tool": name, "input": args,
        },
    )
    checkpoints = []
    emitted_task_plan_states = []
    event_cursor = 0

    def checkpoint(snapshot):
        checkpoints.append(copy.deepcopy(snapshot))

    def emit_events():
        nonlocal event_cursor
        for event in trace.events[event_cursor:]:
            if event["event_type"] in {"task_intent_update", "plan_update"}:
                emitted_task_plan_states.append((event["event_type"], copy.deepcopy(checkpoints[-1])))
        event_cursor = len(trace.events)

    calls = [
        {"id": "same-batch-task", "function": {"name": "declare_task", "arguments": json.dumps({
            "goals": [{"id": "goal", "description": "查询车源", "deliverable": "车源样本"}],
            "requires_plan": False,
        })}},
        {"id": "same-batch-plan", "function": {"name": "set_plan", "arguments": json.dumps({"steps": [
            {"id": "query", "title": "查询车源", "description": "读取符合条件的车源", "tool_names": ["csv_query"], "goal_ids": ["goal"], "kind": "data", "completion_criteria": "tool_result"},
            {"id": "final", "title": "整理结果", "description": "核验并组织最终答案", "tool_names": ["finalize_answer"], "goal_ids": ["goal"], "kind": "finalize", "completion_criteria": "validated_answer"},
        ]})}},
        {"id": "same-batch-query", "function": {"name": "csv_query", "arguments": "{}"}},
    ]

    result = nodes.tool_execute_node(
        state, trace, calls, step=1,
        event_emitter=emit_events,
        checkpoint_sink=checkpoint,
    )

    assert result is None
    query_event = next(
        event for event in trace.events
        if event["event_type"] == "tool_call"
        and event["input"].get("tool_call_id") == "same-batch-query"
    )
    assert query_event["input"]["tool_call_id"] == "same-batch-query"
    assert query_event["input"]["plan_step_id"] == "query"
    assert [event_type for event_type, _ in emitted_task_plan_states] == ["task_intent_update", "plan_update"]
    task_event_state = emitted_task_plan_states[0][1]
    assert task_event_state["task_intent"]["needs_plan"] is True
    assert task_event_state["plan"] == []
    plan_event_state = emitted_task_plan_states[1][1]
    assert plan_event_state["plan"][0]["status"] == "running"
    assert plan_event_state["task_intent"]["goals"][0]["id"] == "goal"
    assert state.tool_results[-1]["plan_step_id"] == "query"


def test_invalid_finalize_answer_is_rejected_before_handler_or_started_event(monkeypatch):
    state = AgentState.create("查询挂牌价", nodes.SYSTEM_PROMPT)
    state.task_intent = normalize_task_intent([
        {"id": "goal", "description": "查询挂牌价", "deliverable": "挂牌价答案"},
    ])
    state.plan = [
        {"id": "query", "title": "查询数据", "description": "读取当前数据",
         "tool_names": ["csv_query"], "goal_ids": ["goal"], "kind": "data",
         "completion_criteria": "tool_result", "status": "complete"},
        {"id": "final", "title": "整理答案", "description": "校验并输出答案",
         "tool_names": ["finalize_answer"], "goal_ids": ["goal"], "kind": "finalize",
         "completion_criteria": "validated_answer", "status": "running"},
    ]
    from backend.data_loader import get_dataset_version
    state.tool_results.append({"tool_call_id": "query-result", "tool_name": "csv_query",
        "plan_step_id": "query", "goal_ids": ["goal"], "constraint_revision": 0,
        "status": "done", "output": {"ok": True}})
    state.evidence_records.append({"evidence_id": "ev-valid", "tool_call_id": "query-result",
        "dataset_version": get_dataset_version(), "constraint_revision": 0,
        "metric": "price.median_yuan", "value": 123456, "unit": "CNY",
        "filters": [], "sample_size": 10, "scope": "population", "goal_ids": ["goal"]})
    trace = RunTrace.create(state.trace_id)
    handler_calls = []
    monkeypatch.setattr(
        nodes, "execute_registered_tool",
        lambda name, args: handler_calls.append((name, args)) or {"ok": True, "control": name},
    )
    draft = {
        "schema_version": 2,
        "result_kind": "data_answer",
        "goal_ids": ["goal"],
        "blocks": [{"kind": "fact_ref", "evidence_id": "missing", "format": "sentence"}],
        "limitation_codes": [],
    }
    call = {"id": "finalize-1", "function": {"name": "finalize_answer", "arguments": json.dumps(draft)}}

    nodes.tool_execute_node(state, trace, [call], step=1)

    assert handler_calls == []
    assert not any(event["event_type"] == "tool_started" for event in trace.events)
    rejection = next(event for event in trace.events if event["event_type"] == "tool_rejected")
    assert rejection["output"]["error_code"] == "answer_validation_failed"


def test_ask_user_pauses_with_a_checkpoint_and_can_resume(monkeypatch):
    client = FakeClient(
        [
            _response(
                _message(
                    tool_calls=[
                        {
                            "id": "call-ask",
                            "name": "ask_user",
                            "arguments": json.dumps(
                                {
                                    "questions": [
                                        {
                                            "id": "budget",
                                            "prompt": "预算上限是多少？",
                                            "options": [{"id": "a", "label": "15 万", "value": "15"}],
                                            "allow_custom": True,
                                            "required": True,
                                        }
                                    ]
                                }
                            ),
                        }
                    ]
                )
            ),
            _response(_message("收到预算后完成分析。")),
        ]
    )
    _patch_client(monkeypatch, client)
    from backend.agent_runtime.tool_registry import execute_registered_tool as real_execute
    monkeypatch.setattr(
        nodes,
        "execute_registered_tool",
        lambda name, args: real_execute(name, args) if name in {"declare_task", "set_plan", "update_plan", "ask_user", "finalize_answer"} else {
            "ok": True,
            "tool": name,
            "input": args,
        },
    )
    events = []

    waiting = loop.run_agent("帮我选 SUV", event_sink=events.append)

    assert waiting["status"] == "waiting_user"
    assert waiting["state"]["pending_input"]["questions"][0]["id"] == "budget"
    assert waiting["checkpoint"]["pending_input"]["tool_call_id"] == "call-ask"
    assert any(event["event_type"] == "input_required" for event in events)

    resumed = loop.run_agent(
        "帮我选 SUV",
        resume_checkpoint=waiting["checkpoint"],
        input_answers=[{"question_id": "budget", "value": "15"}],
        event_sink=events.append,
    )

    assert resumed["status"] == "done"
    assert resumed["summary"] == "收到预算后完成分析。"
    assert resumed["state"]["flags"]["token_usage"]["model_calls"] == 2
    assert resumed["state"]["flags"]["token_usage"]["prompt_tokens"] == 20
    ask_tool_message = next(
        message for message in client.requests[1]["messages"]
        if message.get("role") == "tool" and message.get("tool_call_id") == "call-ask"
    )
    assert "15" in ask_tool_message["content"]


def test_multiple_ask_user_answers_round_trip_as_values_array(monkeypatch):
    client = FakeClient(
        [
            _response(
                _message(
                    tool_calls=[
                        {
                            "id": "call-multi-ask",
                            "name": "ask_user",
                            "arguments": json.dumps(
                                {
                                    "questions": [
                                        {
                                            "id": "brands",
                                            "prompt": "品牌？",
                                            "options": [
                                                {"id": "a", "label": "哈弗", "value": "哈弗"},
                                                {"id": "b", "label": "长安", "value": "长安"},
                                            ],
                                            "allow_custom": False,
                                            "required": True,
                                            "selection_mode": "multiple",
                                            "max_selections": 2,
                                        }
                                    ]
                                }
                            ),
                        }
                    ]
                )
            ),
            _response(_message("已按全部品牌继续分析。")),
        ]
    )
    _patch_client(monkeypatch, client)
    from backend.agent_runtime.tool_registry import execute_registered_tool as real_execute
    monkeypatch.setattr(
        nodes,
        "execute_registered_tool",
        lambda name, args: real_execute(name, args) if name in {"declare_task", "set_plan", "update_plan", "ask_user", "finalize_answer"} else {"ok": True, "tool": name, "input": args},
    )

    waiting = loop.run_agent("帮我比较几个品牌")
    resumed = loop.run_agent(
        "帮我比较几个品牌",
        resume_checkpoint=waiting["checkpoint"],
        input_answers=[{"question_id": "brands", "values": ["哈弗", "长安"]}],
    )

    assert waiting["state"]["pending_input"]["questions"][0]["selection_mode"] == "multiple"
    assert resumed["status"] == "done"
    tool_message = next(
        message for message in client.requests[1]["messages"]
        if message.get("role") == "tool" and message.get("tool_call_id") == "call-multi-ask"
    )
    assert '"values": ["哈弗", "长安"]' in tool_message["content"]


def test_resume_emits_ask_user_tool_completed_before_next_model_round(monkeypatch):
    client = FakeClient(
        [
            _response(
                _message(
                    tool_calls=[
                        {
                            "id": "call-ask-complete",
                            "name": "ask_user",
                            "arguments": json.dumps(
                                {
                                    "questions": [
                                        {
                                            "id": "budget",
                                            "prompt": "预算上限是多少？",
                                            "options": [{"id": "a", "label": "15 万", "value": "15"}],
                                            "allow_custom": True,
                                            "required": True,
                                        }
                                    ]
                                }
                            ),
                        }
                    ]
                )
            ),
            _response(_message("已收到预算，继续分析。")),
        ]
    )
    _patch_client(monkeypatch, client)
    from backend.agent_runtime.tool_registry import execute_registered_tool as real_execute

    monkeypatch.setattr(
        nodes,
        "execute_registered_tool",
        lambda name, args: real_execute(name, args) if name in {"declare_task", "set_plan", "update_plan", "ask_user", "finalize_answer"} else {
            "ok": True,
            "tool": name,
            "input": args,
        },
    )
    events = []

    waiting = loop.run_agent("帮我选车", event_sink=events.append)
    assert waiting["status"] == "waiting_user"

    resumed = loop.run_agent(
        "帮我选车",
        resume_checkpoint=waiting["checkpoint"],
        input_answers=[{"question_id": "budget", "value": "15"}],
        event_sink=events.append,
    )

    assert resumed["status"] == "done"
    completion_index = next(
        index
        for index, event in enumerate(events)
        if event["event_type"] == "tool_completed"
        and event.get("input", {}).get("tool_call_id") == "call-ask-complete"
    )
    resumed_model_index = max(
        index
        for index, event in enumerate(events)
        if event["event_type"] == "model_round"
    )
    assert completion_index > 0
    assert completion_index < resumed_model_index
    assert events[completion_index]["status"] == "done"


def test_unsupported_filter_followup_requires_a_user_decision(monkeypatch):
    monkeypatch.setattr(loop, "DEEPSEEK_MODEL", "deepseek-chat")
    monkeypatch.setattr(nodes, "DEEPSEEK_MODEL", "deepseek-chat")
    client = FakeClient(
        [
            _response(
                _message(
                    tool_calls=[
                        {
                            "id": "call-recommend-unsupported",
                            "name": "recommend",
                            "arguments": '{"budget_max_wan": 15, "body_type": "SUV"}',
                        }
                    ]
                )
            ),
            _response(
                _message(
                    tool_calls=[
                        {
                            "id": "call-next-step",
                            "name": "ask_user",
                            "arguments": json.dumps(
                                {
                                    "questions": [
                                        {
                                            "id": "next_step",
                                            "prompt": "接下来怎么处理？（SUV 条件当前无法生效）",
                                            "options": [
                                                {
                                                    "id": "accept",
                                                    "label": "接受当前结果（不限车型）",
                                                    "value": "accept_current",
                                                },
                                                {
                                                    "id": "brand",
                                                    "label": "限定品牌重算",
                                                    "value": "limit_brand",
                                                },
                                            ],
                                            "allow_custom": True,
                                            "required": True,
                                        }
                                    ]
                                }
                            ),
                        }
                    ]
                )
            ),
            _response(_message(tool_calls=[{
                "id": "call-retry-without-unsupported-filter", "name": "recommend",
                "arguments": json.dumps({"budget_max_wan": 15}),
            }])),
            _response(_message("已按你的选择继续分析，SUV 条件未纳入筛选。")),
        ]
    )
    monkeypatch.setattr(loop, "get_client", lambda: client)
    events = []

    def execute(name, args):
        if name in {"declare_task", "set_plan", "update_plan", "finalize_answer"}:
            from backend.agent_runtime.tool_registry import execute_registered_tool as real_execute
            return real_execute(name, args)
        if name == "recommend" and args.get("body_type"):
            return {
                "ok": False,
                "tool": "recommend",
                "result_status": "error",
                "reason_code": "unsupported_filter",
                "observed": {"unsupported_field": "body_type"},
                "note": "body_type 不支持",
            }
        if name == "recommend":
            return _fake_success_output(name, args)
        if name == "ask_user":
            return {"ok": True, "control": "ask_user", "questions": args.get("questions", [])}
        return {"ok": True, "tool": name, "input": args}

    monkeypatch.setattr(nodes, "execute_registered_tool", execute)

    waiting = loop.run_agent("15 万以内的 SUV 推荐几款", event_sink=events.append)

    assert waiting["status"] == "waiting_user"
    assert client.requests[1]["tool_choice"] == {"type": "function", "function": {"name": "ask_user"}}
    assert waiting["state"]["pending_input"]["questions"][0]["id"] == "next_step"
    assert waiting["checkpoint"]["answered_inputs"] == []
    assert [event["event_type"] for event in events].count("input_required") == 1

    result = loop.run_agent(
        "15 万以内的 SUV 推荐几款",
        resume_checkpoint=waiting["checkpoint"],
        input_answers=[{"question_id": "next_step", "value": "accept_current"}],
        event_sink=events.append,
    )

    assert result["status"] == "done"
    assert result["summary"].startswith("已按你的选择继续分析，SUV 条件未纳入筛选。")
    assert client.requests[2]["tool_choice"] == "auto"


def test_same_user_decision_is_not_asked_again_after_resume(monkeypatch):
    client = FakeClient(
        [
            _response(
                _message(
                    tool_calls=[
                        {
                            "id": "call-ask-first",
                            "name": "ask_user",
                            "arguments": '{"questions":[{"id":"body","prompt":"车型偏好？","options":[{"id":"suv","label":"SUV","value":"body_type=SUV"}],"allow_custom":true,"required":true}]}' ,
                        }
                    ]
                )
            ),
            _response(
                _message(
                    tool_calls=[
                        {
                            "id": "call-ask-again",
                            "name": "ask_user",
                            "arguments": '{"questions":[{"id":"body_again","prompt":"车型偏好？","options":[{"id":"suv","label":"SUV","value":"body_type=SUV"}],"allow_custom":true,"required":true}]}' ,
                        }
                    ]
                )
            ),
            _response(_message("已继续。")),
        ]
    )
    monkeypatch.setattr(loop, "get_client", lambda: client)
    from backend.agent_runtime.tool_registry import execute_registered_tool as real_execute
    monkeypatch.setattr(nodes, "execute_registered_tool", real_execute)
    events = []

    waiting = loop.run_agent("帮我选 SUV", event_sink=events.append)
    assert waiting["status"] == "waiting_user"

    done = loop.run_agent(
        "帮我选 SUV",
        resume_checkpoint=waiting["checkpoint"],
        input_answers=[{"question_id": "body", "value": "body_type=SUV"}],
        event_sink=events.append,
    )

    assert done["status"] == "done", done
    assert done["summary"] == "已继续。"
    assert [event["event_type"] for event in events].count("input_required") == 1


def test_changed_question_with_same_id_is_not_silently_reused(monkeypatch):
    client = FakeClient(
        [
            _response(
                _message(
                    tool_calls=[
                        {
                            "id": "call-ask-first",
                            "name": "ask_user",
                            "arguments": '{"questions":[{"id":"body","prompt":"车型偏好？","options":[{"id":"suv","label":"SUV","value":"body_type=SUV"}],"allow_custom":true,"required":true}]}' ,
                        }
                    ]
                )
            ),
            _response(
                _message(
                    tool_calls=[
                        {
                            "id": "call-ask-revised",
                            "name": "ask_user",
                            "arguments": '{"questions":[{"id":"body","prompt":"SUV 条件无法生效，你希望怎么继续？","options":[{"id":"accept","label":"按其他条件继续","value":"accept_current"}],"allow_custom":true,"required":true}]}' ,
                        }
                    ]
                )
            ),
        ]
    )
    monkeypatch.setattr(loop, "get_client", lambda: client)
    from backend.agent_runtime.tool_registry import execute_registered_tool as real_execute
    monkeypatch.setattr(nodes, "execute_registered_tool", real_execute)

    waiting = loop.run_agent("帮我选 SUV")
    resumed = loop.run_agent(
        "帮我选 SUV",
        resume_checkpoint=waiting["checkpoint"],
        input_answers=[{"question_id": "body", "value": "body_type=SUV"}],
    )

    assert resumed["status"] == "waiting_user"
    assert resumed["state"]["pending_input"]["questions"][0]["prompt"] == "SUV 条件无法生效，你希望怎么继续？"


def test_react_plan_question_tool_and_data_tool_finish_in_one_run(monkeypatch):
    from backend.agent_runtime.evidence import evidence_from_tool_result
    evidence = evidence_from_tool_result(
        "recommend", {"ok": True, "result_status": "success", "recommendations": [], "budget_count": 10},
        "call-recommend", constraint_revision=1,
    )[0]
    answer_draft = {
        "schema_version": 2,
        "result_kind": "data_answer",
        "goal_ids": ["test_goal"],
        "blocks": [
            {"kind": "text", "text": "预算内车源统计已经通过受信证据核验。"},
            {"kind": "fact_ref", "evidence_id": evidence["evidence_id"], "format": "sentence"},
        ],
        "limitation_codes": [],
    }
    client = FakeClient(
        [
            _response(
                _message(
                    tool_calls=[
                        {
                            "id": "call-plan",
        "name": "set_plan",
                            "arguments": '{"steps":[{"id":"clarify","title":"确认预算","description":"收集预算条件","tool_names":["ask_user"],"kind":"clarification","completion_criteria":"user_answer","goal_ids":["test_goal"]},{"id":"query","title":"查询样本","description":"按条件查询","tool_names":["recommend"],"kind":"data","completion_criteria":"tool_result","goal_ids":["test_goal"]},{"id":"finalize","title":"核验答案","description":"形成最终答案","tool_names":["finalize_answer"],"kind":"finalize","completion_criteria":"validated_answer","goal_ids":["test_goal"]}]}',
                        }
                    ]
                )
            ),
            _response(
                _message(
                    tool_calls=[
                        {
                            "id": "call-ask",
                            "name": "ask_user",
                            "arguments": '{"questions":[{"id":"budget","prompt":"预算？","options":[],"allow_custom":true,"required":true}]}',
                        }
                    ]
                )
            ),
            _response(
                _message(
                    tool_calls=[
                        {
                            "id": "call-recommend",
                            "name": "recommend",
                            "arguments": '{"budget_max_wan":15}',
                        }
                    ]
                )
            ),
            _response(
                _message(
                    tool_calls=[
                        {
                            "id": "call-plan-complete",
                            "name": "update_plan",
                            "arguments": '{"updates":[{"id":"query","status":"complete"},{"id":"finalize","status":"running"}]}',
                        }
                    ]
                )
            ),
            _response(_message(tool_calls=[{
                "id": "call-finalize",
                "name": "finalize_answer",
                "arguments": json.dumps(answer_draft),
            }])),
        ]
    )
    monkeypatch.setattr(loop, "get_client", lambda: client)
    from backend.agent_runtime.tool_registry import execute_registered_tool as real_execute
    monkeypatch.setattr(
        nodes,
        "execute_registered_tool",
        lambda name, args: real_execute(name, args)
        if name in {"declare_task", "set_plan", "ask_user", "update_plan", "finalize_answer"}
        else {"ok": True, "result_status": "success", "tool": name, "recommendations": [], "budget_count": 10},
    )
    events = []

    waiting = loop.run_agent("帮我选车", run_id="run-react", event_sink=events.append)
    assert waiting["status"] == "waiting_user"
    assert [event["event_type"] for event in events].count("plan_update") == 1
    assert any(event["event_type"] == "input_required" for event in events)

    done = loop.run_agent(
        "帮我选车",
        run_id="run-react",
        resume_checkpoint=waiting["checkpoint"],
        input_answers=[{"question_id": "budget", "value": "15"}],
        event_sink=events.append,
    )

    assert done["status"] == "done"
    assert done["summary_source"] == "validated"
    assert done["summary"] != "已根据你的预算完成分析。"
    assert any(event["event_type"] == "tool_completed" for event in events)
    assert any(message.get("role") == "tool" and message.get("name") == "recommend"
               for request in client.requests for message in request["messages"])


def test_session_context_is_replayed_on_the_next_turn(monkeypatch, tmp_path):
    client = FakeClient(
        [_response(_message(text)) for text in ["已记住你的预算。"] * 8
         + ["基于刚才的 15 万预算继续分析。"] * 8], auto_data=False
    )
    _patch_client(monkeypatch, client)
    store = SessionStore(tmp_path / "sessions")

    loop.run_agent("我的预算是 15 万", session_id="chat-1", run_id="run-1", session_store=store)
    result = loop.run_agent(
        "继续基于上一轮信息分析",
        session_id="chat-1",
        run_id="run-2",
        session_store=store,
    )

    messages = _request_with_user_content(client, "继续基于上一轮信息分析")["messages"]
    user_contents = [message["content"] for message in messages if message.get("role") == "user"]
    assert user_contents[-2:] == ["我的预算是 15 万", "继续基于上一轮信息分析"]
    assert result["state"]["flags"]["prefix_hash"]
    assert len(store.active_messages("chat-1")) >= 8


@pytest.mark.parametrize("storage", ["json", "sqlite"])
def test_owned_new_context_can_start_and_replay_history(monkeypatch, tmp_path, storage):
    store = (SessionStore(tmp_path / "sessions") if storage == "json"
             else SQLiteSessionStore(SQLiteRuntimeRepository(tmp_path / "runtime.sqlite3")))
    client = FakeClient([_response(_message(text)) for text in ["已记住预算。"] * 8 + ["继续比较。"] * 8], auto_data=False)
    _patch_client(monkeypatch, client)

    first = loop.run_agent(
        "预算 15 万", session_id="history-owned", context_id="context-owned",
        run_id="owned-1", session_store=store, owner_id="owner-a",
    )
    second = loop.run_agent(
        "继续比较", session_id="history-owned", context_id="context-owned",
        run_id="owned-2", session_store=store, owner_id="owner-a",
    )

    assert first["status"] == second["status"] == "done"
    assert [m["content"] for m in _request_with_user_content(client, "继续比较")["messages"] if m["role"] == "user"][-2:] == ["预算 15 万", "继续比较"]
    assert len(store.active_messages("context-owned", owner_id="owner-a")) >= 8


@pytest.mark.parametrize("storage", ["json", "sqlite"])
def test_owned_context_cannot_be_taken_over_before_model_request(monkeypatch, tmp_path, storage):
    store = (SessionStore(tmp_path / "sessions") if storage == "json"
             else SQLiteSessionStore(SQLiteRuntimeRepository(tmp_path / "runtime.sqlite3")))
    store.append_messages("context-private", [{"role": "user", "content": "私人预算"}], owner_id="owner-a")
    client = FakeClient([_response(_message("不应执行"))])
    _patch_client(monkeypatch, client)

    with pytest.raises(KeyError, match="会话不存在"):
        loop.run_agent(
            "读取其他用户预算", session_id="history-other", context_id="context-private",
            run_id="other-run", session_store=store, owner_id="owner-b",
        )

    assert client.requests == []
    assert store.active_messages("context-private", owner_id="owner-a") == [{"role": "user", "content": "私人预算"}]


def test_http_submissions_with_new_contexts_reach_real_agent_loop(monkeypatch, tmp_path):
    import time
    from fastapi.testclient import TestClient
    from backend.service import create_app

    model = FakeClient([_response(_message(text)) for text in ["第一份回答"] * 8 + ["第二份回答"] * 8], auto_data=False)
    _patch_client(monkeypatch, model)
    repository = SQLiteRuntimeRepository(tmp_path / "http.sqlite3")
    app = create_app(runtime_repository=repository, runner=loop.run_agent, owner_cookie_secret="session-regression")

    with TestClient(app) as client:
        for index in (1, 2):
            accepted = client.post("/v1/runs", json={
                "session_id": "history-http", "context_id": f"context-http-{index}",
                "input": f"问题{index}",
            })
            assert accepted.status_code == 202
            run_id = accepted.json()["run_id"]
            for _ in range(100):
                response = client.get(f"/v1/runs/{run_id}")
                assert response.status_code == 200
                snapshot = response.json()
                if snapshot["status"] in {"done", "error"}:
                    break
                time.sleep(0.01)
            assert snapshot["status"] == "done", snapshot.get("error")
        assert len(client.get("/v1/history?session_id=history-http").json()) == 2

    assert len(model.requests) >= 4
    assert [m["content"] for m in _request_with_user_content(model, "问题2")["messages"] if m["role"] == "user"] == ["问题2"]


def test_same_history_session_does_not_cross_contaminate_distinct_contexts(monkeypatch, tmp_path):
    client = FakeClient([_response(_message(text)) for text in ["可以分析。"] * 8 + ["可以继续分析。"] * 8], auto_data=False)
    _patch_client(monkeypatch, client)
    store = SessionStore(tmp_path / "sessions")

    loop.run_agent(
        "第一条问题",
        session_id="history-1",
        context_id="context-1",
        run_id="run-1",
        session_store=store,
    )
    loop.run_agent(
        "第二条问题",
        session_id="history-1",
        context_id="context-2",
        run_id="run-2",
        session_store=store,
    )

    assert [message["content"] for message in _request_with_user_content(client, "第二条问题")["messages"] if message["role"] == "user"] == ["第二条问题"]
    assert any(message["content"] == "第一条问题" for message in store.active_messages("context-1"))
    assert any(message["content"] == "第二条问题" for message in store.active_messages("context-2"))


def test_skill_routes_are_dynamic_context_and_load_skill_schema_is_stable(monkeypatch, tmp_path):
    skill_dir = tmp_path / ".agent" / "skills" / "budget"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# 预算\n按预算分层分析。", encoding="utf-8")
    client = FakeClient([_response(_message("我可以按预算继续分析。"))])
    _patch_client(monkeypatch, client)

    loop.run_agent("继续分析", skills_root=tmp_path)

    messages = client.requests[0]["messages"]
    assert messages[0]["role"] == "system"
    assert messages[-1]["role"] == "system"
    assert any("budget" in message.get("content", "") for message in messages if message.get("role") == "system")
    assert "load_skill" in {
        item["function"]["name"] for item in client.requests[0]["tools"]
    }


def test_mcp_schema_is_frozen_into_prefix_and_tool_result_returns_to_agent(monkeypatch):
    class Provider:
        def list_tools(self):
            return [MCPToolDefinition("quote", "外部报价适配器", {"type": "object"}, "market")]

        def call_tool(self, name, arguments):
            return {"ok": True, "tool": name, "price_wan": 12.3}

        def close(self):
            return None

    manager = MCPManager()
    manager.register("market", Provider(), {"quote"})
    tool_name = "mcp__market__quote"
    client = FakeClient(
        [
            _response(
                _message(
                    tool_calls=[
                        {"id": "mcp-call", "name": tool_name, "arguments": "{}"},
                    ]
                )
            ),
            _response(_message("已读取 MCP 外部报价工具的返回结果。")),
            _response(_message("")),
        ]
    )
    _patch_client(monkeypatch, client)

    result = loop.run_agent("查询扩展报价", mcp_manager=manager)

    assert result["summary"].startswith("已读取 MCP 外部报价工具的返回结果。")
    assert tool_name in {item["function"]["name"] for item in client.requests[0]["tools"]}
    assert next(
        item for item in result["state"]["tool_results"] if item["tool_name"] == tool_name
    )["output"]["price_wan"] == 12.3
    assert any(
        message.get("role") == "tool" and message.get("tool_call_id") == "mcp-call"
        and "12.3" in str(message.get("content") or "")
        for message in client.requests[1]["messages"]
    )


def test_model_truncation_retry_recomputes_deadline_and_settles_each_response(monkeypatch):
    import backend.agent_runtime.execution as execution

    clock = [100.0]
    monkeypatch.setattr(execution.time, "monotonic", lambda: clock[0])
    state = AgentState.create("请回答", nodes.SYSTEM_PROMPT)
    cancel = threading.Event()

    class Client:
        def __init__(self):
            self.requests = []

        def complete(self, **request):
            self.requests.append(request)
            if len(self.requests) == 1:
                clock[0] += 8.0
                return _response(_message("partial"), finish_reason="length")
            return _response(_message("complete"))

    client = Client()
    prefix = PrefixContext("test", nodes.SYSTEM_PROMPT, [], "prefix", False)
    result = nodes.agent_execute_node(
        state, RunTrace.create("deadline-test"), client, 1, prefix=prefix,
        execution_context=ExecutionContext("run", "segment", cancel, 110.0),
    )
    assert result["assistant"]["content"] == "complete"
    assert client.requests[0]["timeout"] == 10.0
    assert client.requests[1]["timeout"] == 2.0
    assert state.flags["token_usage"]["total_tokens"] == 30


def test_model_empty_response_usage_is_settled_before_rejection():
    state = AgentState.create("请回答", nodes.SYSTEM_PROMPT)
    client = FakeClient([_response(_message(""), usage={"prompt_tokens": 13, "completion_tokens": 2, "total_tokens": 15})])
    prefix = PrefixContext("test", nodes.SYSTEM_PROMPT, [], "prefix", False)
    result = nodes.agent_execute_node(state, RunTrace.create("empty-usage"), client, 1, prefix=prefix)
    assert result["error_code"] == "model_empty_response"
    assert state.flags["token_usage"]["total_tokens"] == 15

def test_stale_evidence_cannot_publish_a_report_after_constraints_change(monkeypatch):
    stale_answer = {
        "schema_version": 2, "result_kind": "data_answer", "goal_ids": ["test_goal"],
        "blocks": [{"kind": "fact_ref", "evidence_id": "old", "format": "sentence"}],
        "limitation_codes": [],
    }
    responses = [
        _response(_message(tool_calls=[
            {"id": "open-finalize", "name": "update_plan",
             "arguments": '{"updates":[{"id":"finalize","status":"running"}]}'},
            {"id": f"stale-finalize-{index}", "name": "finalize_answer", "arguments": json.dumps(stale_answer)},
        ])) if index == 1 else _response(_message(tool_calls=[
            {"id": f"stale-finalize-{index}", "name": "finalize_answer", "arguments": json.dumps(stale_answer)},
        ]))
        for index in (1, 2, 3)
    ]
    client = FakeClient(responses, auto_declare=False)
    monkeypatch.setattr(loop, "get_client", lambda: client)
    state = nodes.AgentState.create("choose", nodes.SYSTEM_PROMPT)
    state.flags["original_user_text"] = "choose"
    state.task_intent = normalize_task_intent([{
        "id": "test_goal", "description": "完成当前车辆分析", "deliverable": "核验旧证据是否仍可用",
    }])
    state.task_context = {"goal": "choose", "constraints": {"budget_max_wan": {"value": 15, "status": "confirmed"}},
                          "decisions": [], "rejected_options": [], "evidence_refs": [], "blockers": [], "revision": 1}
    state.plan = [
        {"id": "query", "title": "query", "description": "query", "tool_names": ["csv_query"],
         "goal_ids": ["test_goal"], "kind": "data", "completion_criteria": "tool_result", "status": "complete"},
        {"id": "finalize", "title": "validate answer", "description": "submit v2 answer",
         "tool_names": ["finalize_answer"], "goal_ids": ["test_goal"], "kind": "finalize",
         "completion_criteria": "validated_answer", "status": "queued"},
    ]
    state.evidence_records = [{"evidence_id": "old", "constraint_revision": 1, "metric": "count", "value": 3,
                               "unit": "count", "sample_size": 3, "scope": "filtered",
                               "dataset_version": "old-dataset", "tool_call_id": "query-call",
                               "goal_ids": ["test_goal"]}]
    from backend.agent_runtime.evidence import evidence_from_tool_result
    from backend.data_loader import get_dataset_version
    query_output = {"ok": True, "match_count": 3, "rows": []}
    state.tool_results = [{"tool_call_id": "query-call", "tool_name": "csv_query", "plan_step_id": "query",
        "goal_ids": ["test_goal"], "status": "done", "constraint_revision": 1, "output": query_output}]
    current_evidence = evidence_from_tool_result("csv_query", query_output, "query-call",
        dataset_version=get_dataset_version(), constraint_revision=1)
    for item in current_evidence:
        item["goal_ids"] = ["test_goal"]
    state.evidence_records.extend(current_evidence)
    result = loop.run_agent("choose", resume_checkpoint=state.to_checkpoint())
    assert result["status"] == "error"
    assert result["summary_source"] == "runtime"
    assert "report" not in result
    assert result["failure_code"] == "evidence_stale"

def test_steering_budget_replaces_snapshot_confirmed_filter(monkeypatch):
    from backend.agent_runtime.task_context import initial_task_context
    from backend.agent_runtime.evidence import evidence_from_tool_result
    query_output = _fake_success_output("csv_query", {})
    evidence_id = evidence_from_tool_result("csv_query", query_output, "steering-query", constraint_revision=1)[0]["evidence_id"]
    answer = {"schema_version": 2, "result_kind": "data_answer", "goal_ids": ["test_goal"],
        "blocks": [{"kind": "text", "text": "已按更新后的条件重新查询。"},
            {"kind": "fact_ref", "evidence_id": evidence_id, "format": "sentence"}], "limitation_codes": []}
    question = normalize_questions([{"id": "budget", "prompt": "预算？", "options": [],
        "allow_custom": True, "required": True}])[0]
    client = FakeClient([
        _response(_message(tool_calls=[
            {"id": "steering-open-data", "name": "update_plan", "arguments": json.dumps({"updates": [
                {"id": "clarify", "status": "complete"}, {"id": "query", "status": "running"},
            ]}, ensure_ascii=False)},
            {"id": "steering-query", "name": "csv_query", "arguments": "{}"},
        ])),
        _response(_message(tool_calls=[
            {"id": "steering-open-finalize", "name": "update_plan", "arguments": json.dumps({"updates": [
                {"id": "query", "status": "complete"}, {"id": "finalize", "status": "running"},
            ]}, ensure_ascii=False)},
            {"id": "steering-finalize", "name": "finalize_answer", "arguments": json.dumps(answer, ensure_ascii=False)},
        ])),
    ], auto_declare=False)
    monkeypatch.setattr(loop, "get_client", lambda: client)
    state = nodes.AgentState.create("choose", nodes.SYSTEM_PROMPT)
    state.task_context = initial_task_context("choose", {"budget_max_wan": 20})
    state.task_intent = normalize_task_intent([{"id": "test_goal", "description": "按最新预算查询车辆",
        "deliverable": "返回当前条件下可用的车源证据"}])
    state.plan = [
        {"id": "clarify", "title": "确认预算", "description": "记录用户预算", "tool_names": ["ask_user"],
         "goal_ids": ["test_goal"], "kind": "clarification", "completion_criteria": "user_answer", "status": "complete"},
        {"id": "query", "title": "按新预算查询", "description": "使用更新后的预算查找车源", "tool_names": ["csv_query"],
         "goal_ids": ["test_goal"], "kind": "data", "completion_criteria": "tool_result", "status": "queued"},
        {"id": "finalize", "title": "核验最终答案", "description": "提交结构化答案", "tool_names": ["finalize_answer"],
         "goal_ids": ["test_goal"], "kind": "finalize", "completion_criteria": "validated_answer", "status": "queued"},
    ]
    ask_call = {"id": "ask-old", "type": "function", "function": {"name": "ask_user", "arguments": json.dumps({"questions": [question]}, ensure_ascii=False)}}
    state.messages.append({"role": "assistant", "tool_calls": [ask_call]})
    state.messages.append({"role": "tool", "tool_call_id": "ask-old", "name": "ask_user",
        "content": json.dumps({"ok": True, "control": "ask_user", "questions": [question]}, ensure_ascii=False)})
    state.answered_inputs.append({"question_id": "budget", "prompt": question["prompt"],
        "fingerprint": question_fingerprint(question), "values": ["20"], "value": "20",
        "tool_call_id": "ask-old", "source": "user"})
    state.tool_results.append({"tool_call_id": "ask-old", "tool_name": "ask_user", "status": "done",
        "plan_step_id": "clarify", "goal_ids": ["test_goal"], "constraint_revision": 0,
        "input": {"questions": [question]}, "output": {"ok": True, "questions": [question]}})
    pending_messages = [{"client_message_id": "correct-1", "content": "budget is 15", "revision": 1}]

    def message_source():
        return [pending_messages.pop(0)] if pending_messages else []

    result = loop.run_agent(
        "choose", resume_checkpoint=state.to_checkpoint(), input_answers=None,
        message_source=message_source,
    )
    assert result["status"] == "done"
    assert result["confirmed_filters"]["budget_max_wan"] == 15.0

def test_report_input_drops_old_domain_results_when_new_constraints_have_partial_data():
    state = nodes.AgentState.create("choose", nodes.SYSTEM_PROMPT)
    state.task_context = {"goal": "choose", "constraints": {}, "revision": 1}
    result = {
        "tool_results": [
            {"tool_name": "recommend", "constraint_revision": 0, "output": {"ok": True}},
            {"tool_name": "price_compare", "constraint_revision": 1, "output": {"ok": True}},
            {"tool_name": "update_plan", "constraint_revision": 0, "output": {"ok": True}},
        ],
    }
    safe = loop._current_report_result(state, result)
    assert [item["tool_name"] for item in safe["tool_results"]] == ["price_compare", "update_plan"]


def test_task_context_prompt_exposes_current_claimable_evidence():
    state = nodes.AgentState.create("比较挂牌价", nodes.SYSTEM_PROMPT)
    state.task_context = {"revision": 2}
    state.evidence_records = [
        {"evidence_id": "ev-current", "constraint_revision": 2, "metric": "price.median_yuan",
         "value": 162800, "unit": "CNY", "scope": "population", "sample_size": 54},
        {"evidence_id": "ev-stale", "constraint_revision": 1, "metric": "price.mean_yuan",
         "value": 160000, "unit": "CNY", "scope": "population", "sample_size": 50},
    ]

    loop._sync_task_context_message(state)

    prompt = state.messages[-1]["content"]
    assert "ev-current" in prompt
    assert "price.median_yuan" in prompt
    assert "ev-stale" not in prompt
