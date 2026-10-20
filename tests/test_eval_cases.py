import json

import pytest

from backend.evals.models import load_cases, parse_case


def test_eval_suite_has_unique_valid_cases():
    cases = load_cases(__import__("pathlib").Path("evals/cases/v1.jsonl"))
    assert len(cases) == 33
    assert len({case.id for case in cases}) == 33
    assert all(any(turn.role == "user" for turn in case.turns) for case in cases)


def test_eval_loader_filters_tags_and_rejects_duplicate_ids(tmp_path):
    cases = load_cases(__import__("pathlib").Path("evals/cases/v1.jsonl"), tags={"smoke"})
    assert cases and all("smoke" in case.tags for case in cases)
    item = {"id": "bad", "category": "price_comparison", "turns": [{"role": "user", "content": "x"}], "expect": {"status": "done"}}
    source = tmp_path / "cases.jsonl"
    source.write_text(json.dumps(item) + "\n" + json.dumps(item) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="重复"):
        load_cases(source)


def test_eval_loader_rejects_unknown_action(tmp_path):
    item = {"id": "bad-action", "category": "clarification", "turns": [{"role": "user", "action": "silently_skip", "content": "x"}], "expect": {"status": "done"}}
    source = tmp_path / "cases.jsonl"
    source.write_text(json.dumps(item), encoding="utf-8")
    with pytest.raises(ValueError, match="action 不受支持"):
        load_cases(source)
