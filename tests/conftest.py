# -*- coding: utf-8 -*-
"""测试公共路径配置。模型交互全部由各测试显式 fake。"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def offline_model_boundary(monkeypatch):
    """Default service runners cannot use a developer's configured paid model.

    Individual tests and the trajectory harness explicitly replace this with
    their own scripted provider when they need to exercise the model loop.
    """
    from backend.agent_runtime import loop
    unavailable = SimpleNamespace(is_available=lambda: False, reason="offline test: inject a scripted provider")
    monkeypatch.setattr(loop, "get_client", lambda: unavailable)
