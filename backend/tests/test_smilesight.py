import json
from uuid import uuid4
from fastapi.testclient import TestClient
from app.main import app
import importlib

main_module = importlib.import_module("app.main")

def test_health():
    with TestClient(app) as client:
        response = client.get('/api/v1/health')
        assert response.status_code == 200
        assert response.json()['data']['status'] == 'healthy'

def test_openapi_import_and_generation():
    with TestClient(app) as client:
        project_id = f'demo-{uuid4().hex[:8]}'
        project = client.post('/api/v1/projects', json={'project_id': project_id, 'name': 'Demo'})
        assert project.status_code == 200
        spec = {'openapi': '3.0.0', 'info': {'title': 'Demo', 'version': '1'}, 'paths': {'/ping': {'get': {'tags': ['system'], 'responses': {'200': {'description': 'ok'}}}}}}
        result = client.post(f'/api/v1/projects/{project_id}/openapi/import', files={'file': ('openapi.json', json.dumps(spec), 'application/json')})
        assert result.status_code == 200
        assert result.json()['data']['endpoints_imported'] >= 1
        generated = client.post(f'/api/v1/projects/{project_id}/test-cases/generate')
        assert generated.status_code == 200
        assert client.get(f'/api/v1/projects/{project_id}/test-cases').json()['data']

def test_requirement_upload_and_preview():
    with TestClient(app) as client:
        project_id = f'requirements-{uuid4().hex[:8]}'
        project = client.post('/api/v1/projects', json={'project_id': project_id, 'name': 'Requirements Demo'})
        assert project.status_code == 200
        result = client.post(f'/api/v1/projects/{project_id}/inputs', data={'category': 'requirement'}, files={'file': ('requirement.md', '# Login\n\nUsers can sign in.', 'text/markdown')})
        assert result.status_code == 200
        input_id = result.json()['data']['input_id']
        preview = client.get(f'/api/v1/projects/{project_id}/inputs/{input_id}/preview')
        assert preview.status_code == 200
        assert 'Login' in preview.json()['data']['headings']

def test_empty_run_and_run_detail():
    with TestClient(app) as client:
        project_id = f'run-{uuid4().hex[:8]}'
        project = client.post('/api/v1/projects', json={'project_id': project_id, 'name': 'Run Demo'})
        assert project.status_code == 200
        environment = client.post(f'/api/v1/projects/{project_id}/environments', json={'name': 'QA', 'base_url': 'http://localhost:8001'})
        assert environment.status_code == 200
        environment_id = environment.json()['data']['id']
        run = client.post(f'/api/v1/projects/{project_id}/runs', json={'environment_id': environment_id})
        assert run.status_code == 200
        assert run.json()['data']['summary']['total'] == 0
        run_id = run.json()['data']['run_id']
        detail = client.get(f'/api/v1/projects/{project_id}/runs/{run_id}')
        assert detail.status_code == 200
        assert detail.json()['data']['results'] == []

def test_functional_case_generation_and_manual_result():
    with TestClient(app) as client:
        project_id = f'functional-{uuid4().hex[:8]}'
        project = client.post('/api/v1/projects', json={'project_id': project_id, 'name': 'Functional Demo'})
        assert project.status_code == 200
        spec = {'openapi': '3.0.0', 'info': {'title': 'Functional', 'version': '1'}, 'paths': {'/login': {'post': {'tags': ['auth'], 'summary': '登录', 'responses': {'200': {'description': 'ok'}}}}}}
        imported = client.post(f'/api/v1/projects/{project_id}/openapi/import', files={'file': ('openapi.json', json.dumps(spec), 'application/json')})
        assert imported.status_code == 200
        generated = client.post(f'/api/v1/projects/{project_id}/functional-cases/generate')
        assert generated.status_code == 200
        assert generated.json()['data']['created'] >= 1
        cases = client.get(f'/api/v1/projects/{project_id}/test-cases?test_type=functional').json()['data']
        assert cases and cases[0]['test_type'] == 'functional'
        environment = client.post(f'/api/v1/projects/{project_id}/environments', json={'name': 'QA', 'base_url': 'http://localhost:8001'})
        run = client.post(f'/api/v1/projects/{project_id}/functional-runs', json={'environment_id': environment.json()['data']['id']})
        assert run.status_code == 200
        result = client.post(f"/api/v1/projects/{project_id}/functional-runs/{run.json()['data']['run_id']}/results", json={'case_id': cases[0]['id'], 'status': 'failed', 'summary': '登录按钮点击后页面未跳转', 'failure': {'category': 'manual_observation', 'api': 'POST /login'}})
        assert result.status_code == 200
        assert result.json()['data']['summary']['failed'] == 1

