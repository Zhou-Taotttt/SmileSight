from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app
import app.main as main_module
from app.interactive_service import _redact_body, _safe_url


def _functional_project(client: TestClient) -> tuple[str, int, int]:
    project_id = f"interactive-{uuid4().hex[:8]}"
    assert client.post(
        "/api/v1/projects",
        json={"project_id": project_id, "name": "Interactive Functional"},
    ).status_code == 200
    environment = client.post(
        f"/api/v1/projects/{project_id}/environments",
        json={"name": "Docker", "base_url": "http://frontend", "variables": {}},
    ).json()["data"]
    case = client.post(
        f"/api/v1/projects/{project_id}/test-cases",
        json={
            "title": "首页接口失败归因",
            "test_type": "functional",
            "steps": [{"step_no": 1, "action": "打开首页", "expected": "页面可见"}],
            "assertions": [
                {"type": "status_code", "expected": 200, "url_contains": "/api/v1/projects", "step_no": 1}
            ],
        },
    ).json()["data"]
    return project_id, environment["id"], case["id"]


def test_browser_checkpoint_correlates_page_api_assertion_and_result(monkeypatch):
    closed: list[int] = []
    monkeypatch.setattr(
        main_module.interactive_gateway,
        "start_browser",
        lambda run_id, base_url, settings: {"connected": True, "channel": "browser", "session_id": "fake"},
    )
    monkeypatch.setattr(main_module.interactive_gateway, "is_connected", lambda run_id: True)
    monkeypatch.setattr(main_module.interactive_gateway, "close", closed.append)
    monkeypatch.setattr(
        main_module.interactive_gateway,
        "capture",
        lambda run_id, case, step_no, evidence_dir: {
            "channel": "browser",
            "page": {"url": "http://frontend/", "title": "SmileSight"},
            "network": [
                {
                    "method": "GET",
                    "url": "http://frontend/api/v1/projects",
                    "status_code": 500,
                    "response_body": '{"detail":"database unavailable"}',
                },
                {
                    "method": "GET",
                    "url": "http://frontend/favicon.ico",
                    "status_code": 404,
                    "response_body": '{"detail":"not found"}',
                }
            ],
            "console_logs": [{"level": "SEVERE", "message": "request failed"}],
            "device_logs": [],
            "assertions": [
                {
                    "type": "status_code",
                    "expected": 200,
                    "actual": 500,
                    "passed": False,
                    "source": "network",
                    "url_contains": "/api/v1/projects",
                }
            ],
            "screenshot": {"relative_path": "screenshots/fake.png", "filename": "fake.png"},
        },
    )

    with TestClient(app) as client:
        project_id, environment_id, case_id = _functional_project(client)
        started = client.post(
            f"/api/v1/projects/{project_id}/functional-runs",
            json={"environment_id": environment_id, "case_ids": [case_id], "channel": "browser", "mode": "strict"},
        )
        assert started.status_code == 200
        session = started.json()["data"]
        assert session["status"] == "interactive_running"
        assert session["live_view_url"].startswith("http://localhost:7900")

        checkpoint = client.post(
            f"/api/v1/projects/{project_id}/functional-runs/{session['run_id']}/checkpoint",
            json={"case_id": case_id, "step_no": 1, "note": "检查首页"},
        )
        assert checkpoint.status_code == 200
        assert checkpoint.json()["data"]["status"] == "failed"
        assert checkpoint.json()["data"]["payload"]["network"][0]["status_code"] == 500

        recorded = client.post(
            f"/api/v1/projects/{project_id}/functional-runs/{session['run_id']}/results",
            json={"case_id": case_id, "step_no": 1, "status": "failed", "summary": "项目列表加载失败"},
        )
        assert recorded.status_code == 200
        assert recorded.json()["data"]["run_status"] == "completed"
        assert closed == [session["run_id"]]

        detail = client.get(f"/api/v1/projects/{project_id}/runs/{session['run_id']}").json()["data"]
        result = detail["results"][0]
        assert result["case"]["title"] == "首页接口失败归因"
        assert result["failure"]["step_no"] == 1
        assert result["failure"]["interface"]["status_code"] == 500
        assert result["failure"]["interface"]["response"] == '{"detail":"database unavailable"}'
        assert result["assertions"][0]["passed"] is False
        assert [event["event_type"] for event in detail["events"]] == [
            "session_started", "checkpoint", "case_result", "session_finished"
        ]

        state = client.get(
            f"/api/v1/projects/{project_id}/functional-runs/{session['run_id']}"
        ).json()["data"]
        assert [event["event_type"] for event in state["events"]] == [
            "session_started", "checkpoint", "case_result", "session_finished"
        ]


def test_manual_session_uploads_evidence_and_finishes_incomplete():
    with TestClient(app) as client:
        project_id, environment_id, case_id = _functional_project(client)
        session = client.post(
            f"/api/v1/projects/{project_id}/functional-runs",
            json={"environment_id": environment_id, "case_ids": [case_id], "channel": "manual", "mode": "exploratory"},
        ).json()["data"]
        assert session["connected"] is True

        uploaded = client.post(
            f"/api/v1/projects/{project_id}/functional-runs/{session['run_id']}/evidence?case_id={case_id}&step_no=1",
            files={"file": ("note.txt", b"visible failure", "text/plain")},
        )
        assert uploaded.status_code == 200
        evidence = uploaded.json()["data"]["evidence"]
        artifact = client.get(
            f"/api/v1/projects/{project_id}/functional-runs/{session['run_id']}/artifacts/{evidence['relative_path']}"
        )
        assert artifact.status_code == 200
        assert artifact.content == b"visible failure"

        observation = client.post(
            f"/api/v1/projects/{project_id}/functional-runs/{session['run_id']}/events",
            json={
                "case_id": case_id,
                "step_no": 1,
                "source": "user",
                "event_type": "manual_note",
                "status": "warning",
                "summary": "等待测试账号",
            },
        )
        assert observation.status_code == 200

        finished = client.post(
            f"/api/v1/projects/{project_id}/functional-runs/{session['run_id']}/finish",
            json={"status": "completed", "summary": "提前结束"},
        )
        assert finished.status_code == 200
        assert finished.json()["data"]["status"] == "manual_incomplete"
        assert finished.json()["data"]["summary"]["pending"] == 1


def test_functional_run_rejects_foreign_or_disabled_cases():
    with TestClient(app) as client:
        project_id, environment_id, _ = _functional_project(client)
        _, _, foreign_case_id = _functional_project(client)
        response = client.post(
            f"/api/v1/projects/{project_id}/functional-runs",
            json={"environment_id": environment_id, "case_ids": [foreign_case_id], "channel": "manual"},
        )
        assert response.status_code == 400
        assert "case_ids" in response.json()["detail"]


def test_interactive_network_evidence_redacts_credentials():
    body = _redact_body('{"email":"demo@example.com","password":"secret","token":"abc"}')
    assert "demo@example.com" in body
    assert "secret" not in body
    assert "abc" not in body
    assert _safe_url("https://example.test/login?token=abc&page=1") == "https://example.test/login?token=%2A%2A%2A&page=1"
