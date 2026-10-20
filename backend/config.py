# -*- coding: utf-8 -*-
"""
配置加载 —— 统一读取项目根 config.yaml 与 .env

职责：
  - 定位项目根目录
  - 读取根 config.yaml（数据路径、异常阈值等）
  - 读取 .env 中的 DEEPSEEK_API_KEY（不写死在代码里）

对外导出：
  ROOT            项目根目录 Path
  PARQUET_PATH    清洗后 parquet 绝对路径
  TH              异常阈值字典
  DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL
  is_thinking_model
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

# 项目根目录 = backend/ 的上一级
ROOT = Path(__file__).resolve().parent.parent

# ---- 读取 .env（key 从环境变量来，不硬编码）----
# Existing process/deployment values take precedence over local development files.
try:
    from dotenv import load_dotenv
except ImportError:
    pass
else:
    load_dotenv(ROOT / ".env", override=False)


def _load_yaml() -> dict:
    cfg_path = ROOT / "config.yaml"
    if not cfg_path.exists():
        return {}
    return yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}


CFG = _load_yaml()

# ---- 数据路径 ----
_paths = CFG.get("paths", {})
PARQUET_PATH = ROOT / _paths.get("clean_parquet", "data/clean/dcd_clean.parquet")

# ---- 异常阈值（与清洗阶段口径保持一致）----
_anom = (CFG.get("standardize", {}) or {}).get("anomaly", {}) or {}
TH = {
    "price_min_yuan": _anom.get("price_min_yuan", 0),
    "price_max_yuan": _anom.get("price_max_yuan", 5_000_000),
    "mileage_max_km": _anom.get("mileage_max_km", 600_000),
    "year_min": _anom.get("year_min", 1980),
}
# 年份上界 = current_year + 1
TH["year_max"] = (CFG.get("standardize", {}) or {}).get("current_year", 2026) + 1

# ---- DeepSeek 配置 ----
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash").strip()

# Anonymous browser resource ownership. The development-only fallback signing
# key is generated in backend.auth and is never suitable for production.
OWNER_COOKIE_SECRET = os.getenv("OWNER_COOKIE_SECRET", "").strip()
OWNER_COOKIE_SECURE = os.getenv("OWNER_COOKIE_SECURE", "").strip().lower() in {"1", "true", "yes", "on"}
OWNER_COOKIE_MAX_AGE_S = int(os.getenv("OWNER_COOKIE_MAX_AGE_S", "2592000"))

_agent_context = ((CFG.get("agent_runtime") or {}).get("context") or {})
_model_capabilities = (
    ((CFG.get("agent_runtime") or {}).get("model_capabilities") or {}).get(DEEPSEEK_MODEL) or {}
)
CONTEXT_MAX_CONTEXT_TOKENS = int(
    _model_capabilities.get(
        "max_context_tokens",
        _agent_context.get("model_max_context_tokens", _agent_context.get("max_context_tokens", 64000)),
    )
)
CONTEXT_MAX_OUTPUT_TOKENS = int(
    _agent_context.get("max_output_tokens", _agent_context.get("reserved_output_tokens", 2048))
)
CONTEXT_RESERVED_OUTPUT_TOKENS = CONTEXT_MAX_OUTPUT_TOKENS
CONTEXT_MAX_INPUT_TOKENS = int(
    _agent_context.get(
        "max_input_tokens",
        CONTEXT_MAX_CONTEXT_TOKENS - CONTEXT_MAX_OUTPUT_TOKENS - int(_agent_context.get("safety_margin_tokens", 4096)),
    )
)
CONTEXT_MAX_INPUT_CHARACTERS = int(_agent_context.get("max_input_characters", 1_500_000))
MODEL_MAX_OUTPUT_TOKENS = int(
    _model_capabilities.get(
        "max_output_tokens",
        _agent_context.get("model_max_output_tokens", CONTEXT_MAX_OUTPUT_TOKENS),
    )
)
CONTEXT_SAFETY_MARGIN_TOKENS = int(_agent_context.get("safety_margin_tokens", 4096))
CONTEXT_MIN_RECENT_TURNS = int(_agent_context.get("min_recent_turns", 2))
CONTEXT_MAX_SINGLE_TOOL_RESULT_TOKENS = int(_agent_context.get("max_single_tool_result_tokens", 6000))
if CONTEXT_MAX_CONTEXT_TOKENS <= 0 or MODEL_MAX_OUTPUT_TOKENS <= 0:
    raise ValueError("模型上下文和输出能力必须为正数")
if CONTEXT_MAX_INPUT_TOKENS <= 0 or CONTEXT_MAX_OUTPUT_TOKENS < 0 or CONTEXT_SAFETY_MARGIN_TOKENS < 0:
    raise ValueError("输入/输出预算和安全余量配置无效")
if CONTEXT_MAX_INPUT_CHARACTERS <= 0:
    raise ValueError("max_input_characters 必须为正数")
if CONTEXT_MAX_OUTPUT_TOKENS > MODEL_MAX_OUTPUT_TOKENS:
    raise ValueError(
        f"Agent max_output_tokens={CONTEXT_MAX_OUTPUT_TOKENS} 超过模型 {DEEPSEEK_MODEL} "
        f"能力上限 {MODEL_MAX_OUTPUT_TOKENS}"
    )
if CONTEXT_MAX_INPUT_TOKENS + CONTEXT_MAX_OUTPUT_TOKENS + CONTEXT_SAFETY_MARGIN_TOKENS > CONTEXT_MAX_CONTEXT_TOKENS:
    raise ValueError(
        "Agent 输入、输出与安全余量之和超过模型上下文能力："
        f"{CONTEXT_MAX_INPUT_TOKENS}+{CONTEXT_MAX_OUTPUT_TOKENS}+{CONTEXT_SAFETY_MARGIN_TOKENS}"
        f">{CONTEXT_MAX_CONTEXT_TOKENS}"
    )
if CONTEXT_MIN_RECENT_TURNS < 0:
    raise ValueError("min_recent_turns 不能为负数")
if CONTEXT_MAX_SINGLE_TOOL_RESULT_TOKENS <= 0:
    raise ValueError("max_single_tool_result_tokens 必须为正数")

STORAGE_URL = str((CFG.get("storage") or {}).get("url") or "sqlite:///.cache/runtime.sqlite3").strip()
if not STORAGE_URL.startswith("sqlite:///"):
    raise ValueError("storage.url 当前只支持 sqlite:/// 相对路径")
_database_path = os.getenv("RUNTIME_DATABASE_PATH", "").strip() or STORAGE_URL.removeprefix("sqlite:///")
RUNTIME_DATABASE_PATH = (ROOT / _database_path).resolve()

_agent_scheduler = ((CFG.get("agent_runtime") or {}).get("scheduler") or {})
SCHEDULER_MAX_CONCURRENCY = int(_agent_scheduler.get("max_concurrency", 4))
SCHEDULER_MAX_QUEUE_SIZE = int(_agent_scheduler.get("max_queue_size", 32))
SCHEDULER_RUN_TIMEOUT_S = float(_agent_scheduler.get("run_timeout_seconds", 180))
SCHEDULER_HEARTBEAT_INTERVAL_S = float(_agent_scheduler.get("heartbeat_interval_seconds", 5))
SCHEDULER_LEASE_TTL_S = float(_agent_scheduler.get("lease_ttl_seconds", 30))
SCHEDULER_SHUTDOWN_GRACE_S = float(_agent_scheduler.get("shutdown_grace_seconds", 15))
if SCHEDULER_MAX_CONCURRENCY <= 0 or SCHEDULER_MAX_QUEUE_SIZE < 0:
    raise ValueError("scheduler 并发数必须为正数，队列长度不能为负数")
if min(SCHEDULER_RUN_TIMEOUT_S, SCHEDULER_HEARTBEAT_INTERVAL_S, SCHEDULER_LEASE_TTL_S) <= 0:
    raise ValueError("scheduler timeout 和 lease 配置必须为正数")
if SCHEDULER_SHUTDOWN_GRACE_S < 0:
    raise ValueError("scheduler.shutdown_grace_seconds 不能为负数")

_agent_network = ((CFG.get("agent_runtime") or {}).get("network") or {})
MODEL_CONNECT_TIMEOUT_S = float(_agent_network.get("connect_timeout_seconds", 10))
MODEL_READ_TIMEOUT_S = float(_agent_network.get("read_timeout_seconds", 60))
if min(MODEL_CONNECT_TIMEOUT_S, MODEL_READ_TIMEOUT_S) <= 0:
    raise ValueError("agent_runtime.network 超时必须为正数")

_agent_budget = ((CFG.get("agent_runtime") or {}).get("budget") or {})
RUN_BUDGET_LIMITS = {
    "model_calls": int(_agent_budget.get("model_calls", 12)),
    "tool_executions": int(_agent_budget.get("tool_executions", 24)),
    "answer_repairs": int(_agent_budget.get("answer_repairs", 2)),
    "replans": int(_agent_budget.get("replans", 2)),
    "active_execution_seconds": float(_agent_budget.get("active_execution_seconds", SCHEDULER_RUN_TIMEOUT_S)),
    "truncation_retries": int(_agent_budget.get("truncation_retries", 1)),
}
if min(RUN_BUDGET_LIMITS.values()) <= 0:
    raise ValueError("agent_runtime.budget 各上限必须为正数")

_agent_features = ((CFG.get("agent_runtime") or {}).get("features") or {})
MIDRUN_INPUT_ENABLED = bool(_agent_features.get("midrun_input", False))
RUN_RECOVERY_ENABLED = bool(_agent_features.get("recovery", False))
TOOL_PARALLELISM_ENABLED = bool(_agent_features.get("tool_parallelism", False))

_agent_mcp = ((CFG.get("agent_runtime") or {}).get("mcp") or {})
MCP_TIMEOUT_S = float(_agent_mcp.get("timeout_seconds", 10))
MCP_MAX_WORKERS = int(_agent_mcp.get("max_workers", 8))
MCP_FAILURE_THRESHOLD = int(_agent_mcp.get("failure_threshold", 3))
MCP_COOLDOWN_S = float(_agent_mcp.get("cooldown_seconds", 60))


def is_thinking_model(model: str | None = None) -> bool:
    """判断模型是否使用 DeepSeek 思考模式的工具调用约束。

    允许通过 DEEPSEEK_THINKING_MODE 显式覆盖，未配置时按官方模型命名约定
    识别 reasoner 与 V4/Flash/Pro 模型。思考模式支持 auto 工具选择，但不支持
    required 或具名 tool_choice。
    """
    override = os.getenv("DEEPSEEK_THINKING_MODE", "").strip().lower()
    if override in {"0", "false", "off", "disabled", "disable", "none"}:
        return False
    if override in {"1", "true", "on", "enabled", "enable", "thinking"}:
        return True

    normalized = str(model or DEEPSEEK_MODEL).strip().lower()
    return (
        "reasoner" in normalized
        or normalized.startswith("deepseek-v4-")
        or normalized in {"deepseek-flash", "deepseek-pro"}
    )