def test_project_crud():
    with TestClient(app) as client:
        project_id = f'crud-{uuid4().hex[:8]}'
        created = client.post('/api/v1/projects', json={'project_id': project_id, 'name': 'CRUD Demo', 'description': 'old'})
        assert created.status_code == 200
        assert client.get(f'/api/v1/projects/{project_id}').status_code == 200
        updated = client.patch(f'/api/v1/projects/{project_id}', json={'name': 'CRUD Updated', 'description': 'new'})
        assert updated.status_code == 200
        assert updated.json()['data']['name'] == 'CRUD Updated'
        deleted = client.delete(f'/api/v1/projects/{project_id}')
        assert deleted.status_code == 200
        assert client.get(f'/api/v1/projects/{project_id}').status_code == 404

def test_test_case_crud():
    with TestClient(app) as client:
        project_id = f'case-crud-{uuid4().hex[:8]}'
        assert client.post('/api/v1/projects', json={'project_id': project_id, 'name': 'Case CRUD'}).status_code == 200
        spec = {'openapi': '3.0.0', 'info': {'title': 'Case CRUD', 'version': '1'}, 'paths': {'/ping': {'get': {'responses': {'200': {'description': 'ok'}}}}}}
        assert client.post(f'/api/v1/projects/{project_id}/openapi/import', files={'file': ('openapi.json', json.dumps(spec), 'application/json')}).status_code == 200
        assert client.post(f'/api/v1/projects/{project_id}/test-cases/generate').status_code == 200
        case = client.get(f'/api/v1/projects/{project_id}/test-cases').json()['data'][0]
        updated = client.patch(f"/api/v1/test-cases/{case['id']}", json={'title': '编辑后的用例', 'steps': [{'action': 'GET'}]})
        assert updated.status_code == 200
        assert updated.json()['data']['title'] == '编辑后的用例'
        deleted = client.delete(f"/api/v1/test-cases/{case['id']}")
        assert deleted.status_code == 200
        assert client.get(f'/api/v1/projects/{project_id}/test-cases').json()['data'] == []
        assert client.delete(f"/api/v1/test-cases/{case['id']}").status_code == 404

def test_manual_test_case_create():
    with TestClient(app) as client:
        project_id = f'manual-case-{uuid4().hex[:8]}'
        assert client.post('/api/v1/projects', json={'project_id': project_id, 'name': 'Manual Case'}).status_code == 200
        created = client.post(f'/api/v1/projects/{project_id}/test-cases', json={
            'title': '用户手动新增用例', 'test_type': 'functional', 'priority': 'P2',
            'steps': [{'action': '打开登录页'}], 'assertions': [{'type': 'manual', 'description': '页面打开成功'}]
        })
        assert created.status_code == 200
        assert created.json()['data']['title'] == '用户手动新增用例'
        assert created.json()['data']['status'] == 'draft'

def test_performance_run_metrics():
    with TestClient(app) as client:
        project_id = f'performance-{uuid4().hex[:8]}'
        assert client.post('/api/v1/projects', json={'project_id': project_id, 'name': 'Performance Demo'}).status_code == 200
        spec = {'openapi': '3.0.0', 'info': {'title': 'Performance', 'version': '1'}, 'paths': {'/api/v1/health': {'get': {'responses': {'200': {'description': 'ok'}}}}}}
        assert client.post(f'/api/v1/projects/{project_id}/openapi/import', files={'file': ('openapi.json', json.dumps(spec), 'application/json')}).status_code == 200
        api = client.get(f'/api/v1/projects/{project_id}/apis').json()['data'][0]
        environment = client.post(f'/api/v1/projects/{project_id}/environments', json={'name': 'Local', 'base_url': 'http://backend:8000'}).json()['data']
        plan = client.post(f'/api/v1/projects/{project_id}/performance-plans', json={'environment_id': environment['id'], 'api_id': api['id'], 'concurrency': 2, 'total_requests': 4, 'duration_seconds': 5})
        assert plan.status_code == 200
        assert plan.json()['data']['plan_path'].endswith('.jmx')
        result = client.post(f'/api/v1/projects/{project_id}/performance-runs', json={'environment_id': environment['id'], 'api_id': api['id'], 'concurrency': 2, 'total_requests': 4, 'max_p95_ms': 5000, 'max_error_rate': 0})
        assert result.status_code == 200
        assert result.json()['data']['summary']['total'] == 4
        assert 'p95_ms' in result.json()['data']['summary']
        assert result.json()['data']['summary']['gate']['passed'] is True

