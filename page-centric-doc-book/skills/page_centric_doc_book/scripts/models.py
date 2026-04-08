from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TaskRecord:
    """最小任务描述，用于进度追踪。"""

    task_id: str
    kind: str
    output: str
    status: str = "pending"
    attempt: int = 0
    depends_on: List[str] = field(default_factory=list)
    parent_id: Optional[str] = None
    milestone: Optional[str] = None
    error_message: Optional[str] = None
    last_result_digest: Optional[str] = None
    stale: bool = False
    review_round: int = 0
    review_result: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProgressState:
    """全局进度快照，只记录必要字段。"""

    version: int
    inputs: Dict[str, Any]
    current_phase: str = "init"
    current_task_id: Optional[str] = None
    tasks: Dict[str, TaskRecord] = field(default_factory=dict)  # ProgressStore 负责序列化并持久化
    page_review_state: Dict[str, Any] = field(default_factory=dict)
