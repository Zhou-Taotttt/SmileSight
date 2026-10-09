from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import json
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

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
