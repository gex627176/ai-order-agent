from fastapi.testclient import TestClient

from app.main import create_app


def test_recognition_pauses_at_human_review_and_resumes(tmp_path):
    database_path = str(tmp_path / "checkpoint-flow.db")
    app = create_app(database_path)

    with TestClient(app) as client:
        draft = client.post(
            "/api/drafts/recognize",
            json={"customer_id": 1, "site_id": 1, "text": "番茄5斤"},
        ).json()
        paused = client.get(f"/api/drafts/{draft['id']}/workflow")

        assert paused.status_code == 200
        assert paused.json()["thread_id"] == draft["id"]
        assert paused.json()["checkpoint_exists"] is True
        assert paused.json()["waiting_for_review"] is True
        assert paused.json()["next_nodes"] == ["human_review"]

        order = client.post(f"/api/drafts/{draft['id']}/confirm")
        completed = client.get(f"/api/drafts/{draft['id']}/workflow")

    assert order.status_code == 200
    assert completed.json()["waiting_for_review"] is False
    assert completed.json()["next_nodes"] == []


def test_sqlite_checkpoint_survives_application_restart(tmp_path):
    database_path = str(tmp_path / "restart-flow.db")

    with TestClient(create_app(database_path)) as client:
        draft = client.post(
            "/api/drafts/recognize",
            json={"customer_id": 2, "site_id": 1, "text": "苹果2箱"},
        ).json()

    # 新建应用实例模拟 Python 进程重启；恢复依赖磁盘 checkpoint，而不是内存对象。
    with TestClient(create_app(database_path)) as client:
        checkpoint = client.get(f"/api/drafts/{draft['id']}/workflow")
        order = client.post(f"/api/drafts/{draft['id']}/confirm")

    assert checkpoint.json()["waiting_for_review"] is True
    assert order.status_code == 200
    assert order.json()["customer_id"] == 2
    assert order.json()["total_amount"] == 136.0
