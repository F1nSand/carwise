FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt ./
RUN uv pip install --system --no-cache -r requirements.txt

COPY config.yaml ./
COPY backend ./backend
COPY .agent/skills ./.agent/skills
COPY docker-entrypoint.sh ./docker-entrypoint.sh
COPY data/clean/dcd_clean.parquet ./data/clean/dcd_clean.parquet

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

ENTRYPOINT ["sh", "/app/docker-entrypoint.sh"]
CMD ["uvicorn", "backend.service:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
