import hashlib
import json

from backend.agent_runtime import nodes, tool_policy
from backend.agent_runtime import loop as agent_loop
from backend.agent_runtime.state import AgentState
from test_agent_loop import FakeClient, _message, _response


def _ambiguous_state():
    state = AgentState.create("分析汉", nodes.SYSTEM_PROMPT)
    state.task_context = {"revision": 2, "constraints": {"city": {"value": ["北京", "上海"]}}}
    state.task_intent = {"goals": [{"id": "g1"}]}
    state.plan = [{"id": "data", "kind": "data", "status": "running", "tool_names": ["region_gap"], "goal_ids": ["g1"]}]
    call_id = "call-ambiguous"
    output = {
        "ok": False, "result_status": "error", "reason_code": "ambiguous_series",
        "requested_series": "汉", "candidate_selectors": [
            {"brand_name": "比亚迪", "series": "汉DM"},
            {"brand_name": "比亚迪", "series": "汉DM-i"},
        ], "repair_hint": "请用户选择车型，并保留城市条件。",
    }
    state.tool_results.append({
        "tool_call_id": call_id, "tool_name": "region_gap", "status": "error", "output": output,
        "plan_step_id": "data", "goal_ids": ["g1"], "constraint_revision": 2,
    })
    nodes._track_tool_recovery(state, "region_gap", {"filters": {"series": "汉"}}, output,
                               tool_call_id=call_id, plan_step_id="data", goal_ids=["g1"])
    return state


def test_ambiguous_series_error_authorizes_only_bound_ask_user():
    state = _ambiguous_state()
    call = {"id": "ask", "type": "function", "function": {"name": "ask_user", "arguments": {
        "questions": [{"id": "series", "prompt": "请选择车型", "options": [
            {"id": "dm", "label": "汉DM", "value": "汉DM"},
            {"id": "dm_i", "label": "汉DM-i", "value": "汉DM-i"}], "allow_custom": False,
                       "selection_mode": "single", "required": True}]
    }}}

    result = tool_policy.preflight_tool_call(call, state)

    assert not isinstance(result, tool_policy.Reject)
    assert state.flags["pending_user_decision"]["reason_code"] == "ambiguous_series"


def test_ambiguous_series_permission_requires_matching_bound_result():
    state = _ambiguous_state()
    state.plan[0]["goal_ids"] = ["other"]
    call = {"id": "ask", "type": "function", "function": {"name": "ask_user", "arguments": {
        "questions": [{"id": "series", "prompt": "请选择车型", "options": [
            {"id": "dm", "label": "汉DM", "value": "汉DM"}], "allow_custom": False, "required": True}]
    }}}

    result = tool_policy.preflight_tool_call(call, state)

    assert isinstance(result, tool_policy.Reject)
    assert result.error_code == "clarification_step_required"


def test_unknown_catalog_keyword_without_candidates_allows_bound_correction_question():
    state = _ambiguous_state()
    result = state.tool_results[-1]
    result["tool_name"] = "price_compare"
    output = {"ok": False, "result_status": "error", "reason_code": "unknown_brand",
              "keyword_field": "brand", "requested_keyword": "错别字品牌",
              "candidate_values": [], "candidate_selectors": [],
              "normalized_filters": {"cities": ["北京"]}, "dataset_version": "snapshot-1"}
    result["output"] = output
    state.plan[0]["tool_names"] = ["price_compare"]
    state.flags.pop("pending_user_decision", None)
    nodes._track_tool_recovery(state, "price_compare", {"brand": "错别字品牌"}, output,
                               tool_call_id="call-ambiguous", plan_step_id="data", goal_ids=["g1"])
    call = {"id": "ask-correct", "type": "function", "function": {"name": "ask_user", "arguments": {
        "questions": [{"id": "brand", "prompt": "请更正品牌名称", "options": [], "allow_custom": True,
                       "selection_mode": "single", "required": True}]
    }}}

    assert not isinstance(tool_policy.preflight_tool_call(call, state), tool_policy.Reject)
    assert state.flags["pending_user_decision"]["candidate_values"] == []


