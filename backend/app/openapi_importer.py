from pathlib import Path
from typing import Any
import json
import yaml

METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}

def load_spec(path: str | Path) -> dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return yaml.safe_load(text) or {}

def iter_endpoints(spec):
    for path, item in (spec.get("paths") or {}).items():
        for method, operation in (item or {}).items():
            if method.lower() not in METHODS or not isinstance(operation, dict):
                continue
            tags = operation.get("tags") or [path.strip("/").split("/")[0] or "default"]
            yield {"method": method.upper(), "path": path, "operation_id": operation.get("operationId", ""), "summary": operation.get("summary") or operation.get("description") or "", "module": tags[0], "request_schema": request_schema(operation), "response_schema": response_schema(operation)}

def request_schema(operation):
    content = (operation.get("requestBody") or {}).get("content") or {}
    value = content.get("application/json") or next(iter(content.values()), {})
    return value.get("schema") or {}

def response_schema(operation):
    responses = operation.get("responses") or {}
    response = responses.get("200") or responses.get("201") or next(iter(responses.values()), {})
    content = response.get("content") or {}
    value = content.get("application/json") or next(iter(content.values()), {})
    return value.get("schema") or {}

def sample_from_schema(schema):
    if not schema: return {}
    if "example" in schema: return schema["example"]
    if "default" in schema: return schema["default"]
    if schema.get("enum"): return schema["enum"][0]
    typ = schema.get("type")
    if typ == "object" or "properties" in schema: return {k: sample_from_schema(v) for k, v in (schema.get("properties") or {}).items()}
    if typ == "array": return [sample_from_schema(schema.get("items") or {})]
    if typ == "integer": return 1
    if typ == "number": return 1.0
    if typ == "boolean": return True
    return "test-value"
