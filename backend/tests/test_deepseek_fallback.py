from fastapi.testclient import TestClient

from app.llm_provider import DeepSeekExtractor
from app.main import create_app


def test_deepseek_failure_falls_back_to_rules(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-never-sent")

    def fail_without_network(self, text):
        raise RuntimeError("simulated outage")

    monkeypatch.setattr(DeepSeekExtractor, "extract", fail_without_network)
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        response = client.post(
            "/api/drafts/recognize",
            json={"customer_id": 1, "site_id": 1, "text": "番茄5斤"},
        )

    assert response.status_code == 200
    assert response.json()["provider"] == "rules(fallback)"
    assert response.json()["items"][0]["product_name"] == "西红柿"
