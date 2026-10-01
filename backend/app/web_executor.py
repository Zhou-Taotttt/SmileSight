from __future__ import annotations
import base64
import os
import time
from pathlib import Path
from urllib.parse import urljoin

# Selenium 只在真正启动浏览器时才导入，保证未安装/未部署浏览器时后端仍可正常启动、
# 其它测试类型不受影响，Web 用例执行则以“环境未就绪”的可判定失败返回。
SELENIUM_URL = os.getenv("SELENIUM_REMOTE_URL", "")

BY_ALIASES = {
    "css": "css selector",
    "css_selector": "css selector",
    "xpath": "xpath",
    "id": "id",
    "name": "name",
    "class": "class name",
    "class_name": "class name",
    "tag": "tag name",
    "link_text": "link text",
    "partial_link_text": "partial link text",
}


def friendly_web_error(exc, step=None):
    """Normalize Selenium's noisy driver errors into actionable Chinese text.

    Selenium's timeout exceptions often stringify to ``Message:`` followed by
    a long ChromeDriver stack trace.  That output is useful for debugging but
    not for a test report, so expose the failed action, locator and timeout in
    a stable human-readable message while retaining the exception class.
    """

    step = step or {}
    action = str(step.get("action") or "操作").lower()
    by = str(step.get("by") or "css")
    selector = step.get("selector") or ""
    timeout = step.get("timeout", 10)
    name = exc.__class__.__name__
    raw = str(exc or "").strip()
    lower = raw.lower()
    locator = f"{by}={selector}" if selector else "未提供定位器"
    if "timeout" in name.lower() or "timed out" in lower or "timeoutexception" in lower:
        if action in {"click", "tap"}:
            reason = "元素未找到或不可点击"
        elif action in {"input", "type", "fill"}:
            reason = "输入元素未找到或不可用"
        elif action in {"wait_for", "wait_element"}:
            reason = "等待的元素未出现"
        else:
            reason = "元素未在规定时间内出现"
        return f"{reason}（定位器：{locator}，等待 {timeout}s）"
    if "nosuchelement" in name.lower() or "no such element" in lower:
        return f"未找到元素（定位器：{locator}）"
    if "elementnotinteractable" in name.lower() or "not interactable" in lower:
        return f"元素存在但不可交互（定位器：{locator}）"
    if "elementclickintercepted" in name.lower() or "click intercepted" in lower:
        return f"点击被其他元素遮挡（定位器：{locator}）"
    if raw and not raw.startswith("Message:"):
        return f"{raw[:500]}（定位器：{locator}）"
    return f"Web 操作失败（定位器：{locator}，异常：{name}）"


def render(value, variables):
    if not isinstance(value, str):
        return value
    for key, val in (variables or {}).items():
        value = value.replace("{" + key + "}", str(val))
    return value


def resolve_url(target, base_url):
    target = (target or "").strip()
    if not target:
        return base_url
    if target.startswith("http://") or target.startswith("https://"):
        return target
    return urljoin(base_url.rstrip("/") + "/", target.lstrip("/"))


def _by(kind):
    return BY_ALIASES.get((kind or "css").lower(), "css selector")


def build_driver(settings=None):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    settings = settings or {}
    options = Options()
    if settings.get("headless", True):
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1280,900")
    options.set_capability("goog:loggingPrefs", {"browser": "ALL"})
    remote_url = settings.get("selenium_remote_url") or SELENIUM_URL
    if remote_url:
        return webdriver.Remote(command_executor=remote_url, options=options)
    return webdriver.Chrome(options=options)


def execute_web_case(case, base_url, variables=None, evidence_dir=None):
    """执行一个 Web 自动化用例：驱动浏览器完成步骤，采集截图与控制台日志，
    再对页面/元素做确定性断言。返回结构与接口执行器一致，可复用 TestRun/TestResult。"""
    variables = variables or {}
    base_url = (base_url or "").rstrip("/")
    started = time.perf_counter()
    request_info = {"base_url": base_url, "steps": [s for s in (case.steps or [])]}

    try:
        # Keep the zero-argument call compatible with lightweight test doubles
        # and local callers that do not provide project settings.
        driver = build_driver(variables) if variables else build_driver()
    except Exception as exc:
        return {
            "status": "error",
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "request": request_info,
            "response": {},
            "assertions": [],
            "summary": "无法启动浏览器（Selenium 环境未就绪）",
            "failure": {"category": "environment_or_driver_error", "summary": str(exc)},
        }

    step_log = []
    try:
        for index, step in enumerate(case.steps or [], start=1):
            step_no = step.get("step_no", index)
            action = (step.get("action") or "").lower()
            entry = {"step_no": step_no, "action": action, "status": "passed", "detail": ""}
            try:
                run_step(driver, action, step, base_url, variables)
                entry["detail"] = describe_step(action, step, base_url, variables)
            except Exception as exc:
                error_step = {
                    **step,
                    "action": action,
                    "timeout": step.get("timeout", variables.get("default_timeout_seconds", 10)),
                }
                entry["status"] = "failed"
                entry["detail"] = friendly_web_error(exc, error_step)
                step_log.append(entry)
                return finalize(
                    driver, case, request_info, step_log, started, evidence_dir,
                    status="failed", summary=f"步骤 {step_no} 执行失败：{action}",
                    failure={
                        "category": "web_step_failed",
                        "summary": friendly_web_error(exc, error_step),
                        "step_no": step_no,
                        "action": action,
                        "locator": {"by": step.get("by") or "css", "selector": step.get("selector")},
                    },
                )
            step_log.append(entry)

        assertions = [evaluate_web_assertion(a, driver) for a in case.assertions or []]
        passed = all(a["passed"] for a in assertions) if assertions else True
        return finalize(
            driver, case, request_info, step_log, started, evidence_dir,
            status="passed" if passed else "failed",
            summary="页面断言全部通过" if passed else "页面断言失败",
            failure={} if passed else {"category": "assertion_failed", "summary": "页面未满足断言"},
            assertions=assertions,
        )
    except Exception as exc:
        return finalize(
            driver, case, request_info, step_log, started, evidence_dir,
            status="error", summary="Web 用例执行异常",
            failure={"category": "web_execution_error", "summary": str(exc)},
        )


