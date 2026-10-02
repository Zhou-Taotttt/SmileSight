from __future__ import annotations
import time
import httpx
from jsonpath_ng.ext import parse as jsonpath_parse
from jsonschema import validate as jsonschema_validate

def render_path(path, values):
    for key, value in values.items(): path = path.replace("{" + key + "}", str(value))
    return path

def execute_case(case, base_url, variables=None):
    variables = variables or {}
    timeout = float(variables.get("default_timeout_seconds", 30))
    api = case.api
    url = base_url.rstrip("/") + render_path(api.path, variables)
    payload = (case.test_data or {}).get("json")
    headers = (case.test_data or {}).get("headers") or {}
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.request(api.method, url, json=payload if payload not in ({}, None) else None, headers=headers)
        duration = int((time.perf_counter() - started) * 1000)
        try: body = response.json()
        except Exception: body = response.text[:10000]
        assertions = [evaluate_assertion(a, response.status_code, body) for a in case.assertions or []]
        passed = all(x["passed"] for x in assertions)
        return {"status": "passed" if passed else "failed", "duration_ms": duration, "request": {"method": api.method, "url": url, "json": payload, "headers": safe_headers(headers)}, "response": {"status_code": response.status_code, "body": body}, "assertions": assertions, "summary": "接口断言全部通过" if passed else "接口断言失败", "failure": {} if passed else {"category": "assertion_failed", "summary": "接口响应未满足断言"}}
    except Exception as exc:
        return {"status": "error", "duration_ms": int((time.perf_counter() - started) * 1000), "request": {"method": api.method, "url": url, "json": payload}, "response": {}, "assertions": [], "summary": "接口执行异常", "failure": {"category": "environment_or_request_error", "summary": str(exc)}}

def safe_headers(headers):
    return {k: ("***" if k.lower() in {"authorization", "cookie", "x-api-key"} else v) for k, v in headers.items()}

def evaluate_assertion(assertion, status_code, body):
    typ, expected = assertion.get("type"), assertion.get("expected")
    actual, passed, error = None, False, ""
    try:
        if typ == "status_code": actual, passed = status_code, status_code == expected
        elif typ == "json_path":
            actual = [m.value for m in jsonpath_parse(assertion["path"]).find(body)]; passed = bool(actual) and (expected is None or expected in actual)
        elif typ == "json_schema": jsonschema_validate(body, assertion.get("schema") or {}); actual, passed = "valid", True
        else: error = f"unsupported assertion type: {typ}"
    except Exception as exc: error = str(exc)
    return {"type": typ, "expected": expected or assertion.get("schema"), "actual": actual, "passed": passed, "error": error}
