"""Configuration precedence using isolated fake files, never the real .env."""
from pathlib import Path
import runpy


def _config_copy(tmp_path):
    backend = tmp_path / "backend"
    backend.mkdir()
    target = backend / "config.py"
    target.write_text((Path(__file__).parents[1] / "backend/config.py").read_text(encoding="utf-8"), encoding="utf-8")
    return target


def test_process_values_override_local_env_and_share_database_path(tmp_path, monkeypatch):
    target = _config_copy(tmp_path)
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=local-fake\nRUNTIME_DATABASE_PATH=.cache/local.sqlite3\n", encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "process-fake")
    monkeypatch.setenv("RUNTIME_DATABASE_PATH", "custom/runtime.sqlite3")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
    result = runpy.run_path(str(target))
    assert result["DEEPSEEK_API_KEY"] == "process-fake"
    assert result["RUNTIME_DATABASE_PATH"] == (tmp_path / "custom/runtime.sqlite3").resolve()


def test_database_yaml_path_is_fallback_and_absolute_override_is_supported(tmp_path, monkeypatch):
    target = _config_copy(tmp_path)
    (tmp_path / "config.yaml").write_text("storage:\n  url: sqlite:///custom/yaml.sqlite3\n", encoding="utf-8")
    monkeypatch.delenv("RUNTIME_DATABASE_PATH", raising=False)
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
    assert runpy.run_path(str(target))["RUNTIME_DATABASE_PATH"] == (tmp_path / "custom/yaml.sqlite3").resolve()
    absolute = tmp_path / "absolute.sqlite3"
    monkeypatch.setenv("RUNTIME_DATABASE_PATH", str(absolute))
    assert runpy.run_path(str(target))["RUNTIME_DATABASE_PATH"] == absolute.resolve()
