from unittest.mock import create_autospec
from typing import get_type_hints

from fastapi.testclient import TestClient

from app.database import Database
from app.main import create_app, get_database


def test_health_and_seed_catalog(tmp_path):
    app = create_app(str(tmp_path / "test.db"))

    with TestClient(app) as client:
        health = client.get("/api/health")
        customers = client.get("/api/catalog/customers")
        products = client.get("/api/catalog/products")

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["database"] == "sqlite"
    assert customers.status_code == 200
    assert [customer["name"] for customer in customers.json()] == [
        "惠民餐厅",
        "阳光幼儿园",
        "青禾生鲜店",
    ]
    assert products.status_code == 200
    assert products.json()[0]["name"] == "西红柿"
    assert products.json()[0]["aliases"] == ["番茄"]


def test_database_dependency_can_be_overridden(tmp_path):
    assert get_type_hints(get_database)["return"] is Database

    app = create_app(str(tmp_path / "dependency-override.db"))
    database = create_autospec(Database, instance=True)
    database.list_customers.return_value = [
        {"id": 99, "name": "依赖替换客户", "contact": ""}
    ]
    app.dependency_overrides[get_database] = lambda: database

    with TestClient(app) as client:
        response = client.get("/api/catalog/customers")

    assert response.status_code == 200
    assert response.json() == [
        {"id": 99, "name": "依赖替换客户", "contact": ""}
    ]
    database.list_customers.assert_called_once_with()
