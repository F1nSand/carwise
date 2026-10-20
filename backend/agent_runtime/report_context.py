"""Owner- and conversation-bound access to committed historical reports."""

from __future__ import annotations

import contextvars
import hashlib
import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator


@dataclass
class ReportAccess:
    conversation_id: str
    read_owned_report: Callable[[str], dict[str, Any]]
    state: Any = None


_CURRENT_ACCESS: contextvars.ContextVar[ReportAccess | None] = contextvars.ContextVar("report_access", default=None)
_VERSION = re.compile(r"(?:报告\s*)?[vVＶｖ]\s*(\d{1,5})")
_REVISION = re.compile(r"(?:改|调整|修改|更改|提高|降低|换成|重新).{0,24}(?:预算|城市|年份|条件|筛选|推荐|车系|品牌)|重新推荐|按新条件")
_REPORT_REFERENCE = re.compile(r"报告|这份分析|上次分析|刚才的分析|上一版")


@contextmanager
def bind_report_access(access: ReportAccess | None) -> Iterator[None]:
    token = _CURRENT_ACCESS.set(access)
    try:
        yield
    finally:
        _CURRENT_ACCESS.reset(token)


def report_access_from_store(store: Any, owner_id: str | None, conversation_id: str | None) -> ReportAccess | None:
    """Build a reader from the authenticated repository already bound to this run."""
    repository = getattr(store, "repository", None)
    owner = str(owner_id or "").strip()
    conversation = str(conversation_id or "").strip()
    reader = getattr(repository, "get_report_owned", None)
    if not owner or not conversation or not callable(reader):
        return None

    def read(report_id: str) -> dict[str, Any]:
        artifact = reader(str(report_id), owner)
        if str(artifact.get("conversation_id") or "") != conversation:
            raise KeyError("报告不存在")
        return artifact

    return ReportAccess(conversation, read)


def report_followup_mode(user_text: str) -> str | None:
    text = str(user_text or "")
    if _REVISION.search(text):
        return "revision"
    if _REPORT_REFERENCE.search(text):
        return "explanation"
    return None


def _version_from_text(text: str) -> int | None:
    match = _VERSION.search(str(text or ""))
    return int(match.group(1)) if match else None


def _select_report_reference(access: ReportAccess, user_text: str) -> dict[str, Any] | None:
    repository_reader = access.read_owned_report
    latest_getter = getattr(repository_reader, "latest_reference", None)
    if latest_getter is not None:
        latest = latest_getter()
    else:
        # ReportAccess reader may carry an attached repository helper. Resolve
        # only through its closure-backed latest reference, never client owner IDs.
        latest = None
    requested_version = _version_from_text(user_text)
    if requested_version is not None:
        entries_getter = getattr(repository_reader, "list_entries", None)
        if entries_getter is None:
            return None
        entries = entries_getter()
        matches = [entry.get("payload") for entry in entries
                   if entry.get("kind") == "report_card" and isinstance(entry.get("payload"), dict)
                   and int(entry["payload"].get("version") or 0) == requested_version]
        if not matches:
            return None
        return matches[-1]
    return latest


def bind_repository_helpers(access: ReportAccess, store: Any) -> ReportAccess:
    """Attach owner-scoped latest/entry lookups without exposing repository to the model."""
    repository = getattr(store, "repository", None)
    list_entries = getattr(repository, "list_conversation_entries_owned", None)
    conversation_get = getattr(repository, "get_conversation_owned", None)
    if callable(conversation_get):
        access.read_owned_report.latest_reference = lambda: (
            conversation_get(access.conversation_id, _owner_from_reader(access.read_owned_report)).get("latest_report")
        )  # type: ignore[attr-defined]
    if callable(list_entries):
        owner = _owner_from_reader(access.read_owned_report)
        def all_entries() -> list[dict[str, Any]]:
            entries: list[dict[str, Any]] = []
            before_seq = None
            while True:
                page = list_entries(access.conversation_id, owner, before_seq=before_seq, limit=100)
                entries.extend(page.get("entries", []))
                before_seq = page.get("next_before_seq")
                if before_seq is None:
                    return entries
        access.read_owned_report.list_entries = all_entries  # type: ignore[attr-defined]
    return access


def _owner_from_reader(_reader: Callable[[str], dict[str, Any]]) -> str:
    # Access is created in report_access_from_store, which replaces this helper
    # with a closure attribute. Keeping owner private prevents tool arguments
    # from selecting one.
    return str(getattr(_reader, "owner_id", ""))


def prepare_report_followup(store: Any, owner_id: str | None, conversation_id: str | None,
                           user_text: str, *, state: Any, max_chars: int = 6000,
                           source_report_id: str | None = None) -> tuple[ReportAccess | None, str | None]:
    mode = report_followup_mode(user_text)
    source_id = str(source_report_id or "").strip()
    if source_id and mode is None:
        # A report page's reanalysis action explicitly identifies the historical
        # artifact even when the edited prompt no longer mentions “report”.
        mode = "revision"
    access = report_access_from_store(store, owner_id, conversation_id)
    if access is None:
        return None, None
    # Bind owner-only helpers on the closure; they are not serializable or model visible.
    access.read_owned_report.owner_id = str(owner_id or "")  # type: ignore[attr-defined]
    access = bind_repository_helpers(access, store)
    if mode is None:
        access.state = state
        return access, None
    reference = (
        _select_report_reference(access, user_text)
        if _version_from_text(user_text) is not None
        else {"report_id": source_id} if source_id
        else _select_report_reference(access, user_text)
    )
    if not isinstance(reference, dict) or not reference.get("report_id"):
        access.state = state
        return access, ("本会话没有找到用户指定的已提交报告版本。不得猜测或读取其他会话报告；"
                        "请告知用户该版本不可用，或使用当前数据工具重新核验。")
    try:
        artifact = access.read_owned_report(str(reference["report_id"]))
    except (KeyError, ValueError):
        access.state = state
        return access, "本会话未找到该已提交报告。不得猜测或读取其他会话报告。"
    if requested := _version_from_text(user_text):
        if int(artifact.get("version") or 0) != requested:
            access.state = state
            return access, "所请求报告版本不可用；不得回退到其他版本。"
    if mode == "explanation":
        records = historical_evidence_records(artifact)
        _merge_historical_evidence(state, records)
    access.state = state
    content = format_report_context(artifact, mode=mode, max_chars=max_chars)
    return access, content


