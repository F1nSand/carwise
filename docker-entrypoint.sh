#!/bin/sh
set -eu

DB_PATH="$(python -c 'from backend.config import RUNTIME_DATABASE_PATH; print(RUNTIME_DATABASE_PATH)')"
MARKER="${DB_PATH}.json-runtime-imported"
mkdir -p "$(dirname "$DB_PATH")"
# A deleted/replaced database must not inherit a marker from a previous DB.
if [ ! -f "$DB_PATH" ]; then
  rm -f "$MARKER"
fi
if [ ! -f "$MARKER" ]; then
  python -m backend.migrations.import_json_runtime \
    --runs /app/.cache/runs \
    --sessions /app/.cache/sessions \
    --database "$DB_PATH" \
    --legacy-owner-id "${LEGACY_OWNER_ID:-legacy-import}" || exit $?
  date -u +%Y-%m-%dT%H:%M:%SZ > "$MARKER"
fi
exec "$@"
