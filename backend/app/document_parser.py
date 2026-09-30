from pathlib import Path
import json
import re
import yaml

TEXT_SUFFIXES = {'.txt', '.md', '.markdown', '.csv', '.log'}

def parse_document(path: str):
    source = Path(path)
    raw = source.read_text(encoding='utf-8', errors='replace')
    suffix = source.suffix.lower()
    if suffix in {'.json', '.yaml', '.yml'}:
        try:
            data = json.loads(raw) if suffix == '.json' else yaml.safe_load(raw)
            return {'format': suffix.lstrip('.'), 'text': raw[:20000], 'structured': data, 'headings': []}
        except Exception:
            pass
    headings = [re.sub(r'^#+\s*', '', line).strip() for line in raw.splitlines() if line.strip().startswith('#')]
    return {'format': 'text' if suffix in TEXT_SUFFIXES else 'unknown', 'text': raw[:20000], 'structured': None, 'headings': headings}
