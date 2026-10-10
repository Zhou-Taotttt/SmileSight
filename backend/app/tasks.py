from datetime import datetime, timezone
import traceback

from .api_run_service import execute_api_run
from .automation_run_service import execute_app_run, execute_web_run
from .performance_service import execute_jmeter_run
from .db import SessionLocal
from .models import AsyncTask, Project


def execute_async_task(task_id: int, expected_attempt: int | None = None) -> dict:
    """RQ entry point for one persisted asynchronous test task."""

    db = SessionLocal()
    task = None
    try:
        task = db.get(AsyncTask, task_id)
        if task is None:
            return {"task_id": task_id, "status": "missing"}
        if expected_attempt is not None and task.attempt != expected_attempt:
            return {
                "task_id": task.id,
                "status": "stale",
                "expected_attempt": expected_attempt,
                "current_attempt": task.attempt,
            }
        if task.status in {"cancelled", "cancel_requested"}:
            task.status = "cancelled"
            task.message = "任务已取消"
            task.finished_at = datetime.now(timezone.utc)
            db.commit()
            return {"task_id": task.id, "status": task.status}
        if task.status != "queued":
            return {"task_id": task.id, "status": task.status, "ignored": True}

        project = db.get(Project, task.project_id)
        if project is None:
            raise ValueError("project not found")
        task.status = "running"
        task.progress = 0
        task_label = {"api": "接口测试", "web": "Web 自动化", "app": "App 自动化", "performance": "性能测试"}.get(task.task_type, task.task_type)
        task.message = f"正在准备{task_label}"
        task.started_at = datetime.now(timezone.utc)
        task.finished_at = None
        db.commit()

        def cancellation_requested() -> bool:
            db.refresh(task, attribute_names=["status"])
            return task.status in {"cancel_requested", "cancelled"}

        def update_case_progress(completed: int, total: int, _result: dict) -> None:
            db.refresh(task, attribute_names=["status"])
            if task.status not in {"cancel_requested", "cancelled"}:
                task.status = "running"
            task.progress = 100 if total == 0 else int(completed * 100 / total)
            task.message = f"已执行 {completed}/{total} 条{task_label}用例"
            db.commit()

        payload = task.payload or {}
        if task.task_type == "performance":
            def update_performance_progress(completed: int, total: int, message: str) -> None:
                db.refresh(task, attribute_names=["status"])
                if task.status not in {"cancel_requested", "cancelled"}:
                    task.status = "running"
                task.progress = min(int(completed * 100 / total), 99) if total else 0
                task.message = message
                db.commit()

            result = execute_jmeter_run(
                db,
                project,
                payload,
                progress_callback=update_performance_progress,
                cancel_callback=cancellation_requested,
            )
        elif task.task_type == "web":
            result = execute_web_run(
                db,
                project,
                int(payload["environment_id"]),
                payload.get("case_ids"),
                progress_callback=update_case_progress,
                cancel_callback=cancellation_requested,
            )
        elif task.task_type == "app":
            result = execute_app_run(
                db,
                project,
                int(payload["environment_id"]),
                payload.get("case_ids"),
                capabilities=payload.get("capabilities"),
                device_config=payload.get("device_config"),
                server_url=payload.get("server_url"),
                progress_callback=update_case_progress,
                cancel_callback=cancellation_requested,
            )
        else:
            result = execute_api_run(
                db,
                project,
                int(payload["environment_id"]),
                payload.get("case_ids"),
                progress_callback=update_case_progress,
                cancel_callback=cancellation_requested,
            )
        db.refresh(task)
        task.result = result
        task.error = {}
        task.finished_at = datetime.now(timezone.utc)
        if result.get("cancelled") or task.status in {"cancel_requested", "cancelled"}:
            task.status = "cancelled"
            task.message = (
                "性能任务已取消，JMeter 进程已终止"
                if task.task_type == "performance"
                else f"{task_label}任务已取消，当前用例执行完毕后停止"
            )
            task.progress = min(task.progress, 99)
        else:
            task.status = "completed"
            task.progress = 100
            task.message = f"{task_label}执行完成"
        db.commit()
        return {"task_id": task.id, "status": task.status, "result": result}
    except Exception as exc:
        db.rollback()
        task = db.get(AsyncTask, task_id)
        if task is not None:
            message = str(exc).strip() or exc.__class__.__name__
            task.status = "failed"
            task.message = f"任务执行失败：{message}"[:500]
            task.error = {
                "type": exc.__class__.__name__,
                "message": message[:1000],
                "traceback": traceback.format_exc(limit=8)[-4000:],
            }
            task.finished_at = datetime.now(timezone.utc)
            db.commit()
        return {"task_id": task_id, "status": "failed", "error": str(exc)}
    finally:
        db.close()
