"""Small, deterministic Appium executor used by the SmileSight App MVP.

The module deliberately does not import Appium at module import time.  The
backend can therefore start when the optional Appium client, server, or an
Android device is not installed.  In that situation ``execute_app_case``
returns the same normalized result shape used by the Web executor, with an
``environment_or_device_error`` failure category.

The executor is intentionally conservative: it executes explicit steps and
evaluates explicit assertions.  It does not infer selectors or turn an AI
draft into a passing result.
"""

from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any


DEFAULT_APPIUM_SERVER_URL = "http://127.0.0.1:4723"


class _EnvironmentOrDeviceError(RuntimeError):
    """Internal exception used to normalize setup failures."""


def _value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _case_key(case: Any) -> str:
    return str(_value(case, "case_key", "app")) or "app"


def _case_steps(case: Any) -> list[dict[str, Any]]:
    steps = _value(case, "steps", []) or []
    return [dict(step) if isinstance(step, Mapping) else {"action": str(step)} for step in steps]


def _case_assertions(case: Any) -> list[dict[str, Any]]:
    assertions = _value(case, "assertions", []) or []
    return [dict(assertion) if isinstance(assertion, Mapping) else {"type": str(assertion)} for assertion in assertions]


def _merge_config(capabilities: Any, config: Any) -> dict[str, Any]:
    """Accept either a capabilities dict or a config dict.

    Callers from the API layer commonly pass one dictionary, while device
    integrations sometimes pass ``config={capabilities: {...}}``.  Supporting
    both here keeps the executor independent from the HTTP/schema layer.
    """

    merged: dict[str, Any] = {}
    for source in (config, capabilities):
        if isinstance(source, Mapping):
            merged.update(source)
    nested = merged.get("capabilities")
    if isinstance(nested, Mapping):
        flattened = dict(nested)
        flattened.update({k: v for k, v in merged.items() if k != "capabilities"})
        merged = flattened
    return merged


def _server_url(settings: Mapping[str, Any]) -> str:
    value = (
        settings.get("server_url")
        or settings.get("appium_server_url")
        or settings.get("remote_url")
        or os.getenv("APPIUM_SERVER_URL")
        or os.getenv("APPIUM_URL")
        or DEFAULT_APPIUM_SERVER_URL
    )
    return str(value).rstrip("/")


def _redact(value: Any, key: str = "") -> Any:
    """Redact likely credentials before putting settings in execution evidence."""

    if isinstance(value, Mapping):
        return {str(k): _redact(v, str(k)) for k, v in value.items() if str(k) not in {"capabilities"}}
    if isinstance(value, (list, tuple)):
        return [_redact(item, key) for item in value]
    if any(token in key.lower() for token in ("password", "secret", "token", "key")):
        return "***"
    return value


def _friendly_error(exc: BaseException) -> str:
    """Return a short actionable error without exposing a Python traceback."""

    message = str(exc).strip().replace("\r", " ").replace("\n", " ")
    if len(message) > 500:
        message = message[:497] + "..."
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _import_appium():
    try:
        from appium import webdriver  # type: ignore[import-not-found]
    except ImportError as exc:
        raise _EnvironmentOrDeviceError(
            "未安装 Appium Python 客户端，请安装 Appium-Python-Client"
        ) from exc
    return webdriver


def _build_options(settings: Mapping[str, Any]):
    """Build Android/iOS options when available, with a generic fallback."""

    caps = {
        str(key): value
        for key, value in settings.items()
        if key not in {
            "server_url",
            "appium_server_url",
            "remote_url",
            "capabilities",
            "appium",
            "timeout",
            # These keys are SmileSight execution settings rather than W3C
            # capabilities.  Passing them through can make Appium reject an
            # otherwise valid session as an unknown capability.
            "variables",
        }
    }
    # Accept the snake_case names used by the project environment while
    # emitting the standard capability names expected by Appium.
    for source, target in (
        ("platform_name", "platformName"),
        ("device_name", "deviceName"),
        ("automation_name", "automationName"),
        ("app_package", "appPackage"),
        ("app_activity", "appActivity"),
    ):
        if source in caps and target not in caps:
            caps[target] = caps.pop(source)
        elif source in caps:
            # Prefer an explicitly supplied standard capability when both
            # spellings are present; never send the project-only snake_case
            # alias to Appium as an unknown capability.
            caps.pop(source)
    platform = str(caps.get("platformName", caps.get("platform_name", "Android"))).lower()
    # Appium Python Client 2/3 expose platform-specific option classes.  Keep
    # this import lazy and use a plain capability dictionary if an older client
    # does not provide the class.
    try:
        if platform == "ios":
            from appium.options.ios import XCUITestOptions  # type: ignore[import-not-found]

            options = XCUITestOptions()
        else:
            from appium.options.android import UiAutomator2Options  # type: ignore[import-not-found]

            options = UiAutomator2Options()
        options.load_capabilities(caps)
        return options, caps
    except (ImportError, AttributeError):
        return None, caps


