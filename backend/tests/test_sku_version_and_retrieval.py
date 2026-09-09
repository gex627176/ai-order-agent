from fastapi.testclient import TestClient

from app.main import create_app
from app.retrieval import local_text_vector


def test_draft_captures_site_sku_version_and_retrieval_evidence(tmp_path):
    with TestClient(create_app(str(tmp_path / "sku-version.db"))) as client:
        draft = client.post(
            "/api/drafts/recognize",
            json={"customer_id": 1, "site_id": 1, "text": "番茄5斤"},
        ).json()

    assert draft["site_id"] == 1
    assert draft["sku_version"] == 1
    assert draft["retrieval"][0]["mode"] == "exact"
    assert draft["retrieval"][0]["selected_product_id"] == 1


def test_new_sku_version_does_not_change_old_draft_snapshot(tmp_path):
    app = create_app(str(tmp_path / "sku-snapshot.db"))
    with TestClient(app) as client:
        old_draft = client.post(
            "/api/drafts/recognize",
            json={"customer_id": 1, "site_id": 1, "text": "苹果2箱"},
        ).json()
        publish_result = client.post("/api/catalog/versions?site_id=1").json()
        new_version = publish_result["version"]
        new_draft = client.post(
            "/api/drafts/recognize",
            json={"customer_id": 1, "site_id": 1, "text": "苹果1箱"},
        ).json()
        old_order = client.post(f"/api/drafts/{old_draft['id']}/confirm").json()

    assert new_version["version"] == 2
    assert publish_result["index_synced"] is True
    assert old_draft["sku_version"] == 1
    assert new_draft["sku_version"] == 2
    assert old_order["sku_version"] == 1
    assert old_order["total_amount"] == 136.0


def test_unknown_text_only_returns_candidates_and_is_not_auto_matched(tmp_path):
    with TestClient(create_app(str(tmp_path / "safe-retrieval.db"))) as client:
        draft = client.post(
            "/api/drafts/recognize",
            json={"customer_id": 1, "site_id": 1, "text": "番茄酱1箱"},
        ).json()

    assert draft["items"][0]["matched"] is False
    assert draft["retrieval"][0]["selected_product_id"] is None
    assert draft["warnings"]


def test_local_vector_is_deterministic_and_has_fixed_dimensions():
    first = local_text_vector("番茄 西红柿")
    second = local_text_vector("番茄 西红柿")
    assert first == second
    assert len(first) == 64
