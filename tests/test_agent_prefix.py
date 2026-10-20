import copy

from backend.agent_runtime.prefix import (
    FilePrefixCacheTracker,
    PrefixCacheTracker,
    build_prefix_context,
    compute_prefix_hash,
)
from backend.agent_runtime.state import AgentState
from backend.agent_runtime.tool_registry import tool_schemas


def test_same_static_prefix_has_same_hash_and_second_request_hits_local_tracker():
    tracker = PrefixCacheTracker()
    tools = tool_schemas()

    first = build_prefix_context("deepseek-chat", "固定系统约束", tools, tracker=tracker)
    second = build_prefix_context("deepseek-chat", "固定系统约束", tools, tracker=tracker)

    assert first.prefix_hash == second.prefix_hash == compute_prefix_hash(
        "deepseek-chat", "固定系统约束", tools
    )
    assert first.cache_hit is False
    assert second.cache_hit is True


def test_dynamic_history_is_after_system_and_not_part_of_system_prompt():
    history = [
        {"role": "user", "content": "上一轮预算是 15 万"},
        {"role": "assistant", "content": "我记住了"},
    ]
    state = AgentState.create("那继续推荐", "固定系统约束", history=history)

    assert [message["role"] for message in state.messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert state.messages[0]["content"] == "固定系统约束"
    assert "上一轮" not in state.messages[0]["content"]
    assert state.dynamic_messages() == state.messages[1:]


def test_file_tracker_reports_hit_across_tracker_instances(tmp_path):
    first = build_prefix_context(
        "deepseek-chat",
        "固定系统约束",
        tool_schemas(),
        tracker=FilePrefixCacheTracker(tmp_path / "prefixes"),
    )
    second = build_prefix_context(
        "deepseek-chat",
        "固定系统约束",
        tool_schemas(),
        tracker=FilePrefixCacheTracker(tmp_path / "prefixes"),
    )

    assert first.cache_hit is False
    assert second.cache_hit is True


def test_tool_schemas_are_stably_sorted_and_safe_to_mutate_per_request():
    schemas = tool_schemas()
    names = [item["function"]["name"] for item in schemas]
    assert names == sorted(names)

    mutated = copy.deepcopy(schemas)
    mutated[0]["function"]["description"] = "request-local mutation"
    assert tool_schemas()[0]["function"]["description"] != "request-local mutation"
