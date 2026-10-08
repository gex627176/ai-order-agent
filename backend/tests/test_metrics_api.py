from fastapi.testclient import TestClient

from app.llm_provider import DeepSeekExtractor
from app.main import create_app
from app.parser import parse_order_text


def test_metrics_are_empty_in_rules_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "rules")
    app = create_app(str(tmp_path / "test.db"))

    with TestClient(app) as client:
        response = client.get("/api/metrics/overview")

    assert response.status_code == 200
    assert response.json() == {
        "total_calls": 0,
        "success_count": 0,
        "fallback_count": 0,
        "failure_count": 0,
        "success_rate": 0.0,
        "fallback_rate": 0.0,
        "average_duration_ms": 0.0,
        "p95_duration_ms": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "estimated_cost": 0.0,
    }


def test_successful_model_call_records_usage_cost_and_model_metrics(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AI_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-never-sent")
    monkeypatch.setenv("DEEPSEEK_MODEL", "test-model")
    monkeypatch.setenv("DEEPSEEK_INPUT_COST_PER_MILLION", "1")
    monkeypatch.setenv("DEEPSEEK_OUTPUT_COST_PER_MILLION", "2")

    def succeed_without_network(self, text):
        self.last_usage = {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        }
        return parse_order_text(text)

    monkeypatch.setattr(DeepSeekExtractor, "extract", succeed_without_network)
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        recognized = client.post(
            "/api/drafts/recognize",
            json={"customer_id": 1, "site_id": 1, "text": "番茄5斤"},
        )
        overview = client.get("/api/metrics/overview")
        models = client.get("/api/metrics/models")
        failures = client.get("/api/metrics/failures")

    assert recognized.status_code == 200
    assert recognized.json()["provider"] == "deepseek"
    assert overview.status_code == 200
    assert overview.json()["total_calls"] == 1
    assert overview.json()["success_rate"] == 100.0
    assert overview.json()["total_tokens"] == 150
    assert overview.json()["estimated_cost"] == 0.0002
    assert models.status_code == 200
    assert models.json()[0]["model"] == "test-model"
    assert models.json()[0]["prompt_version"] == "order-extraction-v1"
    assert failures.json() == []


def test_model_failure_records_fallback_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-never-sent")

    def fail_without_network(self, text):
        raise RuntimeError("simulated outage")

    monkeypatch.setattr(DeepSeekExtractor, "extract", fail_without_network)
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        recognized = client.post(
            "/api/drafts/recognize",
            json={"customer_id": 1, "site_id": 1, "text": "番茄5斤"},
        )
        overview = client.get("/api/metrics/overview")
        failures = client.get("/api/metrics/failures?limit=1")

    assert recognized.status_code == 200
    assert recognized.json()["provider"] == "rules(fallback)"
    assert overview.json()["fallback_count"] == 1
    assert overview.json()["fallback_rate"] == 100.0
    assert failures.status_code == 200
    assert failures.json()[0]["status"] == "fallback"
    assert failures.json()[0]["error_type"] == "RuntimeError"
    assert failures.json()[0]["fallback_reason"] == "外部模型调用失败，已回退本地规则"
    assert failures.json()[0]["draft_id"] == recognized.json()["id"]
