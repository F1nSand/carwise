import pytest

from backend.agent_runtime.tool_registry import (
    ToolSpec,
    execute_registered_tool,
    get_tool,
    register_tool,
    apply_data_capability_contract,
    tool_schemas,
)


def test_default_registry_contains_domain_and_csv_tools():
    names = {item["function"]["name"] for item in tool_schemas()}

    assert {"price_compare", "region_gap", "price_decay", "recommend", "csv_query"} <= names
    assert get_tool("csv_query").read_only is True


def test_legacy_price_tools_expose_brand_for_series_disambiguation():
    schemas = {item["function"]["name"]: item["function"]["parameters"]["properties"]
               for item in tool_schemas()}
    from backend.agent_runtime.tool_policy import _FIELDS_BY_TOOL

    for name in ("price_compare", "price_decay"):
        assert "brand" in schemas[name]
        assert "brand" in _FIELDS_BY_TOOL[name]


def test_registry_rejects_duplicate_names():
    with pytest.raises(ValueError, match="工具名已注册"):
        register_tool(
            ToolSpec(
                name="csv_query",
                description="duplicate",
                params_schema={"type": "object"},
                handler=lambda: {},
            )
        )


def test_registry_returns_none_for_unknown_tool():
    assert get_tool("not_registered") is None


def test_registry_rejects_unknown_and_invalid_parameters():
    unknown = execute_registered_tool("csv_query", {"path": "outside.csv"})
    wrong_type = execute_registered_tool("csv_query", {"limit": "many"})

    assert unknown["ok"] is False
    assert "参数校验失败" in unknown["error"]
    assert wrong_type["ok"] is False
    assert "参数校验失败" in wrong_type["error"]


def test_recommend_schema_projects_current_capability_contract_without_mutating_source():
    source = [{
        "type": "function",
        "function": {
            "name": "recommend",
            "description": "推荐",
            "parameters": {"properties": {
                "energy_type": {"description": "旧能源描述"},
                "body_type": {"description": "旧车型描述"},
            }},
        },
    }]
    capabilities = {
        "dataset_version": "version-1", "row_count": 42,
        "supported_filters": ["budget", "energy_type"],
        "unsupported_filters": ["body_type"],
        "energy_values": ["电动"],
        "energy_aliases": {"EV": ["电动"]},
        "fields": {"body_type": {"coverage": 0.0}},
    }

    projected = apply_data_capability_contract(source, capabilities)

    assert "version-1" in projected[0]["function"]["description"]
    assert "电动" in projected[0]["function"]["parameters"]["properties"]["energy_type"]["description"]
    assert "EV→电动" in projected[0]["function"]["description"]
    assert "0.0%" in projected[0]["function"]["parameters"]["properties"]["body_type"]["description"]
    assert source[0]["function"]["description"] == "推荐"
    assert source[0]["function"]["parameters"]["properties"]["energy_type"]["description"] == "旧能源描述"
