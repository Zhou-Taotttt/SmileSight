from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import json
import os
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from .app_executor import DEFAULT_APPIUM_SERVER_URL, execute_app_case
from .config import REPORT_DIR
from .models import Environment, Project, TestCase, TestResult, TestRun
from .schemas import ProjectSettingsUpdate
from .web_executor import execute_web_case


class AutomationExecutionError(ValueError):
    """Raised when an automation run payload is invalid for the project."""


def _project_settings(project: Project) -> dict[str, Any]:
    settings = ProjectSettingsUpdate().model_dump()
    stored = project.settings
    if stored is None:
        return settings
    for section in settings:
        value = getattr(stored, section, None)
        if isinstance(value, Mapping):
            settings[section].update(dict(value))
    return settings


def _cases(
    db: Session,
    project: Project,
    test_type: str,
    case_ids: list[int] | None,
) -> list[TestCase]:
    statement = select(TestCase).where(
        TestCase.project_id == project.id,
        TestCase.test_type == test_type,
        TestCase.enabled.is_(True),
    )
    if case_ids:
        statement = statement.where(TestCase.id.in_(case_ids))
    cases = list(db.scalars(statement.order_by(TestCase.id)).all())
    if case_ids and len(cases) != len(set(case_ids)):
        raise AutomationExecutionError(
            f"one or more {test_type} cases are invalid or disabled"
        )
    return cases


def _environment(db: Session, project: Project, environment_id: int) -> Environment:
    environment = db.get(Environment, environment_id)
    if not environment or environment.project_id != project.id:
        raise AutomationExecutionError("invalid environment")
    return environment


