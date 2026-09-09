import asyncio
from io import BytesIO, StringIO
import json
import logging
from unittest.mock import create_autospec
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from app.database import Database
from app.main import create_app, get_retriever, read_upload_limited
from app.retrieval import SkuRetriever


class ChunkedUpload:
    def __init__(self, chunks: list[bytes]):
        self.chunks = iter(chunks)
        self.read_count = 0
        self.closed = False

    async def read(self, size: int = -1) -> bytes:
        self.read_count += 1
        return next(self.chunks, b"")

    async def close(self) -> None:
        self.closed = True


def test_success_access_logs_are_suppressed_but_agent_logs_remain(tmp_path):
    logger = logging.getLogger("agent.workflow")
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    logger.addHandler(handler)

    try:
        with TestClient(create_app(str(tmp_path / "quiet-access.db"))) as client:
            assert client.get("/api/health").status_code == 200
            assert client.post("/api/drafts/recognize", json={}).status_code == 422
            logger.info("AGENT name=模型抽取 status=completed summary=保留解析日志")
    finally:
        logger.removeHandler(handler)

    output = stream.getvalue()
    assert "HTTP method=GET path=/api/health status=200" not in output
    assert "HTTP method=POST path=/api/drafts/recognize status=422" in output
    assert "AGENT name=模型抽取 status=completed summary=保留解析日志" in output
    assert logging.getLogger("uvicorn.access").disabled is True


def test_chunked_upload_stops_reading_and_closes_after_limit():
    upload = ChunkedUpload([b"a" * 4, b"b" * 4, b"c", b"not-read"])

    async def read() -> None:
        with pytest.raises(ValueError, match="不能超过"):
            await read_upload_limited(upload, 8, "文件不能超过限制")

    asyncio.run(read())

    assert upload.read_count == 3
    assert upload.closed is True


def test_chunked_upload_closes_after_successful_read():
    upload = ChunkedUpload([b"first", b"second"])

    content = asyncio.run(
        read_upload_limited(upload, 32, "文件不能超过限制")
    )

    assert content == b"firstsecond"
    assert upload.closed is True


def test_content_length_preflight_rejects_obviously_oversized_upload(tmp_path):
    with TestClient(create_app(str(tmp_path / "content-length.db"))) as client:
        response = client.post(
            "/api/catalog/import",
            headers={"Content-Length": str(3 * 1024 * 1024)},
        )

    assert response.status_code == 413
    assert response.json()["detail"] == "上传文件过大"


def test_catalog_write_survives_index_sync_failure(tmp_path):
    app = create_app(str(tmp_path / "index-failure.db"))
    retriever = create_autospec(SkuRetriever, instance=True)
    retriever.backend = "elasticsearch_hybrid"
    retriever.sync_catalog.side_effect = RuntimeError("private ES failure")
    app.dependency_overrides[get_retriever] = lambda: retriever
    content = (
        "sku,name,aliases,unit,unit_price,active\n"
        "SKU-CABBAGE,白菜,大白菜,斤,1.9,1\n"
    ).encode("utf-8")

    with TestClient(app) as client:
        response = client.post(
            "/api/catalog/import",
            data={"site_id": "1"},
            files={"file": ("catalog.csv", content, "text/csv")},
        )
        products = client.get("/api/catalog/products").json()

    assert response.status_code == 200
    assert response.json()["index_synced"] is False
    assert "已保存" in response.json()["index_error"]
    assert any(product["sku"] == "SKU-CABBAGE" for product in products)


def test_bulk_partial_failure_is_not_reported_as_success():
    database = create_autospec(Database, instance=True)
    database.list_sku_snapshot.return_value = [
        {
            "id": 1,
            "sku": "SKU-TOMATO",
            "name": "西红柿",
            "aliases": ["番茄"],
            "unit": "斤",
            "unit_price": 3.8,
            "active": True,
        }
    ]

    class FakeIndices:
        @staticmethod
        def exists(index: str) -> bool:
            return True

    class FakeClient:
        indices = FakeIndices()

        @staticmethod
        def bulk(**kwargs):
            return {
                "errors": True,
                "items": [{"index": {"status": 400, "error": {"type": "bad"}}}],
            }

    retriever = SkuRetriever.__new__(SkuRetriever)
    retriever.database = database
    retriever.index_name = "test-index"
    retriever.client = FakeClient()

    with pytest.raises(RuntimeError, match="bulk 同步失败"):
        retriever.sync_catalog(1, 1)


