from fastapi.testclient import TestClient

from app.main import create_app
from app.parser import parse_order_text


def test_rule_parser_handles_multiple_chinese_items():
    items = parse_order_text("番茄5斤，胡萝卜3公斤切丁，苹果2箱")
    assert [(item["raw_product_name"], item["quantity"], item["unit"]) for item in items] == [
        ("番茄", 5.0, "斤"),
        ("胡萝卜", 3.0, "公斤"),
        ("苹果", 2.0, "箱"),
    ]
    assert items[1]["note"] == "切丁"


def test_minimal_order_flow_is_persisted_and_idempotent(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        recognized = client.post(
            "/api/drafts/recognize",
            json={"customer_id": 1, "site_id": 1, "text": "番茄5斤，胡萝卜3公斤切丁，苹果2箱"},
        )
        assert recognized.status_code == 200
        draft = recognized.json()
        assert draft["status"] == "needs_review"
        assert draft["warnings"] == []
        assert [item["product_name"] for item in draft["items"]] == [
            "西红柿", "胡萝卜", "苹果"
        ]
        assert len(draft["trace"]) == 3

        first_confirm = client.post(f"/api/drafts/{draft['id']}/confirm")
        second_confirm = client.post(f"/api/drafts/{draft['id']}/confirm")
        history = client.get("/api/orders")

    assert first_confirm.status_code == 200
    assert second_confirm.status_code == 200
    assert first_confirm.json()["id"] == second_confirm.json()["id"]
    assert first_confirm.json()["total_amount"] == 168.8
    assert len(history.json()) == 1


def test_unmatched_product_cannot_be_confirmed(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as client:
        draft = client.post(
            "/api/drafts/recognize",
            json={"customer_id": 1, "site_id": 1, "text": "榴莲1箱"},
        ).json()
        confirmed = client.post(f"/api/drafts/{draft['id']}/confirm")

    assert "未匹配" in draft["warnings"][0]
    assert confirmed.status_code == 422
