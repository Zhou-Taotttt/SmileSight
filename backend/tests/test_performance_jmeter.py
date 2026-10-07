from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
import app.main as main_module
from app.models import Project, TestRun as RunModel
from app.performance_service import build_jmeter_plan, parse_jtl, summarize_samples
import app.tasks as tasks_module


def _project_with_api(client: TestClient) -> tuple[str, int, int]:
    project_id = f"jmeter-{uuid4().hex[:8]}"
    assert client.post("/api/v1/projects", json={"project_id": project_id, "name": "JMeter Demo"}).status_code == 200
    environment = client.post(
        f"/api/v1/projects/{project_id}/environments",
        json={"name": "Docker", "base_url": "http://backend:8000"},
    ).json()["data"]
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "JMeter", "version": "1"},
        "paths": {"/api/v1/health": {"get": {"responses": {"200": {"description": "ok"}}}}},
    }
    assert client.post(
        f"/api/v1/projects/{project_id}/openapi/import",
        files={"file": ("openapi.json", json.dumps(spec), "application/json")},
    ).status_code == 200
    api_id = client.get(f"/api/v1/projects/{project_id}/apis").json()["data"][0]["id"]
    return project_id, environment["id"], api_id


def test_jmeter_plan_distributes_exact_request_count():
    plan = build_jmeter_plan(
        "POST",
        "http://backend:8000/demo/login",
        {"email": "demo@example.com"},
        {"X-Test": "yes"},
        concurrency=3,
        total_requests=10,
        duration_seconds=30,
    )
    root = ET.fromstring(plan)
    total = 0
    for group in root.findall(".//ThreadGroup"):
        threads = int(group.find("stringProp[@name='ThreadGroup.num_threads']").text)
        loops = int(group.find(".//stringProp[@name='LoopController.loops']").text)
        total += threads * loops
    assert total == 10
    assert "demo@example.com" in plan
    assert "Content-Type" in plan
    assert "X-Test" in plan


def test_jtl_parser_and_metrics(tmp_path: Path):
    jtl = tmp_path / "result.jtl"
    jtl.write_text(
        "timeStamp,elapsed,label,responseCode,responseMessage,threadName,dataType,success,failureMessage,bytes,sentBytes,Latency,Connect\n"
        "1000,100,GET health,200,OK,t-1,text,true,,120,30,80,20\n"
        "1100,300,GET health,500,Error,t-2,text,false,assertion,90,30,250,30\n",
        encoding="utf-8",
    )
    samples = parse_jtl(jtl)
    assert len(samples) == 2
    assert samples[1]["success"] is False
    summary = summarize_samples(
        samples,
        {"api_id": 1, "method": "GET", "url": "http://backend:8000/api/v1/health"},
        {"concurrency": 2, "total_requests": 2, "duration_seconds": 10},
    )
    assert summary["p95_ms"] == 300
    assert summary["error_rate"] == 50
    assert summary["received_bytes"] == 210


def test_performance_task_submission_and_worker(monkeypatch):
    monkeypatch.setattr(
        main_module,
        "enqueue_async_task",
        lambda task_id, task_key, attempt: f"{task_key}-attempt-{attempt}",
    )

    def fake_jmeter(db, project, payload, **kwargs):
        kwargs["progress_callback"](5, 10, "JMeter 已完成 5/10 个样本")
        return {
            "run_id": 99,
            "run_key": "PERF-JMETER-TEST",
            "status": "completed",
            "summary": {
                "engine": "jmeter", "total": 10, "passed": 10, "failed": 0,
                "error": 0, "p95_ms": 20, "error_rate": 0, "throughput_rps": 100,
            },
            "report": "/tmp/report.json",
            "jtl": "/tmp/results.jtl",
            "cancelled": False,
        }

    monkeypatch.setattr(tasks_module, "execute_jmeter_run", fake_jmeter)
    with TestClient(app) as client:
        project_id, environment_id, api_id = _project_with_api(client)
        response = client.post(
            f"/api/v1/projects/{project_id}/tasks",
            json={
                "task_type": "performance", "environment_id": environment_id,
                "api_id": api_id, "concurrency": 2, "total_requests": 10,
                "duration_seconds": 30,
            },
        )
        assert response.status_code == 200
        task = response.json()["data"]
        assert task["task_type"] == "performance"
        result = tasks_module.execute_async_task(task["id"], task["attempt"])
        assert result["status"] == "completed"
        stored = client.get(f"/api/v1/projects/{project_id}/tasks/{task['id']}").json()["data"]
        assert stored["progress"] == 100
        assert stored["result"]["summary"]["engine"] == "jmeter"


def test_performance_trends_default_to_previous_baseline():
    with TestClient(app) as client:
        project_id, environment_id, api_id = _project_with_api(client)
        with SessionLocal() as db:
            project = db.query(Project).filter(Project.project_id == project_id).one()
            for index, metrics in enumerate(((100, 0, 50), (130, 2, 45)), start=1):
                p95, error_rate, throughput = metrics
                db.add(
                    RunModel(
                        project_id=project.id,
                        run_key=f"PERF-TREND-{uuid4().hex}",
                        test_type="performance",
                        environment_id=environment_id,
                        status="completed",
                        started_at=datetime.now(timezone.utc),
                        finished_at=datetime.now(timezone.utc),
                        summary={
                            "engine": "jmeter", "total": 10, "p95_ms": p95,
                            "avg_ms": p95 / 2, "error_rate": error_rate,
                            "throughput_rps": throughput,
                            "target": {"api_id": api_id, "method": "GET", "url": "http://backend:8000/api/v1/health"},
                            "gate": {"enabled": True, "passed": index == 1},
                        },
                    )
                )
            db.commit()
        response = client.get(f"/api/v1/projects/{project_id}/performance-trends?api_id={api_id}")
        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data["runs"]) == 2
        assert data["baseline"]["p95_ms"] == 100
        assert data["latest"]["p95_ms"] == 130
        assert data["comparison"]["p95_ms"]["change"] == 30
