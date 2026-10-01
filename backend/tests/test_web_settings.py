from app import web_executor


def test_web_executor_forwards_project_settings(monkeypatch, tmp_path):
    observed = {}

    class FakeDriver:
        current_url = "http://example.test/"
        title = "Example"

        def get_screenshot_as_png(self):
            return b"png"

        def get_log(self, _kind):
            return []

        def quit(self):
            observed["quit"] = True

    def fake_build_driver(settings=None):
        observed["settings"] = settings
        return FakeDriver()

    def fake_run_step(_driver, _action, step, _base_url, variables):
        observed["step_timeout"] = variables["default_timeout_seconds"]
        observed["step"] = step

    monkeypatch.setattr(web_executor, "build_driver", fake_build_driver)
    monkeypatch.setattr(web_executor, "run_step", fake_run_step)

    case = type(
        "Case",
        (),
        {
            "case_key": "WEB-SETTINGS",
            "steps": [{"action": "navigate", "value": "/"}],
            "assertions": [],
        },
    )()
    result = web_executor.execute_web_case(
        case,
        "http://example.test",
        variables={
            "selenium_remote_url": "http://selenium:4444/wd/hub",
            "headless": False,
            "default_timeout_seconds": 18,
        },
        evidence_dir=str(tmp_path),
    )

    assert result["status"] == "passed"
    assert observed["settings"] == {
        "selenium_remote_url": "http://selenium:4444/wd/hub",
        "headless": False,
        "default_timeout_seconds": 18,
    }
    assert observed["step_timeout"] == 18
    assert observed["quit"] is True
