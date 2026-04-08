from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import sys
from typing import Any, Optional, TYPE_CHECKING

from .models import ProgressState, TaskRecord

if TYPE_CHECKING:  # pragma: no cover
    from .book_assembler import BookAssembler
    from .doc_generator import DocumentGenerator
    from .progress_store import ProgressStore


BuildTaskContext = Callable[[TaskRecord, Mapping[str, Any]], Mapping[str, Any]]
BuildBookContext = Callable[[ProgressState], Mapping[str, Any]]


class Controller:
    """自治控制器的最小入口。

    Task 4 的目标是把“执行入口”从 run_pipeline 的顺序流程抽出来，形成可扩展的控制器边界：
    - Task 5/6 将在此基础上扩展策略、恢复、归档等能力
    - 当前阶段只要求最小可用的任务驱动与状态落盘
    """

    _LEAF_MAX_ROUNDS = 10

    def __init__(
        self,
        *,
        progress: ProgressState,
        generator: Optional["DocumentGenerator"],
        progress_store: Optional["ProgressStore"],
        book_assembler: Optional["BookAssembler"],
        build_task_context: Optional[BuildTaskContext] = None,
        build_book_context: Optional[BuildBookContext] = None,
        codex_executor: Any = None,
        prompt_builder: Any = None,
        archive_manager: Any = None,
        recovery_manager: Any = None,
        max_attempts: int = 3,
        max_workers: int = 10,
    ) -> None:
        self.progress = progress
        self.generator = generator
        self.progress_store = progress_store
        self.book_assembler = book_assembler
        self.build_task_context = build_task_context
        self.build_book_context = build_book_context
        self.codex_executor = codex_executor
        self.prompt_builder = prompt_builder
        self.archive_manager = archive_manager
        self.recovery_manager = recovery_manager
        self.max_attempts = max_attempts
        self.max_workers = max_workers

    def run(self, index_data: Mapping[str, Any]) -> None:
        """执行任务，直到成功完成或抛出异常。

        说明：
        - 当前实现是最小控制器逻辑，不包含自治策略、重试策略编排等（留给后续任务）
        - 任务状态语义保持与现有 pipeline 一致：pending/ready/running/done/failed
        """
        try:
            self._validate_dependencies()
            try:
                self._execute_tasks(index_data)
            except Exception:
                # 任务阶段的非任务级失败（例如依赖循环/无可执行任务）不应被误标为 book_assembler 失败。
                if not str(self.progress.current_phase or "").startswith("failed:"):
                    self.progress.current_phase = "failed:task_execution"
                self._save_safely(suppress=True)
                raise

            try:
                book_context = self.build_book_context(self.progress)
            except Exception:
                # build_book_context 的异常不应归因为 book_assembler.build(...) 失败。
                if not str(self.progress.current_phase or "").startswith("failed:"):
                    self.progress.current_phase = "failed:book_context"
                self._save_safely(suppress=True)
                raise

            try:
                self.book_assembler.build(book_context)
            except Exception:
                if not str(self.progress.current_phase or "").startswith("failed:"):
                    self.progress.current_phase = "failed:book_assembler"
                self._save_safely(suppress=True)
                raise
            else:
                self.progress.current_phase = "completed"
                self._save()
        finally:
            # 无论任何阶段失败，都必须清理 current_task_id，避免落盘残留“运行中任务”指针。
            self.progress.current_task_id = None
            pending_exception = sys.exc_info()[0] is not None
            self._save_safely(suppress=pending_exception)

    def _execute_tasks(self, index_data: Mapping[str, Any]) -> None:
        running: dict[Future[Any], TaskRecord] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            while True:
                self._submit_ready_tasks(executor, running, index_data)
                if not running:
                    if all(self._is_task_succeeded(task) for task in self.progress.tasks.values()):
                        return
                    failed_task = self._first_failed_task()
                    if failed_task is not None:
                        # resume 场景下可能已存在 failed 任务。此时不应把阻塞误归因为 task_execution。
                        # 将归因收敛到真实失败任务，保留 failed:{task_id} 语义。
                        failed_task_ids = self._failed_task_ids()
                        self._converge_phase_to_failed_task(failed_task)
                        self._save_safely(suppress=True)
                        summary = ", ".join(failed_task_ids) if failed_task_ids else failed_task.task_id
                        raise RuntimeError(f"存在失败任务，无法继续执行：{summary}")
                    raise RuntimeError("依赖无法满足或存在循环，无法继续执行任务")

                completed, _ = wait(running.keys(), return_when=FIRST_COMPLETED)
                for future in completed:
                    task = running.pop(future)
                    exc = future.exception()
                    if exc is None:
                        result = future.result()
                        self._handle_task_success(task, result)
                        continue
                    self._handle_task_failure(task, exc)

    def _submit_ready_tasks(
        self,
        executor: ThreadPoolExecutor,
        running: dict[Future[Any], TaskRecord],
        index_data: Mapping[str, Any],
    ) -> None:
        available_slots = self.max_workers - len(running)
        if available_slots <= 0:
            return
        for task in self._select_ready_tasks(limit=available_slots):
            self._mark_running(task)
            running[executor.submit(self._build_artifact, task, index_data)] = task

    def _select_ready_tasks(self, limit: int) -> list[TaskRecord]:
        ready_tasks: list[TaskRecord] = []
        for task in sorted(self.progress.tasks.values(), key=lambda record: record.task_id):
            if len(ready_tasks) >= limit:
                break
            if not self._can_run_task(task):
                continue
            ready_tasks.append(task)
        return ready_tasks

    def _can_run_task(self, task: TaskRecord) -> bool:
        if self._is_task_succeeded(task):
            return False
        if task.status in {"failed", "running"}:
            return False
        return all(
            (self.progress.tasks.get(dep) is not None) and self._is_task_succeeded(self.progress.tasks[dep])
            for dep in task.depends_on
        )

    def _run_task(self, task: TaskRecord, index_data: Mapping[str, Any]) -> None:
        self._mark_running(task)
        try:
            self._build_artifact(task, index_data)
        except Exception as exc:
            self._mark_failed(task, exc)
            raise
        else:
            self._mark_succeeded(task)

    def _build_artifact(self, task: TaskRecord, index_data: Mapping[str, Any]) -> Any:
        raw_context = self.build_task_context(task, index_data) if self.build_task_context else {}
        context = dict(raw_context or {})
        context = self._augment_leaf_context(task, context)
        if self._supports_codex_task(task):
            if self.codex_executor is None:
                raise ValueError(f"codex_executor 不能为空：{task.task_id}")
            if self.prompt_builder is None:
                raise ValueError(f"prompt_builder 不能为空：{task.task_id}")
            prompt = self.prompt_builder.build(task, context)
            markdown = self.codex_executor.generate_markdown(prompt)
            if task.kind == "page_leaf_review":
                self._capture_leaf_review_result(task, markdown)
            return self.generator.write_markdown(task, markdown)  # type: ignore[union-attr]
        return self.generator.generate(task, context)  # type: ignore[union-attr]

    def _mark_running(self, task: TaskRecord) -> None:
        task.attempt = int(task.attempt or 0) + 1
        task.status = "running"
        task.error_message = None
        self.progress.current_task_id = task.task_id
        self._save_safely(suppress=task.attempt > 1)

    def _mark_failed(self, task: TaskRecord, exc: BaseException) -> None:
        task.status = "failed"
        self.progress.current_phase = f"failed:{task.task_id}"
        task.error_message = str(exc)
        self._save_safely(suppress=True)

    def _mark_succeeded(self, task: TaskRecord) -> None:
        # 保持与现有测试/语义兼容：成功终态为 "done"。
        task.status = "done"
        task.error_message = None
        self._save()

    def _handle_task_failure(self, task: TaskRecord, exc: BaseException) -> None:
        if int(task.attempt or 0) < self.max_attempts:
            task.status = "ready"
            task.error_message = str(exc)
            self._save_safely(suppress=True)
            return
        self._mark_failed(task, exc)

    def _is_task_succeeded(self, task: TaskRecord) -> bool:
        return task.status == "done"

    def _first_failed_task(self) -> TaskRecord | None:
        failed = [task for task in self.progress.tasks.values() if task.status == "failed"]
        if not failed:
            return None
        return sorted(failed, key=lambda record: record.task_id)[0]

    def _failed_task_ids(self) -> list[str]:
        failed_ids = [task.task_id for task in self.progress.tasks.values() if task.status == "failed"]
        return sorted(str(task_id) for task_id in failed_ids if task_id)

    def _validate_dependencies(self) -> None:
        if self.generator is None:
            raise ValueError("generator 不能为空")
        if self.book_assembler is None:
            raise ValueError("book_assembler 不能为空")
        if self.build_task_context is None:
            raise ValueError("build_task_context 不能为空")
        if self.build_book_context is None:
            raise ValueError("build_book_context 不能为空")
        if self.max_attempts < 1:
            raise ValueError("max_attempts 必须大于 0")
        if self.max_workers < 1:
            raise ValueError("max_workers 必须大于 0")

    def _supports_codex_task(self, task: TaskRecord) -> bool:
        if task.kind in {
            "page",
            "page_leaf_draft",
            "page_leaf_review",
            "page_leaf_enrich",
            "page_leaf_finalize",
            "api",
            "topic",
            "knowledge_card",
            "reference",
        }:
            return True
        return task.kind == "flow" and str(task.task_id or "").startswith("feature.")

    def _handle_task_success(self, task: TaskRecord, result: Any) -> None:
        if task.kind == "page_leaf_review":
            self._mark_leaf_review_succeeded(task)
            return
        if task.kind == "page_leaf_enrich":
            self._mark_leaf_enrich_succeeded(task)
            return
        self._mark_succeeded(task)

    def _mark_leaf_review_succeeded(self, task: TaskRecord) -> None:
        self._mark_succeeded(task)

    def _mark_leaf_enrich_succeeded(self, task: TaskRecord) -> None:
        page_id = self._extract_leaf_page_id(task.task_id)
        self._mark_succeeded(task)
        if not page_id:
            return
        state = self._leaf_state(page_id)
        if self._should_continue_leaf_loop(state):
            review_task = self._find_leaf_task(page_id, "page_leaf_review")
            enrich_task = self._find_leaf_task(page_id, "page_leaf_enrich")
            if review_task:
                self._reset_task_for_loop(review_task)
            if enrich_task:
                self._reset_task_for_loop(enrich_task)
            self._save()
            return
        reason = self._leaf_termination_reason(state)
        state["final_status"] = self._derive_leaf_final_status(state, reason)
        self._save()

    def _augment_leaf_context(self, task: TaskRecord, context: dict[str, Any]) -> dict[str, Any]:
        page_id = self._extract_leaf_page_id(task.task_id)
        if not page_id or task.kind not in {"page_leaf_review", "page_leaf_enrich"}:
            return context
        state = self._leaf_state(page_id)
        if task.kind == "page_leaf_review":
            round_number = self._prepare_leaf_review_round(page_id)
        else:
            round_number = state.get("last_review_round") or 1
        context.setdefault("page_leaf_round", round_number)
        context.setdefault("page_leaf_review_round", round_number)
        context.setdefault("page_leaf_gaps", list(state.get("gaps") or []))
        return context

    def _prepare_leaf_review_round(self, page_id: str) -> int:
        state = self._leaf_state(page_id)
        active_round = state.get("active_review_round")
        if active_round:
            return active_round
        next_round = state.get("next_review_round") or 1
        state["active_review_round"] = next_round
        return next_round

    def _leaf_state(self, page_id: str) -> dict[str, Any]:
        state = self.progress.page_review_state.setdefault(page_id, {})
        state.setdefault("next_review_round", 1)
        state.setdefault("active_review_round", None)
        state.setdefault("last_review_round", 0)
        state.setdefault("consecutive_empty_rounds", 0)
        state.setdefault("gaps", [])
        return state

    def _capture_leaf_review_result(self, task: TaskRecord, markdown: str) -> None:
        page_id = self._extract_leaf_page_id(task.task_id)
        if not page_id:
            return
        state = self._leaf_state(page_id)
        round_number = state.get("active_review_round") or state.get("next_review_round") or 1
        parsed_gaps = self._parse_leaf_gaps(markdown)
        state["active_review_round"] = None
        state["last_review_round"] = round_number
        state["next_review_round"] = round_number + 1
        state["gaps"] = parsed_gaps
        if parsed_gaps:
            state["consecutive_empty_rounds"] = 0
        else:
            state["consecutive_empty_rounds"] = state.get("consecutive_empty_rounds", 0) + 1
        task.review_result["last_gaps"] = parsed_gaps
        task.review_result["last_markdown"] = markdown
        task.review_result["round"] = round_number
        task.review_round = round_number
        self._save()

    def _parse_leaf_gaps(self, markdown: str) -> list[str]:
        if not isinstance(markdown, str):
            return []
        gaps: list[str] = []
        bullet_prefixes = ("- ", "* ", "• ")
        for raw_line in markdown.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            lowered = line.lower()
            if "无缺口" in lowered or "没有缺口" in lowered:
                return []
            if "缺口清单" in lowered or "检查项" in lowered:
                continue
            if line.startswith(bullet_prefixes):
                entry = line[2:].strip()
                if entry:
                    gaps.append(entry)
                continue
            if line[0].isdigit():
                parts = line.split(".", 1)
                if len(parts) > 1:
                    entry = parts[1].strip()
                    if entry:
                        gaps.append(entry)
                continue
        return gaps

    def _should_continue_leaf_loop(self, state: dict[str, Any]) -> bool:
        if state.get("consecutive_empty_rounds", 0) >= 2:
            return False
        if state.get("last_review_round", 0) >= self._leaf_max_rounds():
            return False
        return True

    def _leaf_termination_reason(self, state: dict[str, Any]) -> str:
        if state.get("consecutive_empty_rounds", 0) >= 2:
            return "empty_rounds"
        if state.get("last_review_round", 0) >= self._leaf_max_rounds():
            return "max_rounds"
        return "completed"

    def _derive_leaf_final_status(self, state: dict[str, Any], reason: str) -> str:
        if not state.get("gaps"):
            return "complete"
        if reason == "empty_rounds":
            return "blocked"
        return "partial"

    def _reset_task_for_loop(self, task: TaskRecord) -> None:
        task.status = "ready"
        task.attempt = 0
        task.error_message = None
        task.review_result.clear()

    def _find_leaf_task(self, page_id: str, kind: str) -> TaskRecord | None:
        for candidate in self.progress.tasks.values():
            if candidate.kind != kind:
                continue
            if self._extract_leaf_page_id(candidate) == page_id:
                return candidate
        return None

    def _extract_leaf_page_id(self, task: TaskRecord | str | None) -> str:
        raw = task.task_id if isinstance(task, TaskRecord) else str(task or "")
        parts = raw.split(".")
        if len(parts) >= 3 and parts[0] == "page_leaf":
            return parts[1]
        return ""

    def _leaf_max_rounds(self) -> int:
        return self._LEAF_MAX_ROUNDS

    def _save_safely(self, *, suppress: bool) -> None:
        if not suppress:
            self._save()
            return
        try:
            self._save()
        except Exception:
            return

    def _converge_phase_to_failed_task(self, failed_task: TaskRecord) -> None:
        current_phase = str(self.progress.current_phase or "")
        failed_ids = {task.task_id for task in self.progress.tasks.values() if task.status == "failed"}
        if current_phase.startswith("failed:"):
            suffix = current_phase.split(":", 1)[1]
            if suffix in failed_ids:
                return
        self.progress.current_phase = f"failed:{failed_task.task_id}"

    def _save(self) -> None:
        if self.progress_store is None:
            return
        self.progress_store.save(self.progress)
