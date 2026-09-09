from fastapi.testclient import TestClient

from app.main import create_app


def test_manual_correction_becomes_auditable_preference_evidence(tmp_path):
    app = create_app(str(tmp_path / "corrections.db"))
    with TestClient(app) as client:
        draft = client.post(
            "/api/drafts/recognize",
            json={"customer_id": 1, "site_id": 1, "text": "胡萝卜3公斤"},
        ).json()
        edited_item = {**draft["items"][0], "quantity": 4, "note": "切丁"}
        updated = client.put(
            f"/api/drafts/{draft['id']}",
            json={"customer_id": 1, "items": [edited_item]},
        )
        corrections = client.get(
            f"/api/drafts/{draft['id']}/corrections"
        ).json()
        order = client.post(
            f"/api/drafts/{draft['id']}/confirm",
            headers={"Idempotency-Key": "correction-demo-001"},
        )
        preferences = client.get("/api/customers/1/preferences").json()
        next_draft = client.post(
            "/api/drafts/recognize",
            json={"customer_id": 1, "site_id": 1, "text": "胡萝卜2公斤"},
        ).json()

    assert updated.status_code == 200
    assert order.status_code == 200
    assert {event["field_path"] for event in corrections} == {
        "items[1].quantity",
        "items[1].note",
    }
    assert preferences[0]["preference_key"] == "product:2:note"
    assert preferences[0]["preference_value"] == "切丁"
    assert preferences[0]["evidence_count"] == 1
    assert next_draft["preferences"][0]["preference_value"] == "切丁"
    assert next_draft["items"][0]["note"] == ""


def test_unchanged_draft_does_not_create_false_correction(tmp_path):
    with TestClient(create_app(str(tmp_path / "no-false-correction.db"))) as client:
        draft = client.post(
            "/api/drafts/recognize",
            json={"customer_id": 2, "site_id": 1, "text": "苹果1箱"},
        ).json()
        client.put(
            f"/api/drafts/{draft['id']}",
            json={"customer_id": 2, "items": draft["items"]},
        )
        corrections = client.get(
            f"/api/drafts/{draft['id']}/corrections"
        ).json()

    assert corrections == []