def _write_report(
    project: Project,
    run: TestRun,
    details: list[dict[str, Any]],
    summary: dict[str, Any],
) -> str:
    folder = REPORT_DIR / project.project_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{run.run_key}.json"
    path.write_text(
        json.dumps(
            {
                "project": project.project_id,
                "run_key": run.run_key,
                "summary": summary,
                "results": details,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return str(path)


def _create_run(
    db: Session,
    project: Project,
    environment: Environment,
    test_type: str,
) -> TestRun:
    prefix = "WEB" if test_type == "web" else "APP"
    run = TestRun(
        project_id=project.id,
        run_key=f"{prefix}-{uuid4().hex}",
        test_type=test_type,
        environment_id=environment.id,
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.flush()
    return run


def _persist_result(db: Session, run: TestRun, case: TestCase, result: dict) -> None:
    db.add(
        TestResult(
            run_id=run.id,
            case_id=case.id,
            status=result["status"],
            duration_ms=result["duration_ms"],
            summary=result["summary"],
            request=result["request"],
            response=result["response"],
            assertions=result["assertions"],
            failure=result["failure"],
        )
    )


def _finish_run(
    db: Session,
    project: Project,
    run: TestRun,
    cases: list[TestCase],
    details: list[dict[str, Any]],
    totals: dict[str, int],
    cancelled: bool,
) -> dict[str, Any]:
    run.status = "cancelled" if cancelled else "completed"
    run.finished_at = datetime.now(timezone.utc)
    run.summary = {
        **totals,
        "planned": len(cases),
        "cancelled": max(len(cases) - totals["total"], 0) if cancelled else 0,
    }
    report = _write_report(project, run, details, run.summary)
    return {
        "run_id": run.id,
        "run_key": run.run_key,
        "status": run.status,
        "summary": run.summary,
        "report": report,
        "cancelled": cancelled,
    }


def _cancelled(cancel_callback: Callable[[], bool] | None) -> bool:
    return bool(cancel_callback and cancel_callback())


def execute_web_run(
    db: Session,
    project: Project,
    environment_id: int,
    case_ids: list[int] | None = None,
    *,
    case_executor: Callable[..., dict] = execute_web_case,
    progress_callback: Callable[[int, int, dict], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Execute a Web batch with persisted evidence and cooperative cancellation."""

    environment = _environment(db, project, environment_id)
    cases = _cases(db, project, "web", case_ids)
    run = _create_run(db, project, environment, "web")
    evidence_dir = REPORT_DIR / project.project_id / run.run_key
    settings = _project_settings(project)
    variables = {
        "selenium_remote_url": settings["web"].get("remote_url"),
        "browser": settings["web"].get("browser"),
        "headless": settings["web"].get("headless"),
        "default_timeout_seconds": settings["execution"].get(
            "default_timeout_seconds"
        ),
        **(environment.variables or {}),
    }
    totals = {"total": 0, "passed": 0, "failed": 0, "error": 0}
    details: list[dict[str, Any]] = []
    cancelled = False
    for case in cases:
        if _cancelled(cancel_callback):
            cancelled = True
            break
        result = case_executor(
            case,
            environment.base_url,
            variables,
            evidence_dir=str(evidence_dir),
        )
        _persist_result(db, run, case, result)
        totals["total"] += 1
        totals[result["status"]] = totals.get(result["status"], 0) + 1
        details.append({"case": case.case_key, **result})
        if progress_callback:
            progress_callback(totals["total"], len(cases), result)
    if _cancelled(cancel_callback):
        cancelled = True
    return _finish_run(db, project, run, cases, details, totals, cancelled)


def _validated_appium_url(value: object) -> str:
    raw = str(value or DEFAULT_APPIUM_SERVER_URL).strip()
    if any(character in raw for character in ("\r", "\n", "\t")):
        raise AutomationExecutionError("invalid Appium server_url")
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AutomationExecutionError("invalid Appium server_url")
    if parsed.username is not None or parsed.password is not None:
        raise AutomationExecutionError("Appium server_url must not contain credentials")
    return raw.rstrip("/")


def _app_settings(
    project: Project,
    environment: Environment,
    capabilities: Mapping[str, Any] | None,
    device_config: Mapping[str, Any] | None,
    server_url: str | None,
) -> dict[str, Any]:
    project_app = _project_settings(project)["app"]
    settings: dict[str, Any] = {
        "server_url": project_app.get("server_url"),
        "platformName": project_app.get("platform_name"),
        "automationName": project_app.get("automation_name"),
        "deviceName": project_app.get("device_name"),
        "launch_wait_seconds": project_app.get("launch_wait_seconds"),
    }
    settings = {key: value for key, value in settings.items() if value not in (None, "")}
    settings.update(dict(environment.variables or {}))
    nested = settings.get("appium")
    if isinstance(nested, Mapping):
        settings.update(dict(nested))
    settings.update(dict(device_config or {}))
    settings.update(dict(capabilities or {}))
    if server_url is not None:
        settings["server_url"] = server_url
    settings["server_url"] = _validated_appium_url(
        settings.get("server_url")
        or settings.get("appium_server_url")
        or settings.get("remote_url")
        or os.getenv("APPIUM_SERVER_URL")
        or os.getenv("APPIUM_URL")
        or DEFAULT_APPIUM_SERVER_URL
    )
    return settings


def _app_error_result(case: TestCase, settings: Mapping[str, Any], exc: Exception) -> dict:
    message = f"App 执行异常：{type(exc).__name__}: {exc}"
    return {
        "status": "error",
        "duration_ms": 0,
        "request": {
            "server_url": str(settings.get("server_url") or DEFAULT_APPIUM_SERVER_URL),
            "capabilities": dict(settings),
            "steps": case.steps or [],
        },
        "response": {"screenshots": [], "device_logs": []},
        "assertions": [],
        "summary": message,
        "failure": {"category": "environment_or_device_error", "summary": message},
    }


def execute_app_run(
    db: Session,
    project: Project,
    environment_id: int,
    case_ids: list[int] | None = None,
    *,
    capabilities: Mapping[str, Any] | None = None,
    device_config: Mapping[str, Any] | None = None,
    server_url: str | None = None,
    case_executor: Callable[..., dict] = execute_app_case,
    progress_callback: Callable[[int, int, dict], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Execute an Appium batch with task-level overrides and cooperative cancellation."""

    environment = _environment(db, project, environment_id)
    cases = _cases(db, project, "app", case_ids)
    settings = _app_settings(project, environment, capabilities, device_config, server_url)
    run = _create_run(db, project, environment, "app")
    evidence_dir = REPORT_DIR / project.project_id / run.run_key
    totals = {"total": 0, "passed": 0, "failed": 0, "error": 0}
    details: list[dict[str, Any]] = []
    cancelled = False
    for case in cases:
        if _cancelled(cancel_callback):
            cancelled = True
            break
        try:
            result = case_executor(case, capabilities=settings, evidence_dir=str(evidence_dir))
        except Exception as exc:
            result = _app_error_result(case, settings, exc)
        _persist_result(db, run, case, result)
        totals["total"] += 1
        totals[result["status"]] = totals.get(result["status"], 0) + 1
        details.append({"case": case.case_key, **result})
        if progress_callback:
            progress_callback(totals["total"], len(cases), result)
    if _cancelled(cancel_callback):
        cancelled = True
    return _finish_run(db, project, run, cases, details, totals, cancelled)
