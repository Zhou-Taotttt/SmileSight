from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from .app_executor import _create_driver as create_app_driver
from .app_executor import _evaluate_assertion as evaluate_app_assertion
from .web_executor import build_driver, evaluate_web_assertion, get_driver_log


class InteractiveSessionError(RuntimeError):
    pass


SENSITIVE_KEYS = {"authorization", "cookie", "set-cookie", "password", "secret", "token", "api_key", "apikey"}


def _redact(value: Any, key: str = "") -> Any:
    if any(token in key.lower() for token in SENSITIVE_KEYS):
        return "***"
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item, key) for item in value]
    text = str(value) if value is not None else value
    return text[:4000] if isinstance(text, str) else text


def _safe_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        query = []
        for key, item in parse_qsl(parts.query, keep_blank_values=True):
            query.append((key, "***" if any(token in key.lower() for token in SENSITIVE_KEYS) else item))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
    except Exception:
        return value[:1000]


def _redact_body(value: Any) -> Any:
    if value in (None, ""):
        return value
    if not isinstance(value, str):
        return _redact(value)
    try:
        return json.dumps(_redact(json.loads(value)), ensure_ascii=False)[:4000]
    except (TypeError, ValueError, json.JSONDecodeError):
        return value[:4000]


def _screenshot(driver: Any, evidence_dir: Path, prefix: str) -> dict[str, str]:
    folder = evidence_dir / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    filename = f"{prefix}-{uuid4().hex}.png"
    target = folder / filename
    try:
        target.write_bytes(driver.get_screenshot_as_png())
    except Exception as exc:
        return {"error": f"截图失败：{type(exc).__name__}: {str(exc)[:300]}"}
    return {"path": str(target), "relative_path": f"screenshots/{filename}", "filename": filename}