def _create_driver(settings: Mapping[str, Any]):
    webdriver = _import_appium()
    server_url = _server_url(settings)
    options, caps = _build_options(settings)
    try:
        if options is not None:
            return webdriver.Remote(command_executor=server_url, options=options)
        # Compatibility path for Appium Python Client 1.x and test doubles.
        return webdriver.Remote(command_executor=server_url, desired_capabilities=caps)
    except TypeError:
        # Some client versions reject ``desired_capabilities`` but accept a
        # plain ``options`` object; retrying here keeps setup compatibility
        # without hiding connection errors.
        if options is None:
            raise
        return webdriver.Remote(command_executor=server_url, options=options)
    except Exception as exc:
        raise _EnvironmentOrDeviceError(
            f"无法连接 Appium 服务或设备（{server_url}）：{_friendly_error(exc)}"
        ) from exc


def _find(driver: Any, step_or_assertion: Mapping[str, Any]):
    selector = step_or_assertion.get("selector")
    if selector in (None, ""):
        raise ValueError("操作或断言缺少选择器（selector）")
    raw_by = str(step_or_assertion.get("by") or "id").strip().lower()
    by = _locator_strategy(raw_by)
    selector_value = str(selector)
    # ``text`` is a friendly project-level alias.  Appium's Android driver
    # expects a UiSelector expression for the corresponding locator strategy.
    if raw_by == "text" and not selector_value.lstrip().startswith("new UiSelector"):
        selector_value = f"new UiSelector().text({json.dumps(selector_value, ensure_ascii=False)})"

    find_elements = getattr(driver, "find_elements", None)
    if callable(find_elements):
        found = find_elements(by, selector_value)
        if found is None:
            return []
        if isinstance(found, (list, tuple)):
            return list(found)
        # A small number of test doubles return one element instead of the
        # list mandated by WebDriver.  Normalize that shape for callers.
        try:
            return list(found)
        except TypeError:
            return [found]

    # Keep compatibility with minimal Appium/test doubles that expose only
    # ``find_element``.  Missing-element exceptions are normalized to an empty
    # list so assertions can report a deterministic false result.
    find_element = getattr(driver, "find_element", None)
    if callable(find_element):
        try:
            element = find_element(by, selector_value)
        except Exception as exc:
            if isinstance(exc, LookupError) or exc.__class__.__name__ in {
                "NoSuchElementException",
                "NoSuchElementError",
                "ElementNotFoundError",
            }:
                return []
            raise
        return [] if element is None else [element]
    raise ValueError("当前驱动不支持元素定位")


def _find_one(driver: Any, step: Mapping[str, Any]):
    elements = _find(driver, step)
    if not elements:
        raise ValueError(f"未找到元素：{step.get('selector')}")
    return elements[0]


def _locator_strategy(value: Any) -> str:
    strategy = str(value or "id").strip().lower()
    aliases = {
        "accessibility": "accessibility id",
        "accessibility_id": "accessibility id",
        "content-desc": "accessibility id",
        "content_description": "accessibility id",
        "class_name": "class name",
        "uiautomator": "-android uiautomator",
        "android_uiautomator": "-android uiautomator",
        "android ui automator": "-android uiautomator",
        "text": "-android uiautomator",
    }
    return aliases.get(strategy, strategy)


def _render(value: Any, variables: Mapping[str, Any]) -> Any:
    if not isinstance(value, str):
        return value
    for key, item in variables.items():
        value = value.replace("{" + str(key) + "}", str(item))
    return value


def _settings_variables(settings: Mapping[str, Any]) -> Mapping[str, Any]:
    variables = settings.get("variables")
    return variables if isinstance(variables, Mapping) else {}


