"""把 Agent 的结构化工具结果投影为前端可渲染的分析报告。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


_CONFIDENCE_LABELS = {
    "high": "较高可靠性",
    "medium": "中等可靠性",
    "low": "谨慎参考",
    "none": "暂无结论",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _tool_output_items(result: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    items: list[tuple[str, dict[str, Any]]] = []
    for item in result.get("tool_results") or []:
        if not isinstance(item, dict) or item.get("status") not in {None, "done"}:
            continue
        name = str(item.get("tool_name") or item.get("tool") or "")
        output = item.get("output")
        if name and isinstance(output, dict) and output.get("ok", True):
            items.append((name, output))
    return items


def _tool_outputs(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {name: output for name, output in _tool_output_items(result)}


_SHARED_ANALYSIS_TOOLS = {
    "price_distribution", "mileage_price_correlation", "model_comparison",
    "brand_profile", "region_gap", "age_price_relation",
}


def _analysis_blocks(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep the complete, bounded shared results for the report renderer."""
    blocks: list[dict[str, Any]] = []
    for name, output in _tool_output_items(result):
        if name not in _SHARED_ANALYSIS_TOOLS:
            continue
        analysis = output.get("data")
        if not isinstance(analysis, dict):
            analysis = output.get("analysis_result")
        if not isinstance(analysis, dict) or analysis.get("analysis_type") not in _SHARED_ANALYSIS_TOOLS:
            continue
        blocks.append(analysis)
    return blocks


