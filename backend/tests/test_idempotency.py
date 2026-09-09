from fastapi.testclient import TestClient

from app.main import create_app


def recognize(client: TestClient, text: str) -> dict:
    return client.post(
        "/api/drafts/recognize",
        json={"customer_id": 1, "site_id": 1, "text": text},
    ).json()


def test_same_idempotency_key_and_payload_returns_same_order(tmp_path):
    with TestClient(create_app(str(tmp_path / "idempotency.db"))) as client:
        draft = recognize(client, "番茄5斤")
        headers = {"Idempotency-Key": "interview-demo-key-001"}
        first = client.post(f"/api/drafts/{draft['id']}/confirm", headers=headers)
        replay = client.post(f"/api/drafts/{draft['id']}/confirm", headers=headers)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert first.json()["id"] == replay.json()["id"]
    assert first.json()["idempotency_key"] == "interview-demo-key-001"
    assert len(first.json()["payload_md5"]) == 32


def test_same_idempotency_key_with_different_payload_returns_conflict(tmp_path):
    with TestClient(create_app(str(tmp_path / "idempotency-conflict.db"))) as client:
        first_draft = recognize(client, "番茄5斤")
        second_draft = recognize(client, "苹果2箱")
        headers = {"Idempotency-Key": "reused-key"}
        first = client.post(
            f"/api/drafts/{first_draft['id']}/confirm", headers=headers
        )
        conflict = client.post(
            f"/api/drafts/{second_draft['id']}/confirm", headers=headers
        )

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert "不同的订单载荷" in conflict.json()["detail"]