def test_web_case_generation_and_run():
    with TestClient(app) as client:
        project_id = f'web-{uuid4().hex[:8]}'
        assert client.post('/api/v1/projects', json={'project_id': project_id, 'name': 'Web Demo'}).status_code == 200
        spec = {'openapi': '3.0.0', 'info': {'title': 'Web', 'version': '1'}, 'paths': {'/login': {'post': {'tags': ['auth'], 'summary': '登录页', 'responses': {'200': {'description': 'ok'}}}}}}
        assert client.post(f'/api/v1/projects/{project_id}/openapi/import', files={'file': ('openapi.json', json.dumps(spec), 'application/json')}).status_code == 200
        generated = client.post(f'/api/v1/projects/{project_id}/web-cases/generate')
        assert generated.status_code == 200
        assert generated.json()['data']['created'] >= 1
        cases = client.get(f'/api/v1/projects/{project_id}/test-cases?test_type=web').json()['data']
        assert cases and cases[0]['test_type'] == 'web'
        environment = client.post(f'/api/v1/projects/{project_id}/environments', json={'name': 'QA', 'base_url': 'http://frontend'}).json()['data']
        run = client.post(f'/api/v1/projects/{project_id}/web-runs', json={'environment_id': environment['id']})
        assert run.status_code == 200
        summary = run.json()['data']['summary']
        assert summary['total'] == len(cases)
        # 测试环境无浏览器/Selenium 节点，执行器返回可判定的 error（环境未就绪），而非崩溃。
        detail = client.get(f"/api/v1/projects/{project_id}/runs/{run.json()['data']['run_id']}").json()['data']
        assert detail['test_type'] == 'web'
        assert all(r['status'] in ('error', 'failed', 'passed') for r in detail['results'])


def test_web_failure_message_is_human_readable(monkeypatch):
    from app import web_executor

    class TimeoutException(Exception):
        pass

    class FakeDriver:
        current_url = 'http://frontend/'
        title = ''

        def quit(self):
            return None

        def get_screenshot_as_png(self):
            return b'png'

        def get_log(self, _kind):
            return []

    monkeypatch.setattr(web_executor, 'build_driver', lambda: FakeDriver())
    monkeypatch.setattr(
        web_executor,
        'run_step',
        lambda *_args: (_ for _ in ()).throw(TimeoutException('Message:\nStacktrace:\n#0 noisy')),
    )
    case = type('Case', (), {
        'case_key': 'WEB-TEST',
        'steps': [{'action': 'click', 'by': 'css', 'selector': '#not-exist', 'timeout': 3}],
        'assertions': [],
    })()
    result = web_executor.execute_web_case(case, 'http://frontend')
    assert result['status'] == 'failed'
    assert '元素未找到或不可点击' in result['failure']['summary']
    assert '#not-exist' in result['failure']['summary']
    assert 'Stacktrace' not in result['failure']['summary']


def test_app_case_generation_is_idempotent():
    """App 草稿按接口幂等生成，避免重复点击产生重复用例。"""
    with TestClient(app) as client:
        project_id = f'app-generation-{uuid4().hex[:8]}'
        assert client.post('/api/v1/projects', json={'project_id': project_id, 'name': 'App Generation'}).status_code == 200
        spec = {
            'openapi': '3.0.0',
            'info': {'title': 'App', 'version': '1'},
            'paths': {'/login': {'post': {'summary': '登录', 'responses': {'200': {'description': 'ok'}}}}},
        }
        assert client.post(
            f'/api/v1/projects/{project_id}/openapi/import',
            files={'file': ('openapi.json', json.dumps(spec), 'application/json')},
        ).status_code == 200
        first = client.post(f'/api/v1/projects/{project_id}/app-cases/generate')
        second = client.post(f'/api/v1/projects/{project_id}/app-cases/generate')
        assert first.status_code == 200 and first.json()['data']['created'] == 1
        assert second.status_code == 200 and second.json()['data']['created'] == 0
        cases = client.get(f'/api/v1/projects/{project_id}/test-cases?test_type=app').json()['data']
        assert len(cases) == 1
        assert cases[0]['case_key'].startswith('APP-')
        assert cases[0]['test_type'] == 'app'