def test_catalog_recovery_binding_rejects_stale_revision_or_unbound_result():
    state = _ambiguous_state()
    result = state.tool_results[-1]
    result["output"]["reason_code"] = "unknown_city"
    result["output"]["candidate_values"] = []
    result["output"].pop("candidate_selectors", None)
    result["tool_name"] = "price_compare"
    state.flags["pending_user_decision"]["reason_code"] = "unknown_city"
    state.flags["pending_user_decision"]["tool_name"] = "price_compare"
    state.plan[0]["tool_names"] = ["price_compare"]
    state.task_context["revision"] = 3
    call = {"id": "ask", "type": "function", "function": {"name": "ask_user", "arguments": {
        "questions": [{"id": "city", "prompt": "请更正城市名称", "options": [], "allow_custom": True,
                       "selection_mode": "single", "required": True}]
    }}}

    assert isinstance(tool_policy.preflight_tool_call(call, state), tool_policy.Reject)


def test_empty_recovery_budget_is_scoped_and_success_resets_scope():
    state = AgentState.create("对比多车型", nodes.SYSTEM_PROMPT)
    state.task_context = {"revision": 3}
    state.plan = [{"id": "one", "kind": "data", "status": "running", "goal_ids": ["g1"]}]
    for _ in range(3):
        nodes._record_empty_recovery_result(state, "empty", "region_gap")
    assert nodes._empty_recovery_attempts_for_step(state, state.plan[0]) == 3
    state.plan = [{"id": "two", "kind": "data", "status": "running", "goal_ids": ["g2"]}]
    assert nodes._empty_recovery_attempts_for_step(state, state.plan[0]) == 0
    nodes._record_empty_recovery_result(state, "success", "region_gap")
    assert nodes._empty_recovery_attempts_for_step(state, state.plan[0]) == 0


def test_control_success_does_not_reset_empty_streak_and_revision_scopes_it():
    state = AgentState.create("对比车型", nodes.SYSTEM_PROMPT)
    state.task_context = {"revision": 3}
    state.plan = [{"id": "one", "kind": "data", "status": "running", "goal_ids": ["g1"]}]
    nodes._record_empty_recovery_result(state, "empty", "region_gap")
    nodes._record_empty_recovery_result(state, "success", "load_skill")
    assert nodes._empty_recovery_attempts_for_step(state, state.plan[0]) == 1
    state.task_context["revision"] = 4
    assert nodes._empty_recovery_attempts_for_step(state, state.plan[0]) == 0
    # An old, unscoped checkpoint counter cannot safely be attributed to a goal/revision.
    state.flags["empty_recovery_attempts"] = 7
    assert nodes._empty_recovery_attempts_for_step(state, state.plan[0]) == 0


