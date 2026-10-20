"""二手车领域工具注册表。

注册表只描述和分发受控工具；工具本身仍由 backend.tools 中的纯 Python 函数实现。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    params_schema: dict[str, Any]
    handler: Callable[..., dict[str, Any]]
    read_only: bool = True
    category: str = "domain_query"
    effect: str = "read_only"
    parallel_safe: bool = False
    input_schema: dict[str, Any] | None = None
    result_normalizer: Callable[[str, Any], dict[str, Any]] | None = None
    compact_result: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.params_schema,
            },
        }


_REGISTRY: dict[str, ToolSpec] = {}


def register_tool(spec: ToolSpec) -> None:
    if not spec.name or not callable(spec.handler):
        raise ValueError("工具必须有名称和可调用 handler")
    if spec.name in _REGISTRY:
        raise ValueError(f"工具名已注册：{spec.name}")
    _REGISTRY[spec.name] = spec


def get_tool(name: str) -> ToolSpec | None:
    return _REGISTRY.get(str(name))


def tool_schemas(extra_tools: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """返回稳定排序且与注册表隔离的 schema 快照。"""
    schemas = [spec.schema() for spec in sorted(_REGISTRY.values(), key=lambda item: item.name)]
    for schema in extra_tools or []:
        schemas.append(copy.deepcopy(schema))
    names = [str((schema.get("function") or {}).get("name") or "") for schema in schemas]
    if len(names) != len(set(names)):
        raise ValueError("工具 schema 名称冲突")
    ordered = copy.deepcopy(sorted(schemas, key=lambda item: str((item.get("function") or {}).get("name") or "")))
    try:
        from backend.data_loader import get_dataset_snapshot
        from backend.tools.data_contract import get_data_capabilities
        snapshot = get_dataset_snapshot()
        capabilities = get_data_capabilities(snapshot)
    except (FileNotFoundError, OSError):
        capabilities = None
    return apply_data_capability_contract(ordered, capabilities)


def apply_data_capability_contract(
    schemas: list[dict[str, Any]], capabilities: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Project the authoritative dataset contract into model-visible schemas."""
    projected = copy.deepcopy(schemas)
    if not isinstance(capabilities, dict):
        return projected
    for schema in projected:
        function = schema.get("function") or {}
        if function.get("name") != "recommend":
            continue
        fields = capabilities.get("fields") or {}
        unsupported = ", ".join(map(str, capabilities.get("unsupported_filters") or [])) or "无"
        supported = ", ".join(map(str, capabilities.get("supported_filters") or [])) or "无"
        energy_values = ", ".join(map(str, capabilities.get("energy_values") or [])) or "无"
        aliases = capabilities.get("energy_aliases") or {}
        alias_summary = "; ".join(
            f"{alias}→{'/'.join(map(str, values))}"
            for alias, values in aliases.items()
            if isinstance(values, list) and values
        ) or "无"
        function["description"] = (
            str(function.get("description") or "")
            + "\n当前数据能力（运行时合同）："
            + f"版本 {capabilities.get('dataset_version') or 'unknown'}，"
            + f"{int(capabilities.get('row_count') or 0)} 条记录；"
            + f"全局数据可支持条件 {supported}（各工具仅使用其已声明参数）；不支持字段 {unsupported}；"
            + f"可识别能源枚举 {energy_values}；可用别名 {alias_summary}。"
            + "字段覆盖率见对应参数说明；禁止把缺失值推断为另一种分类。"
        )
        properties = (function.get("parameters") or {}).get("properties") or {}
        if "energy_type" in properties:
            properties["energy_type"]["description"] = (
                "当前数据可识别的能源枚举：" + energy_values + "。"
                + "可用别名：" + alias_summary + "。"
                + "不能将缺失值推断为燃油。"
            )
        if "body_type" in properties:
            coverage = (fields.get("body_type") or {}).get("coverage", 0)
            properties["body_type"]["description"] = (
                f"车型类别字段覆盖率 {float(coverage or 0):.1%}；"
                "当它位于不支持字段列表时，传入会返回 unsupported_filter，必须先询问用户是否放宽。"
            )
    return projected


