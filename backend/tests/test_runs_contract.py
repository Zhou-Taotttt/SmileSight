"""Execution-record list/detail contract coverage."""

import json
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app
import app.main as main_module


def test_runs_filter_and_normalized_evidence(monkeypatch):
    def fake_execute(case, base_url, variables):
        return {
            "status": "failed",
            "duration_ms": 12,
            "request": {"url": f"{base_url}/ping"},
            "response": {
                "screenshots": ["API-1.png"],
                "console_logs": [{"message": "warning"}],
            },
            "assertions": [{"type": "status_code", "passed": False}],
            "summary": "状态码断言失败",
            "failure": {"category": "assertion_failed", "api": "GET /ping"},
        }

    monkeypatch.setattr(main_module, "execute_case", fake_execute)
    with TestClient(app) as client:
        project_id = f"runs-contract-{uuid4().hex[:8]}"
        assert client.post(
            "/api/v1/projects", json={"project_id": project_id, "name": "Runs Contract"}
        ).status_code == 200
        environment = client.post(
            f"/api/v1/projects/{project_id}/environments",
            json={"name": "QA", "base_url": "http://qa.local"},
        ).json()["data"]
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Runs", "version": "1"},
            "paths": {"/ping": {"get": {"responses": {"200": {"description": "ok"}}}}},
        }
        assert client.post(
            f"/api/v1/projects/{project_id}/openapi/import",
            files={"file": ("openapi.json", json.dumps(spec), "application/json")},
        ).status_code == 200
        assert client.post(f"/api/v1/projects/{project_id}/test-cases/generate").status_code == 200
        run = client.post(
            f"/api/v1/projects/{project_id}/runs",
            json={"environment_id": environment["id"]},
        )
        assert run.status_code == 200
        run_id = run.json()["data"]["run_id"]

        listed = client.get(f"/api/v1/projects/{project_id}/runs?test_type=api")
        assert listed.status_code == 200
        item = listed.json()["data"][0]
        assert item["test_type"] == "api"
        assert item["environment_id"] == environment["id"]
        assert item["started_at"] is not None
        assert item["report"].endswith(f"{item['run_key']}.json")
        assert client.get(f"/api/v1/projects/{project_id}/runs?test_type=web").json()["data"] == []
        assert client.get(f"/api/v1/projects/{project_id}/runs?test_type=unknown").status_code == 400

        detail = client.get(f"/api/v1/projects/{project_id}/runs/{run_id}")
        assert detail.status_code == 200
        assert detail.json()["data"]["report"].endswith(
            f"{detail.json()['data']['run_key']}.json"
        )
        result = detail.json()["data"]["results"][0]
        assert result["evidence"] == {
            "screenshots": ["API-1.png"],
            "screenshot": None,
            "video": None,
            "device_logs": [],
            "console_logs": [{"message": "warning"}],
            "network": [],
            "failure": {"category": "assertion_failed", "api": "GET /ping"},
        }


def test_project_detail_returns_environment_variables():
    with TestClient(app) as client:
        project_id = f"env-variables-{uuid4().hex[:8]}"
        assert client.post(
            "/api/v1/projects", json={"project_id": project_id, "name": "Environment Variables"}
        ).status_code == 200
        variables = {
            "appium": {
                "server_url": "http://host.docker.internal:4723",
                "platformName": "Android",
                "deviceName": "emulator-5554",
            }
        }
        created = client.post(
            f"/api/v1/projects/{project_id}/environments",
            json={"name": "Android", "base_url": "http://frontend", "variables": variables},
        )
        assert created.status_code == 200
        detail = client.get(f"/api/v1/projects/{project_id}")
        assert detail.status_code == 200
        assert detail.json()["data"]["environments"][0]["variables"] == variables


def test_environment_update_persists_variables():
    with TestClient(app) as client:
        project_id = f"env-update-{uuid4().hex[:8]}"
        assert client.post("/api/v1/projects", json={"project_id": project_id, "name": "Environment Update"}).status_code == 200
        created = client.post(
            f"/api/v1/projects/{project_id}/environments",
            json={"name": "QA", "base_url": "http://qa.local", "variables": {"deviceName": "old"}},
        ).json()["data"]
        updated = client.patch(
            f"/api/v1/projects/{project_id}/environments/{created['id']}",
            json={"name": "Android QA", "base_url": "http://frontend", "variables": {"deviceName": "emulator-5554"}},
        )
        assert updated.status_code == 200
        assert updated.json()["data"]["variables"]["deviceName"] == "emulator-5554"
