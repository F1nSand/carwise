from types import SimpleNamespace

import pytest

from backend.agent_runtime.report_context import (
    bind_report_access,
    historical_evidence_records,
    prepare_report_followup,
    read_report_handler,
    report_access_from_store,
    report_followup_mode,
)


class _Repository:
    def __init__(self):
        self.artifacts = {
            "report-1": self._artifact("report-1", 1, "old-v1"),
            "report-2": self._artifact("report-2", 2, "old-v2"),
            "foreign": {**self._artifact("foreign", 1, "foreign"), "conversation_id": "other-chat"},
        }

    @staticmethod
    def _artifact(report_id, version, dataset_version):
        return {
            "report_id": report_id, "conversation_id": "chat-a", "origin_run_id": f"run-{version}",
            "version": version, "dataset_version": dataset_version,
            "context_text": f"validated_answer: saved answer v{version}",
            "content": {"question": "15 万预算", "summaryText": f"旧报告摘要 v{version}",
                        "answerEvidence": {"claims": [{"evidenceId": "evidence-1", "metric": "price.median_wan",
                                                         "value": 12.3, "unit": "wan", "scope": "population",
                                                         "sampleSize": 10, "filters": ["budget_max_wan=15"],
                                                         "datasetVersion": dataset_version}] }},
        }

    def get_conversation_owned(self, conversation_id, owner_id):
        if conversation_id != "chat-a" or owner_id != "owner-a":
            raise KeyError("not found")
        return {"latest_report": {"report_id": "report-2", "version": 2, "origin_run_id": "run-2"}}

    def get_report_owned(self, report_id, owner_id):
        if owner_id != "owner-a" or report_id not in self.artifacts:
            raise KeyError("not found")
        return dict(self.artifacts[report_id])

    def list_conversation_entries_owned(self, conversation_id, owner_id, *, before_seq=None, limit=100):
        if conversation_id != "chat-a" or owner_id != "owner-a":
            raise KeyError("not found")
        return {"entries": [
            {"kind": "report_card", "payload": {"report_id": "report-1", "version": 1}},
            {"kind": "report_card", "payload": {"report_id": "report-2", "version": 2}},
        ], "next_before_seq": None}


def _store(repository=None):
    return SimpleNamespace(repository=repository or _Repository())


def test_report_followup_modes_distinguish_explanation_from_new_conditions():
    assert report_followup_mode("报告中的中位数是什么意思？") == "explanation"
    assert report_followup_mode("预算改到 10 万，重新推荐") == "revision"
    assert report_followup_mode("帮我看看新能源") is None


def test_explanation_context_selects_exact_same_conversation_version_and_marks_provenance():
    state = SimpleNamespace(evidence_records=[])
    access, context = prepare_report_followup(
        _store(), "owner-a", "chat-a", "请解释报告 v1 中位数", state=state,
    )
    assert access is not None
    assert "report_id=report-1; version=v1" in context
    assert "dataset_version=old-v1" in context
    assert state.evidence_records[0]["source"] == "historical_report"
    assert state.evidence_records[0]["report_id"] == "report-1"
    assert state.evidence_records[0]["evidence_id"].startswith("hist:report-1:1:")


def test_changed_conditions_get_historical_reference_but_no_reusable_old_evidence():
    state = SimpleNamespace(evidence_records=[])
    _access, context = prepare_report_followup(
        _store(), "owner-a", "chat-a", "预算改到 10 万，重新推荐", state=state,
    )
    assert "source=historical_report" in context
    assert "必须调用对应数据工具重新取数" in context
    assert state.evidence_records == []


def test_explicit_source_report_id_survives_edited_prompt_without_report_keyword():
    state = SimpleNamespace(evidence_records=[])
    _access, context = prepare_report_followup(
        _store(), "owner-a", "chat-a", "改成 18 万预算", state=state,
        source_report_id="report-1",
    )
    assert "report_id=report-1; version=v1" in context
    assert "必须调用对应数据工具重新取数" in context
    assert state.evidence_records == []


def test_report_reader_is_bound_to_owner_and_conversation_even_for_direct_tool_ids():
    reader = report_access_from_store(_store(), "owner-a", "chat-a")
    assert reader is not None
    reader.state = SimpleNamespace(evidence_records=[])
    with bind_report_access(reader):
        accepted = read_report_handler("report-2")
        rejected = read_report_handler("foreign")
        assert accepted["ok"] is True and accepted["dataset_version"] == "old-v2"
        assert rejected["ok"] is False and rejected["error_code"] == "report_not_found"


def test_read_report_supports_shared_analysis_blocks_section():
    reader = report_access_from_store(_store(), "owner-a", "chat-a")
    artifact = _Repository._artifact("report-2", 2, "old-v2")
    blocks = [{"analysis_type": "price_distribution", "dataset_version": "old-v2"}]
    artifact["content"]["analysisBlocks"] = blocks
    reader.read_owned_report = lambda report_id: artifact
    reader.state = SimpleNamespace(evidence_records=[])
    with bind_report_access(reader):
        result = read_report_handler("report-2", section="analysisBlocks")
    assert result["ok"] is True
    assert result["content"] == {"analysisBlocks": blocks}

    foreign_owner = report_access_from_store(_store(), "owner-b", "chat-a")
    assert foreign_owner is not None
    with bind_report_access(foreign_owner):
        assert read_report_handler("report-2")["ok"] is False


def test_no_cross_conversation_artifact_is_returned_even_if_repository_lookup_finds_it():
    access = report_access_from_store(_store(), "owner-a", "chat-a")
    assert access is not None
    with pytest.raises(KeyError):
        access.read_owned_report("foreign")


def test_historical_evidence_records_preserve_dataset_version_and_report_identity():
    records = historical_evidence_records(_Repository._artifact("report-x", 4, "data-2024"))
    assert records[0]["dataset_version"] == "data-2024"
    assert records[0]["report_version"] == 4
    assert records[0]["source"] == "historical_report"