def stable_tool_schemas(extra_tools: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """语义化别名：模型请求和 prefix hash 都使用同一注册表快照。"""
    return tool_schemas(extra_tools)


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    return True


def _validate_args(spec: ToolSpec, args: dict[str, Any]) -> str | None:
    if not isinstance(args, dict):
        return "参数必须是对象"

    schema = spec.input_schema or spec.params_schema or {}
    try:
        from jsonschema import Draft202012Validator
        errors = sorted(Draft202012Validator(schema).iter_errors(args), key=lambda error: list(error.absolute_path))
        if errors:
            error = errors[0]
            path = ".".join(str(part) for part in error.absolute_path) or "参数"
            return f"{path}：{error.message}"
    except ImportError:
        pass
    properties = schema.get("properties") or {}
    unknown = sorted(set(args) - set(properties))
    if schema.get("additionalProperties") is False and unknown:
        return f"包含未声明参数：{', '.join(unknown)}"

    missing = [name for name in schema.get("required", []) if name not in args]
    if missing:
        return f"缺少必填参数：{', '.join(missing)}"

    for name, value in args.items():
        expected = properties.get(name, {}).get("type")
        expected_types = expected if isinstance(expected, list) else [expected]
        if expected and not any(_matches_type(value, item) for item in expected_types):
            return f"参数 {name} 类型错误，期望 {expected_types}"
    return None


def execute_registered_tool(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    spec = get_tool(name)
    if spec is None:
        return {"ok": False, "tool": name, "error": f"未注册的工具：{name}"}
    params = args or {}
    validation_error = _validate_args(spec, params)
    if validation_error:
        return {"ok": False, "tool": name, "error": f"参数校验失败：{validation_error}"}
    try:
        result = spec.handler(**params)
        return spec.result_normalizer(name, result) if spec.result_normalizer else result
    except Exception as exc:
        return {"ok": False, "tool": name, "error": f"工具执行失败：{exc}"}


def _register_defaults() -> None:
    from backend.agent_runtime.control_tools import control_tool_specs
    from backend.tools.csv_query import query_csv_dataset
    from backend.tools.price_compare import compare_same_model
    from backend.tools.price_decay import price_decay_curve
    from backend.tools.recommend import recommend_cars
    from backend.tools.region_gap import region_price_gap
    from backend.tools.load_skill import load_skill_handler
    from backend.agent_runtime.report_context import read_report_handler
    from backend.tools.shared_analysis import (
        age_price_relation, brand_profile, mileage_price_correlation,
        model_comparison, price_distribution, shared_region_gap,
    )

    for spec in control_tool_specs():
        register_tool(spec)

    common_object = {"type": "object", "additionalProperties": False}
    filter_schema = {
        **common_object,
        "properties": {
            "brands": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
            "series_selectors": {"type": "array", "maxItems": 6, "items": {
                **common_object, "properties": {"brand_name": {"type": "string"}, "series": {"type": "string"}},
                "required": ["brand_name", "series"],
            }},
            "cities": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
            "price_min_yuan": {"type": ["number", "null"], "minimum": 0},
            "price_max_yuan": {"type": ["number", "null"], "minimum": 0},
            "mileage_min_km": {"type": ["number", "null"], "minimum": 0},
            "mileage_max_km": {"type": ["number", "null"], "minimum": 0},
            "year_min": {"type": ["integer", "null"]}, "year_max": {"type": ["integer", "null"]},
            "age_min_years": {"type": ["number", "null"], "minimum": 0},
            "age_max_years": {"type": ["number", "null"], "minimum": 0},
            "energy_types": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
        },
    }
    def shared_schema(params_properties=None):
        return {**common_object, "properties": {
            "filters": filter_schema,
            "params": {**common_object, "properties": params_properties or {}},
        }, "required": []}

    shared_specs = (
        ("price_distribution", "按统一筛选统计挂牌价格分布与分位数。", price_distribution, {"bins": {"type": "integer", "minimum": 5, "maximum": 40}}),
        ("mileage_price_correlation", "按统一筛选分析里程与挂牌价格相关性；相关不代表因果。", mileage_price_correlation, {}),
        ("model_comparison", "按品牌与车系联合身份比较多个车系的样本和价格区间。", model_comparison, {}),
        ("brand_profile", "按统一筛选汇总品牌样本画像；占比仅针对当前筛选范围。", brand_profile, {"top_k": {"type": "integer", "minimum": 1, "maximum": 20}}),
        ("age_price_relation", "按快照记录车龄分析其与挂牌价格的关系。", age_price_relation, {"bin_width_years": {"type": "number", "exclusiveMinimum": 0, "maximum": 10}}),
    )
    for name, description, handler, params_properties in shared_specs:
        register_tool(ToolSpec(name, description, shared_schema(params_properties), handler,
                               input_schema=shared_schema(params_properties), parallel_safe=True))
    register_tool(
        ToolSpec(
            "price_compare",
            "查询指定品牌与车系的挂牌价分布；同名车系跨品牌时必须指定 brand。",
            {
                **common_object,
                "properties": {
                    "series": {"type": "string"},
                    "brand": {"type": ["string", "null"]},
                    "year": {"type": ["integer", "null"]},
                    "city": {"type": ["string", "null"]},
                },
                "required": ["series"],
            },
            compare_same_model,
        )
    )
    register_tool(
        ToolSpec(
            "region_gap",
            "按统一筛选比较城市挂牌价差异；series 需与 filters.series_selectors 一致，使用共享分析结果。",
            {
                **common_object,
                "properties": {
                    "series": {"type": "string"},
                    "year": {"type": ["integer", "null"]},
                    "min_city_n": {"type": "integer"},
                    "filters": filter_schema,
                },
                "required": ["series"],
            },
            shared_region_gap,
            input_schema={
                **common_object,
                "properties": {
                    "series": {"type": "string"}, "year": {"type": ["integer", "null"]},
                    "min_city_n": {"type": "integer", "minimum": 1, "maximum": 10000}, "filters": filter_schema,
                },
                "required": ["series"],
            },
            parallel_safe=True,
        )
    )
    register_tool(
        ToolSpec(
            "price_decay",
            "按生产年份分析指定品牌与车系；同名车系跨品牌时必须指定 brand。",
            {
                **common_object,
                "properties": {
                    "series": {"type": "string"},
                    "brand": {"type": ["string", "null"]},
                    "city": {"type": ["string", "null"]},
                    "min_year_n": {"type": "integer"},
                },
                "required": ["series"],
            },
            price_decay_curve,
        )
    )
    register_tool(
        ToolSpec(
            "recommend",
            "按预算、品牌、城市、车系和数据实际支持的能源条件推荐车系。当前数据字段覆盖与枚举以运行时能力摘要为准；不能把缺失值当作某个分类。工具返回空结果时请根据 reason_code、relaxation_candidates 和 data_capabilities 判断是否调整条件后重试。",
            {
                **common_object,
                "properties": {
                    "budget_max_wan": {"type": ["number", "null"]},
                    "budget_min_wan": {"type": "number"},
                    "city": {"type": ["string", "null"]},
                    "brand": {"type": ["string", "null"]},
                    "series": {"type": ["string", "null"]},
                    "energy_type": {
                        "type": ["string", "null"],
                        "description": "真实数据可识别：纯电动、插电式混合动力、增程式；也可传纯电、插混、增程、混动、新能源。燃油/汽油不可由缺失值推断。",
                    },
                    "body_type": {
                        "type": ["string", "null"],
                        "description": "运行时数据能力摘要中声明不支持时会返回 unsupported_filter，不要假装已筛选。",
                    },
                    "max_age": {"type": ["integer", "null"]},
                    "top_k": {"type": "integer"},
                    "min_n": {"type": "integer"},
                },
                "required": ["budget_max_wan"],
            },
            recommend_cars,
        )
    )
    register_tool(
        ToolSpec(
            "csv_query",
            "只读检索项目内清洗车源数据，返回代表性样本行和命中数。",
            {
                **common_object,
                "properties": {
                    "series": {"type": ["string", "null"]},
                    "brand": {"type": ["string", "null"]},
                    "city": {"type": ["string", "null"]},
                    "year": {"type": ["integer", "null"]},
                    "min_price_wan": {"type": ["number", "null"]},
                    "max_price_wan": {"type": ["number", "null"]},
                    "limit": {"type": "integer"},
                },
                "required": [],
            },
            query_csv_dataset,
        )
    )
    register_tool(
        ToolSpec(
            "load_skill",
            "按名称读取项目内 .agent/skills 下的 Skill 正文；不能读取任意路径。",
            {
                **common_object,
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            load_skill_handler,
        )
    )
    register_tool(
        ToolSpec(
            "read_report",
            "只读访问当前会话中已提交的历史报告。解释某一报告时可读取对应版本或部分；任何跨会话报告都会被拒绝。",
            {
                **common_object,
                "properties": {
                    "report_id": {"type": "string", "maxLength": 128},
                    "section": {"type": ["string", "null"], "maxLength": 40},
                },
                "required": ["report_id"],
            },
            read_report_handler,
            read_only=True,
            category="report_read",
            effect="read_only",
        )
    )


_register_defaults()