def format_report_context(artifact: dict[str, Any], *, mode: str, max_chars: int = 6000) -> str:
    report_id = str(artifact.get("report_id") or "")
    version = int(artifact.get("version") or 0)
    dataset_version = str(artifact.get("dataset_version") or "unknown")
    content = artifact.get("content") if isinstance(artifact.get("content"), dict) else {}
    context_text = str(artifact.get("context_text") or "").strip()
    if mode == "revision":
        body = (
            f"source=historical_report; report_id={report_id}; version=v{version}; "
            f"dataset_version={dataset_version}\n"
            "这些是旧报告的背景信息，不是当前数据证据。用户改变了条件；必须调用对应数据工具重新取数。"
            "不得把旧报告数值当作新条件下的结果，也不得仅改写旧表格。\n"
            f"question={str(content.get('question') or '')[:500]}\n"
            f"historical_summary={str(content.get('summaryText') or '')[:900]}\n"
        )
        return "[HISTORICAL_REPORT_CONTEXT_V1]\n" + body[:max_chars]
    claims = historical_evidence_records(artifact)
    evidence_lines = [
        f"{item['evidence_id']} | {item.get('metric')}={item.get('value')} {item.get('unit')} | "
        f"scope={item.get('scope')} | filters={'; '.join(item.get('filters') or [])}"
        for item in claims[:24]
    ]
    body = (
        f"source=historical_report; report_id={report_id}; version=v{version}; "
        f"origin_run_id={artifact.get('origin_run_id')}; dataset_version={dataset_version}\n"
        "只用于解释此历史版本；回答数值须标出报告版本与数据版本，不得当成当前统计。条件有变时重新查询。\n"
        f"{context_text}\n"
        "historical_evidence (cite only for explanation of this saved report):\n"
        + "\n".join(evidence_lines)
    )
    return "[HISTORICAL_REPORT_CONTEXT_V1]\n" + body[:max_chars]


def historical_evidence_records(artifact: dict[str, Any]) -> list[dict[str, Any]]:
    report_id = str(artifact.get("report_id") or "")
    version = int(artifact.get("version") or 0)
    content = artifact.get("content") if isinstance(artifact.get("content"), dict) else {}
    answer_evidence = content.get("answerEvidence") if isinstance(content.get("answerEvidence"), dict) else {}
    records: list[dict[str, Any]] = []
    for claim in answer_evidence.get("claims") or []:
        if not isinstance(claim, dict) or claim.get("value") is None or not claim.get("unit"):
            continue
        source_id = str(claim.get("evidenceId") or claim.get("claimId") or "evidence")
        digest = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:12]
        records.append({
            "evidence_id": f"hist:{report_id}:{version}:{digest}",
            "metric": claim.get("metric"), "value": claim.get("value"), "unit": claim.get("unit"),
            "scope": claim.get("scope") or "population", "sample_size": claim.get("sampleSize"),
            "filters": list(claim.get("filters") or []), "dataset_version": claim.get("datasetVersion") or artifact.get("dataset_version"),
            "constraint_revision": None, "source": "historical_report", "report_id": report_id,
            "report_version": version, "origin_run_id": str(artifact.get("origin_run_id") or ""),
        })
    return records


def _merge_historical_evidence(state: Any, records: list[dict[str, Any]]) -> None:
    if state is None or not records:
        return
    existing = {str(item.get("evidence_id")) for item in state.evidence_records if isinstance(item, dict)}
    state.evidence_records.extend(item for item in records if str(item["evidence_id"]) not in existing)


def read_report_handler(report_id: str, section: str | None = None) -> dict[str, Any]:
    access = _CURRENT_ACCESS.get()
    if access is None:
        return {"ok": False, "error_code": "report_context_unavailable", "error": "当前没有绑定的会话报告访问权限。"}
    try:
        artifact = access.read_owned_report(report_id)
    except (KeyError, ValueError):
        return {"ok": False, "error_code": "report_not_found", "error": "该报告不存在或不属于当前会话。"}
    content = artifact.get("content") if isinstance(artifact.get("content"), dict) else {}
    if section:
        if section not in {"answer", "answerEvidence", "analysisBlocks", "reliability", "methodology", "filtersApplied", "limitations", "recommendations", "priceAnalysis", "dataAsOf", "summaryText"}:
            return {"ok": False, "error_code": "report_section_invalid", "error": "不支持读取该报告部分。"}
        content = {section: content[section]} if section in content else {}
    records = historical_evidence_records(artifact)
    _merge_historical_evidence(access.state, records)
    return {
        "ok": True, "source": "historical_report", "report_id": artifact["report_id"],
        "version": artifact["version"], "origin_run_id": artifact["origin_run_id"],
        "dataset_version": artifact.get("dataset_version"), "context_text": str(artifact.get("context_text") or ""),
        "content": content, "evidence_refs": [item["evidence_id"] for item in records],
    }