class InteractiveSessionGateway:
    """Keep live Selenium/Appium sessions and collect deterministic evidence.

    Database state and reports remain the source of truth.  The in-memory map
    only owns active driver objects, which cannot be serialized safely.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._sessions: dict[int, dict[str, Any]] = {}

    def start_browser(self, run_id: int, base_url: str, settings: dict[str, Any]) -> dict[str, Any]:
        browser_settings = {**settings, "headless": False}
        driver = None
        try:
            driver = build_driver(browser_settings)
            try:
                driver.execute_cdp_cmd("Network.enable", {})
            except Exception:
                pass
            driver.get(base_url)
        except Exception as exc:
            try:
                if driver is not None:
                    driver.quit()
            except Exception:
                pass
            raise InteractiveSessionError(f"无法创建人工浏览器会话：{type(exc).__name__}: {str(exc)[:500]}") from exc
        context = {"channel": "browser", "driver": driver, "requests": {}}
        with self._lock:
            self._sessions[run_id] = context
        return {"connected": True, "channel": "browser", "session_id": getattr(driver, "session_id", None)}

    def start_app(self, run_id: int, settings: dict[str, Any]) -> dict[str, Any]:
        try:
            driver = create_app_driver(settings)
        except Exception as exc:
            raise InteractiveSessionError(f"无法创建人工 App 会话：{type(exc).__name__}: {str(exc)[:500]}") from exc
        with self._lock:
            self._sessions[run_id] = {"channel": "app", "driver": driver, "requests": {}}
        return {"connected": True, "channel": "app", "session_id": getattr(driver, "session_id", None)}

    def is_connected(self, run_id: int) -> bool:
        with self._lock:
            return run_id in self._sessions

    def close(self, run_id: int) -> None:
        with self._lock:
            context = self._sessions.pop(run_id, None)
        if not context:
            return
        try:
            context["driver"].quit()
        except Exception:
            pass

    def close_all(self) -> None:
        with self._lock:
            run_ids = list(self._sessions)
        for run_id in run_ids:
            self.close(run_id)

    def capture(
        self,
        run_id: int,
        case: Any,
        step_no: int | None,
        evidence_dir: str,
    ) -> dict[str, Any]:
        with self._lock:
            context = self._sessions.get(run_id)
        if not context:
            raise InteractiveSessionError("交互驱动未连接或后端已重启，请结束当前会话后重新开始")
        if context["channel"] == "browser":
            return self._capture_browser(context, case, step_no, Path(evidence_dir))
        return self._capture_app(context, case, step_no, Path(evidence_dir))

    def _capture_browser(self, context: dict[str, Any], case: Any, step_no: int | None, evidence_dir: Path) -> dict[str, Any]:
        driver = context["driver"]
        screenshot = _screenshot(driver, evidence_dir, f"step-{step_no or 'explore'}")
        try:
            page = {"url": _safe_url(driver.current_url), "title": str(driver.title or "")[:500]}
        except Exception as exc:
            page = {"error": f"读取页面状态失败：{type(exc).__name__}: {str(exc)[:300]}"}
        console = []
        try:
            console = [
                {"level": item.get("level"), "message": str(item.get("message", ""))[:1000]}
                for item in get_driver_log(driver, "browser")[-100:]
            ]
        except Exception:
            pass
        network = self._browser_network(driver, context)
        assertions = []
        for assertion in self._assertions_for_step(case, step_no):
            typ = str(assertion.get("type") or "").lower()
            if typ in {"status_code", "api_status", "response_status", "response_contains", "request_seen"}:
                assertions.append(self._evaluate_network_assertion(assertion, network))
            else:
                assertions.append(evaluate_web_assertion(assertion, driver))
        return {
            "channel": "browser",
            "page": page,
            "network": network,
            "console_logs": console,
            "device_logs": [],
            "assertions": assertions,
            "screenshot": screenshot,
        }

    def _capture_app(self, context: dict[str, Any], case: Any, step_no: int | None, evidence_dir: Path) -> dict[str, Any]:
        driver = context["driver"]
        screenshot = _screenshot(driver, evidence_dir, f"device-step-{step_no or 'explore'}")
        device = {}
        for attr, key in (("current_package", "package"), ("current_activity", "activity"), ("orientation", "orientation")):
            try:
                device[key] = getattr(driver, attr)
            except Exception:
                pass
        logs = []
        try:
            logs = [
                {"level": item.get("level"), "message": str(item.get("message", ""))[:1000]}
                for item in get_driver_log(driver, "logcat")[-100:]
            ]
        except Exception:
            pass
        assertions = [evaluate_app_assertion(item, driver) for item in self._assertions_for_step(case, step_no)]
        return {
            "channel": "app",
            "device": device,
            "network": [],
            "console_logs": [],
            "device_logs": logs,
            "assertions": assertions,
            "screenshot": screenshot,
        }

    @staticmethod
    def _assertions_for_step(case: Any, step_no: int | None) -> list[dict[str, Any]]:
        assertions = getattr(case, "assertions", None) or []
        return [
            dict(item)
            for item in assertions
            if isinstance(item, dict) and (item.get("step_no") in (None, step_no))
        ]

    def _browser_network(self, driver: Any, context: dict[str, Any]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        try:
            raw_entries = get_driver_log(driver, "performance")
        except Exception:
            return events
        requests = context.setdefault("requests", {})
        for raw in raw_entries[-500:]:
            try:
                message = json.loads(raw.get("message", "{}"))["message"]
                method = message.get("method")
                params = message.get("params") or {}
                request_id = params.get("requestId")
                if method == "Network.requestWillBeSent":
                    request = params.get("request") or {}
                    requests[request_id] = {
                        "request_id": request_id,
                        "method": request.get("method"),
                        "url": _safe_url(str(request.get("url") or "")),
                        "request_headers": _redact(request.get("headers") or {}),
                        "request_body": _redact_body(request.get("postData")),
                    }
                elif method == "Network.responseReceived":
                    response = params.get("response") or {}
                    item = {
                        **requests.get(request_id, {}),
                        "request_id": request_id,
                        "url": _safe_url(str(response.get("url") or requests.get(request_id, {}).get("url") or "")),
                        "status_code": int(response.get("status") or 0),
                        "status_text": str(response.get("statusText") or "")[:300],
                        "mime_type": response.get("mimeType"),
                        "response_headers": _redact(response.get("headers") or {}),
                    }
                    if str(item.get("mime_type") or "").lower().startswith(("application/json", "text/")):
                        try:
                            body = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": request_id}).get("body", "")
                            item["response_body"] = _redact_body(body)
                        except Exception:
                            pass
                    events.append(item)
            except Exception:
                continue
        return events[-100:]

    @staticmethod
    def _evaluate_network_assertion(assertion: dict[str, Any], network: list[dict[str, Any]]) -> dict[str, Any]:
        typ = str(assertion.get("type") or "").lower()
        expected = assertion.get("expected")
        contains = str(assertion.get("url_contains") or assertion.get("path") or "")
        matches = [item for item in network if not contains or contains in str(item.get("url") or "")]
        latest = matches[-1] if matches else {}
        actual: Any = None
        if typ in {"status_code", "api_status", "response_status"}:
            actual = latest.get("status_code")
            try:
                passed = actual == int(expected)
            except (TypeError, ValueError):
                passed = False
        elif typ == "response_contains":
            actual = latest.get("response_body")
            passed = str(expected or "") in str(actual or "")
        else:
            actual = latest.get("url")
            passed = bool(matches)
        return {
            "type": typ,
            "expected": expected if typ != "request_seen" else contains,
            "actual": actual,
            "passed": passed,
            "error": "" if matches else "本次检查点未采集到匹配的接口事件",
            "source": "network",
            "url_contains": contains,
        }


interactive_gateway = InteractiveSessionGateway()