def finalize(driver, case, request_info, step_log, started, evidence_dir, status, summary, failure, assertions=None):
    response = {}
    try:
        response["final_url"] = driver.current_url
        response["title"] = driver.title
    except Exception:
        pass
    response["screenshot"] = capture_screenshot(driver, case, evidence_dir)
    response["console"] = capture_console(driver)
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


def run_step(driver, action, step, base_url, variables):
    from selenium.webdriver.common.by import By  # noqa: F401  (触发驱动就绪)
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    by = _by(step.get("by"))
    selector = render(step.get("selector"), variables)
    value = render(step.get("value"), variables)
    timeout = float(step.get("timeout", variables.get("default_timeout_seconds", 10)))

    if action in ("navigate", "open", "goto"):
        driver.get(resolve_url(render(step.get("target") or value, variables), base_url))
    elif action in ("click",):
        el = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((by, selector)))
        el.click()
    elif action in ("input", "type", "fill"):
        el = WebDriverWait(driver, timeout).until(EC.presence_of_element_located((by, selector)))
        el.clear()
        el.send_keys(value or "")
    elif action in ("wait", "sleep"):
        time.sleep(float(value or step.get("seconds") or 1))
    elif action in ("wait_for", "wait_element"):
        WebDriverWait(driver, timeout).until(EC.presence_of_element_located((by, selector)))
    elif action in ("submit",):
        el = WebDriverWait(driver, timeout).until(EC.presence_of_element_located((by, selector)))
        el.submit()
    elif action in ("script", "execute_script"):
        driver.execute_script(value or step.get("script") or "")
    else:
        raise ValueError(f"unsupported web step action: {action}")


def describe_step(action, step, base_url, variables):
    if action in ("navigate", "open", "goto"):
        return f"打开 {resolve_url(render(step.get('target') or step.get('value'), variables), base_url)}"
    if action in ("input", "type", "fill"):
        return f"在 {step.get('selector')} 输入内容"
    if action in ("wait", "sleep"):
        return f"等待 {step.get('value') or step.get('seconds') or 1}s"
    return f"{action} {step.get('selector') or ''}".strip()


def capture_screenshot(driver, case, evidence_dir):
    try:
        png = driver.get_screenshot_as_png()
    except Exception:
        return ""
    if evidence_dir:
        folder = Path(evidence_dir)
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{getattr(case, 'case_key', 'web')}.png"
        (folder / name).write_bytes(png)
        return name
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def capture_console(driver):
    try:
        logs = driver.get_log("browser")
    except Exception:
        return []
    return [{"level": entry.get("level"), "message": str(entry.get("message"))[:500]} for entry in logs[-50:]]


def evaluate_web_assertion(assertion, driver):
    typ = assertion.get("type")
    expected = assertion.get("expected")
    actual, passed, error = None, False, ""
    try:
        if typ == "url_contains":
            actual = driver.current_url
            passed = str(expected or "") in (actual or "")
        elif typ == "title_contains":
            actual = driver.title
            passed = str(expected or "") in (actual or "")
        elif typ in ("element_present", "element_visible"):
            elements = _find(driver, assertion)
            actual = len(elements)
            passed = actual > 0 and (typ != "element_visible" or elements[0].is_displayed())
        elif typ == "element_text":
            elements = _find(driver, assertion)
            actual = elements[0].text if elements else None
            passed = bool(elements) and str(expected or "") in (actual or "")
        elif typ == "element_count":
            actual = len(_find(driver, assertion))
            passed = actual == expected
        else:
            error = f"unsupported web assertion type: {typ}"
    except Exception as exc:
        error = str(exc)
    return {"type": typ, "expected": expected, "actual": actual, "passed": passed, "error": error}


def _find(driver, assertion):
    selector = assertion.get("selector")
    if not selector:
        raise ValueError("断言缺少选择器（selector）")
    return driver.find_elements(_by(assertion.get("by")), selector)