def _activate(driver: Any, step: Mapping[str, Any], settings: Mapping[str, Any]) -> str:
    package = _render(
        step.get("package")
        or step.get("app_package")
        or settings.get("app_package")
        or settings.get("appPackage"),
        _settings_variables(settings),
    )
    activity = _render(
        step.get("activity")
        or step.get("app_activity")
        or settings.get("app_activity")
        or settings.get("appActivity"),
        _settings_variables(settings),
    )
    result = ""
    if package and activity and hasattr(driver, "start_activity"):
        driver.start_activity(str(package), str(activity))
        result = f"启动 {package}/{activity}"
    elif package and hasattr(driver, "activate_app"):
        driver.activate_app(str(package))
        result = f"激活 {package}"
    elif hasattr(driver, "launch_app"):
        driver.launch_app()
        result = "启动当前应用"
    else:
        raise ValueError("缺少 app_package/app_activity，且驱动不支持 launch_app")

    # Starting/activating an Android app returns before the first frame is
    # necessarily rendered.  A generated App case often contains only this
    # step, so an immediate final screenshot would capture a white loading
    # surface.  Allow projects to tune the delay while keeping a safe default.
    try:
        settle_seconds = float(
            step.get("wait_after", settings.get("launch_wait_seconds", 2.5)) or 0
        )
    except (TypeError, ValueError):
        settle_seconds = 2.5
    if settle_seconds > 0:
        time.sleep(min(settle_seconds, 30))
    return result


def _tap_coordinates(driver: Any, x: Any, y: Any) -> str:
    if x is None or y is None:
        raise ValueError("坐标点击需要 x 和 y")
    point = (int(float(x)), int(float(y)))
    if hasattr(driver, "tap"):
        driver.tap([point])
    elif hasattr(driver, "execute_script"):
        driver.execute_script("mobile: clickGesture", {"x": point[0], "y": point[1]})
    else:
        raise ValueError("当前驱动不支持坐标点击")
    return f"点击坐标 ({point[0]}, {point[1]})"


def _swipe(driver: Any, step: Mapping[str, Any]) -> str:
    start_x = step.get("start_x", step.get("x1"))
    start_y = step.get("start_y", step.get("y1"))
    end_x = step.get("end_x", step.get("x2"))
    end_y = step.get("end_y", step.get("y2"))
    if None in (start_x, start_y, end_x, end_y):
        raise ValueError("滑动需要 start_x/start_y/end_x/end_y 坐标")
    duration = int(float(step.get("duration", step.get("duration_ms", 500))))
    values = (int(float(start_x)), int(float(start_y)), int(float(end_x)), int(float(end_y)))
    if hasattr(driver, "swipe"):
        driver.swipe(*values, duration=duration)
    elif hasattr(driver, "execute_script"):
        driver.execute_script(
            "mobile: swipeGesture",
            {
                "left": min(values[0], values[2]),
                "top": min(values[1], values[3]),
                "width": abs(values[2] - values[0]) or 1,
                "height": abs(values[3] - values[1]) or 1,
                "direction": "up" if values[3] < values[1] else "down",
                "percent": 0.8,
            },
        )
    else:
        raise ValueError("当前驱动不支持滑动")
    return f"滑动 ({values[0]}, {values[1]}) → ({values[2]}, {values[3]})"


