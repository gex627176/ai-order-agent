from fastapi.testclient import TestClient

from app.main import create_app


def test_draft_list_survives_restart_and_filters_status(tmp_path):
    database_path = str(tmp_path / "test.db")
    first_app = create_app(database_path)
    with TestClient(first_app) as client:
        first = client.post(
            "/api/drafts/recognize",
            json={"customer_id": 1, "site_id": 1, "text": "番茄5斤"},
        ).json()
        second = client.post(
            "/api/drafts/recognize",
            json={"customer_id": 2, "site_id": 1, "text": "苹果2箱"},
        ).json()
        confirmed = client.post(f"/api/drafts/{first['id']}/confirm")
        assert confirmed.status_code == 200

    restarted_app = create_app(database_path)
    with TestClient(restarted_app) as client:
        all_drafts = client.get("/api/drafts")
        pending = client.get("/api/drafts?status=needs_review&limit=1")
        confirmed_drafts = client.get("/api/drafts?status=confirmed")
        invalid_status = client.get("/api/drafts?status=deleted")

    assert all_drafts.status_code == 200
    assert {draft["id"] for draft in all_drafts.json()} == {first["id"], second["id"]}
    assert pending.status_code == 200
    assert [draft["id"] for draft in pending.json()] == [second["id"]]
    assert confirmed_drafts.status_code == 200
    assert [draft["id"] for draft in confirmed_drafts.json()] == [first["id"]]
    assert invalid_status.status_code == 422
