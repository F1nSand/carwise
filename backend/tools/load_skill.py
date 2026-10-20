"""固定名称的项目内 Skill 加载工具。"""

from __future__ import annotations

from pathlib import Path

from backend.agent_runtime.skills import load_skill
from backend.config import ROOT

PROJECT_ROOT = ROOT


def load_skill_handler(name: str) -> dict:
    return load_skill(PROJECT_ROOT, name)
