"""Validate and render structured, evidence-backed answer drafts."""

from __future__ import annotations

import re
import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from backend.agent_runtime.answer_contract import ValidationIssue, validate_answer_shape

_UNIT_ALIASES = {
    "yuan": "CNY", "元": "CNY", "CNY": "CNY",
    "万元": "wan", "万": "wan", "wan": "wan",
    "条": "count", "台": "count", "辆": "count", "个": "count", "count": "count",
    "%": "percent", "百分比": "percent", "percent": "percent",
    "比例": "ratio", "ratio": "ratio",
    "相关系数": "correlation", "correlation": "correlation",
    "年": "year", "year": "year", "公里": "km", "km": "km",
    "万公里": "wan_km", "wan_km": "wan_km",
    "score": "score", "分": "score",
}
_PRECISION = {"CNY": 0, "wan": 2, "wan_km": 2, "count": 0, "percent": 1, "ratio": 4, "correlation": 3, "year": 0, "km": 0, "score": 3}
_PLACEHOLDER = re.compile(r"\{\{claim:([A-Za-z0-9_-]{1,64})\}\}")
_ARABIC_NUMBER = re.compile(
    r"(?<![A-Za-z0-9])[+-]?(?:[0-9０-９]+(?:[.,，][0-9０-９]+)*)(?:\s*(?:万元|万|亿元|亿|元|辆|台|条|款|年|岁|公里|km|百分比|个百分点|%|％|分))?(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_CHINESE_NUMBER_WITH_UNIT = re.compile(
    r"[零〇一二两三四五六七八九十百千万亿]+(?:点[零〇一二两三四五六七八九]+)?\s*(?:万元|亿元|万|亿|元|辆|台|条|款|年|岁|公里|百分比|个百分点|%|％)"
)
_COMMON_CHINESE_NUMBER_PHRASES = re.compile(r"同一(?:个|辆|台|款|年|条|组)?")
_QUARTILE_LABEL = re.compile(r"(?<![A-Za-z0-9])(?:(?:25\s*/\s*75)|(?:25|75))\s*(?:分位|百分位)")

_METRIC_LABELS = {
    "price.p25_yuan": "挂牌价 25 分位",
    "price.median_yuan": "挂牌价中位数",
    "price.p75_yuan": "挂牌价 75 分位",
    "price.mean_yuan": "挂牌价均值",
    "price.min_yuan": "挂牌价最低值",
    "price.max_yuan": "挂牌价最高值",
    "sample_size": "车源样本量",
}
_FILTER_FIELD_ALIASES = {
    "车系": "series", "series": "series",
    "年份": "year", "生产年份": "year", "year": "year",
    "城市": "city", "city": "city",
    "品牌": "brand", "brand": "brand",
    "能源": "energy_type", "能源类型": "energy_type", "energy_type": "energy_type",
    "预算下限": "budget_min_wan", "最低预算": "budget_min_wan", "budget_min_wan": "budget_min_wan",
    "预算上限": "budget_max_wan", "最高预算": "budget_max_wan", "budget_max_wan": "budget_max_wan",
    "车龄": "max_age", "最大车龄": "max_age", "max_age": "max_age",
}
_NON_USER_FILTERS = (
    re.compile(r"^价格在\s*\([^)]*\)\s*元区间内$"),
    re.compile(r"^仅纳入样本量\s*>=\s*\d+\s*台的(?:年份|车系|城市)$"),
)
_BUDGET_RANGE_FILTER = re.compile(
    r"^挂牌价(?:\s*在)?\s*(-|\d+(?:\.\d+)?)\s*[~～\-–—]\s*"
    r"(-|\d+(?:\.\d+)?)\s*万元(?:之间)?$"
)
_MAX_AGE_FILTER = re.compile(r"^车龄\s*<=\s*(\d+)\s*年$")


def _condition_value(value: str) -> str:
    """Canonicalize numeric spellings while preserving categorical values."""
    normalized = " ".join(value.split()).casefold()
    try:
        number = Decimal(normalized)
    except InvalidOperation:
        return normalized
    if not number.is_finite():
        return normalized
    return format(number.normalize(), "f")