def test_ambiguous_series_question_resumes_from_checkpoint_and_retries_data(monkeypatch):
    client = FakeClient([
        _response(_message(tool_calls=[
            {"id": "declare", "name": "declare_task", "arguments":
             '{"goals":[{"id":"g1","description":"查询车型","deliverable":"样本"}],"requires_plan":true}'},
            {"id": "plan", "name": "set_plan", "arguments":
             '{"steps":[{"id":"query","title":"查询车型","description":"按选中条件查询","tool_names":["region_gap"],"goal_ids":["g1"],"kind":"data","completion_criteria":"tool_result"},{"id":"final","title":"整理结果","description":"回答","tool_names":["finalize_answer"],"goal_ids":["g1"],"kind":"finalize","completion_criteria":"validated_answer"}]}'},
        ])),
        _response(_message(tool_calls=[{"id": "query-ambiguous", "name": "region_gap", "arguments":
            '{"series":"汉","filters":{"series_selectors":[{"brand_name":"比亚迪","series":"汉"}],"cities":["北京","上海"]}}'}])),
        _response(_message(tool_calls=[{"id": "ask-series", "name": "ask_user", "arguments":
                '{"questions":[{"id":"series","prompt":"请选择车型","options":[{"id":"dm","label":"汉DM","value":"汉DM"},{"id":"ev","label":"汉EV","value":"汉EV"}],"allow_custom":false,"required":true,"selection_mode":"single"}]}'}])),
        _response(_message(tool_calls=[{"id": "query-retry", "name": "region_gap", "arguments":
            '{"series":"汉DM","filters":{"series_selectors":[{"brand_name":"比亚迪","series":"汉DM"}],"cities":["北京","上海"]}}'}])),
        _response(_message(tool_calls=[
            {"id": "complete-query", "name": "update_plan", "arguments":
             '{"updates":[{"id":"query","status":"complete"},{"id":"final","status":"running"}]}'},
            {"id": "final-answer", "name": "finalize_answer", "arguments": "__FINAL_ANSWER__"},
        ])),
    ], auto_declare=False)
    monkeypatch.setattr(agent_loop, "get_client", lambda: client)
    from backend.data_loader import get_dataset_version
    dataset_version = get_dataset_version()

    def execute(name, args):
        if name in {"declare_task", "set_plan", "update_plan", "finalize_answer"}:
            from backend.agent_runtime.tool_registry import execute_registered_tool
            return execute_registered_tool(name, args)
        if name == "region_gap" and args.get("filters", {}).get("series_selectors", [{}])[0].get("series") == "汉":
            return {"ok": False, "tool": name, "result_status": "error", "reason_code": "ambiguous_series",
                    "candidate_selectors": [{"brand_name": "比亚迪", "series": "汉DM"},
                                            {"brand_name": "比亚迪", "series": "汉EV"}],
                    "requested_series": "汉", "repair_hint": "请消歧并保留城市。"}
        if name == "region_gap":
            return {"ok": True, "tool": name, "result_status": "success", "analysis_type": "region_gap",
                    "dataset_version": dataset_version, "data": {"counts": {"valid_rows": 5}, "data": {"cities": [
                        {"city": "北京", "n": 2, "median_yuan": 120000},
                        {"city": "上海", "n": 3, "median_yuan": 130000},
                    ]}}}
        if name == "ask_user":
            return {"ok": True, "control": "ask_user", "questions": args.get("questions", [])}
        raise AssertionError(name)

    monkeypatch.setattr(nodes, "execute_registered_tool", execute)
    fact_ref = "ev_" + hashlib.sha256(
        f"query-retry:{dataset_version}:data.data.cities.0.median_yuan".encode("utf-8")
    ).hexdigest()[:20]
    client.responses[-1].choices[0].message.tool_calls[1].function.arguments = json.dumps({
        "schema_version": 2, "result_kind": "data_answer", "goal_ids": ["g1"],
        "blocks": [{"kind": "text", "text": "已按选定车型和城市完成查询。"},
                   {"kind": "fact_ref", "evidence_id": fact_ref, "format": "sentence"}],
        "limitation_codes": [],
    }, ensure_ascii=False)
    waiting = agent_loop.run_agent("比较北京、上海的比亚迪汉")

    assert waiting["status"] == "waiting_user", (waiting.get("summary"), waiting.get("state", {}).get("flags"), waiting.get("state", {}).get("tool_results"))
    assert waiting["checkpoint"]["pending_input"]["caused_by_tool_call_id"] == "query-ambiguous"
    resumed = agent_loop.run_agent(
        "比较北京、上海的比亚迪汉", resume_checkpoint=waiting["checkpoint"],
        input_answers=[{"question_id": "series", "value": "汉DM"}],
    )
    retry_result = next(item for item in resumed["state"]["tool_results"]
                        if item["tool_call_id"] == "query-retry")
    assert resumed["status"] == "done"
    assert resumed["state"]["flags"].get("answer_finalized") is True
    assert retry_result["status"] == "done"
    assert next(step for step in resumed["state"]["plan"] if step["id"] == "query")["status"] == "complete"
    retry_args = retry_result["input"]
    assert retry_args["filters"]["cities"] == ["北京", "上海"]
    assert retry_args["filters"]["series_selectors"] == [{"brand_name": "比亚迪", "series": "汉DM"}]


