from fastapi.testclient import TestClient

from app.main import create_app


def test_empty_recognition_can_be_completed_with_manual_price(tmp_path):
    with TestClient(create_app(str(tmp_path / "manual-entry.db"))) as client:
        draft_response = client.post(
            "/api/drafts/recognize",
            json={"customer_id": 1, "site_id": 1, "text": "张三一百斤"},
        )
        assert draft_response.status_code == 200
        draft = draft_response.json()
        assert draft["items"] == []

        manual_item = {
            "line_no": 1,
            "raw_product_name": "西红柿",
            "product_id": 1,
            "product_name": "西红柿",
            "quantity": 100,
            "unit": "斤",
            "unit_price": 5.25,
            "note": "人工补录",
            "matched": True,
        }
        updated_response = client.put(
            f"/api/drafts/{draft['id']}",
            json={"customer_id": 1, "items": [manual_item]},
        )
        assert updated_response.status_code == 200
        assert updated_response.json()["items"][0]["unit_price"] == 5.25

        order_response = client.post(
            f"/api/drafts/{draft['id']}/confirm",
            headers={"Idempotency-Key": "manual-entry-price"},
        )
        assert order_response.status_code == 200
        assert order_response.json()["items"][0]["unit_price"] == 5.25
        assert order_response.json()["items"][0]["subtotal"] == 525


def test_draft_update_rejects_empty_items_and_invalid_price(tmp_path):
    with TestClient(create_app(str(tmp_path / "manual-entry-validation.db"))) as client:
        draft = client.post(
            "/api/drafts/recognize",
            json={"customer_id": 1, "site_id": 1, "text": "张三一百斤"},
        ).json()

        empty_response = client.put(
            f"/api/drafts/{draft['id']}",
            json={"customer_id": 1, "items": []},
        )
        invalid_price_response = client.put(
            f"/api/drafts/{draft['id']}",
            json={
                "customer_id": 1,
                "items": [{
                    "line_no": 1,
                    "raw_product_name": "西红柿",
                    "product_id": 1,
                    "product_name": "西红柿",
                    "quantity": 1,
                    "unit": "斤",
                    "unit_price": -0.01,
                    "note": "",
                    "matched": True,
                }],
            },
        )

    assert empty_response.status_code == 422
    assert invalid_price_response.status_code == 422
