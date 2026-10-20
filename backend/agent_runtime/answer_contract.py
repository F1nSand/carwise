"""Versioned, bounded schema for model-authored final-answer drafts.

The model selects trusted evidence references and writes short prose. Values,
units, entity labels, filters and scope are always supplied by the runtime.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any


ANSWER_SCHEMA_VERSION = 2
RESULT_KINDS = {"data_answer", "no_data", "explanation", "partial_result"}
BLOCK_KINDS = {"text", "fact_ref", "recommendation_table"}
FACT_FORMATS = {"sentence", "value"}
TABLE_COLUMNS = {
    "series", "brand", "rank", "median_price", "price_interval", "sample_size", "mileage",
    "median_year", "score",
}
LIMITATION_CODES = {
    "listing_price_not_transaction", "sample_scope", "unsupported_filter",
    "partial_coverage", "stale_data_unavailable",
}

_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_GOAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_MAX_GOALS = 8
_MAX_BLOCKS = 16
_MAX_REFS = 32
_MAX_TEXT = 1_000


@dataclass(frozen=True)
class ValidationIssue:
    """Safe, field-addressable diagnostic suitable for repair prompts/UI."""

    code: str
    path: str
    category: str
    message: str
    repair_hint: str = ""
    expected_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["expected_refs"] = list(self.expected_refs)
        return result


def _issue(
    code: str,
    path: str,
    message: str,
    hint: str = "",
    category: str = "schema",
    expected_refs: list[str] | tuple[str, ...] = (),
) -> ValidationIssue:
    return ValidationIssue(code, path, category, message, hint, tuple(map(str, expected_refs)))


def validate_answer_shape(draft: Any) -> list[ValidationIssue]:
    """Validate schema v2 without resolving any evidence references."""
    issues: list[ValidationIssue] = []
    if not isinstance(draft, dict):
        return [_issue("draft_type", "$", "答案草稿必须是 JSON 对象。", "提交符合 schema v2 的结构化答案。")]

    allowed = {"schema_version", "result_kind", "goal_ids", "blocks", "limitation_codes"}
    extra = sorted(set(draft) - allowed)
    for key in extra:
        issues.append(_issue("unexpected_field", key, "答案中包含未声明字段。", "删除该字段并使用受支持的引用类型。"))
    if draft.get("schema_version") != ANSWER_SCHEMA_VERSION:
        issues.append(_issue("schema_version", "schema_version", "答案 schema_version 必须为 2。", "按当前 finalize_answer 工具 schema 重试。"))

    result_kind = draft.get("result_kind")
    if result_kind not in RESULT_KINDS:
        issues.append(_issue("result_kind", "result_kind", "result_kind 不受支持。", "选择 data_answer、no_data、explanation 或 partial_result。"))

    goals = draft.get("goal_ids")
    if not isinstance(goals, list) or not 1 <= len(goals) <= _MAX_GOALS:
        issues.append(_issue("goal_ids", "goal_ids", "必须引用 1 到 8 个任务目标。", "覆盖本次已声明的目标 ID。"))
    else:
        seen_goals: set[str] = set()
        for index, goal_id in enumerate(goals):
            if not isinstance(goal_id, str) or not _GOAL.fullmatch(goal_id):
                issues.append(_issue("goal_id", f"goal_ids[{index}]", "目标 ID 格式无效。", "仅引用 declare_task 返回的目标 ID。"))
            elif goal_id in seen_goals:
                issues.append(_issue("duplicate_goal", f"goal_ids[{index}]", "目标 ID 不能重复。"))
            seen_goals.add(goal_id)

    blocks = draft.get("blocks")
    if not isinstance(blocks, list) or not 1 <= len(blocks) <= _MAX_BLOCKS:
        issues.append(_issue("blocks", "blocks", "答案必须包含 1 到 16 个内容块。", "添加自然语言说明或受信事实引用。"))
        blocks = []

    has_text = False
    has_fact = False
    for index, block in enumerate(blocks):
        path = f"blocks[{index}]"
        if not isinstance(block, dict):
            issues.append(_issue("block_type", path, "内容块必须是对象。"))
            continue
        kind = block.get("kind")
        if kind == "text":
            if set(block) != {"kind", "text"}:
                issues.append(_issue("text_fields", path, "text 块只允许 kind 和 text 字段。"))
            value = block.get("text")
            if not isinstance(value, str) or not value.strip() or len(value) > _MAX_TEXT:
                issues.append(_issue("text_value", f"{path}.text", "文本块须为 1 到 1000 个字符。", "移除空文本或拆分过长段落。"))
            else:
                has_text = True
        elif kind == "fact_ref":
            has_fact = True
            if set(block) != {"kind", "evidence_id", "format"}:
                issues.append(_issue("fact_fields", path, "fact_ref 只允许 kind、evidence_id 和 format。"))
            evidence_id = block.get("evidence_id")
            if not isinstance(evidence_id, str) or not _REF.fullmatch(evidence_id):
                issues.append(_issue("evidence_ref", f"{path}.evidence_id", "证据引用格式无效。", "仅引用工具返回的 evidence_id。"))
            if block.get("format") not in FACT_FORMATS:
                issues.append(_issue("fact_format", f"{path}.format", "事实显示格式不受支持。", "选择 sentence 或 value。"))
        elif kind == "recommendation_table":
            has_fact = True
            if set(block) != {"kind", "entity_group_refs", "columns"}:
                issues.append(_issue("table_fields", path, "recommendation_table 字段无效。"))
            refs = block.get("entity_group_refs")
            if not isinstance(refs, list) or not 1 <= len(refs) <= _MAX_REFS:
                issues.append(_issue("entity_refs", f"{path}.entity_group_refs", "表格必须引用 1 到 32 个候选实体组。", "只选择模型可见结果中的实体组。"))
            else:
                seen_refs: set[str] = set()
                for ref_index, ref in enumerate(refs):
                    if not isinstance(ref, str) or not _REF.fullmatch(ref):
                        issues.append(_issue("entity_ref", f"{path}.entity_group_refs[{ref_index}]", "候选实体引用格式无效。"))
                    elif ref in seen_refs:
                        issues.append(_issue("duplicate_entity_ref", f"{path}.entity_group_refs[{ref_index}]", "候选实体引用不能重复。"))
                    seen_refs.add(ref)
            columns = block.get("columns")
            if not isinstance(columns, list) or not columns or len(columns) > len(TABLE_COLUMNS):
                issues.append(_issue("table_columns", f"{path}.columns", "表格列必须是非空数组。"))
            elif any(column not in TABLE_COLUMNS for column in columns) or len(columns) != len(set(columns)):
                issues.append(_issue("table_columns", f"{path}.columns", "表格包含不支持或重复的列。", "选择 schema 声明的列。"))
        else:
            issues.append(_issue("block_kind", f"{path}.kind", "内容块类型不受支持。", "使用 text、fact_ref 或 recommendation_table。"))

    if result_kind == "data_answer" and not has_fact:
        issues.append(_issue("data_evidence_required", "blocks", "数据答案必须引用至少一项受信事实。", "添加当前运行的 fact_ref 或推荐实体表。", "coverage"))
    if result_kind in {"no_data", "explanation", "partial_result"} and not has_text:
        issues.append(_issue("explanation_required", "blocks", "该类结果必须包含文字说明。", "说明结果或缺少的数据条件。", "coverage"))

    codes = draft.get("limitation_codes", [])
    if not isinstance(codes, list) or len(codes) > 8:
        issues.append(_issue("limitation_codes", "limitation_codes", "limitation_codes 最多为 8 项数组。"))
    else:
        seen_codes: set[str] = set()
        for index, code in enumerate(codes):
            if code not in LIMITATION_CODES:
                issues.append(_issue("limitation_code", f"limitation_codes[{index}]", "限制说明代码不受支持。", "选择工具和 schema 返回的受信限制代码。"))
            elif code in seen_codes:
                issues.append(_issue("duplicate_limitation", f"limitation_codes[{index}]", "限制说明不能重复。"))
            seen_codes.add(code)

    return issues