def test_unknown_city_on_shared_tool_resumes_custom_answer_and_preserves_filters(monkeypatch):
    client = FakeClient([
        _response(_message(tool_calls=[
            {"id": "declare-city", "name": "declare_task", "arguments":
             '{"goals":[{"id":"g1","description":"查询价格分布","deliverable":"统计"}],"requires_plan":true}'},
            {"id": "plan-city", "name": "set_plan", "arguments":
             '{"steps":[{"id":"query","title":"查询价格","description":"按条件统计挂牌价","tool_names":["price_distribution"],"goal_ids":["g1"],"kind":"data","completion_criteria":"tool_result"},{"id":"final","title":"整理结果","description":"回答","tool_names":["finalize_answer"],"goal_ids":["g1"],"kind":"finalize","completion_criteria":"validated_answer"}]}'},
        ])),
        _response(_message(tool_calls=[{"id": "query-unknown-city", "name": "price_distribution", "arguments":
            '{"filters":{"cities":["北亰"],"year_min":2020,"year_max":2022,"price_min_yuan":50000,"price_max_yuan":150000},"params":{"bins":8}}'}])),
        _response(_message(tool_calls=[{"id": "ask-city", "name": "ask_user", "arguments":
            '{"questions":[{"id":"city","prompt":"城市名称无法识别，请更正或重新输入。","options":[],"allow_custom":true,"required":true,"selection_mode":"single"}]}'}])),
        _response(_message(tool_calls=[{"id": "query-city-retry", "name": "price_distribution", "arguments":
            '{"filters":{"cities":["北京"],"year_min":2020,"year_max":2022,"price_min_yuan":50000,"price_max_yuan":150000},"params":{"bins":8}}'}])),
        _response(_message(tool_calls=[
            {"id": "complete-city-query", "name": "update_plan", "arguments":
             '{"updates":[{"id":"query","status":"complete"},{"id":"final","status":"running"}]}'},
            {"id": "final-city-answer", "name": "finalize_answer", "arguments": "__FINAL_ANSWER__"},
        ])),
    ], auto_declare=False)
    monkeypatch.setattr(agent_loop, "get_client", lambda: client)
    from backend.data_loader import get_dataset_version
    dataset_version = get_dataset_version()
    seen_retry_args = {}

    def execute(name, args):
        if name in {"declare_task", "set_plan", "update_plan", "finalize_answer"}:
            from backend.agent_runtime.tool_registry import execute_registered_tool
            return execute_registered_tool(name, args)
        if name == "price_distribution" and args["filters"]["cities"] == ["北亰"]:
            return {"ok": False, "tool": name, "result_status": "error", "reason_code": "unknown_city",
                    "keyword_field": "city", "requested_keyword": "北亰", "candidate_values": [],
                    "candidate_selectors": [], "normalized_filters": dict(args["filters"]),
                    "dataset_version": dataset_version, "repair_hint": "请更正城市名称后重试。"}
        if name == "price_distribution":
            seen_retry_args.update(args)
            return {"ok": True, "tool": name, "result_status": "success", "analysis_type": name,
                    "dataset_version": dataset_version,
                    "data": {"counts": {"valid_rows": 9}, "data": {"n": 9, "min_yuan": 50000,
                    "max_yuan": 150000, "mean_yuan": 100000, "median_yuan": 98000,
                    "p25_yuan": 80000, "p75_yuan": 120000}}}
        if name == "ask_user":
            return {"ok": True, "control": "ask_user", "questions": args.get("questions", [])}
        raise AssertionError(name)

    monkeypatch.setattr(nodes, "execute_registered_tool", execute)
    fact_ref = "ev_" + hashlib.sha256(
        f"query-city-retry:{dataset_version}:data.data.n".encode("utf-8")
    ).hexdigest()[:20]
    client.responses[-1].choices[0].message.tool_calls[1].function.arguments = json.dumps({
        "schema_version": 2, "result_kind": "data_answer", "goal_ids": ["g1"],
        "blocks": [{"kind": "text", "text": "已按更正后的城市和原筛选条件完成统计。"},
                   {"kind": "fact_ref", "evidence_id": fact_ref, "format": "sentence"}],
        "limitation_codes": [],
    }, ensure_ascii=False)

    waiting = agent_loop.run_agent("统计北亰，2020至2022年，5万至15万元的挂牌价分布")

    assert waiting["status"] == "waiting_user", (waiting.get("summary"), waiting.get("state", {}).get("flags"))
    assert waiting["checkpoint"]["pending_input"]["caused_by_tool_call_id"] == "query-unknown-city"
    question = waiting["checkpoint"]["pending_input"]["questions"][0]
    assert question["options"] == []
    assert question["allow_custom"] is True
    assert waiting["state"]["flags"].get("empty_recovery_attempts", 0) == 0

    resumed = agent_loop.run_agent(
        "统计北亰，2020至2022年，5万至15万元的挂牌价分布", resume_checkpoint=waiting["checkpoint"],
        input_answers=[{"question_id": "city", "value": "北京"}],
    )
    retry_result = next(item for item in resumed["state"]["tool_results"]
                        if item["tool_call_id"] == "query-city-retry")
    assert resumed["status"] == "done", (resumed.get("summary"), resumed.get("state", {}).get("flags"))
    assert resumed["state"]["flags"].get("answer_finalized") is True
    assert retry_result["status"] == "done"
    assert seen_retry_args["filters"] == {
        "cities": ["北京"], "year_min": 2020, "year_max": 2022,
        "price_min_yuan": 50000, "price_max_yuan": 150000,
    }
    assert seen_retry_args["params"] == {"bins": 8}
    assert resumed["state"]["flags"].get("empty_recovery_attempts", 0) == 0
    assert len([item for item in resumed["state"]["evidence_records"]
                if item.get("tool_call_id") == "query-city-retry"]) > 0
