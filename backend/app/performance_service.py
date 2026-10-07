from __future__ import annotations

import csv
from datetime import datetime, timezone
from html import escape
import json
import math
from pathlib import Path
import shlex
import statistics
import subprocess
import time
from typing import Callable
from urllib.parse import urlsplit
from uuid import uuid4
import xml.etree.ElementTree as ET

from sqlalchemy.orm import Session

from .config import REPORT_DIR
from .executor import render_path
from .models import ApiEndpoint, Environment, Project, TestCase, TestRun
from .schemas import PerformanceSettings


class PerformanceExecutionError(ValueError):
    pass


def _performance_settings(project: Project) -> dict:
    defaults = PerformanceSettings().model_dump()
    if project.settings and isinstance(project.settings.performance, dict):
        defaults.update(project.settings.performance)
    return defaults


def _validate_target(
    db: Session,
    project: Project,
    environment_id: int,
    api_id: int,
    case_id: int | None,
) -> tuple[Environment, ApiEndpoint, TestCase | None]:
    environment = db.get(Environment, environment_id)
    api = db.get(ApiEndpoint, api_id)
    if not environment or environment.project_id != project.id:
        raise PerformanceExecutionError("invalid environment")
    if not api or api.project_id != project.id:
        raise PerformanceExecutionError("invalid api_id")
    case = db.get(TestCase, case_id) if case_id else None
    if case and (case.project_id != project.id or case.api_id != api.id):
        raise PerformanceExecutionError("invalid case_id")
    return environment, api, case


def _text_prop(parent: ET.Element, name: str, value: object) -> ET.Element:
    node = ET.SubElement(parent, "stringProp", {"name": name})
    node.text = str(value)
    return node


def _bool_prop(parent: ET.Element, name: str, value: bool) -> ET.Element:
    node = ET.SubElement(parent, "boolProp", {"name": name})
    node.text = "true" if value else "false"
    return node


def _request_sampler(
    method: str,
    target_url: str,
    request_json: object,
    headers: dict,
) -> ET.Element:
    parsed = urlsplit(target_url)
    sampler = ET.Element(
        "HTTPSamplerProxy",
        {
            "guiclass": "HttpTestSampleGui",
            "testclass": "HTTPSamplerProxy",
            "testname": f"{method.upper()} {parsed.path or '/'}",
            "enabled": "true",
        },
    )
    arguments = ET.SubElement(
        sampler,
        "elementProp",
        {
            "name": "HTTPsampler.Arguments",
            "elementType": "Arguments",
            "guiclass": "HTTPArgumentsPanel",
            "testclass": "Arguments",
            "enabled": "true",
        },
    )
    collection = ET.SubElement(arguments, "collectionProp", {"name": "Arguments.arguments"})
    if request_json not in (None, {}):
        argument = ET.SubElement(
            collection,
            "elementProp",
            {"name": "", "elementType": "HTTPArgument"},
        )
        _bool_prop(argument, "HTTPArgument.always_encode", False)
        _text_prop(argument, "Argument.value", json.dumps(request_json, ensure_ascii=False))
        _text_prop(argument, "Argument.metadata", "=")
    _text_prop(sampler, "HTTPSampler.domain", parsed.hostname or "")
    _text_prop(sampler, "HTTPSampler.port", parsed.port or "")
    _text_prop(sampler, "HTTPSampler.protocol", parsed.scheme or "http")
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    _text_prop(sampler, "HTTPSampler.path", path)
    _text_prop(sampler, "HTTPSampler.method", method.upper())
    _bool_prop(sampler, "HTTPSampler.follow_redirects", True)
    _bool_prop(sampler, "HTTPSampler.auto_redirects", False)
    _bool_prop(sampler, "HTTPSampler.use_keepalive", True)
    _bool_prop(sampler, "HTTPSampler.DO_MULTIPART_POST", False)
    _bool_prop(sampler, "HTTPSampler.postBodyRaw", request_json not in (None, {}))
    _text_prop(sampler, "HTTPSampler.connect_timeout", "10000")
    _text_prop(sampler, "HTTPSampler.response_timeout", "30000")
    return sampler


def _header_manager(headers: dict, has_body: bool) -> ET.Element:
    manager = ET.Element(
        "HeaderManager",
        {
            "guiclass": "HeaderPanel",
            "testclass": "HeaderManager",
            "testname": "HTTP Headers",
            "enabled": "true",
        },
    )
    collection = ET.SubElement(manager, "collectionProp", {"name": "HeaderManager.headers"})
    normalized = {str(key): str(value) for key, value in (headers or {}).items()}
    if has_body and not any(key.lower() == "content-type" for key in normalized):
        normalized["Content-Type"] = "application/json"
    for index, (key, value) in enumerate(normalized.items()):
        prop = ET.SubElement(
            collection,
            "elementProp",
            {"name": str(index), "elementType": "Header"},
        )
        _text_prop(prop, "Header.name", key)
        _text_prop(prop, "Header.value", value)
    return manager