def _run_step(driver: Any, action: str, step: Mapping[str, Any], settings: Mapping[str, Any]) -> str:
    variables = _settings_variables(settings)
    if action in {"launch", "activate", "open", "start", "launch_app", "activate_app"}:
        return _activate(driver, step, settings)
    if action in {"tap", "click"}:
        if step.get("x") is not None or step.get("y") is not None:
            return _tap_coordinates(driver, step.get("x"), step.get("y"))
        element = _find_one(driver, {**step, "selector": _render(step.get("selector"), variables)})
        element.click()
        return f"点击元素 {step.get('selector')}"
    if action in {"input", "type", "fill", "send_keys"}:
        element = _find_one(driver, {**step, "selector": _render(step.get("selector"), variables)})
        if step.get("clear", True) and hasattr(element, "clear"):
            element.clear()
        value = _render(step.get("value", step.get("text", "")), variables)
        if hasattr(element, "send_keys"):
            element.send_keys("" if value is None else str(value))
        elif hasattr(element, "set_value"):
            element.set_value("" if value is None else str(value))
        else:
            raise ValueError("当前元素不支持输入")
        return f"在 {step.get('selector')} 输入内容"
    if action in {"swipe", "scroll"}:
        return _swipe(driver, step)
    if action in {"wait", "sleep"}:
        seconds = float(step.get("seconds", step.get("value", 1)) or 1)
        time.sleep(max(0, seconds))
        return f"等待 {seconds:g}s"
    if action in {"wait_for", "wait_element", "element_wait"}:
        timeout = float(step.get("timeout", 10))
        deadline = time.monotonic() + max(0, timeout)
        while True:
            if _find(driver, {**step, "selector": _render(step.get("selector"), variables)}):
                return f"等待元素 {step.get('selector')} 出现"
            if time.monotonic() >= deadline:
                raise TimeoutError(f"等待元素超时：{step.get('selector')}")
            time.sleep(0.2)
    if action in {"screenshot", "capture_screenshot"}:
        return "已请求截图"
    raise ValueError(f"unsupported app step action: {action}")


def _evaluate_assertion(assertion: Mapping[str, Any], driver: Any) -> dict[str, Any]:
    typ = str(assertion.get("type", "")).strip().lower()
    expected = assertion.get("expected")
    actual: Any = None
    passed = False
    error = ""
    try:
        if typ in {"element_present", "element_exists", "present"}:
            elements = _find(driver, assertion)
            actual = len(elements)
            passed = actual > 0
        elif typ in {"element_visible", "visible"}:
            elements = _find(driver, assertion)
            actual = bool(elements and elements[0].is_displayed())
            passed = actual is True
        elif typ in {"element_text", "text_contains", "text"}:
            elements = _find(driver, assertion)
            actual = elements[0].text if elements else None
            passed = bool(elements) and str(expected or "") in str(actual or "")
        else:
            error = f"unsupported app assertion type: {typ}"
    except Exception as exc:
        error = _friendly_error(exc)
    return {"type": typ, "expected": expected, "actual": actual, "passed": passed, "error": error}


def _capture_screenshot(driver: Any, case_key: str, evidence_dir: str | os.PathLike[str] | None, suffix: str = "") -> str:
    try:
        png = driver.get_screenshot_as_png()
    except Exception:
        return ""
    if evidence_dir:
        folder = Path(evidence_dir)
        folder.mkdir(parents=True, exist_ok=True)
        filename = f"{case_key}{suffix}.png"
        (folder / filename).write_bytes(png)
        return filename
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def _collect_device_logs(driver: Any) -> list[dict[str, Any]]:
    try:
        entries = driver.get_log("logcat")
    except Exception:
        return []
    normalized: list[dict[str, Any]] = []
    for entry in entries[-100:]:
        if isinstance(entry, Mapping):
            normalized.append(
                {
                    "timestamp": entry.get("timestamp"),
                    "level": entry.get("level") or entry.get("priority"),
                    "message": str(entry.get("message", ""))[:1000],
                }
            )
        else:
            normalized.append({"message": str(entry)[:1000]})
    return normalized


def _finalize(
    driver: Any,
    case: Any,
    request_info: dict[str, Any],
    step_log: list[dict[str, Any]],
    started: float,
    evidence_dir: str | os.PathLike[str] | None,
    status: str,
    summary: str,
    failure: dict[str, Any],
    assertions: list[dict[str, Any]] | None = None,
    screenshots: list[str] | None = None,
) -> dict[str, Any]:
    case_key = _case_key(case)
    screenshots = list(screenshots or [])
    # Give the last UI action a brief render window before collecting the
    # final evidence image.  This is especially important for hybrid apps and
    # launch-only smoke cases.
    try:
        settle_seconds = float(
            request_info.get("screenshot_wait_seconds", 0.8)
        )
    except (TypeError, ValueError):
        settle_seconds = 0.8
    if settle_seconds > 0:
        time.sleep(min(settle_seconds, 10))
    final_screenshot = _capture_screenshot(driver, case_key, evidence_dir, "-final")
    if final_screenshot:
        screenshots.append(final_screenshot)
    response: dict[str, Any] = {"screenshots": screenshots, "device_logs": _collect_device_logs(driver)}
    try:
        response["session_id"] = getattr(driver, "session_id", None)
        response["current_package"] = getattr(driver, "current_package", None)
        response["current_activity"] = getattr(driver, "current_activity", None)
    except Exception:
        pass
    if final_screenshot:
        response["screenshot"] = final_screenshot
    request_info = {**request_info, "step_results": step_log}
    try:
        driver.quit()
    except Exception:
        pass
    return {
        "status": status,
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "request": request_info,
        "response": response,
        "assertions": assertions or [],
        "summary": summary,
        "failure": failure,
    }


