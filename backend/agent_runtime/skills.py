"""项目内 Skills 的安全发现和按需读取。"""

from __future__ import annotations

import re
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_DEFAULT_MAX_CHARS = 12_000
SKILL_FORMAT_VERSION = "1"


@dataclass(frozen=True)
class SkillRoute:
    name: str
    description: str
    relative_path: str


def _skills_root(project_root: Path | str) -> Path:
    return Path(project_root).resolve() / ".agent" / "skills"


def _description(text: str) -> str:
    frontmatter = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, flags=re.DOTALL)
    if frontmatter:
        for line in frontmatter.group(1).splitlines():
            if line.lower().startswith("description:"):
                value = line.split(":", 1)[1].strip().strip('"\'')
                if value:
                    return value[:200]
        text = text[frontmatter.end() :]
    for line in text.splitlines():
        value = line.strip()
        if value and not value.startswith("#"):
            return value[:200]
    return ""


def discover_skills(project_root: Path | str) -> list[SkillRoute]:
    root = _skills_root(project_root)
    if not root.is_dir():
        return []
    routes: list[SkillRoute] = []
    for child in sorted(root.iterdir(), key=lambda path: path.name):
        skill_file = child / "SKILL.md"
        if not child.is_dir() or not _NAME.fullmatch(child.name) or not skill_file.is_file():
            continue
        try:
            text = skill_file.read_text(encoding="utf-8-sig").strip()
        except (OSError, UnicodeDecodeError):
            continue
        description = _description(text)
        if not description:
            continue
        routes.append(SkillRoute(child.name, description, f".agent/skills/{child.name}/SKILL.md"))
    return routes


def load_skill(
    project_root: Path | str,
    name: str,
    max_chars: int = _DEFAULT_MAX_CHARS,
) -> dict[str, Any]:
    value = str(name or "").strip()
    if not _NAME.fullmatch(value):
        raise ValueError("Skill 名称非法")
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 1 or max_chars > _DEFAULT_MAX_CHARS:
        raise ValueError(f"Skill 正文长度上限必须在 1-{_DEFAULT_MAX_CHARS} 之间")
    root = _skills_root(project_root)
    skill_file = (root / value / "SKILL.md").resolve()
    if skill_file.parent.parent != root or not skill_file.is_file():
        raise ValueError("Skill 不存在或不在项目 Skills 目录内")
    try:
        content = skill_file.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError("Skill 无法读取") from exc
    if len(content) > max_chars:
        raise ValueError("Skill 正文超过长度上限")
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {
        "ok": True,
        "skill": value,
        "content": content,
        "chars": len(content),
        "content_hash": content_hash,
        "version": SKILL_FORMAT_VERSION,
    }


def skill_route_context(routes: list[SkillRoute]) -> str:
    if not routes:
        return ""
    lines = ["[可用 Skills]", "需要完整步骤时调用 load_skill(name) 按名称加载正文："]
    lines.extend(f"- {route.name}: {route.description}" for route in routes)
    return "\n".join(lines)