def _thread_group(
    name: str,
    threads: int,
    loops: int,
    duration_seconds: int,
    sampler: ET.Element,
    headers: dict,
    has_body: bool,
) -> tuple[ET.Element, ET.Element]:
    group = ET.Element(
        "ThreadGroup",
        {
            "guiclass": "ThreadGroupGui",
            "testclass": "ThreadGroup",
            "testname": name,
            "enabled": "true",
        },
    )
    _text_prop(group, "ThreadGroup.on_sample_error", "continue")
    controller = ET.SubElement(
        group,
        "elementProp",
        {
            "name": "ThreadGroup.main_controller",
            "elementType": "LoopController",
            "guiclass": "LoopControlPanel",
            "testclass": "LoopController",
            "enabled": "true",
        },
    )
    _bool_prop(controller, "LoopController.continue_forever", False)
    _text_prop(controller, "LoopController.loops", loops)
    _text_prop(group, "ThreadGroup.num_threads", threads)
    _text_prop(group, "ThreadGroup.ramp_time", min(threads, 10))
    _bool_prop(group, "ThreadGroup.scheduler", True)
    _text_prop(group, "ThreadGroup.duration", duration_seconds)
    _text_prop(group, "ThreadGroup.delay", 0)

    children = ET.Element("hashTree")
    children.append(ET.fromstring(ET.tostring(sampler, encoding="unicode")))
    sampler_tree = ET.SubElement(children, "hashTree")
    sampler_tree.append(_header_manager(headers, has_body))
    ET.SubElement(sampler_tree, "hashTree")
    return group, children


def build_jmeter_plan(
    method: str,
    target_url: str,
    request_json: object,
    headers: dict,
    concurrency: int,
    total_requests: int,
    duration_seconds: int,
) -> str:
    root = ET.Element(
        "jmeterTestPlan",
        {"version": "1.2", "properties": "5.0", "jmeter": "5.6.3"},
    )
    root_tree = ET.SubElement(root, "hashTree")
    plan = ET.SubElement(
        root_tree,
        "TestPlan",
        {
            "guiclass": "TestPlanGui",
            "testclass": "TestPlan",
            "testname": "SmileSight Performance Plan",
            "enabled": "true",
        },
    )
    _text_prop(plan, "TestPlan.comments", "Generated by SmileSight")
    _bool_prop(plan, "TestPlan.functional_mode", False)
    _bool_prop(plan, "TestPlan.serialize_threadgroups", False)
    user_variables = ET.SubElement(
        plan,
        "elementProp",
        {
            "name": "TestPlan.user_defined_variables",
            "elementType": "Arguments",
            "guiclass": "ArgumentsPanel",
            "testclass": "Arguments",
            "enabled": "true",
        },
    )
    ET.SubElement(user_variables, "collectionProp", {"name": "Arguments.arguments"})
    _text_prop(plan, "TestPlan.user_define_classpath", "")
    plan_tree = ET.SubElement(root_tree, "hashTree")

    effective_threads = min(concurrency, total_requests)
    base_loops, extra_threads = divmod(total_requests, effective_threads)
    groups = []
    if extra_threads:
        groups.append((extra_threads, base_loops + 1))
    if effective_threads - extra_threads:
        groups.append((effective_threads - extra_threads, base_loops))
    sampler = _request_sampler(method, target_url, request_json, headers)
    for index, (threads, loops) in enumerate(groups, start=1):
        group, children = _thread_group(
            f"Load Group {index}",
            threads,
            loops,
            duration_seconds,
            sampler,
            headers,
            request_json not in (None, {}),
        )
        plan_tree.append(group)
        plan_tree.append(children)
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


def create_jmeter_plan(
    db: Session,
    project: Project,
    environment_id: int,
    api_id: int,
    case_id: int | None,
    concurrency: int,
    total_requests: int,
    duration_seconds: int,
    *,
    filename: str | None = None,
) -> dict:
    environment, api, case = _validate_target(
        db, project, environment_id, api_id, case_id
    )
    variables = environment.variables or {}
    target_url = environment.base_url.rstrip("/") + render_path(api.path, variables)
    test_data = (case.test_data or {}) if case else {}
    request_json = test_data.get("json")
    headers = test_data.get("headers") or {}
    plan = build_jmeter_plan(
        api.method,
        target_url,
        request_json,
        headers,
        concurrency,
        total_requests,
        duration_seconds,
    )
    folder = REPORT_DIR / project.project_id / "plans"
    folder.mkdir(parents=True, exist_ok=True)
    plan_path = folder / (filename or f"PERF-PLAN-{uuid4().hex}.jmx")
    plan_path.write_text(plan, encoding="utf-8")
    return {
        "plan_path": str(plan_path),
        "method": api.method,
        "url": target_url,
        "api_id": api.id,
        "concurrency": concurrency,
        "total_requests": total_requests,
        "duration_seconds": duration_seconds,
    }


