from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_path: str
    checkpoint_path: str
    ai_provider: str
    deepseek_api_key: str
    deepseek_base_url: str
    deepseek_model: str
    deepseek_prompt_version: str
    elasticsearch_url: str
    sku_index_name: str
    deepseek_input_cost_per_million: float
    deepseek_output_cost_per_million: float
    recognition_task_max_attempts: int
    recognition_task_poll_interval: float
    agent_harness_provider: str
    agent_harness_max_rounds: int
    agent_harness_max_tool_calls: int

    @classmethod
    def from_env(cls, database_path: str | None = None) -> "Settings":
        resolved_database_path = database_path or os.getenv(
            "DATABASE_PATH", "data/ai_order.db"
        )
        return cls(
            database_path=resolved_database_path,
            checkpoint_path=os.getenv(
                "CHECKPOINT_PATH", f"{resolved_database_path}.checkpoints"
            ),
            ai_provider=os.getenv("AI_PROVIDER", "rules").strip().lower(),
            deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
            deepseek_base_url=os.getenv(
                "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
            ).rstrip("/"),
            deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro").strip(),
            deepseek_prompt_version=os.getenv(
                "DEEPSEEK_PROMPT_VERSION", "order-extraction-v1"
            ).strip(),
            elasticsearch_url=os.getenv("ELASTICSEARCH_URL", "").strip().rstrip("/"),
            sku_index_name=os.getenv("SKU_INDEX_NAME", "ai-order-skus").strip(),
            deepseek_input_cost_per_million=_float_env(
                "DEEPSEEK_INPUT_COST_PER_MILLION", 0.0
            ),
            deepseek_output_cost_per_million=_float_env(
                "DEEPSEEK_OUTPUT_COST_PER_MILLION", 0.0
            ),
            recognition_task_max_attempts=_int_env(
                "RECOGNITION_TASK_MAX_ATTEMPTS", 3, minimum=1
            ),
            recognition_task_poll_interval=_float_env(
                "RECOGNITION_TASK_POLL_INTERVAL", 0.25
            ),
            agent_harness_provider=os.getenv(
                "AGENT_HARNESS_PROVIDER",
                os.getenv("AI_PROVIDER", "rules"),
            ).strip().lower(),
            agent_harness_max_rounds=_int_env(
                "AGENT_HARNESS_MAX_ROUNDS", 6, minimum=1
            ),
            agent_harness_max_tool_calls=_int_env(
                "AGENT_HARNESS_MAX_TOOL_CALLS", 8, minimum=1
            ),
        )


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是数字") from exc
    if value < 0:
        raise ValueError(f"{name} 不能小于 0")
    return value


def _int_env(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if value < minimum:
        raise ValueError(f"{name} 不能小于 {minimum}")
    return value
