from __future__ import annotations

import json
import time
from typing import Any

import httpx

from ..settings import Settings
from .errors import PlannerError
from .models import PlannerToolCall, PlannerTurn


class DeepSeekToolPlanner:
    def __init__(self, settings: Settings):
        self._settings = settings

    @property
    def enabled(self) -> bool:
        return (
            self._settings.agent_harness_provider == "deepseek"
            and bool(self._settings.deepseek_api_key)
        )

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> PlannerTurn:
        if not self.enabled:
            raise PlannerError("Harness 模型编排未启用")
        started = time.perf_counter()
        try:
            with httpx.Client(
                timeout=self._settings.deepseek_timeout_seconds
            ) as client:
                response = client.post(
                    f"{self._settings.deepseek_base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._settings.deepseek_api_key}"
                    },
                    json={
                        "model": self._settings.deepseek_model,
                        "messages": messages,
                        "tools": tools,
                        "tool_choice": "auto",
                        "thinking": {"type": "disabled"},
                        "temperature": 0,
                        "max_tokens": 1000,
                    },
                )
                response.raise_for_status()
                payload = response.json()
            message = payload["choices"][0]["message"]
            calls: list[PlannerToolCall] = []
            for raw_call in message.get("tool_calls") or []:
                function = raw_call.get("function") or {}
                arguments = json.loads(function.get("arguments") or "{}")
                if not isinstance(arguments, dict):
                    raise ValueError("工具参数必须是 JSON 对象")
                calls.append(PlannerToolCall(
                    id=str(raw_call.get("id") or ""),
                    name=str(function.get("name") or ""),
                    arguments=arguments,
                ))
            content = message.get("content") or ""
            if not calls and not str(content).strip():
                raise ValueError("模型返回空响应")
            usage = payload.get("usage") or {}
            return PlannerTurn(
                content=str(content).strip(),
                tool_calls=calls,
                prompt_tokens=int(
                    usage.get("prompt_tokens") or usage.get("input_tokens") or 0
                ),
                completion_tokens=int(
                    usage.get("completion_tokens") or usage.get("output_tokens") or 0
                ),
                total_tokens=int(usage.get("total_tokens") or 0),
                duration_ms=round((time.perf_counter() - started) * 1000),
            )
        except PlannerError:
            raise
        except Exception as exc:
            raise PlannerError("Harness 模型规划失败") from exc