def execute_app_case(
    case: Any,
    capabilities: Mapping[str, Any] | None = None,
    evidence_dir: str | os.PathLike[str] | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute one Appium case and return the normalized SmileSight result.

    ``capabilities`` may contain Appium capabilities directly or a nested
    ``capabilities`` object.  ``config`` is accepted as an alias so callers can
    pass a device configuration without coupling to a particular API schema.
    Setup failures never escape this function: they are returned as
    ``environment_or_device_error`` results.
    """

    started = time.perf_counter()
    settings = _merge_config(capabilities, config)
    server_url = _server_url(settings)
    request_info: dict[str, Any] = {
        "server_url": server_url,
        "capabilities": _redact(settings),
        "steps": _case_steps(case),
    }
    try:
        driver = _create_driver(settings)
    except _EnvironmentOrDeviceError as exc:
        message = str(exc)
        return {
            "status": "error",
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "request": request_info,
            "response": {"server_url": server_url, "device_logs": [], "screenshots": []},
            "assertions": [],
            "summary": message,
            "failure": {"category": "environment_or_device_error", "summary": message},
        }
    except Exception as exc:
        message = f"Appium 环境初始化失败：{_friendly_error(exc)}"
        return {
            "status": "error",
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "request": request_info,
            "response": {"server_url": server_url, "device_logs": [], "screenshots": []},
            "assertions": [],
            "summary": message,
            "failure": {"category": "environment_or_device_error", "summary": message},
        }

    if driver is None or not getattr(driver, "session_id", True):
        message = "Appium 未创建有效设备会话，请检查模拟器/真机和 Appium Server"
        try:
            driver.quit()
        except Exception:
            pass
        return {
            "status": "error",
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "request": request_info,
            "response": {"server_url": server_url, "device_logs": [], "screenshots": []},
            "assertions": [],
            "summary": message,
            "failure": {"category": "environment_or_device_error", "summary": message},
        }

    step_log: list[dict[str, Any]] = []
    screenshots: list[str] = []
    try:
        for index, step in enumerate(_case_steps(case), start=1):
            step_no = step.get("step_no", index)
            action = str(step.get("action", "")).strip().lower()
            entry = {"step_no": step_no, "action": action, "status": "passed", "detail": ""}
            try:
                entry["detail"] = _run_step(driver, action, step, settings)
                if action in {"screenshot", "capture_screenshot"}:
                    shot = _capture_screenshot(driver, _case_key(case), evidence_dir, f"-step-{step_no}")
                    if shot:
                        screenshots.append(shot)
                        entry["screenshot"] = shot
            except Exception as exc:
                entry["status"] = "failed"
                entry["detail"] = _friendly_error(exc)
                step_log.append(entry)
                return _finalize(
                    driver,
                    case,
                    request_info,
                    step_log,
                    started,
                    evidence_dir,
                    "failed",
                    f"步骤 {step_no} 执行失败：{action}",
                    {"category": "app_step_failed", "summary": _friendly_error(exc), "step_no": step_no},
                    screenshots=screenshots,
                )
            step_log.append(entry)

        assertions = [_evaluate_assertion(assertion, driver) for assertion in _case_assertions(case)]
        passed = all(item["passed"] for item in assertions) if assertions else True
        return _finalize(
            driver,
            case,
            request_info,
            step_log,
            started,
            evidence_dir,
            "passed" if passed else "failed",
            "App 断言全部通过" if passed else "App 断言失败",
            {} if passed else {"category": "assertion_failed", "summary": "App 页面未满足断言"},
            assertions=assertions,
            screenshots=screenshots,
        )
    except Exception as exc:
        return _finalize(
            driver,
            case,
            request_info,
            step_log,
            started,
            evidence_dir,
            "error",
            "App 用例执行异常",
            {"category": "app_execution_error", "summary": _friendly_error(exc)},
            screenshots=screenshots,
        )