def _condition_pairs(filters: Any) -> dict[str, str] | None:
    """Normalize model-described conditions against evidence's trusted filters."""
    if not isinstance(filters, list):
        return None
    normalized: dict[str, str] = {}

    def add_condition(key: str, value: str) -> bool:
        if key in normalized and normalized[key] != value:
            return False
        normalized[key] = value
        return True

    for item in filters:
        if not isinstance(item, str):
            return None
        item = item.strip()
        budget_range = _BUDGET_RANGE_FILTER.fullmatch(item)
        if budget_range:
            minimum, maximum = budget_range.groups()
            if minimum != "-" and not add_condition("budget_min_wan", _condition_value(minimum)):
                return None
            if maximum != "-" and not add_condition("budget_max_wan", _condition_value(maximum)):
                return None
            continue
        max_age = _MAX_AGE_FILTER.fullmatch(item)
        if max_age:
            if not add_condition("max_age", _condition_value(max_age.group(1))):
                return None
            continue
        # This metadata uses the operator token >=, so classify it before the
        # generic key=value parser below.
        if any(pattern.fullmatch(item) for pattern in _NON_USER_FILTERS):
            continue
        if "=" not in item:
            # Dataset cleaning bounds are evidence metadata, not user-selected
            # query conditions. Keep them in rendered evidence summaries while
            # excluding them from claim-vs-user-filter equality checks.
            return None
        raw_key, raw_value = item.split("=", 1)
        key = _FILTER_FIELD_ALIASES.get(raw_key.strip().casefold())
        value = _condition_value(raw_value)
        if not key or not value:
            return None
        if not add_condition(key, value):
            return None
    return normalized


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    errors: list[str]
    answer: str = ""
    claims: list[dict[str, Any]] | None = None
    limitations: list[str] | None = None
    issues: list[dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _round(value: Any, unit: str) -> Decimal | None:
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            return None
        places = _PRECISION[unit]
        return number.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return None


def _convert(value: Any, source_unit: str, target_unit: str) -> Any:
    if source_unit == target_unit:
        return value
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if (source_unit, target_unit) == ("CNY", "wan"):
        return number / Decimal(10000)
    if (source_unit, target_unit) == ("wan", "CNY"):
        return number * Decimal(10000)
    if (source_unit, target_unit) == ("ratio", "percent"):
        return number * Decimal(100)
    if (source_unit, target_unit) == ("percent", "ratio"):
        return number / Decimal(100)
    return None


def _claim_text(claim: dict[str, Any], record: dict[str, Any]) -> str:
    unit = _UNIT_ALIASES.get(str(claim.get("unit") or ""), record["unit"])
    value = _round(_convert(record.get("value"), record["unit"], unit), unit)
    unit_label = {"CNY": "元", "wan": "万元", "wan_km": "万公里", "count": "条", "percent": "%", "year": "年", "km": "公里", "score": "分", "correlation": "相关系数", "ratio": "比例"}[unit]
    label = _METRIC_LABELS.get(str(record["metric"]), "经核验指标")
    scope_label = "代表性样本" if record.get("scope") == "representative_sample" else "总体统计"
    sample = "未知" if record.get("sample_size") is None else str(record["sample_size"])
    filters = "；".join(record.get("filters") or [])
    condition = f"；条件：{filters}" if filters else ""
    entity = str(record.get("entity_label") or "").strip()
    subject = f"{entity}的" if entity else ""
    return f"{subject}{label}：{value}{unit_label}（{scope_label}，样本量 {sample}{condition}）。"


def _unverified_number(text: str) -> str | None:
    """Find an actual numeric expression, not isolated Chinese characters.

    The old character class rejected ordinary words such as “同一”, “点名”
    and model names such as “A4L”. Digits inside alpha-numeric identifiers are
    excluded; measurements/currency expressed in Chinese numerals still count.
    """
    visible = _COMMON_CHINESE_NUMBER_PHRASES.sub(" ", str(text or ""))
    visible = _QUARTILE_LABEL.sub(" ", visible)
    match = _ARABIC_NUMBER.search(visible)
    if match:
        return match.group(0)
    match = _CHINESE_NUMBER_WITH_UNIT.search(visible)
    return match.group(0) if match else None


def _issue_dict(
    code: str,
    path: str,
    category: str,
    message: str,
    repair_hint: str = "",
    expected_refs: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    return ValidationIssue(
        code, path, category, message, repair_hint, tuple(map(str, expected_refs))
    ).to_dict()


def _legacy_issues(errors: list[str]) -> list[dict[str, str]]:
    """Keep v1 compatibility while returning structured repair locations."""
    issues: list[dict[str, Any]] = []
    for error in errors:
        match = re.match(r"(claims\[\d+\]|narrative|limitations|claims)(?:\s|中|含|引用|指|必|必须|\]|：|:)", error)
        path = match.group(1) if match else "draft"
        lowered = error.casefold()
        if "旧数据" in error or "旧条件" in error:
            category, code = "freshness", "stale_evidence"
        elif "条件" in error:
            category, code = "constraint", "condition_mismatch"
        elif "代表性样本" in error:
            category, code = "scope", "scope_mismatch"
        elif "未绑定的数字" in error or "数值" in error:
            category, code = "semantic", "unbound_number"
        elif "单位" in error:
            category, code = "semantic", "unit_mismatch"
        elif "claim" in lowered or "证据" in error:
            category, code = "reference", "evidence_reference"
        else:
            category, code = "schema", "answer_invalid"
        issues.append(_issue_dict(code, path, category, error, "根据对应字段的校验信息修正后再提交。"))
    return issues


def _safe_cell(value: Any) -> str:
    return str(value if value is not None else "—").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _record_current(
    record: dict[str, Any], *, dataset_version: str | None,
    constraint_revision: str | int | None,
) -> bool:
    if record.get("source") == "historical_report":
        return False
    return (
        (dataset_version is None or record.get("dataset_version") == dataset_version)
        and (constraint_revision is None or record.get("constraint_revision") == constraint_revision)
    )


def _fact_value(record: dict[str, Any]) -> str:
    unit = str(record.get("unit") or "")
    display_unit = "percent" if unit == "ratio" else unit
    raw_value = _convert(record.get("value"), unit, display_unit)
    value = _round(raw_value, display_unit)
    if value is None:
        return ""
    if unit == "ratio" and value == value.to_integral_value():
        value = value.quantize(Decimal("1"))
    unit_label = {"CNY": "元", "wan": "万元", "wan_km": "万公里", "count": "条", "percent": "%", "year": "年", "km": "公里", "score": "分", "correlation": "相关系数", "ratio": "比例"}.get(display_unit, display_unit)
    return f"{value}{unit_label}"


def _fact_sentence(record: dict[str, Any]) -> str:
    unit = str(record.get("unit") or "")
    value = _fact_value(record)
    if not value:
        return ""
    label = _METRIC_LABELS.get(str(record.get("metric") or ""))
    if not label:
        metric = str(record.get("metric") or "")
        tail = metric.rsplit(".", 1)[-1]
        label = {
            "median_wan": "中位挂牌价", "p25_wan": "挂牌价 25 分位",
            "p75_wan": "挂牌价 75 分位", "sample_size": "车源样本量",
            "median_year": "平均生产年份", "score": "推荐评分",
        }.get(tail, "经核验指标")
    subject = f"{record.get('entity_label')}：" if record.get("entity_label") else ""
    scope = "代表性样本" if record.get("scope") == "representative_sample" else "总体统计"
    sample = "未知" if record.get("sample_size") is None else str(record.get("sample_size"))
    filters = "；".join(map(str, record.get("filters") or []))
    condition = f"；条件：{filters}" if filters else ""
    return f"{subject}{label}：{value}（{scope}，样本量 {sample}{condition}）。"


def _render_recommendation_table(
    block: dict[str, Any], records: dict[str, dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    refs = [str(item) for item in block.get("entity_group_refs") or []]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records.values():
        group_id = str(record.get("entity_group_id") or "")
        if group_id in refs:
            grouped.setdefault(group_id, []).append(record)

    column_labels = {
        "series": "车系", "brand": "品牌", "rank": "排序", "mileage": "中位里程",
        "median_price": "中位挂牌价", "price_interval": "主流区间",
        "sample_size": "样本量", "median_year": "平均生产年份", "score": "推荐评分",
    }
    lines = ["| " + " | ".join(column_labels[item] for item in block["columns"]) + " |",
             "| " + " | ".join("---" for _ in block["columns"]) + " |"]
    used: list[dict[str, Any]] = []
    for group_id in refs:
        group = grouped.get(group_id)
        if not group:
            continue
        first = group[0]
        metrics = {str(item.get("metric") or "").rsplit(".", 1)[-1]: item for item in group}
        row: list[str] = []
        for column in block["columns"]:
            if column == "series":
                value: Any = first.get("entity_label")
            elif column == "brand":
                value = first.get("brand")
            elif column == "rank":
                value = first.get("rank")
            elif column == "sample_size":
                value = first.get("sample_size")
                if value is None:
                    value = next((item.get("value") for key, item in metrics.items() if key == "sample_size"), None)
                value = f"{value} 条" if value is not None else None
            elif column == "median_year":
                item = metrics.get("median_year")
                value = _fact_value(item) if item else None
            elif column == "mileage":
                item = metrics.get("median_mileage_wan_km")
                value = _fact_value(item) if item else None
            elif column == "score":
                item = metrics.get("score")
                value = _fact_value(item) if item else None
            elif column == "median_price":
                item = metrics.get("median_wan") or metrics.get("median_yuan")
                value = _fact_value(item) if item else None
            elif column == "price_interval":
                low = metrics.get("p25_wan") or metrics.get("p25_yuan")
                high = metrics.get("p75_wan") or metrics.get("p75_yuan")
                value = f"{_fact_value(low)}–{_fact_value(high)}" if low and high else None
            else:
                value = None
            if value is None:
                return "", []
            row.append(_safe_cell(value))
        lines.append("| " + " | ".join(row) + " |")
        used.extend(group)
    if len(lines) == 2:
        return "", []
    return "\n".join(lines), used


def _validate_v2(
    draft: dict[str, Any], evidence_records: list[dict[str, Any]], *,
    dataset_version: str | None = None,
    constraint_revision: str | int | None = None,
    required_goal_ids: list[str] | None = None,
) -> ValidationResult:
    issues = [item.to_dict() for item in validate_answer_shape(draft)]
    if issues:
        return ValidationResult(False, [item["message"] for item in issues], issues=issues)

    expected_goals = set(map(str, required_goal_ids or []))
    supplied_goals = set(map(str, draft.get("goal_ids") or []))
    if expected_goals and supplied_goals != expected_goals:
        issue = _issue_dict(
            "goal_coverage", "goal_ids", "coverage", "最终答案必须覆盖全部已声明目标。",
            "逐项引用本次任务的全部 goal IDs。", sorted(expected_goals),
        )
        issues.append(issue)

    current_records = {
        str(item.get("evidence_id")): item
        for item in evidence_records
        if isinstance(item, dict) and item.get("evidence_id") and _record_current(
            item, dataset_version=dataset_version, constraint_revision=constraint_revision
        )
    }
    historical_records = {
        str(item.get("evidence_id")): item
        for item in evidence_records
        if isinstance(item, dict) and item.get("evidence_id")
        and item.get("source") == "historical_report"
        and item.get("report_id") and isinstance(item.get("report_version"), int)
    }
    available_records = dict(current_records)
    if draft.get("result_kind") == "explanation":
        # Historical evidence is usable only for explanations and keeps its
        # saved report/version provenance. It never counts as current data.
        available_records.update(historical_records)
    used_records: list[dict[str, Any]] = []
    evidence_goal_coverage: set[str] = set()
    rendered: list[str] = []
    for index, block in enumerate(draft.get("blocks") or []):
        path = f"blocks[{index}]"
        if block["kind"] == "text":
            number = _unverified_number(block["text"])
            if number:
                issues.append(_issue_dict(
                    "unbound_number", f"{path}.text", "semantic",
                    f"文字中包含未绑定的数字表达（{number[:32]}）。",
                    "将事实数值改为 fact_ref 或推荐实体表，由系统填入受信值。",
                ))
            rendered.append(block["text"].strip())
        elif block["kind"] == "fact_ref":
            evidence_id = str(block["evidence_id"])
            record = available_records.get(evidence_id)
            if record is None:
                all_ids = {str(item.get("evidence_id")) for item in evidence_records if isinstance(item, dict)}
                code = "stale_evidence" if evidence_id in all_ids else "unknown_evidence"
                message = "引用的证据已过期。" if code == "stale_evidence" else "引用的证据不存在。"
                issues.append(_issue_dict(
                    code, f"{path}.evidence_id",
                    "freshness" if code == "stale_evidence" else "reference",
                    message, "重新引用本次条件和数据版本下的 evidence_id。",
                    sorted(available_records)[:32],
                ))
                continue
            if record.get("source") == "historical_report" and draft.get("result_kind") != "explanation":
                issues.append(_issue_dict(
                    "historical_evidence_requires_explanation", f"{path}.evidence_id", "freshness",
                    "历史报告证据不能作为当前条件下的数据结论。",
                    "当前条件变化时重新查询数据工具；解释旧报告时选择 result_kind=explanation。",
                    [evidence_id],
                ))
                continue
            text = _fact_sentence(record) if block["format"] == "sentence" else _fact_value(record)
            if not text:
                issues.append(_issue_dict("evidence_value", f"{path}.evidence_id", "reference", "证据缺少可渲染的值或单位。"))
                continue
            if record.get("source") == "historical_report":
                text += (
                    f"（来自历史报告 {record.get('report_id')} v{record.get('report_version')}，"
                    f"数据版本 {record.get('dataset_version') or 'unknown'}；不是当前统计。）"
                )
            rendered.append(text)
            used_records.append(record)
            evidence_goal_coverage.update(map(str, record.get("goal_ids") or []))
        elif block["kind"] == "recommendation_table":
            text, rows = _render_recommendation_table(block, current_records)
            if not text:
                expected_groups = sorted({
                    str(item.get("entity_group_id"))
                    for item in current_records.values()
                    if item.get("entity_group_id") and item.get("entity_label")
                })[:32]
                issues.append(_issue_dict(
                    "entity_group_unavailable", f"{path}.entity_group_refs", "reference",
                    "候选实体组缺失或没有完整的价格证据。",
                    "只引用上下文中完整可见的 entity_group_id。", expected_groups,
                ))
                continue
            rendered.append(text)
            used_records.extend(rows)
            evidence_goal_coverage.update(
                str(goal_id) for row in rows for goal_id in row.get("goal_ids") or []
            )

    if draft["result_kind"] == "data_answer":
        uncovered = expected_goals - evidence_goal_coverage
        if uncovered:
            expected_evidence_refs = sorted({
                str(item.get("evidence_id"))
                for item in current_records.values()
                if set(map(str, item.get("goal_ids") or [])) & uncovered
            })[:32]
            issues.append(_issue_dict(
                "goal_without_evidence", "blocks", "coverage",
                f"部分目标没有被答案引用的证据覆盖：{', '.join(sorted(uncovered))}。",
                "每项目标都至少引用一项本次运行的受信数据证据。",
                expected_evidence_refs,
            ))

    limitation_text = {
        "listing_price_not_transaction": "价格为平台挂牌价，不代表实际成交价。",
        "sample_scope": "代表性样本不代表总体统计；请按报告所列样本口径理解。",
        "unsupported_filter": "部分筛选条件不受当前数据支持，结果未按该条件筛选。",
        "partial_coverage": "结果只覆盖当前证据能够支持的部分目标。",
        "stale_data_unavailable": "当前无法确认数据版本是否与此前查询一致。",
    }
    rendered.extend(limitation_text[code] for code in draft["limitation_codes"])
    answer = "\n\n".join(part for part in rendered if part).strip()
    if not answer:
        issues.append(_issue_dict("empty_answer", "blocks", "coverage", "最终答案不能为空。", "添加说明文字或有效的事实引用。"))
    if not issues and draft["result_kind"] in {"no_data", "explanation", "partial_result"} and not draft["limitation_codes"] and not draft["blocks"]:
        issues.append(_issue_dict("outcome_reason_required", "limitation_codes", "coverage", "此类结果需要说明结果边界或原因。"))
    if issues:
        return ValidationResult(False, list(dict.fromkeys(item["message"] for item in issues)), issues=issues)

    unique_used = {str(item.get("evidence_id")): item for item in used_records}
    return ValidationResult(
        True,
        [],
        answer=answer,
        claims=[{"evidence": item} for item in unique_used.values()],
        limitations=[limitation_text[code] for code in draft["limitation_codes"]],
        issues=[],
    )


def validate_answer_draft(
    draft: dict[str, Any],
    evidence_records: list[dict[str, Any]],
    *,
    dataset_version: str | None = None,
    constraint_revision: str | int | None = None,
    required_goal_ids: list[str] | None = None,
) -> ValidationResult:
    """Check claims against evidence and require narrative numbers to use claim placeholders."""
    if isinstance(draft, dict) and draft.get("schema_version") == 2:
        return _validate_v2(
            draft, evidence_records, dataset_version=dataset_version,
            constraint_revision=constraint_revision, required_goal_ids=required_goal_ids,
        )
    errors: list[str] = []
    if not isinstance(draft, dict):
        issue = _issue_dict("draft_type", "$", "schema", "答案草稿必须是对象。", "提交 JSON 对象。")
        return ValidationResult(False, [issue["message"]], issues=[issue])
    claims = draft.get("claims")
    narrative = draft.get("narrative", [])
    limitations = draft.get("limitations", [])
    if not isinstance(claims, list) or not claims:
        errors.append("数据型答案必须至少包含一项结构化 claim")
        claims = []
    if not isinstance(narrative, list) or any(not isinstance(text, str) for text in narrative):
        errors.append("narrative 必须是字符串数组")
        narrative = []
    if not isinstance(limitations, list) or any(not isinstance(text, str) for text in limitations):
        errors.append("limitations 必须是字符串数组")
        limitations = []

    records = {str(item.get("evidence_id")): item for item in evidence_records if isinstance(item, dict)}
    checked: list[dict[str, Any]] = []
    claim_ids: set[str] = set()
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict):
            errors.append(f"claims[{index}] 必须是对象")
            continue
        claim_id = str(claim.get("claim_id") or "")
        if not claim_id or claim_id in claim_ids:
            errors.append(f"claims[{index}] 缺少唯一 claim_id")
            continue
        claim_ids.add(claim_id)
        record = records.get(str(claim.get("evidence_id") or ""))
        if record is None:
            errors.append(f"claims[{index}] 引用了不存在的证据")
            continue
        if claim.get("metric") != record.get("metric"):
            errors.append(f"claims[{index}] 指标与证据不匹配")
        expected_unit = _UNIT_ALIASES.get(str(claim.get("unit") or ""))
        actual_unit = str(record.get("unit") or "")
        if expected_unit is None or (expected_unit != actual_unit and (actual_unit, expected_unit) not in {("CNY", "wan"), ("wan", "CNY")}):
            errors.append(f"claims[{index}] 单位与证据不匹配")
        supplied = _round(claim.get("value"), expected_unit or actual_unit)
        recorded = _round(_convert(record.get("value"), actual_unit, expected_unit or actual_unit), expected_unit or actual_unit)
        if supplied is None or recorded is None or supplied != recorded:
            errors.append(f"claims[{index}] 数值与证据不匹配")
        if claim.get("scope") == "population" and record.get("scope") != "population":
            errors.append(f"claims[{index}] 代表性样本不能作为总体统计")
        expected_filters = claim.get("filters")
        if expected_filters is not None:
            expected_conditions = _condition_pairs(expected_filters)
            evidence_conditions = _condition_pairs(record.get("filters") or [])
            if expected_conditions is None or evidence_conditions is None or any(
                evidence_conditions.get(key) != value for key, value in expected_conditions.items()
            ):
                errors.append(f"claims[{index}] 条件与证据不匹配")
        if dataset_version is not None and record.get("dataset_version") != dataset_version:
            errors.append(f"claims[{index}] 使用了旧数据版本证据")
        if constraint_revision is not None and record.get("constraint_revision") != constraint_revision:
            errors.append(f"claims[{index}] 使用了旧条件版本证据")
        safe_claim = {key: value for key, value in claim.items() if key != "filters"}
        checked.append({"claim": safe_claim, "record": record})

    rendered_claims = {str(pair["claim"].get("claim_id")): _claim_text(pair["claim"], pair["record"]) for pair in checked}
    for text in narrative:
        placeholders = _PLACEHOLDER.findall(text)
        residue = _PLACEHOLDER.sub("", text)
        if "{{claim:" in residue or "}}" in residue:
            errors.append("narrative 中存在格式错误的 claim 占位符")
        if _unverified_number(residue):
            errors.append("narrative 含有未绑定的数字表达；请用 {{claim:claim_id}} 引用已验证 claim")
        for claim_id in placeholders:
            if claim_id not in rendered_claims:
                errors.append(f"narrative 引用了未验证的 claim：{claim_id}")
    for text in limitations:
        if _unverified_number(text):
            errors.append("limitations 含有未绑定的数字表达；请改为无数字限制说明")

    if errors:
        unique_errors = list(dict.fromkeys(errors))
        return ValidationResult(False, unique_errors, issues=_legacy_issues(unique_errors))
    paragraphs = [*limitations]
    paragraphs.extend(_PLACEHOLDER.sub(lambda match: rendered_claims[match.group(1)], text) for text in narrative)
    answer = "\n\n".join(paragraphs).strip()
    if not answer:
        issue = _issue_dict("empty_answer", "narrative", "coverage", "最终答案不能为空。", "添加自然语言说明或引用已验证 claim。")
        return ValidationResult(False, [issue["message"]], issues=[issue])
    used_claim_ids = {claim_id for text in narrative for claim_id in _PLACEHOLDER.findall(text)}
    unused = claim_ids - used_claim_ids
    if unused:
        issue = _issue_dict("unused_claim", "claims", "coverage", "答案中存在未引用的 claim。", "删除未使用 claim，或在正文中引用它。")
        return ValidationResult(False, [issue["message"]], issues=[issue])
    return ValidationResult(
        True, [], answer=answer,
        claims=[{**pair["claim"], "evidence": pair["record"]} for pair in checked],
        limitations=list(limitations), issues=[],
    )


def render_validated_answer(draft: dict[str, Any], evidence_records: list[dict[str, Any]], **kwargs: Any) -> str:
    """Convenience renderer; raises on invalid drafts so callers cannot bypass the gate."""
    result = validate_answer_draft(draft, evidence_records, **kwargs)
    if not result.valid:
        raise ValueError("；".join(result.errors))
    return result.answer


def answer_candidate_fingerprint(draft: Any) -> str:
    """Stable fingerprint used to detect a repeated, unchanged failed draft."""
    try:
        payload = json.dumps(draft, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        payload = repr(draft)
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


def record_answer_validation_failure(state: Any, draft: Any, validation: ValidationResult, *, phase: str) -> dict[str, Any]:
    """Persist complete, deduplicated diagnostics and candidate identity."""
    fingerprint = answer_candidate_fingerprint(draft)
    issues: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for issue in validation.issues or []:
        if not isinstance(issue, dict):
            continue
        normalized: dict[str, Any] = {
            key: str(issue.get(key) or "")
            for key in ("code", "path", "category", "message", "repair_hint")
        }
        raw_expected_refs = issue.get("expected_refs")
        expected_refs = raw_expected_refs if isinstance(raw_expected_refs, (list, tuple)) else []
        normalized["expected_refs"] = list(dict.fromkeys(
            str(value) for value in expected_refs if str(value)
        ))[:32]
        identity = (normalized["code"], normalized["path"], normalized["message"])
        if identity not in seen:
            seen.add(identity)
            issues.append(normalized)
    record = {
        "valid": False,
        "errors": list(dict.fromkeys(str(item) for item in validation.errors)),
        "issues": issues,
        "candidate_fingerprint": fingerprint,
        "phase": phase,
    }
    previous = state.flags.get("answer_validation")
    state.flags["answer_validation"] = record
    state.flags["answer_validation_candidate_fingerprint"] = fingerprint
    state.flags["answer_validation_attempts"] = int(state.flags.get("answer_repair_count") or 0)
    if isinstance(previous, dict) and previous.get("candidate_fingerprint") == fingerprint:
        state.flags["answer_no_progress"] = True
    else:
        state.flags.pop("answer_no_progress", None)
    return record


def deterministic_answer_from_evidence(state: Any) -> ValidationResult | None:
    """Build an answer only from complete, fresh evidence bound to all data goals."""
    from backend.agent_runtime.answer_contract import ANSWER_SCHEMA_VERSION
    from backend.data_loader import get_dataset_version

    def reject(code: str, message: str, goals: list[str] | None = None) -> None:
        state.flags["answer_fallback_gaps"] = [{"code": code, "message": message, "goal_ids": goals or []}]

    state.flags.pop("answer_fallback_gaps", None)
    plan = list(getattr(state, "plan", []) or [])
    if (
        not plan or plan[-1].get("kind") != "finalize" or plan[-1].get("status") != "running"
        or any(str(step.get("status") or "") not in {"complete", "skipped"} for step in plan[:-1])
        or state.pending_input is not None or state.pending_tool_batch is not None
        or state.flags.get("pending_plan_revision")
    ):
        reject("finalize_preconditions", "计划前置步骤、finalize 阶段或等待状态尚未收口。")
        return None
    goals = [goal for goal in (state.task_intent or {}).get("goals") or [] if isinstance(goal, dict)]
    required_goals = [str(goal.get("id") or "") for goal in goals]
    if not required_goals or any(not value for value in required_goals):
        reject("task_goals_missing", "任务目标列表缺失或无效。")
        return None
    data_goals = {
        str(goal_id) for step in plan if step.get("kind") == "data"
        for goal_id in step.get("goal_ids") or []
    }
    if data_goals != set(required_goals):
        reject("non_data_or_unplanned_goal", "并非所有目标都是当前计划中的数据目标。", sorted(set(required_goals) - data_goals))
        return None

    try:
        dataset_version = get_dataset_version()
    except Exception:
        reject("dataset_version_unavailable", "无法确认当前数据版本。", required_goals)
        return None
    revision = (state.task_context or {}).get("revision", 0)
    successful_calls: set[str] = set()
    for result in (state.tool_results or []):
        if not isinstance(result, dict):
            continue
        step = next((item for item in plan if str(item.get("id") or "") == str(result.get("plan_step_id") or "")), None)
        output = result.get("output")
        if (
            result.get("status") == "done" and isinstance(output, dict) and output.get("ok") is True
            and step is not None and step.get("kind") == "data"
            and str(result.get("tool_name") or "") in set(map(str, step.get("tool_names") or []))
            and int(result.get("constraint_revision", revision)) == int(revision)
            and set(map(str, step.get("goal_ids") or [])).issubset(set(map(str, result.get("goal_ids") or [])))
        ):
            successful_calls.add(str(result.get("tool_call_id") or ""))
    evidence = [
        item for item in (state.evidence_records or [])
        if isinstance(item, dict) and item.get("tool_call_id") in successful_calls
        and item.get("dataset_version") == dataset_version and item.get("constraint_revision") == revision
    ]
    stale_goal_ids = {
        str(goal_id) for item in (state.evidence_records or []) if isinstance(item, dict)
        and (item.get("dataset_version") != dataset_version or item.get("constraint_revision") != revision)
        for goal_id in item.get("goal_ids") or []
    }
    evidenced_goals = {str(goal_id) for item in evidence for goal_id in item.get("goal_ids") or []}
    empty_result_goals = {
        str(goal_id)
        for result in (state.tool_results or [])
        if isinstance(result, dict)
        and str(result.get("tool_call_id") or "") in successful_calls
        and int(result.get("constraint_revision", revision)) == int(revision)
        and result.get("dataset_version") == dataset_version
        and isinstance(result.get("output"), dict)
        and result["output"].get("ok") is True
        and (result["output"].get("result_status") == "empty" or result["output"].get("status") == "empty")
        for goal_id in (
            set(map(str, result.get("goal_ids") or []))
            & {
                str(goal_id)
                for step in plan
                if step.get("kind") == "data"
                and str(step.get("id") or "") == str(result.get("plan_step_id") or "")
                for goal_id in step.get("goal_ids") or []
            }
        )
    }
    stale_empty_result_goals: set[str] = set()
    for result in (state.tool_results or []):
        if not isinstance(result, dict):
            continue
        step = next(
            (item for item in plan if str(item.get("id") or "") == str(result.get("plan_step_id") or "")),
            None,
        )
        output = result.get("output")
        if (
            result.get("status") == "done" and isinstance(output, dict) and output.get("ok") is True
            and (output.get("result_status") == "empty" or output.get("status") == "empty")
            and step is not None and step.get("kind") == "data"
        ):
            bound_goals = set(map(str, result.get("goal_ids") or [])) & set(map(str, step.get("goal_ids") or []))
            if (
                result.get("dataset_version") != dataset_version
                or int(result.get("constraint_revision", revision)) != int(revision)
            ):
                stale_empty_result_goals.update(bound_goals)
    if (
        data_goals == set(required_goals)
        and data_goals.issubset(empty_result_goals)
        and not (data_goals & evidenced_goals)
    ):
        draft = {
            "schema_version": ANSWER_SCHEMA_VERSION,
            "result_kind": "no_data",
            "goal_ids": required_goals,
            "blocks": [{
                "kind": "text",
                "text": "当前条件下没有可用于回答的数据，因此不提供数值结论。",
            }],
            "limitation_codes": [],
        }
        return validate_answer_draft(
            draft, evidence, dataset_version=dataset_version,
            constraint_revision=revision, required_goal_ids=required_goals,
        )
    if not data_goals.issubset(evidenced_goals):
        missing = sorted(data_goals - evidenced_goals)
        reject("stale_or_unbound_evidence" if set(missing) & (stale_goal_ids | stale_empty_result_goals) else "goal_evidence_missing",
               "部分目标没有绑定当前条件和数据版本下的成功证据。", missing)
        return None

    blocks: list[dict[str, Any]] = []
    has_price = False
    has_recommend = False
    for goal_id in required_goals:
        records = [item for item in evidence if goal_id in set(map(str, item.get("goal_ids") or []))]
        if not records:
            reject("goal_evidence_missing", "目标缺少可用的当前证据。", [goal_id])
            return None
        price = {str(item.get("metric")): item for item in records if str(item.get("metric")) in {
            "price.p25_yuan", "price.median_yuan", "price.p75_yuan", "price.mean_yuan", "sample_size",
        }}
        required_price = {"price.p25_yuan", "price.median_yuan", "price.p75_yuan", "sample_size"}
        if required_price.issubset(price):
            has_price = True
            for metric in ("price.p25_yuan", "price.median_yuan", "price.p75_yuan", "price.mean_yuan", "sample_size"):
                if metric in price:
                    blocks.append({"kind": "fact_ref", "evidence_id": price[metric]["evidence_id"], "format": "sentence"})
            continue
        if price:
            stale_for_goal = any(
                isinstance(item, dict) and goal_id in set(map(str, item.get("goal_ids") or []))
                and (item.get("dataset_version") != dataset_version or item.get("constraint_revision") != revision)
                for item in (state.evidence_records or [])
            )
            reject("stale_or_unbound_evidence" if stale_for_goal else "price_distribution_incomplete",
                   "价格分布证据过期或缺少必需分位值、样本量。", [goal_id])
            return None
        groups: dict[str, list[dict[str, Any]]] = {}
        for item in records:
            group_id = str(item.get("entity_group_id") or "")
            if group_id and item.get("entity_label"):
                groups.setdefault(group_id, []).append(item)
        complete_groups = []
        for group_id, group in groups.items():
            metrics = {str(item.get("metric") or "").rsplit(".", 1)[-1] for item in group}
            if {"median_wan", "sample_size"}.issubset(metrics):
                complete_groups.append((group_id, metrics))
        if not complete_groups:
            reject("recommendation_group_incomplete", "推荐结果缺少车系、绑定中位价或样本量。", [goal_id])
            return None
        has_recommend = True
        columns = ["series", "median_price", "sample_size"]
        if all({"p25_wan", "p75_wan"}.issubset(metrics) for _, metrics in complete_groups):
            columns.append("price_interval")
        blocks.append({"kind": "recommendation_table", "entity_group_refs": [group_id for group_id, _ in complete_groups], "columns": columns})

    limitations = ["listing_price_not_transaction"] if has_price or has_recommend else []
    if any(item.get("scope") == "representative_sample" for item in evidence):
        limitations.append("sample_scope")
    draft = {
        "schema_version": ANSWER_SCHEMA_VERSION,
        "result_kind": "data_answer",
        "goal_ids": required_goals,
        "blocks": blocks,
        "limitation_codes": limitations,
    }
    return validate_answer_draft(
        draft, evidence, dataset_version=dataset_version,
        constraint_revision=revision, required_goal_ids=required_goals,
    )