def test_task_draft_id_is_reused_after_interruption(tmp_path):
    app = create_app(str(tmp_path / "task-reentry.db"))
    with TestClient(app) as client:
        workflow = client.app.state.workflow
        database = client.app.state.database
        task_id = "task-fixed-draft-id"
        first = workflow.recognize(
            "番茄5斤", 1, 1, task_id=task_id, draft_id=task_id
        )
        second = workflow.recognize(
            "番茄5斤", 1, 1, task_id=task_id, draft_id=task_id
        )
        drafts = database.list_drafts()

    assert first["id"] == task_id
    assert second["id"] == task_id
    assert [draft["id"] for draft in drafts].count(task_id) == 1


def test_orphan_checkpoint_is_deleted_before_task_rerun(tmp_path, monkeypatch):
    app = create_app(str(tmp_path / "orphan-checkpoint.db"))
    with TestClient(app) as client:
        workflow = client.app.state.workflow
        database = client.app.state.database
        task_id = "task-orphan-checkpoint"
        original_create_draft = database.create_draft

        def fail_before_draft(*args, **kwargs):
            raise RuntimeError("simulated interruption before draft persistence")

        monkeypatch.setattr(database, "create_draft", fail_before_draft)
        with pytest.raises(RuntimeError, match="simulated interruption"):
            workflow.recognize(
                "番茄5斤", 1, 1, task_id=task_id, draft_id=task_id
            )
        assert database.get_draft(task_id) is None
        assert workflow.checkpoint_status(task_id)["checkpoint_exists"] is True

        deleted_threads: list[str] = []
        original_delete_thread = workflow._checkpointer.delete_thread

        def record_delete(thread_id: str) -> None:
            deleted_threads.append(thread_id)
            original_delete_thread(thread_id)

        monkeypatch.setattr(database, "create_draft", original_create_draft)
        monkeypatch.setattr(
            workflow._checkpointer, "delete_thread", record_delete
        )
        draft = workflow.recognize(
            "番茄5斤", 1, 1, task_id=task_id, draft_id=task_id
        )

        assert draft["id"] == task_id
        assert deleted_threads == [task_id]
        assert [
            item["id"] for item in database.list_drafts()
        ].count(task_id) == 1
        assert database.list_orders() == []


def test_catalog_writes_require_known_explicit_site(tmp_path):
    payload = {
        "sku": "SKU-CABBAGE",
        "name": "白菜",
        "aliases": [],
        "unit": "斤",
        "unit_price": 1.9,
        "active": True,
    }
    with TestClient(create_app(str(tmp_path / "site-required.db"))) as client:
        missing = client.post("/api/catalog/products", json=payload)
        unknown = client.post("/api/catalog/products?site_id=2", json=payload)

    assert missing.status_code == 422
    assert "站点" in missing.json()["detail"]
    assert unknown.status_code == 404


def test_json_recognition_requires_explicit_site_and_safe_field_errors(tmp_path):
    with TestClient(create_app(str(tmp_path / "recognition-site.db"))) as client:
        sync_missing = client.post(
            "/api/drafts/recognize",
            json={"customer_id": 1, "text": "番茄5斤"},
        )
        async_missing = client.post(
            "/api/recognition-tasks",
            json={"customer_id": 1, "text": "番茄5斤"},
        )
        negative_price = client.post(
            "/api/catalog/products?site_id=1",
            json={
                "sku": "SKU-BAD-PRICE",
                "name": "异常价格商品",
                "aliases": [],
                "unit": "斤",
                "unit_price": -1,
                "active": True,
            },
        )

    assert sync_missing.status_code == 422
    assert async_missing.status_code == 422
    assert "站点" in sync_missing.json()["detail"]
    assert "站点" in async_missing.json()["detail"]
    assert negative_price.status_code == 422
    assert "单价" in negative_price.json()["detail"]
    serialized = json.dumps(
        [sync_missing.json(), async_missing.json(), negative_price.json()],
        ensure_ascii=False,
    ).lower()
    assert all(fragment not in serialized for fragment in (
        '"input"', '"ctx"', "pydantic", "sqlite", "httpx", "traceback"
    ))


