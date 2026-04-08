from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any


def ensure_dir(path: str) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def write_json(path: str, payload: Any) -> None:
    file_path = Path(path)
    ensure_dir(str(file_path.parent))
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(file_path.parent)) as tmp_file:
        tmp_file.write(encoded)
        temp_path = Path(tmp_file.name)
    temp_path.replace(file_path)


def read_json(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))
