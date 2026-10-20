# -*- coding: utf-8 -*-
"""
Prompt 模板工具 —— 安全渲染

为什么不用 str.format()：
  prompt 里包含 JSON 示例（如 {"tool": ...}），花括号会被 format 误判为占位符。
  这里改用字面量替换 {key}，避免该问题。

对外导出：
  load_prompt(name) -> str
  render(name, **kwargs) -> str
"""
from __future__ import annotations

from backend.config import ROOT

PROMPT_DIR = ROOT / "backend" / "prompts"


def load_prompt(name: str) -> str:
    p = PROMPT_DIR / name
    return p.read_text(encoding="utf-8") if p.exists() else ""


def render(name: str, **kwargs) -> str:
    """把模板里的 {key} 用 kwargs 字面量替换（不做 format 解析）。"""
    text = load_prompt(name)
    for k, v in kwargs.items():
        text = text.replace("{" + k + "}", str(v))
    return text
