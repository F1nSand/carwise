"""静态前缀和本地命中观测。

静态前缀只由模型、system prompt 和本轮冻结的工具 schema 构成。会话历史、
工具结果、Skills 路由和状态信息都属于动态上下文，不能参与 system prompt 拼接。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any


def _tool_name(schema: dict[str, Any]) -> str:
    return str((schema.get("function") or {}).get("name") or "")


def _canonical_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(tools, key=_tool_name)


def compute_prefix_hash(model: str, system_prompt: str, tools: list[dict[str, Any]]) -> str:
    """为完全相同的静态模型前缀生成稳定 SHA-256。"""
    canonical = json.dumps(
        {
            "model": str(model),
            "system_prompt": str(system_prompt),
            "tools": _canonical_tools(tools),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PrefixCacheTracker:
    """进程内静态前缀复用观测器。

    这不是供应商 KV/prompt cache 的伪造结果；它只标记本进程是否见过同一个
    prefix_hash。供应商真实命中仍以 response.usage 中的字段为准。
    """

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._lock = Lock()

    def mark(self, prefix_hash: str) -> bool:
        with self._lock:
            hit = prefix_hash in self._seen
            self._seen.add(prefix_hash)
            return hit


class FilePrefixCacheTracker(PrefixCacheTracker):
    """跨进程的前缀复用观测器；用 hash 文件的独占创建避免并发误报。"""

    def __init__(self, root: Path | str) -> None:
        super().__init__()
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def mark(self, prefix_hash: str) -> bool:
        path = self.root / f"{prefix_hash}.seen"
        try:
            with path.open("x", encoding="ascii") as stream:
                stream.write("1")
            return False
        except FileExistsError:
            return True
        except OSError:
            # 缓存观测不能阻断 Agent；回退到进程内 tracker。
            return super().mark(prefix_hash)


@dataclass(frozen=True)
class PrefixContext:
    model: str
    system_prompt: str
    tools: list[dict[str, Any]]
    prefix_hash: str
    cache_hit: bool


def _default_tracker() -> PrefixCacheTracker:
    try:
        from backend.config import ROOT

        return FilePrefixCacheTracker(ROOT / ".cache" / "prefixes")
    except Exception:
        return PrefixCacheTracker()


_GLOBAL_TRACKER = _default_tracker()


def build_prefix_context(
    model: str,
    system_prompt: str,
    tools: list[dict[str, Any]],
    *,
    tracker: PrefixCacheTracker | None = None,
) -> PrefixContext:
    stable_tools = json.loads(json.dumps(_canonical_tools(tools), ensure_ascii=False))
    prefix_hash = compute_prefix_hash(model, system_prompt, stable_tools)
    cache_hit = (tracker or _GLOBAL_TRACKER).mark(prefix_hash)
    return PrefixContext(
        model=str(model),
        system_prompt=str(system_prompt),
        tools=stable_tools,
        prefix_hash=prefix_hash,
        cache_hit=cache_hit,
    )