def parse_jtl(path: str | Path) -> list[dict]:
    source = Path(path)
    if not source.exists() or source.stat().st_size == 0:
        return []
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = []
        for raw in csv.DictReader(handle):
            row = {str(key).lower(): value for key, value in raw.items()}
            try:
                elapsed = float(row.get("elapsed") or 0)
            except (TypeError, ValueError):
                elapsed = 0.0
            try:
                timestamp = int(float(row.get("timestamp") or row.get("timeStamp") or 0))
            except (TypeError, ValueError):
                timestamp = 0
            success = str(row.get("success") or "").lower() == "true"
            rows.append(
                {
                    "timestamp": timestamp,
                    "duration_ms": round(elapsed, 2),
                    "label": row.get("label") or "",
                    "status_code": row.get("responsecode") or None,
                    "response_message": row.get("responsemessage") or "",
                    "success": success,
                    "failure_message": row.get("failuremessage") or "",
                    "bytes": int(float(row.get("bytes") or 0)),
                    "sent_bytes": int(float(row.get("sentbytes") or 0)),
                    "latency_ms": float(row.get("latency") or 0),
                    "connect_ms": float(row.get("connect") or 0),
                }
            )
    return rows


def _percentile(values: list[float], percentile: int) -> float:
    if not values:
        return 0
    index = min(
        len(values) - 1,
        max(0, int(math.ceil(percentile / 100 * len(values))) - 1),
    )
    return round(values[index], 2)


def summarize_samples(samples: list[dict], target: dict, requested: dict) -> dict:
    durations = sorted(float(item["duration_ms"]) for item in samples)
    failed = sum(1 for item in samples if not item["success"])
    if samples:
        starts = [item["timestamp"] for item in samples if item["timestamp"]]
        ends = [
            item["timestamp"] + item["duration_ms"]
            for item in samples
            if item["timestamp"]
        ]
        elapsed_seconds = (
            max(max(ends) - min(starts), 1) / 1000 if starts and ends else 0.001
        )
    else:
        elapsed_seconds = 0.001
    total = len(samples)
    return {
        "engine": "jmeter",
        "total": total,
        "requested_total": requested["total_requests"],
        "passed": total - failed,
        "failed": failed,
        "error": 0,
        "error_rate": round(failed / total * 100, 2) if total else 0,
        "throughput_rps": round(total / elapsed_seconds, 2),
        "avg_ms": round(statistics.mean(durations), 2) if durations else 0,
        "min_ms": round(min(durations), 2) if durations else 0,
        "max_ms": round(max(durations), 2) if durations else 0,
        "p50_ms": _percentile(durations, 50),
        "p95_ms": _percentile(durations, 95),
        "p99_ms": _percentile(durations, 99),
        "received_bytes": sum(item["bytes"] for item in samples),
        "sent_bytes": sum(item["sent_bytes"] for item in samples),
        "concurrency": requested["concurrency"],
        "duration_seconds": requested["duration_seconds"],
        "target": target,
    }


def apply_gates(summary: dict, payload: dict) -> None:
    gates = []
    if payload.get("max_p95_ms") is not None:
        gates.append(
            {
                "metric": "p95_ms",
                "actual": summary["p95_ms"],
                "limit": payload["max_p95_ms"],
                "passed": summary["p95_ms"] <= payload["max_p95_ms"],
            }
        )
    if payload.get("max_error_rate") is not None:
        gates.append(
            {
                "metric": "error_rate",
                "actual": summary["error_rate"],
                "limit": payload["max_error_rate"],
                "passed": summary["error_rate"] <= payload["max_error_rate"],
            }
        )
    if payload.get("min_throughput_rps") is not None:
        gates.append(
            {
                "metric": "throughput_rps",
                "actual": summary["throughput_rps"],
                "limit": payload["min_throughput_rps"],
                "passed": summary["throughput_rps"] >= payload["min_throughput_rps"],
            }
        )
    summary["gate"] = {
        "enabled": bool(gates),
        "passed": all(item["passed"] for item in gates) if gates else None,
        "checks": gates,
    }


def _jtl_sample_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("rb") as handle:
        return max(sum(1 for _ in handle) - 1, 0)


