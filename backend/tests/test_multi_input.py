from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.main import create_app


def test_csv_input_enters_the_same_langgraph_review_flow(tmp_path):
    content = "商品,数量,单位,备注\n番茄,5,斤,\n胡萝卜,3,公斤,切丁\n".encode("utf-8")
    with TestClient(create_app(str(tmp_path / "csv-input.db"))) as client:
        response = client.post(
            "/api/drafts/recognize-file",
            data={"customer_id": "1", "site_id": "1"},
            files={"file": ("订单.csv", content, "text/csv")},
        )
        draft = response.json()
        checkpoint = client.get(f"/api/drafts/{draft['id']}/workflow").json()

    assert response.status_code == 200
    assert draft["input_type"] == "csv"
    assert draft["source_filename"] == "订单.csv"
    assert [item["product_name"] for item in draft["items"]] == ["西红柿", "胡萝卜"]
    assert draft["items"][1]["note"] == "切丁"
    assert checkpoint["waiting_for_review"] is True


def test_xlsx_input_is_normalized_without_external_services(tmp_path):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["商品", "数量", "单位", "备注"])
    sheet.append(["苹果", 2, "箱", ""])
    sheet.append(["土豆", 4.5, "斤", "切块"])
    buffer = BytesIO()
    workbook.save(buffer)

    with TestClient(create_app(str(tmp_path / "xlsx-input.db"))) as client:
        response = client.post(
            "/api/drafts/recognize-file",
            data={"customer_id": "2", "site_id": "1"},
            files={
                "file": (
                    "订单.xlsx",
                    buffer.getvalue(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )

    assert response.status_code == 200
    draft = response.json()
    assert draft["input_type"] == "xlsx"
    assert draft["source_text"] == "苹果2箱\n土豆4.5斤切块"
    assert draft["warnings"] == []


def test_invalid_pdf_is_rejected(tmp_path):
    with TestClient(create_app(str(tmp_path / "bad-input.db"))) as client:
        response = client.post(
            "/api/drafts/recognize-file",
            data={"customer_id": "1", "site_id": "1"},
            files={"file": ("订单.pdf", b"not-a-pdf", "application/pdf")},
        )

    assert response.status_code == 422
    assert "PDF 文件无效" in response.json()["detail"]


def test_unsupported_file_type_is_rejected(tmp_path):
    with TestClient(create_app(str(tmp_path / "unsupported-input.db"))) as client:
        response = client.post(
            "/api/drafts/recognize-file",
            data={"customer_id": "1", "site_id": "1"},
            files={"file": ("订单.doc", b"legacy", "application/msword")},
        )

    assert response.status_code == 422
    assert "仅支持" in response.json()["detail"]
