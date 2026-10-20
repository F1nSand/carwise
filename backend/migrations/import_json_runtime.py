"""One-time, idempotent importer for legacy JSON run/session snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.agent_runtime.sqlite_repository import SQLiteRuntimeRepository


class MigrationConflict(RuntimeError):
    """Raised when a source file changes or collides with stored runtime data."""


@dataclass(frozen=True)
class ImportReport:
    imported: int
    skipped: int
    conflicts: int = 0


def _read_sources(directory: Path | str | None, kind: str) -> list[tuple[str, str, str, dict[str, Any]]]:
    if directory is None:
        return []
    root = Path(directory)
    if not root.exists():
        return []
    if not root.is_dir():
        raise ValueError(f"JSON {kind} 路径必须是目录：{root}")
    rows: list[tuple[str, str, str, dict[str, Any]]] = []
    for path in sorted(root.glob("*.json")):
        raw = path.read_bytes()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"JSON {kind} 文件无法解析：{path}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"JSON {kind} 文件必须包含对象：{path}")
        source_key = f"{kind}:{path.resolve()}"
        content_hash = hashlib.sha256(raw).hexdigest()
        rows.append((source_key, content_hash, str(path.resolve()), payload))
    return rows


def import_json_runtime(
    runs_dir: Path | str | None,
    sessions_dir: Path | str | None,
    database: Path | str,
    *,
    legacy_owner_id: str,
) -> ImportReport:
    owner_id = str(legacy_owner_id or "").strip()
    if not owner_id:
        raise ValueError("legacy_owner_id 不能为空")
    run_rows = _read_sources(runs_dir, "run")
    session_rows = _read_sources(sessions_dir, "session")
    repository = SQLiteRuntimeRepository(database)
    try:
        imported, skipped = repository.import_legacy_batch(
            run_rows,
            session_rows,
            legacy_owner_id=owner_id,
        )
    except ValueError as exc:
        raise MigrationConflict(str(exc)) from exc
    return ImportReport(imported=imported, skipped=skipped)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入旧版 JSON Agent runtime 到 SQLite WAL")
    parser.add_argument("--runs", required=True, help="旧 run JSON 文件目录")
    parser.add_argument("--sessions", required=True, help="旧 session JSON 文件目录")
    parser.add_argument("--database", required=True, help="SQLite 数据库文件")
    parser.add_argument("--legacy-owner-id", required=True, help="要绑定旧资源的 owner ID")
    args = parser.parse_args(argv)
    try:
        report = import_json_runtime(
            args.runs,
            args.sessions,
            args.database,
            legacy_owner_id=args.legacy_owner_id,
        )
    except MigrationConflict as exc:
        print(f"imported=0 skipped=0 conflicts=1 error={exc}", file=sys.stderr)
        return 1
    print(f"imported={report.imported} skipped={report.skipped} conflicts={report.conflicts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