def test_corrupt_xlsx_files_return_422(tmp_path):
    malformed_xml = BytesIO()
    with ZipFile(malformed_xml, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types><broken>")

    with TestClient(create_app(str(tmp_path / "corrupt-xlsx.db"))) as client:
        broken_zip = client.post(
            "/api/catalog/import",
            data={"site_id": "1"},
            files={"file": ("broken.xlsx", b"not-a-zip", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        broken_xml = client.post(
            "/api/catalog/import",
            data={"site_id": "1"},
            files={"file": ("broken-xml.xlsx", malformed_xml.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )

    assert broken_zip.status_code == 422
    assert broken_xml.status_code == 422
    assert broken_zip.json()["detail"] == "XLSX 商品目录无法解析"
    assert broken_xml.json()["detail"] == "XLSX 商品目录无法解析"


def test_non_finite_model_quantity_falls_back_to_rules(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-never-sent")

    class FakeResponse:
        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict:
            return {
                "choices": [{
                    "message": {
                        "content": json.dumps({
                            "items": [{
                                "product_name": "番茄",
                                "quantity": "nan",
                                "unit": "斤",
                                "note": "",
                            }]
                        })
                    }
                }]
            }

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        @staticmethod
        def post(*args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr("app.llm_provider.httpx.Client", FakeClient)

    with TestClient(create_app(str(tmp_path / "nan-fallback.db"))) as client:
        response = client.post(
            "/api/drafts/recognize",
            json={"customer_id": 1, "site_id": 1, "text": "番茄5斤"},
        )

    assert response.status_code == 200
    assert response.json()["provider"] == "rules(fallback)"
    assert response.json()["items"][0]["quantity"] == 5


def test_confirmed_draft_with_new_key_returns_existing_order(tmp_path):
    with TestClient(create_app(str(tmp_path / "confirm-repeat.db"))) as client:
        draft = client.post(
            "/api/drafts/recognize",
            json={"customer_id": 1, "site_id": 1, "text": "番茄1斤"},
        ).json()
        first = client.post(
            f"/api/drafts/{draft['id']}/confirm",
            headers={"Idempotency-Key": "first-confirm-key"},
        )
        second = client.post(
            f"/api/drafts/{draft['id']}/confirm",
            headers={"Idempotency-Key": "different-replay-key"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("quantity", float("nan")),
        ("quantity", float("inf")),
        ("unit_price", float("nan")),
        ("unit_price", float("inf")),
    ],
)
def test_confirm_rejects_historical_non_finite_numbers(
    tmp_path, field, value
):
    with TestClient(create_app(str(tmp_path / f"confirm-{field}.db"))) as client:
        draft = client.post(
            "/api/drafts/recognize",
            json={"customer_id": 1, "site_id": 1, "text": "番茄1斤"},
        ).json()
        items = draft["items"]
        items[0][field] = value
        database = client.app.state.database
        with database.connect() as connection:
            connection.execute(
                "UPDATE drafts SET items_json = ? WHERE id = ?",
                (json.dumps(items), draft["id"]),
            )

        response = client.post(f"/api/drafts/{draft['id']}/confirm")
        orders = client.get("/api/orders").json()

    assert response.status_code == 422
    assert field.replace("quantity", "数量").replace("unit_price", "单价") in response.json()["detail"]
    assert orders == []


def test_historical_external_errors_are_sanitized_on_read(tmp_path):
    app = create_app(str(tmp_path / "historical-errors.db"))
    with TestClient(app) as client:
        database = client.app.state.database
        now = "2026-08-31T00:00:00+00:00"
        database.record_llm_call({
            "task_id": "legacy-task",
            "provider": "deepseek",
            "model": "legacy-model",
            "prompt_version": "order-extraction-v1",
            "duration_ms": 1,
            "status": "fallback",
            "error_type": "HTTPStatusError",
            "fallback_reason": "https://private.example response with secret",
            "created_at": now,
        })
        failure = client.get("/api/metrics/failures?limit=1").json()[0]

    assert failure["fallback_reason"] == "外部模型调用失败，已回退本地规则"
    assert "http" not in failure["fallback_reason"].lower()
