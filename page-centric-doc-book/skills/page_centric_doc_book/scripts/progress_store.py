from __future__ import annotations

import json
from dataclasses import asdict, fields
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .io_utils import read_json, write_json
from .models import ProgressState, TaskRecord


_TASK_RECORD_FIELDS = frozenset(field.name for field in fields(TaskRecord))


class ProgressStore:
    def __init__(self, output_root: str) -> None:
        self.output_root = Path(output_root)
        self.progress_path = self.output_root / "progress.json"

    def save(self, progress: ProgressState) -> None:
        payload = asdict(progress)
        write_json(str(self.progress_path), payload)

    def load(self) -> ProgressState:
        payload = self._load_payload()
        if not isinstance(payload, Mapping):
            payload = {}
        tasks_payload = payload.get("tasks", {})
        if not isinstance(tasks_payload, Mapping):
            tasks_payload = {}
        tasks = self._deserialize_tasks(tasks_payload)
        return ProgressState(
            version=payload.get("version", 1),
            inputs=payload.get("inputs", {}),
            current_phase=payload.get("current_phase", "init"),
            current_task_id=payload.get("current_task_id"),
            tasks=tasks,
        )

    def _load_payload(self) -> dict[str, Any]:
        try:
            return read_json(str(self.progress_path)) or {}
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            return {}

    def _deserialize_tasks(self, payload: Mapping[str, Any]) -> dict[str, TaskRecord]:
        tasks: dict[str, TaskRecord] = {}
        for task_id, task_payload in payload.items():
            if isinstance(task_payload, Mapping):
                tasks[task_id] = self._task_from_payload(task_id, task_payload)
        return tasks

    def _task_from_payload(self, task_id: str, payload: Mapping[str, Any]) -> TaskRecord:
        task_data: dict[str, Any] = {key: value for key, value in payload.items() if key in _TASK_RECORD_FIELDS}
        status = task_data.get("status")
        if status in (None, ""):
            normalized_status = "pending"
        elif status == "running":
            normalized_status = "ready"
        else:
            normalized_status = status
        task_data["status"] = normalized_status
        task_data["kind"] = task_data.get("kind") or ""
        task_data["output"] = task_data.get("output") or ""
        task_data["attempt"] = self._normalize_attempt(task_data.get("attempt"))
        task_data["depends_on"] = self._normalize_depends(task_data.get("depends_on"))
        task_data["task_id"] = task_id
        return TaskRecord(**task_data)

    def _normalize_attempt(self, amount: Any) -> int:
        if amount is None:
            return 0
        try:
            return int(amount)
        except (TypeError, ValueError):
            return 0

    def _normalize_depends(self, depends: Any) -> list[str]:
        if isinstance(depends, list):
            return [str(entry) for entry in depends if entry is not None]
        if depends is None:
            return []
        return [str(depends)]
