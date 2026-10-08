from __future__ import annotations

import io
import time

import pymupdf as fitz
from fastapi.testclient import TestClient
from PIL import Image

from app import input_normalizer
from app.input_normalizer import normalize_uploaded_order_details
from app.main import create_app


def _text_pdf_bytes(text: str) -> bytes:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), text, fontsize=14)
    content = document.tobytes()
    document.close()
    return content


def _blank_pdf_bytes() -> bytes:
    document = fitz.open()
    document.new_page(width=400, height=250)
    content = document.tobytes()
    document.close()
    return content


def _wait_for_task(client: TestClient, task_id: str) -> dict:
    for _ in range(100):
        task = client.get(f"/api/recognition-tasks/{task_id}").json()
        if task["status"] in {"succeeded", "failed", "cancelled"}:
            return task
        time.sleep(0.03)
    raise AssertionError("识别任务未在预期时间内结束")


def test_image_ocr_records_low_confidence_warning(monkeypatch):
    image = Image.new("RGB", (320, 120), "white")
    content = io.BytesIO()
    image.save(content, format="PNG")
    monkeypatch.setattr(
        input_normalizer, "_ocr_image", lambda _: ("番茄5斤", 61.25)
    )

    normalized = normalize_uploaded_order_details("截图.png", content.getvalue())

    assert normalized.text == "番茄5斤"
    assert normalized.input_type == "image_ocr"
    assert normalized.confidence == 61.25
    assert normalized.warnings == ["OCR 平均置信度 61.2%，请逐项核对原图与识别结果"]


def test_tesseract_command_can_be_resolved_from_project_runtime(tmp_path, monkeypatch):
    runtime = tmp_path / "tesseract"
    runtime.mkdir()
    command = runtime / "tesseract.exe"
    command.write_bytes(b"test executable placeholder")
    (runtime / "tessdata").mkdir()
    monkeypatch.setenv("TESSERACT_CMD", str(command))

    config = input_normalizer._configure_tesseract()

    assert input_normalizer.pytesseract.pytesseract.tesseract_cmd == str(command)
    assert config == ""
    assert input_normalizer.os.environ["TESSDATA_PREFIX"] == str(runtime / "tessdata")


def test_ocr_prefers_meaningful_chinese_candidate_over_english_gibberish(monkeypatch):
    calls: list[str] = []

    def fake_image_to_data(_image, *, lang, config, output_type):
        calls.append(lang)
        text = "番茄5斤 胡萝卜3公斤" if lang == "chi_sim" else "mms HB KAA"
        confidence = "54.88" if lang == "chi_sim" else "92.00"
        words = text.split()
        return {
            "text": words,
            "conf": [confidence] * len(words),
            "page_num": [1] * len(words),
            "block_num": [1] * len(words),
            "par_num": [1] * len(words),
            "line_num": [1] * len(words),
        }

    monkeypatch.setattr(input_normalizer, "_configure_tesseract", lambda: "")
    monkeypatch.setattr(input_normalizer.pytesseract, "image_to_data", fake_image_to_data)

    text, confidence = input_normalizer._ocr_image(Image.new("RGB", (10, 10)))

    assert calls == ["chi_sim", "eng"]
    assert text == "番茄5斤胡萝卜3公斤"
    assert confidence == 54.88


def test_ocr_prefers_english_candidate_when_no_chinese_text_exists(monkeypatch):
    def fake_image_to_data(_image, *, lang, config, output_type):
        text = "tomato 5 jin" if lang == "eng" else "tomato S jin"
        words = text.split()
        confidence = "88.00" if lang == "eng" else "93.00"
        return {
            "text": words,
            "conf": [confidence] * len(words),
            "page_num": [1] * len(words),
            "block_num": [1] * len(words),
            "par_num": [1] * len(words),
            "line_num": [1] * len(words),
        }

    monkeypatch.setattr(input_normalizer, "_configure_tesseract", lambda: "")
    monkeypatch.setattr(input_normalizer.pytesseract, "image_to_data", fake_image_to_data)

    text, confidence = input_normalizer._ocr_image(Image.new("RGB", (10, 10)))

    assert text == "tomato 5 jin"
    assert confidence == 88.0


def test_text_pdf_skips_ocr_and_enters_async_review_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "rules")
    monkeypatch.setattr(
        input_normalizer,
        "_ocr_image",
        lambda _: (_ for _ in ()).throw(AssertionError("文本 PDF 不应调用 OCR")),
    )
    content = _text_pdf_bytes("tomato 5 jin")

    with TestClient(create_app(str(tmp_path / "text-pdf.db"))) as client:
        response = client.post(
            "/api/recognition-tasks/file",
            data={"customer_id": "1", "site_id": "1"},
            files={"file": ("order.pdf", content, "application/pdf")},
            headers={"Idempotency-Key": "pdf-text-task"},
        )
        task = _wait_for_task(client, response.json()["id"])
        draft = client.get(f"/api/drafts/{task['draft_id']}").json()

    assert response.status_code == 202
    assert task["status"] == "succeeded"
    assert task["input_type"] == "pdf_text"
    assert task["input_confidence"] is None
    assert draft["input_type"] == "pdf_text"
    assert draft["source_filename"] == "order.pdf"
    assert draft["input_warnings"] == []


def test_scanned_pdf_uses_ocr_and_persists_review_warning(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "rules")
    monkeypatch.setattr(
        input_normalizer, "_ocr_image", lambda _: ("番茄5斤", 55.0)
    )

    with TestClient(create_app(str(tmp_path / "scan-pdf.db"))) as client:
        response = client.post(
            "/api/drafts/recognize-file",
            data={"customer_id": "1", "site_id": "1"},
            files={"file": ("扫描订单.pdf", _blank_pdf_bytes(), "application/pdf")},
        )
        draft = response.json()

    assert response.status_code == 200
    assert draft["input_type"] == "pdf_ocr"
    assert draft["input_confidence"] == 55.0
    assert draft["input_warnings"] == ["OCR 平均置信度 55.0%，请逐项核对原图与识别结果"]
    assert draft["input_warnings"][0] in draft["warnings"]


def test_pdf_page_limit_is_enforced():
    document = fitz.open()
    for _ in range(6):
        document.new_page()
    content = document.tobytes()
    document.close()

    try:
        normalize_uploaded_order_details("too-many-pages.pdf", content)
    except ValueError as exc:
        assert str(exc) == "PDF 最多支持 5 页"
    else:
        raise AssertionError("超过页数限制的 PDF 应被拒绝")