def _with_analysis_blocks(report: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    blocks = _analysis_blocks(result)
    if blocks:
        report["analysisBlocks"] = blocks
    return report


def _failed_tool_output_items(result: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    items: list[tuple[str, dict[str, Any]]] = []
    for item in result.get("tool_results") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("tool_name") or item.get("tool") or "")
        output = item.get("output")
        if not name or not isinstance(output, dict):
            continue
        if output.get("result_status") in {"empty", "error"} or output.get("ok") is False:
            items.append((name, output))
    return items


def _base_report(
    *,
    result: dict[str, Any],
    user_text: str,
    intent: str,
    scenario_label: str,
    confidence: str,
    sample_size: int | None,
    filters_applied: list[str],
    methodology: str,
    limitations: list[str],
    reliability_reason: str | None = None,
) -> dict[str, Any]:
    run_id = str(result.get("run_id") or result.get("trace_id") or "report")
    confidence_value = confidence if confidence in _CONFIDENCE_LABELS else "none"
    return {
        "id": run_id,
        "question": str(user_text or ""),
        "intent": intent,
        "scenarioLabel": scenario_label,
        "generatedAt": _now(),
        "dataAsOf": "项目清洗车源数据",
        "summaryTitle": (
            "预算内车系推荐"
            if intent == "recommend"
            else "车型挂牌行情"
            if intent == "price_compare"
            else "代表性车源证据"
            if intent == "evidence"
            else "数据边界说明"
            if intent == "no_data"
            else "Agent 回答"
        ),
        "summaryText": str(result.get("summary") or "已根据项目清洗数据完成分析。"),
        "reliability": {
            "confidence": confidence_value,
            "label": _CONFIDENCE_LABELS[confidence_value],
            "sampleSize": None if sample_size is None else max(0, int(sample_size)),
            "reason": reliability_reason
            or "可靠性基于工具返回的样本量和价格离散度；挂牌价仅供市场参考。",
        },
        "filtersApplied": _unique(filters_applied),
        "methodology": methodology,
        "limitations": _unique(limitations),
    }


def _recommendation_reason(output: dict[str, Any]) -> str:
    weights = output.get("weights") or {}
    if weights:
        return (
            "按样本量、年份新旧和中位里程的可解释权重排序："
            f"样本量 {weights.get('popularity', 0)}、年份 {weights.get('newness', 0)}、"
            f"里程 {weights.get('low_mileage', 0)}。"
        )
    return "按工具返回的样本量、年份和里程指标综合排序。"


def _recommendation_risk(confidence: str) -> str:
    if confidence == "high":
        return "样本参考性较好，但挂牌价不等于成交价，仍需结合车况和验车。"
    if confidence == "medium":
        return "样本或价格离散度一般，建议结合具体车源和验车结果判断。"
    return "样本有限或价格分散，结论仅供初筛，不建议作为单一决策依据。"


def _build_recommend_report(
    *,
    user_text: str,
    result: dict[str, Any],
    output: dict[str, Any],
) -> dict[str, Any]:
    recommendations: list[dict[str, Any]] = []
    confidence = str(output.get("confidence") or "none")
    risk = _recommendation_risk(confidence)
    reason = _recommendation_reason(output)
    for item in output.get("recommendations") or []:
        if not isinstance(item, dict):
            continue
        recommendations.append(
            {
                "series": item.get("series"),
                "brand": item.get("brand"),
                "sampleSize": item.get("sample_size"),
                "medianWan": item.get("median_wan"),
                "p25Wan": item.get("p25_wan"),
                "p75Wan": item.get("p75_wan"),
                "medianYear": item.get("median_year"),
                "medianMileageWanKm": item.get("median_mileage_wan_km"),
                "score": item.get("score"),
                "reason": reason,
                "risk": risk,
            }
        )

    sample_size = sum(int(item.get("sampleSize") or 0) for item in recommendations)
    filters = list(output.get("filters_applied") or [])
    limitations = [
        "价格为平台挂牌价，不是成交价。",
        "推荐排序来自工具的可解释统计权重，不代表车辆质量保证。",
    ]
    report = _base_report(
        result=result,
        user_text=user_text,
        intent="recommend",
        scenario_label="智能购车",
        confidence=confidence,
        sample_size=sample_size,
        filters_applied=filters,
        methodology="按预算筛选车源，按车系聚合中位挂牌价、分位数、样本量、年份和里程后排序。",
        limitations=limitations,
    )
    report.update(
        {
            "filters": {
                "budgetMinWan": output.get("budget_min_wan"),
                "budgetMaxWan": output.get("budget_max_wan"),
                "series": output.get("series"),
                "city": output.get("city"),
                "energyType": output.get("energy_type"),
            },
            "budgetCount": int(output.get("budget_count") or 0),
            "candidateCount": int(output.get("n_candidates") or 0),
            "recommendations": recommendations,
            "priceChart": [
                {"label": item["series"], "value": item["medianWan"]}
                for item in recommendations
                if item.get("series") is not None and item.get("medianWan") is not None
            ],
            "sampleChart": [
                {"label": item["series"], "value": item["sampleSize"]}
                for item in recommendations
                if item.get("series") is not None and item.get("sampleSize") is not None
            ],
        }
    )
    report["reliability"]["sampleSize"] = int(output.get("budget_count") or sample_size)
    return report


def _wan(value: Any) -> float | None:
    if value is None:
        return None
    return round(float(value) / 10000.0, 2)


def _build_price_report(
    *,
    user_text: str,
    result: dict[str, Any],
    price_output: dict[str, Any],
    outputs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    price = price_output.get("price") or {}
    dispersion = price_output.get("dispersion") or {}
    confidence = str(price_output.get("confidence") or "none")
    filters = list(price_output.get("filters_applied") or [])
    limitations = ["价格为平台挂牌价，不是成交价。"]
    year_output = outputs.get("price_decay")
    region_output = outputs.get("region_gap")
    if not year_output:
        limitations.append("本次未调用 price_decay，暂不展示按生产年份的价格衰减曲线。")
    if not region_output:
        limitations.append("本次未调用 region_gap，暂不展示城市挂牌价差异。")

    representative: list[dict[str, Any]] = []
    for item in price_output.get("representative_samples") or []:
        if not isinstance(item, dict):
            continue
        representative.append(
            {
                "carName": item.get("car_name"),
                "year": item.get("year"),
                "city": item.get("city"),
                "priceWan": _wan(item.get("price_yuan")),
                "mileageWanKm": _wan(item.get("mileage_km")),
            }
        )

    year_chart = [
        {"label": str(item.get("year")), "value": item.get("median_wan")}
        for item in (year_output or {}).get("points") or []
        if item.get("year") is not None and item.get("median_wan") is not None
    ]
    city_chart = [
        {"label": str(item.get("city")), "value": item.get("median_wan")}
        for item in (region_output or {}).get("cities") or []
        if item.get("city") is not None and item.get("median_wan") is not None
    ]

    report = _base_report(
        result=result,
        user_text=user_text,
        intent="price_compare",
        scenario_label="车型行情",
        confidence=confidence,
        sample_size=int(price_output.get("sample_size") or 0),
        filters_applied=filters,
        methodology="对指定车系进行异常值过滤、IQR 价格离群处理，并计算挂牌价分位数和代表性车源。",
        limitations=limitations,
    )
    report.update(
        {
            "filters": {
                "series": price_output.get("series"),
                "year": price_output.get("year"),
                "city": price_output.get("city"),
            },
            "series": price_output.get("series"),
            "price": {
                "p25Wan": price.get("p25_wan"),
                "medianWan": price.get("median_wan"),
                "p75Wan": price.get("p75_wan"),
                "meanWan": price.get("mean_wan"),
                "minWan": _wan(price.get("min_yuan")),
                "maxWan": _wan(price.get("max_yuan")),
                "iqrRatio": dispersion.get("iqr_ratio"),
            },
            "yearChart": year_chart,
            "cityChart": city_chart,
            "representativeListings": representative,
            "mileageInsight": "当前结果未单独估计里程对价格的边际影响，请将里程作为具体车源比较条件。",
        }
    )
    return report


def _final_answer(result: dict[str, Any]) -> str:
    final_message = result.get("final_message")
    answer = ""
    if isinstance(final_message, dict):
        answer = str(final_message.get("content") or "").strip()
    return answer or str(result.get("summary") or "Agent 未返回最终回答。")


def report_context_text(report: dict[str, Any], *, answer: str, report_id: str,
                        version: int, origin_run_id: str) -> str:
    """Build compact semantic context for a committed report artifact."""
    reliability = report.get("reliability") if isinstance(report.get("reliability"), dict) else {}
    evidence = report.get("answerEvidence") if isinstance(report.get("answerEvidence"), dict) else {}
    lines = [
        "[REPORT_CONTEXT]",
        f"report_id: {report_id}",
        f"version: {version}",
        f"origin_run_id: {origin_run_id}",
        f"title: {report.get('summaryTitle') or report.get('scenarioLabel') or '分析报告'}",
        f"question: {report.get('question') or ''}",
        f"validated_answer: {answer}",
        f"summary: {report.get('summaryText') or ''}",
        f"methodology: {report.get('methodology') or ''}",
        f"filters: {', '.join(str(value) for value in report.get('filtersApplied') or [])}",
        f"sample_size: {reliability.get('sampleSize')}",
        f"confidence: {reliability.get('confidence') or ''}",
        f"limitations: {'; '.join(str(value) for value in report.get('limitations') or [])}",
        f"dataset_version: {evidence.get('datasetVersion') or ''}",
    ]
    return "\n".join(lines)


def _build_evidence_report(
    *,
    user_text: str,
    result: dict[str, Any],
    csv_items: list[dict[str, Any]],
) -> dict[str, Any]:
    queries: list[dict[str, Any]] = []
    filters: list[str] = []
    for output in csv_items:
        query_filters = [str(value) for value in output.get("filters_applied") or []]
        filters.extend(query_filters)
        queries.append(
            {
                "filtersApplied": _unique(query_filters),
                "matchCount": max(0, int(output.get("match_count") or 0)),
                "rows": [row for row in output.get("rows") or [] if isinstance(row, dict)],
                "columns": [str(column) for column in output.get("columns") or []],
                "note": str(output.get("note") or ""),
            }
        )

    answer = _final_answer(result)
    report = _base_report(
        result=result,
        user_text=user_text,
        intent="evidence",
        scenario_label="车源证据",
        confidence="none",
        sample_size=0,
        filters_applied=filters,
        methodology="仅展示 csv_query 的查询命中数和代表性样本；未将截断样本转换为总体价格统计。",
        limitations=[
            "代表性样本不等同于总体统计。",
            "本次未生成总体价格分布、推荐排序或可比价格图表。",
        ],
        reliability_reason="仅有查询命中数和代表性样本，未计算总体价格统计。",
    )
    report.update(
        {
            "answer": answer,
            "summaryText": answer,
            "evidence": {"queryCount": len(queries), "queries": queries},
        }
    )
    return report


def _build_no_data_report(
    *,
    user_text: str,
    result: dict[str, Any],
    failed_items: list[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    tool_name, output = failed_items[0]
    note = str(output.get("note") or "当前条件没有形成可比样本。")
    filters: list[str] = []
    candidates: list[dict[str, Any]] = []
    for _, item in failed_items:
        filters.extend(str(value) for value in item.get("filters_applied") or [])
        candidates.extend(value for value in item.get("relaxation_candidates") or [] if isinstance(value, dict))
    answer = _final_answer(result)
    if not answer or answer == "Agent 未返回最终回答。":
        answer = note
    labels = [str(item.get("label") or "") for item in candidates if item.get("label")]
    limitations = [
        "本次工具没有形成有效可比样本，因此没有计算价格区间、推荐排序或图表。",
        "样本量显示为未知，不把无命中直接转换成可比样本数量。",
    ]
    if labels:
        limitations.append("可进一步尝试：" + "、".join(_unique(labels)) + "。")
    report = _base_report(
        result={**result, "summary": note},
        user_text=user_text,
        intent="no_data",
        scenario_label="数据边界说明",
        confidence="none",
        sample_size=None,
        filters_applied=filters,
        methodology=f"工具 {tool_name} 返回空结果（{output.get('reason_code') or 'no_data'}），未进行无依据的统计推断。",
        limitations=limitations,
        reliability_reason=note,
    )
    report.update(
        {
            "summaryText": note,
            "answer": answer,
            "reasonCode": str(output.get("reason_code") or "no_data"),
            "observed": output.get("observed") if isinstance(output.get("observed"), dict) else {},
            "relaxationCandidates": candidates,
        }
    )
    report["reliability"]["sampleSize"] = None
    return report


def _build_answer_report(*, user_text: str, result: dict[str, Any]) -> dict[str, Any]:
    answer = _final_answer(result)
    report = _base_report(
        result=result,
        user_text=user_text,
        intent="unknown",
        scenario_label="Agent 问答",
        confidence="none",
        sample_size=0,
        filters_applied=[],
        methodology="本次由 Agent 直接回答，未调用结构化数据工具。",
        limitations=["本次未调用结构化数据工具，回答不包含价格统计。"],
    )
    report["answer"] = answer
    report["summaryText"] = answer
    return report


def _apply_validated_answer(report: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Expose only compact validated evidence; legacy reports remain readable."""
    validation = result.get("answer_validation")
    if not isinstance(validation, dict) or validation.get("valid") is not True:
        return report
    answer = str(validation.get("answer") or "").strip()
    if not answer:
        return report
    claims = []
    for item in validation.get("claims") or []:
        if not isinstance(item, dict):
            continue
        evidence = item.get("evidence")
        if not isinstance(evidence, dict):
            continue
        projected = {
                "claimId": str(item.get("claim_id") or ""),
                "evidenceId": evidence.get("evidence_id"),
                "metric": evidence.get("metric"),
                "value": evidence.get("value"),
                "unit": evidence.get("unit"),
                "filters": _unique(evidence.get("filters") or []),
                "sampleSize": evidence.get("sample_size"),
                "scope": evidence.get("scope"),
                "datasetVersion": evidence.get("dataset_version"),
            }
        if evidence.get("source") and evidence.get("source") != "current_dataset":
            projected["source"] = evidence["source"]
        for source_key, target_key in (("report_id", "reportId"), ("report_version", "reportVersion"),
                                       ("origin_run_id", "originRunId")):
            if evidence.get(source_key) is not None:
                projected[target_key] = evidence[source_key]
        claims.append(projected)
    report["answer"] = answer
    report["summaryText"] = answer
    report["answerEvidence"] = {
        "validated": True,
        "source": "deterministic_fallback" if result.get("answer_validation_fallback") else "agent_draft",
        "claims": claims[:20],
        "limitations": _unique([str(value) for value in validation.get("limitations") or []]),
    }
    report["limitations"] = _unique(report.get("limitations", []) + report["answerEvidence"]["limitations"])
    return report


def build_report(*, user_text: str, result: dict[str, Any]) -> dict[str, Any] | None:
    """把工具证据或 Agent 直接回答投影成前端可渲染结果。"""
    tool_items = _tool_output_items(result)
    outputs = _tool_outputs(result)
    recommend = outputs.get("recommend")
    if recommend:
        report = _build_recommend_report(user_text=user_text, result=result, output=recommend)
        return _with_analysis_blocks(_apply_validated_answer(report, result), result)
    price_compare = outputs.get("price_compare")
    if price_compare:
        report = _build_price_report(
            user_text=user_text,
            result=result,
            price_output=price_compare,
            outputs=outputs,
        )
        return _with_analysis_blocks(_apply_validated_answer(report, result), result)
    csv_items = [output for name, output in tool_items if name == "csv_query"]
    if csv_items:
        report = _build_evidence_report(user_text=user_text, result=result, csv_items=csv_items)
        return _with_analysis_blocks(_apply_validated_answer(report, result), result)
    failed_items = _failed_tool_output_items(result)
    if failed_items:
        report = _build_no_data_report(user_text=user_text, result=result, failed_items=failed_items)
        return _with_analysis_blocks(_apply_validated_answer(report, result), result)
    if str(result.get("summary_source") or "agent") == "runtime":
        return None
    report = _build_answer_report(user_text=user_text, result=result)
    return _with_analysis_blocks(_apply_validated_answer(report, result), result)