def test_appium_status_returns_stable_diagnostic(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit=-1):
            return json.dumps({'value': {'ready': True, 'message': 'Appium ready'}}).encode()

    monkeypatch.setattr(main_module, 'urlopen', lambda _request, timeout=2: FakeResponse())
    with TestClient(app) as client:
        project_id = f'app-status-{uuid4().hex[:8]}'
        assert client.post('/api/v1/projects', json={'project_id': project_id, 'name': 'App Status'}).status_code == 200
        environment = client.post(
            f'/api/v1/projects/{project_id}/environments',
            json={
                'name': 'Android Emulator',
                'base_url': 'http://frontend',
                'variables': {'appium': {'server_url': 'http://appium.test:4723', 'deviceName': 'emulator-5554'}},
            },
        ).json()['data']
        response = client.get(f'/api/v1/projects/{project_id}/appium/status?environment_id={environment["id"]}')
        assert response.status_code == 200
        data = response.json()['data']
        assert data['ready'] is True
        assert data['available'] is True
        assert data['server_url'] == 'http://appium.test:4723'
        assert data['device_name'] == 'emulator-5554'


def test_appium_status_accepts_appium_1_json_wire_response(monkeypatch):
    """Appium 1.x exposes /wd/hub/status with status=0 and no ready field."""

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit=-1):
            return json.dumps({
                'value': {'build': {'version': '1.22.3'}},
                'sessionId': None,
                'status': 0,
            }).encode()

    monkeypatch.setattr(main_module, 'urlopen', lambda _request, timeout=2: FakeResponse())
    with TestClient(app) as client:
        project_id = f'appium-1-status-{uuid4().hex[:8]}'
        assert client.post('/api/v1/projects', json={'project_id': project_id, 'name': 'Appium 1 Status'}).status_code == 200
        environment = client.post(
            f'/api/v1/projects/{project_id}/environments',
            json={
                'name': 'Android Emulator',
                'base_url': 'http://frontend',
                'variables': {'server_url': 'http://host.docker.internal:4723', 'deviceName': 'emulator-5554'},
            },
        ).json()['data']
        response = client.get(f'/api/v1/projects/{project_id}/appium/status?environment_id={environment["id"]}')
        assert response.status_code == 200
        data = response.json()['data']
        assert data['ready'] is True
        assert data['available'] is True


def test_app_run_persists_results_and_rejects_foreign_cases(monkeypatch):
    def fake_execute(case, capabilities=None, evidence_dir=None, config=None):
        return {
            'status': 'passed',
            'duration_ms': 4,
            'request': {'server_url': capabilities.get('server_url'), 'steps': case.steps},
            'response': {'screenshots': [], 'device_logs': []},
            'assertions': [],
            'summary': 'App 断言全部通过',
            'failure': {},
        }

    monkeypatch.setattr(main_module, 'execute_app_case', fake_execute)
    with TestClient(app) as client:
        project_id = f'app-run-{uuid4().hex[:8]}'
        foreign_id = f'app-foreign-{uuid4().hex[:8]}'
        assert client.post('/api/v1/projects', json={'project_id': project_id, 'name': 'App Run'}).status_code == 200
        assert client.post('/api/v1/projects', json={'project_id': foreign_id, 'name': 'Foreign'}).status_code == 200
        environment = client.post(
            f'/api/v1/projects/{project_id}/environments',
            json={'name': 'QA', 'base_url': 'http://frontend', 'variables': {'server_url': 'http://appium.test:4723'}},
        ).json()['data']
        spec = {
            'openapi': '3.0.0',
            'info': {'title': 'App Run', 'version': '1'},
            'paths': {'/home': {'get': {'responses': {'200': {'description': 'ok'}}}}},
        }
        assert client.post(
            f'/api/v1/projects/{project_id}/openapi/import',
            files={'file': ('openapi.json', json.dumps(spec), 'application/json')},
        ).status_code == 200
        assert client.post(f'/api/v1/projects/{project_id}/app-cases/generate').status_code == 200
        case = client.get(f'/api/v1/projects/{project_id}/test-cases?test_type=app').json()['data'][0]
        run = client.post(
            f'/api/v1/projects/{project_id}/app-runs',
            json={'environment_id': environment['id'], 'case_ids': [case['id']]},
        )
        assert run.status_code == 200
        data = run.json()['data']
        assert data['summary']['total'] == 1
        assert data['summary']['passed'] == 1
        detail = client.get(f'/api/v1/projects/{project_id}/runs/{data["run_id"]}').json()['data']
        assert detail['test_type'] == 'app'
        assert detail['results'][0]['status'] == 'passed'

        foreign_case = client.post(
            f'/api/v1/projects/{foreign_id}/test-cases',
            json={'title': 'Foreign App Case', 'test_type': 'app'},
        ).json()['data']
        rejected = client.post(
            f'/api/v1/projects/{project_id}/app-runs',
            json={'environment_id': environment['id'], 'case_ids': [foreign_case['id']]},
        )
        assert rejected.status_code == 400
