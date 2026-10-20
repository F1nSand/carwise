from backend.evals.models import parse_case
from backend.evals.scorers import score_run


def test_scorer_checks_tools_arguments_and_plan():
    case = parse_case({
        "id": "score-1", "category": "price_comparison",
        "turns": [{"role": "user", "content": "宝马价格"}],
        "expect": {"status": "done", "required_tools": ["price_compare"], "tool_arguments": [{"tool": "price_compare", "path": "series", "equals": "宝马3系"}], "plan_complete": True},
    })
    run = {"status": "done", "tool_calls": [{"tool": "price_compare", "arguments": {"series": "宝马3系"}}], "plan": [{"status": "complete"}], "summary": "完成"}
    assert score_run(case, run).passed


def test_scorer_reports_forbidden_tool_and_status_violation():
    case = parse_case({"id": "score-2", "category": "unsupported_filters", "turns": [{"role": "user", "content": "x"}], "expect": {"status": "done", "forbidden_tools": ["csv_query"]}})
    result = score_run(case, {"status": "error", "tool_calls": [{"tool": "csv_query"}]})
    assert not result.passed
    assert any("forbidden_tools" in item for item in result.violations)


def test_scorer_normalizes_nested_trace_and_deduplicates_call_lifecycle():
    case = parse_case({"id": "trace-dedupe", "category": "price_comparison", "turns": [{"role": "user", "content": "比较宝马"}], "expect": {"status": "done", "required_tools": ["price_compare"], "tool_arguments": [{"tool": "price_compare", "path": "series", "equals": "宝马3系"}]}})
    run = {"status": "done", "trace": {"events": [
        {"event_type": "tool_started", "input": {"tool_call_id": "c1", "tool_name": "price_compare", "arguments": "{}"}},
        {"event_type": "tool_completed", "input": {"tool_call_id": "c1", "tool_name": "price_compare", "arguments": "{\"series\": \"宝马3系\"}"}, "output": {"ok": True}},
    ]}}
    result = score_run(case, run)
    assert result.passed


def test_numeric_claim_requires_value_and_unit():
    case = parse_case({"id": "claim-unit", "category": "price_comparison", "turns": [{"role": "user", "content": "中位价"}], "expect": {"status": "done", "numeric_claims": [{"value": 12.5, "unit": "万元", "tolerance": 0.1}]}})
    assert score_run(case, {"status": "done", "summary": "中位价为12.5万元"}).passed
    result = score_run(case, {"status": "done", "summary": "中位价为12.5元"})
    assert not result.passed
    assert any(item.startswith("numeric_claims") for item in result.violations)
