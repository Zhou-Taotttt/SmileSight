"""Unit coverage for the Appium executor without requiring a live device.

The real Appium server is an external dependency.  These tests exercise the
normalization and execution contract with a deliberately small WebDriver
double so regressions are caught in CI and in the backend container.
"""

from types import SimpleNamespace

import pytest

from app.app_executor import _build_options, _find, execute_app_case


class FakeElement:
    text = "欢迎登录"

    def __init__(self):
        self.clicked = 0
        self.cleared = 0
        self.values = []

    def click(self):
        self.clicked += 1

    def clear(self):
        self.cleared += 1

    def send_keys(self, value):
        self.values.append(value)

    def is_displayed(self):
        return True


class FindElementOnlyDriver:
    """Minimal driver matching older clients/test doubles."""

    session_id = "fake-session"
    current_package = "com.example.demo"
    current_activity = ".MainActivity"

    def __init__(self):
        self.element = FakeElement()
        self.find_calls = []
        self.started = []
        self.quit_called = False

    def find_element(self, by, selector):
        self.find_calls.append((by, selector))
        if selector == "missing":
            raise NoSuchElementError("not found")
        return self.element

    def start_activity(self, package, activity):
        self.started.append((package, activity))

    def get_screenshot_as_png(self):
        return b"fake-png"

    def get_log(self, _kind):
        return [{"timestamp": 1, "priority": "INFO", "message": "ready"}]

    def quit(self):
        self.quit_called = True


class NoSuchElementError(Exception):
    pass


def test_find_supports_find_element_only_driver_and_text_alias():
    driver = FindElementOnlyDriver()

    found = _find(driver, {"by": "text", "selector": "登录"})

    assert found == [driver.element]
    assert driver.find_calls == [
        ("-android uiautomator", 'new UiSelector().text("登录")')
    ]


def test_find_normalizes_missing_element_to_empty_list():
    driver = FindElementOnlyDriver()

    assert _find(driver, {"by": "id", "selector": "missing"}) == []


def test_build_options_maps_project_snake_case_and_drops_internal_settings():
    _options, caps = _build_options(
        {
            "platform_name": "Android",
            "device_name": "emulator-5554",
            "automation_name": "UiAutomator2",
            "app_package": "com.example.demo",
            "app_activity": ".MainActivity",
            "variables": {"user": "demo"},
            "appium": {"server_url": "http://nested-appium:4723"},
            "server_url": "http://appium:4723",
        }
    )

    assert caps["platformName"] == "Android"
    assert caps["deviceName"] == "emulator-5554"
    assert caps["automationName"] == "UiAutomator2"
    assert caps["appPackage"] == "com.example.demo"
    assert caps["appActivity"] == ".MainActivity"
    assert "platform_name" not in caps
    assert "device_name" not in caps
    assert "variables" not in caps
    assert "server_url" not in caps
    assert "appium" not in caps


def test_execute_app_case_works_with_find_element_only_driver(monkeypatch):
    driver = FindElementOnlyDriver()
    monkeypatch.setattr("app.app_executor._create_driver", lambda _settings: driver)
    case = SimpleNamespace(
        case_key="APP-1-001",
        steps=[
            {"action": "launch"},
            {"action": "tap", "by": "id", "selector": "login"},
            {"action": "input", "by": "id", "selector": "username", "value": "demo"},
            {"action": "screenshot"},
        ],
        assertions=[
            {"type": "element_present", "by": "id", "selector": "login"},
            {"type": "element_visible", "by": "id", "selector": "login"},
            {"type": "element_text", "by": "id", "selector": "login", "expected": "登录"},
        ],
    )

    result = execute_app_case(
        case,
        capabilities={
            "app_package": "com.example.demo",
            "app_activity": ".MainActivity",
        },
    )

    assert result["status"] == "passed"
    assert len(result["request"]["step_results"]) == 4
    assert all(item["passed"] for item in result["assertions"])
    assert driver.started == [("com.example.demo", ".MainActivity")]
    assert driver.element.clicked == 1
    assert driver.element.values == ["demo"]
    assert result["response"]["screenshots"][0].startswith("data:image/png;base64,")
    assert result["response"]["device_logs"][0]["message"] == "ready"
    assert driver.quit_called is True


def test_execute_app_case_reports_missing_selector_as_failed_step(monkeypatch):
    driver = FindElementOnlyDriver()
    monkeypatch.setattr("app.app_executor._create_driver", lambda _settings: driver)
    case = SimpleNamespace(
        case_key="APP-1-002",
        steps=[{"action": "tap", "selector": "missing"}],
        assertions=[],
    )

    result = execute_app_case(case, capabilities={})

    assert result["status"] == "failed"
    assert result["failure"]["category"] == "app_step_failed"
    assert result["failure"]["step_no"] == 1
    assert "未找到元素" in result["failure"]["summary"]
    assert driver.quit_called is True
