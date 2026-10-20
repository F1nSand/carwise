import json

import pytest

from backend.agent_runtime.skills import discover_skills, load_skill, skill_route_context
from backend.agent_runtime.tool_registry import execute_registered_tool, get_tool
import backend.tools.load_skill as load_skill_tool


def _write_skill(root, name, body):
    path = root / ".agent" / "skills" / name
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(body, encoding="utf-8")


def test_skill_routes_are_deterministic_and_only_include_non_empty_files(tmp_path):
    _write_skill(tmp_path, "z-price", "---\ndescription: 价格分析\n---\n# 价格\n按步骤分析挂牌价格。")
    _write_skill(tmp_path, "a-budget", "# 预算\n根据预算筛选车系。")
    empty = tmp_path / ".agent" / "skills" / "empty"
    empty.mkdir(parents=True)
    (empty / "SKILL.md").write_text("", encoding="utf-8")

    routes = discover_skills(tmp_path)

    assert [route.name for route in routes] == ["a-budget", "z-price"]
    assert routes[0].description == "根据预算筛选车系。"
    assert "a-budget" in skill_route_context(routes)
    assert "价格分析" in skill_route_context(routes)


def test_load_skill_rejects_traversal_and_enforces_size_limit(tmp_path):
    _write_skill(tmp_path, "safe", "# Safe\n正文")

    loaded = load_skill(tmp_path, "safe")
    assert loaded["ok"] is True
    assert loaded["version"] == "1"
    assert len(loaded["content_hash"]) == 64
    with pytest.raises(ValueError, match="名称"):
        load_skill(tmp_path, "..\\outside")
    with pytest.raises(ValueError, match="长度"):
        load_skill(tmp_path, "safe", max_chars=2)


def test_registered_load_skill_is_read_only_and_reads_only_project_root(tmp_path, monkeypatch):
    _write_skill(tmp_path, "safe", "# Safe\n正文")
    monkeypatch.setattr(load_skill_tool, "PROJECT_ROOT", tmp_path)

    result = execute_registered_tool("load_skill", {"name": "safe"})

    assert get_tool("load_skill").read_only is True
    assert result["ok"] is True
    assert "正文" in result["content"]
    assert result["content_hash"]
    json.dumps(result, ensure_ascii=False)


def test_project_domain_skills_have_applicability_and_evidence_boundaries():
    from backend.config import ROOT

    routes = discover_skills(ROOT)
    assert {route.name for route in routes} >= {"price-analysis", "recommendation", "empty-result-recovery"}
    for name in ("price-analysis", "recommendation", "empty-result-recovery"):
        content = load_skill(ROOT, name)["content"]
        assert "适用：" in content
        assert "不适用：" in content
        assert "适用条件" not in content or len(content) < 12_000
