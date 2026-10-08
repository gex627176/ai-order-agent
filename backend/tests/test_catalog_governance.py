from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.main import create_app


def test_product_update_publishes_new_immutable_version(tmp_path):
    with TestClient(create_app(str(tmp_path / "catalog-update.db"))) as client:
        before = client.get("/api/catalog/versions/current").json()
        response = client.put(
            "/api/catalog/products/1?site_id=1",
            json={
                "sku": "SKU-TOMATO",
                "name": "西红柿",
                "aliases": ["番茄", "番茄仔"],
                "unit": "斤",
                "unit_price": 3.9,
                "active": True,
            },
        )
        current = client.get("/api/catalog/versions/current").json()
        old_snapshot = client.app.state.database.list_sku_snapshot(1, before["version"])
        new_snapshot = client.app.state.database.list_sku_snapshot(1, current["version"])

    assert response.status_code == 200
    assert current["version"] == before["version"] + 1
    assert old_snapshot[0]["unit_price"] == 3.8
    assert new_snapshot[0]["unit_price"] == 3.9
    assert "番茄仔" in new_snapshot[0]["aliases"]


def test_human_product_correction_creates_reviewed_alias_candidate(tmp_path):
    with TestClient(create_app(str(tmp_path / "alias-review.db"))) as client:
        draft = client.post(
            "/api/drafts/recognize",
            json={"customer_id": 1, "site_id": 1, "text": "番茄仔2斤"},
        ).json()
        item = draft["items"][0]
        item.update({
            "product_id": 1,
            "product_name": "西红柿",
            "unit": "斤",
            "unit_price": 3.8,
            "matched": True,
        })
        update = client.put(
            f"/api/drafts/{draft['id']}",
            json={"customer_id": 1, "items": [item]},
        )
        pending = client.get(
            "/api/catalog/alias-candidates?status=pending"
        ).json()
        review = client.post(
            f"/api/catalog/alias-candidates/{pending[0]['id']}/review",
            json={"decision": "approved"},
        )
        recognized = client.post(
            "/api/drafts/recognize",
            json={"customer_id": 1, "site_id": 1, "text": "番茄仔1斤"},
        ).json()

    assert update.status_code == 200
    assert pending[0]["alias"] == "番茄仔"
    assert pending[0]["product_id"] == 1
    assert review.status_code == 200
    assert review.json()["candidate"]["status"] == "approved"
    assert recognized["items"][0]["matched"] is True
    assert recognized["items"][0]["product_id"] == 1


def test_xlsx_import_and_business_metrics(tmp_path):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["sku", "name", "aliases", "unit", "unit_price", "active"])
    sheet.append(["SKU-CABBAGE", "白菜", "大白菜、包菜", "斤", 1.9, 1])
    content = BytesIO()
    workbook.save(content)

    with TestClient(create_app(str(tmp_path / "catalog-import.db"))) as client:
        response = client.post(
            "/api/catalog/import",
            files={
                "file": (
                    "catalog.xlsx",
                    content.getvalue(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
            data={"site_id": "1"},
        )
        metrics = client.get("/api/metrics/business").json()
        products = client.get("/api/catalog/products").json()

    assert response.status_code == 200
    assert response.json()["imported_count"] == 1
    assert any(product["sku"] == "SKU-CABBAGE" for product in products)
    assert metrics["catalog_products"] == 6
    assert metrics["active_products"] == 6
    assert metrics["current_sku_version"] == 2
