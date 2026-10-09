from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
import app.main as main_module
import app.tasks as tasks_module
from app.automation_run_service import execute_web_run
from app.db import SessionLocal
from app.models import Project, TestRun as RunModel


def _create_project_and_environment(client: TestClient) -> tuple[str, int]:
    project_id = f"async-{uuid4().hex[:8]}"
    assert client.post(
        "/api/v1/projects",
        json={"project_id": project_id, "name": "Async Task Demo"},
    ).status_code == 200
    environment = client.post(
        f"/api/v1/projects/{project_id}/environments",
        json={"name": "QA", "base_url": "http://qa.local"},
    )
    assert environment.status_code == 200
    return project_id, environment.json()["data"]["id"]


def _create_case(
    client: TestClient,
    project_id: str,
    test_type: str,
    *,
    enabled: bool = True,
) -> int:
    response = client.post(
        f"/api/v1/projects/{project_id}/test-cases",
        json={
            "title": f"{test_type} async case",
            "test_type": test_type,
            "enabled": enabled,
            "steps": [{"action": "navigate"}] if test_type == "web" else [{"action": "launch"}],
            "assertions": [],
        },
    )
    assert response.status_code == 200
    return response.json()["data"]["id"]


def _passed_result(kind: str) -> dict:
    return {
        "status": "passed",
        "duration_ms": 5,
        "summary": f"{kind} passed",
        "request": {},
        "response": {"screenshots": []},
        "assertions": [],
        "failure": {},
    }


def test_worker_dispatches_web_task_and_persists_result(monkeypatch):
    monkeypatch.setattr(
        main_module,
        "enqueue_async_task",
        lambda task_id, task_key, attempt: f"{task_key}-attempt-{attempt}",
    )
    calls = []

    def fake_web(db, project, environment_id, case_ids, **kwargs):
        calls.append((project.project_id, environment_id, case_ids))
        kwargs["progress_callback"](1, 1, {"status": "passed"})
        return {
            "run_id": 321,
            "run_key": "WEB-ASYNC",
            "status": "completed",
            "summary": {"total": 1, "passed": 1, "failed": 0, "error": 0},
            "report": "/tmp/WEB-ASYNC.json",
            "cancelled": False,
        }

    monkeypatch.setattr(tasks_module, "execute_web_run", fake_web)
    with TestClient(app) as client:
        project_id, environment_id = _create_project_and_environment(client)
        web_id = _create_case(client, project_id, "web")
        task = client.post(
            f"/api/v1/projects/{project_id}/tasks",
            json={"task_type": "web", "environment_id": environment_id, "case_ids": [web_id]},
        ).json()["data"]

        completed = tasks_module.execute_async_task(task["id"], expected_attempt=1)
        assert completed["status"] == "completed"
        assert calls == [(project_id, environment_id, [web_id])]
        stored = client.get(f"/api/v1/projects/{project_id}/tasks/{task['id']}").json()["data"]
        assert stored["progress"] == 100
        assert stored["result"]["run_id"] == 321
        assert stored["message"] == "Web 自动化执行完成"


def test_web_run_service_persists_run_progress_and_cooperative_cancel():
    progress = []
    cancel_checks = 0

    def cancel_after_first_case():
        nonlocal cancel_checks
        cancel_checks += 1
        return cancel_checks >= 2

    with TestClient(app) as client:
        project_id, environment_id = _create_project_and_environment(client)
        first_id = _create_case(client, project_id, "web")
        second_id = _create_case(client, project_id, "web")
        with SessionLocal() as db:
            project = db.scalar(select(Project).where(Project.project_id == project_id))
            result = execute_web_run(
                db,
                project,
                environment_id,
                [first_id, second_id],
                case_executor=lambda *args, **kwargs: _passed_result("web"),
                progress_callback=lambda completed, total, _item: progress.append((completed, total)),
                cancel_callback=cancel_after_first_case,
            )
            run = db.get(RunModel, result["run_id"])
            assert run.status == "cancelled"
            assert run.test_type == "web"
        assert result["cancelled"] is True
        assert result["summary"]["total"] == 1
        assert result["summary"]["cancelled"] == 1
        assert progress == [(1, 2)]
