import pytest
from fastapi.testclient import TestClient

from app.database import Database
from app.main import create_app
from app.parser import parse_order_text
from app.retrieval import SkuRetriever
from app.settings import Settings


class FakeResponse:
    def __init__(self, content: str):
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "choices": [{"message": {"content": self.content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }


class FakeClient:
    def __init__(self, content: str):
        self.response = FakeResponse(content)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def post(self, *args, **kwargs):
        return self.response


@pytest.mark.parametrize(
    ("content", "expected_error"),
    [("", "ValueError"), ("{not valid json", "JSONDecodeError")],
)
def test_empty_or_invalid_model_response_falls_back_and_is_recorded(
    tmp_path, monkeypatch, content, expected_error
):
    monkeypatch.setenv("AI_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-never-sent")
    monkeypatch.setattr(
        "app.llm_provider.httpx.Client", lambda **kwargs: FakeClient(content)
    )
    app = create_app(str(tmp_path / f"{expected_error}.db"))

    with TestClient(app) as client:
        draft = client.post(
            "/api/drafts/recognize",
            json={"customer_id": 1, "site_id": 1, "text": "番茄5斤"},
        )
        failures = client.get("/api/metrics/failures?limit=1")

    assert draft.status_code == 200
    assert draft.json()["provider"] == "rules(fallback)"
    assert draft.json()["items"][0]["product_id"] == 1
    assert failures.json()[0]["error_type"] == expected_error


def test_elasticsearch_failure_returns_no_candidates_and_never_auto_matches(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ELASTICSEARCH_URL", "http://example.invalid:9200")
    settings = Settings.from_env(str(tmp_path / "es-failure.db"))
    database = Database(settings.database_path)
    database.initialize()
    retriever = SkuRetriever(database, settings)

    class FailingSearchClient:
        def search(self, **kwargs):
            raise ConnectionError("simulated Elasticsearch outage")

    retriever.client = FailingSearchClient()
    items, evidence, _ = retriever.enrich(
        parse_order_text("番茄酱1箱"), site_id=1, sku_version=1
    )

    assert items[0]["matched"] is False
    assert items[0]["product_id"] is None
    assert evidence[0]["candidates"] == []