def execute_jmeter_run(
    db: Session,
    project: Project,
    payload: dict,
    *,
    progress_callback: Callable[[int, int, str], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
) -> dict:
    environment, api, _case = _validate_target(
        db,
        project,
        int(payload["environment_id"]),
        int(payload["api_id"]),
        payload.get("case_id"),
    )
    run = TestRun(
        project_id=project.id,
        run_key=f"PERF-JMETER-{uuid4().hex}",
        test_type="performance",
        environment_id=environment.id,
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.flush()
    run_folder = REPORT_DIR / project.project_id / run.run_key
    run_folder.mkdir(parents=True, exist_ok=True)
    plan_info = create_jmeter_plan(
        db,
        project,
        int(payload["environment_id"]),
        int(payload["api_id"]),
        payload.get("case_id"),
        int(payload["concurrency"]),
        int(payload["total_requests"]),
        int(payload["duration_seconds"]),
        filename=f"{run.run_key}.jmx",
    )
    jtl_path = run_folder / "results.jtl"
    log_path = run_folder / "jmeter.log"
    console_path = run_folder / "console.log"
    settings = _performance_settings(project)
    command = shlex.split(str(settings.get("jmeter_path") or "jmeter"))
    if not command:
        raise PerformanceExecutionError("JMeter command is empty")
    command.extend(
        [
            "-n",
            "-t",
            plan_info["plan_path"],
            "-l",
            str(jtl_path),
            "-j",
            str(log_path),
            "-Jjmeter.save.saveservice.output_format=csv",
            "-Jjmeter.save.saveservice.print_field_names=true",
            "-Jjmeter.save.saveservice.timestamp_format=ms",
            "-Jjmeter.save.saveservice.time=true",
            "-Jjmeter.save.saveservice.label=true",
            "-Jjmeter.save.saveservice.response_code=true",
            "-Jjmeter.save.saveservice.response_message=true",
            "-Jjmeter.save.saveservice.successful=true",
            "-Jjmeter.save.saveservice.failure_message=true",
            "-Jjmeter.save.saveservice.bytes=true",
            "-Jjmeter.save.saveservice.sent_bytes=true",
            "-Jjmeter.save.saveservice.latency=true",
            "-Jjmeter.save.saveservice.connect_time=true",
        ]
    )
    db.commit()
    cancelled = False
    try:
        with console_path.open("w", encoding="utf-8") as console:
            process = subprocess.Popen(
                command,
                stdout=console,
                stderr=subprocess.STDOUT,
                text=True,
            )
            while process.poll() is None:
                if cancel_callback and cancel_callback():
                    cancelled = True
                    process.terminate()
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    break
                completed = _jtl_sample_count(jtl_path)
                if progress_callback:
                    progress_callback(
                        completed,
                        int(payload["total_requests"]),
                        f"JMeter 已完成 {completed}/{payload['total_requests']} 个样本",
                    )
                time.sleep(0.5)
            exit_code = process.wait()
        samples = parse_jtl(jtl_path)
        if not cancelled and exit_code != 0:
            tail = console_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            raise RuntimeError(f"JMeter exited with code {exit_code}: {tail}")
        if not cancelled and not samples:
            raise RuntimeError("JMeter did not produce any samples")
        target = {
            "api_id": api.id,
            "method": api.method,
            "url": plan_info["url"],
        }
        summary = summarize_samples(samples, target, payload)
        apply_gates(summary, payload)
        summary["cancelled"] = cancelled
        run.status = "cancelled" if cancelled else "completed"
        run.finished_at = datetime.now(timezone.utc)
        run.summary = summary
        db.commit()
        failures = [item for item in samples if not item["success"]][:20]
        report_path = run_folder / f"{run.run_key}.json"
        report_path.write_text(
            json.dumps(
                {
                    "summary": summary,
                    "plan": plan_info["plan_path"],
                    "jtl": str(jtl_path),
                    "jmeter_log": str(log_path),
                    "console_log": str(console_path),
                    "failure_samples": failures,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return {
            "run_id": run.id,
            "run_key": run.run_key,
            "status": run.status,
            "summary": summary,
            "report": str(report_path),
            "plan": plan_info["plan_path"],
            "jtl": str(jtl_path),
            "jmeter_log": str(log_path),
            "console_log": str(console_path),
            "cancelled": cancelled,
        }
    except Exception as exc:
        db.rollback()
        run = db.get(TestRun, run.id)
        if run is not None:
            run.status = "failed"
            run.finished_at = datetime.now(timezone.utc)
            run.summary = {
                "engine": "jmeter",
                "error": str(exc)[:1000],
                "target": {"api_id": api.id, "method": api.method, "url": plan_info["url"]},
            }
            db.commit()
        raise
