from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from .archive_manager import ArchiveManager
from .book_assembler import BookAssembler
from .codex_executor import CodexExecutor
from .controller import Controller
from .discovery import ProjectDiscovery
from .doc_generator import DocumentGenerator
from .index_builder import IndexBuilder
from .models import ProgressState, TaskRecord
from .prompt_builder import PromptBuilder
from .progress_store import ProgressStore
from .recovery_manager import RecoveryManager
from .task_planner import TaskPlanner

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = PACKAGE_ROOT / "templates"
_INDEX_GENERATED_AT = "1970-01-01T00:00:00Z"
_PAGE_DESCRIPTION = "自动生成的页面摘要；当前版本仅保证页面 ID、路由和关联 API 信息可靠。"
_PAGE_COVERAGE_NOTE = "- 能力边界：当前版本主要基于静态扫描结果，暂未恢复组件状态与表单约束。"
_API_DESCRIPTION = "自动生成的 API 摘要；当前版本仅保证方法、路径和模块信息可靠。"
_API_REQUEST_PARAMS = "- 当前版本未解析请求参数结构"
_API_RESPONSE_FIELDS = "- 当前版本未解析响应字段结构"
_API_COVERAGE_NOTE = "- 能力边界：当前版本暂未解析鉴权、请求体 schema 与响应字段细节。"
_MYSQL_DATA_SOURCE_MARKDOWN = "- `MySQL information_schema`"
_MYSQL_DATA_SOURCE_INLINE = "MySQL information_schema"


def run_pipeline(backend_path: str, frontend_path: str, output_path: str, resume: bool) -> None:
    backend_dir = Path(backend_path)
    frontend_dir = Path(frontend_path)
    output_root = Path(output_path)
    _validate_directory(backend_dir, "backend_path")
    _validate_directory(frontend_dir, "frontend_path")
    ArchiveManager(doc_repo=output_root, backend_root=backend_dir, frontend_root=frontend_dir)
    output_root.mkdir(parents=True, exist_ok=True)

    progress_store = ProgressStore(str(output_root))
    previous_snapshot: Mapping[str, Any] | None = None
    has_recoverable_progress = resume and progress_store.progress_path.exists()
    if has_recoverable_progress:
        progress = progress_store.load()
        previous_snapshot = _load_recovery_snapshot(progress)
    else:
        progress = ProgressState(version=1, inputs={})

    snapshot = ProjectDiscovery(backend_dir, frontend_dir).scan()
    index_data = dict(IndexBuilder().build(snapshot))
    index_data["generated_at"] = _utc_now_iso()
    index_data["warnings"] = _as_string_items(snapshot.get("warnings") or [])
    scan_summary = _build_scan_summary(index_data)
    warnings = _as_string_items(index_data.get("warnings") or [])
    recovery_snapshot = _build_recovery_snapshot(index_data)

    if has_recoverable_progress:
        stale_nodes = RecoveryManager.diff_stale_nodes(previous_snapshot, recovery_snapshot)
        if stale_nodes:
            # 保守策略：检测到源码侧关键输入变更后，重置任务状态并全量重建。
            progress.tasks = {}

    progress.inputs = {
        "backend_path": str(backend_dir),
        "frontend_path": str(frontend_dir),
        "output_path": str(output_root),
        "resume": resume,
        "recovery_snapshot": recovery_snapshot,
        "scan_summary": scan_summary,
        "warnings": warnings,
        "catalog": _build_catalog_snapshot(index_data),
    }

    incoming_tasks = TaskPlanner().build(index_data)

    progress.tasks = _merge_tasks(progress.tasks, incoming_tasks)
    if has_recoverable_progress:
        _prepare_resumable_tasks(progress)
    progress.current_phase = "running"
    progress.current_task_id = None
    progress_store.save(progress)

    generator = DocumentGenerator(TEMPLATE_ROOT, output_root)
    assembler = BookAssembler(TEMPLATE_ROOT, output_root)
    Controller(
        progress=progress,
        generator=generator,
        progress_store=progress_store,
        book_assembler=assembler,
        build_task_context=lambda task, data: _build_task_context(task, data, progress.page_review_state),
        build_book_context=_build_book_context,
        codex_executor=CodexExecutor(max_attempts=1),
        prompt_builder=PromptBuilder(),
        max_attempts=3,
        max_workers=10,
    ).run(index_data)


def _load_recovery_snapshot(progress: ProgressState) -> Mapping[str, Any] | None:
    inputs = progress.inputs
    if not isinstance(inputs, Mapping):
        return None
    snapshot = inputs.get("recovery_snapshot")
    return snapshot if isinstance(snapshot, Mapping) else None


def _build_recovery_snapshot(index_data: Mapping[str, Any]) -> Mapping[str, Any]:
    pages = index_data.get("pages") or {}
    snapshot_pages: dict[str, Mapping[str, str]] = {}
    for page_id in sorted(pages):
        page = pages.get(page_id) or {}
        route_path = str(page.get("route_path") or "").strip()
        requires_auth = str(page.get("requires_auth") or "").strip().lower()
        api_ids = sorted(_as_string_items(page.get("api_ids") or []))
        fingerprint = _to_json({"route_path": route_path, "requires_auth": requires_auth, "api_ids": api_ids})
        snapshot_pages[str(page_id)] = {"fingerprint": fingerprint}
    return {"pages": snapshot_pages}


def _validate_directory(path: Path, label: str) -> None:
    if not path.exists():
        raise FileNotFoundError(f"{label} 不存在：{path}")
    if not path.is_dir():
        raise NotADirectoryError(f"{label} 必须是目录：{path}")


def _merge_tasks(
    existing: Mapping[str, TaskRecord], incoming: Iterable[TaskRecord]
) -> dict[str, TaskRecord]:
    merged: dict[str, TaskRecord] = {}
    for task in incoming:
        if task.task_id in existing:
            record = existing[task.task_id]
            record.kind = task.kind
            record.output = task.output
            record.depends_on = list(task.depends_on)
            merged[task.task_id] = record
        else:
            merged[task.task_id] = TaskRecord(
                task_id=task.task_id,
                kind=task.kind,
                output=task.output,
                depends_on=list(task.depends_on),
            )
    return merged


def _prepare_resumable_tasks(progress: ProgressState) -> None:
    for task in progress.tasks.values():
        if task.status == "running":
            task.status = "ready"
            task.error_message = None
        elif task.status == "failed":
            task.status = "ready"
            task.attempt = 0
            task.error_message = None


def _build_task_context(
    task: TaskRecord,
    index_data: Mapping[str, Any],
    page_review_state: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    if task.kind == "api":
        return _api_context(task, index_data)
    if task.kind == "volume":
        return _volume_context(task, index_data)
    if _is_page_task_kind(task.kind) or _is_leaf_page_stage_kind(task.kind):
        return _page_context(task, index_data, page_review_state)
    if task.kind == "topic":
        return _topic_context(task, index_data)
    if task.kind == "knowledge_card":
        return _knowledge_card_context(task, index_data)
    if task.kind == "reference":
        return _reference_context(task, index_data)
    if task.kind == "dictionary_index":
        return _dictionary_index_context(index_data)
    if task.kind == "dictionary_database":
        return _dictionary_database_context(task, index_data)
    if task.kind == "dictionary_table":
        return _dictionary_table_context(task, index_data)
    if task.kind == "dictionary_db_field":
        return _dictionary_db_field_context(task, index_data)
    if task.kind == "dictionary_form_field":
        return _dictionary_form_field_context(task, index_data)
    if task.kind == "dictionary_grid_column":
        return _dictionary_grid_column_context(task, index_data)
    if task.kind == "dictionary_model":
        return _dictionary_model_context(task, index_data)
    if task.kind == "index":
        return _index_context(task, index_data)
    if task.kind == "flow":
        if str(task.task_id or "").startswith("feature."):
            return _feature_context(task, index_data)
        return _flow_context(task)
    if task.kind == "subflow":
        return _subflow_context(task)
    return {"title": task.task_id}


def _api_context(task: TaskRecord, index_data: Mapping[str, Any]) -> Mapping[str, Any]:
    apis = index_data.get("apis") or {}
    api_info = apis.get(task.task_id) or {}
    method = (api_info.get("method") or "POST").upper()
    path = api_info.get("path") or "/api/placeholder"
    module = api_info.get("module") or "api"
    title = api_info.get("title") or task.task_id
    return {
        "task_id": task.task_id,
        "title": title,
        "module": module,
        "path": path,
        "method": method,
        "description": _API_DESCRIPTION,
        "request_params": _API_REQUEST_PARAMS,
        "response_fields": _API_RESPONSE_FIELDS,
        "coverage_note": _API_COVERAGE_NOTE,
    }


def _page_context(
    task: TaskRecord,
    index_data: Mapping[str, Any],
    page_review_state: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    page_id = _extract_task_page_id(task)
    page_info = (index_data.get("pages") or {}).get(page_id) or {}
    evidence_pool = index_data.get("page_evidence") if isinstance(index_data.get("page_evidence"), Mapping) else {}
    evidence = evidence_pool.get(page_id) if isinstance(evidence_pool, Mapping) else {}
    evidence = evidence if isinstance(evidence, Mapping) else {}
    review_state = page_review_state.get(page_id) if isinstance(page_review_state, Mapping) else {}
    review_state = review_state if isinstance(review_state, Mapping) else {}
    route_path = page_info.get("route_path") or ""
    api_section = _build_api_section(page_info.get("api_ids") or [], index_data)
    go_handler_links = _build_page_go_handler_links(page_id, page_info, index_data)
    go_service_links = _build_page_go_service_links(page_id, index_data)
    table_links = _build_page_table_links(page_id, index_data)
    db_field_links = _build_page_db_field_links(page_id, index_data)
    form_field_links = _build_page_form_field_links(page_id, index_data)
    grid_column_links = _build_page_grid_column_links(page_id, index_data)
    dictionary_links = _build_page_dictionary_links(page_id, index_data)
    topic_section = _build_page_topic_section(page_id, index_data)
    reference_section = _build_page_reference_section(page_id, index_data)
    transitions = [
        row
        for row in (index_data.get("relations") or {}).get("page_transitions", [])
        if row.get("from_page_id") == page_id or row.get("to_page_id") == page_id
    ]
    page_status = str(review_state.get("final_status") or evidence.get("status") or "draft").strip() or "draft"
    gaps = review_state.get("gaps") or evidence.get("gaps") or []
    coverage = evidence.get("coverage") if isinstance(evidence.get("coverage"), Mapping) else {}
    frontend_evidence = evidence.get("evidence") if isinstance(evidence.get("evidence"), Mapping) else {}
    return {
        "task_id": task.task_id,
        "title": page_info.get("title") or page_id or task.task_id,
        "page_id": page_id,
        "route_path": route_path,
        "requires_auth": page_info.get("requires_auth") or "",
        "api_ids": _as_string_items(page_info.get("api_ids") or []),
        "volume_id": str(page_info.get("volume_id") or "root").strip() or "root",
        "topic_links": topic_section,
        "reference_links": reference_section,
        "transitions": transitions,
        "go_handler_links": go_handler_links,
        "go_service_links": go_service_links,
        "table_links": table_links,
        "db_field_links": db_field_links,
        "form_field_links": form_field_links,
        "grid_column_links": grid_column_links,
        "dictionary_links": dictionary_links,
        "mermaid": _build_page_backend_mermaid(page_id, index_data),
        "description": _PAGE_DESCRIPTION,
        "page_status": page_status,
        "review_round": int(review_state.get("last_review_round") or evidence.get("round") or 0),
        "review_checklist": _build_review_checklist_markdown(),
        "remaining_gaps": _markdown_list(gaps, default="- 无"),
        "resolved_gaps": _markdown_list(evidence.get("resolved_gaps") or [], default="- 无"),
        "entry_conditions_section": _build_entry_conditions_section(page_info),
        "steps_section": _build_steps_section(page_info, frontend_evidence),
        "request_response_section": _build_request_response_section(page_info, index_data),
        "table_field_section": _build_table_field_section(table_links, db_field_links),
        "permission_section": _build_permission_section(frontend_evidence),
        "exception_section": _build_exception_section(frontend_evidence),
        "related_pages_section": _build_related_pages_section(page_id, index_data, frontend_evidence),
        "business_logic_section": _build_business_logic_section(page_info, coverage),
        "flow_section": _build_flow_section(page_id, index_data),
        "api_section": api_section,
        "topic_section": topic_section,
        "note_section": "- 暂无批注",
        "reference_section": reference_section,
        "subflow_section": "- 暂无子流程",
        "coverage_note": _PAGE_COVERAGE_NOTE,
    }


def _volume_context(task: TaskRecord, index_data: Mapping[str, Any]) -> Mapping[str, str]:
    volume_id = _extract_volume_id(task.task_id)
    volume = (index_data.get("volumes") or {}).get(volume_id) or {}
    page_ids = volume.get("page_ids") or []
    topic_links = _build_volume_topic_links(page_ids, index_data)
    return {
        "title": volume.get("title") or volume_id or task.task_id,
        "summary": "自动生成的卷概览",
        "page_links": _build_volume_page_links(page_ids, index_data),
        "topic_links": topic_links,
    }


def _topic_context(task: TaskRecord, index_data: Mapping[str, Any]) -> Mapping[str, str]:
    topic = (index_data.get("topics") or {}).get(task.task_id) or {}
    page_ids = topic.get("page_ids") or []
    return {
        "title": topic.get("title") or task.task_id,
        "summary": "自动生成的专题链路",
        "page_links": _build_topic_page_links(page_ids, index_data),
        "knowledge_links": _build_topic_knowledge_links(task.task_id, page_ids, index_data),
        "reference_links": _build_topic_reference_links(index_data),
    }


def _knowledge_card_context(task: TaskRecord, index_data: Mapping[str, Any]) -> Mapping[str, str]:
    card = (index_data.get("knowledge_cards") or {}).get(task.task_id) or {}
    card_id = str(card.get("card_id") or _extract_knowledge_card_id(task.task_id)).strip() or _extract_knowledge_card_id(
        task.task_id
    )
    return {
        "title": card.get("title") or card_id or task.task_id,
        "card_id": card_id,
        "summary": card.get("summary") or "自动生成的知识卡片。",
        "points": _markdown_list(card.get("points") or [], default="- 暂无要点"),
        "reference_links": _build_reference_links(
            card.get("reference_ids") or [],
            index_data,
            prefix="../",
            default="- 暂无关联资料",
        ),
    }


def _reference_context(task: TaskRecord, index_data: Mapping[str, Any]) -> Mapping[str, str]:
    reference = (index_data.get("references") or {}).get(task.task_id) or {}
    return {
        "title": reference.get("title") or task.task_id,
        "source": reference.get("source") or "待补充",
        "summary": reference.get("summary") or "自动生成的引用文档。",
        "links": _markdown_list(reference.get("links") or [], default="- 暂无链接"),
    }


def _index_context(task: TaskRecord, index_data: Mapping[str, Any]) -> Mapping[str, str]:
    generated_at = str(index_data.get("generated_at") or _utc_now_iso() or _INDEX_GENERATED_AT)
    if task.task_id == "index.book":
        return {
            "generated_at": generated_at,
            "book_items": _to_json(
                {
                    "volumes": index_data.get("volumes") or {},
                    "pages": index_data.get("pages") or {},
                    "topics": index_data.get("topics") or {},
                    "knowledge_cards": index_data.get("knowledge_cards") or {},
                    "references": index_data.get("references") or {},
                }
            ),
        }
    if task.task_id == "index.relations":
        return {
            "generated_at": generated_at,
            "relation_items": _to_json(index_data.get("relations") or {}),
        }
    if task.task_id == "index.navigation":
        return {
            "generated_at": generated_at,
            "navigation_items": _to_json(index_data.get("navigation") or {}),
        }
    raise ValueError(f"不支持的索引任务：{task.task_id}")


def _dictionary_index_context(index_data: Mapping[str, Any]) -> Mapping[str, str]:
    databases = _rows_by_key(index_data.get("databases") or (), "database_id")
    tables = _rows_by_key(index_data.get("tables") or (), "table_id")
    db_fields = _rows_by_key(index_data.get("db_fields") or (), "field_id")
    form_fields = _rows_by_key(index_data.get("form_fields") or (), "field_id")
    grid_columns = _rows_by_key(index_data.get("grid_columns") or (), "column_id")
    go_models = _rows_by_key(index_data.get("go_models") or (), "model_id")
    return {
        "title": "字段字典",
        "database_links": _markdown_links(
            [
                (database_id, f"databases/{database_id}.md")
                for database_id in sorted(databases)
            ],
            default="- 暂无数据库",
        ),
        "table_links": _markdown_links(
            [(table_id, f"tables/{table_id}.md") for table_id in sorted(tables)],
            default="- 暂无数据表",
        ),
        "db_field_links": _markdown_links(
            [(field_id, f"db-fields/{field_id}.md") for field_id in sorted(db_fields)],
            default="- 暂无数据库字段",
        ),
        "form_field_links": _markdown_links(
            [
                (field_id, f"form-fields/{_dictionary_suffix(field_id)}.md")
                for field_id in sorted(form_fields)
            ],
            default="- 暂无表单字段",
        ),
        "grid_column_links": _markdown_links(
            [
                (column_id, f"grid-columns/{_dictionary_suffix(column_id)}.md")
                for column_id in sorted(grid_columns)
            ],
            default="- 暂无表格列",
        ),
        "model_links": _markdown_links(
            [
                (
                    str((go_models.get(model_id) or {}).get("model_name") or model_id),
                    f"models/{model_id}.md",
                )
                for model_id in sorted(go_models)
            ],
            default="- 暂无 Go Model",
        ),
    }


def _dictionary_database_context(task: TaskRecord, index_data: Mapping[str, Any]) -> Mapping[str, str]:
    database_id = str(task.task_id or "").replace("dictionary.database.", "", 1)
    database = (_rows_by_key(index_data.get("databases") or (), "database_id").get(database_id) or {})
    tables = _rows_by_key(index_data.get("tables") or (), "table_id")
    table_links = [
        (table_id, f"../tables/{table_id}.md")
        for table_id, table in sorted(tables.items())
        if _table_belongs_to_database(table, database_id)
    ]
    return {
        "title": database_id,
        "database_id": database_id,
        "table_links": _markdown_links(table_links, default="- 暂无数据表"),
        "data_source": _MYSQL_DATA_SOURCE_MARKDOWN,
    }


def _dictionary_table_context(task: TaskRecord, index_data: Mapping[str, Any]) -> Mapping[str, str]:
    table_id = str(task.task_id or "").replace("dictionary.table.", "", 1)
    table = (_rows_by_key(index_data.get("tables") or (), "table_id").get(table_id) or {})
    table_fields = [
        field
        for field in (index_data.get("db_fields") or [])
        if isinstance(field, Mapping) and str(field.get("table_id") or "").strip() == table_id
    ]
    page_table_links = index_data.get("page_table_links") or []
    page_usage_links = [
        (
            page_id,
            f"../../{_page_doc_path(page_id, index_data)}",
        )
        for row in page_table_links
        for page_id in [str(row.get("page_id") or "").strip()]
        if str(row.get("table_id") or "").strip() == table_id and page_id
    ]
    indexes = []
    for index in table.get("indexes") or []:
        if not isinstance(index, Mapping):
            continue
        index_name = str(index.get("index_name") or "").strip()
        columns = ", ".join(_as_string_items(index.get("columns") or []))
        if index_name and columns:
            indexes.append(f"`{index_name}({columns})`")
        elif index_name:
            indexes.append(f"`{index_name}`")
    field_rows = [
        [
            str(field.get("name") or "").strip(),
            str(field.get("type") or field.get("field_type") or "").strip(),
            _format_nullable(field.get("nullable")),
            str(field.get("default") or "").strip(),
            str(field.get("comment") or "").strip(),
            f"[详情](../db-fields/{str(field.get('field_id') or '').strip()}.md)",
        ]
        for field in table_fields
        if str(field.get("field_id") or "").strip()
    ]
    return {
        "title": table_id,
        "table_id": table_id,
        "schema": str(table.get("schema") or table.get("database_id") or "").strip(),
        "table_name": str(table.get("table_name") or table.get("name") or "").strip(),
        "data_source": _MYSQL_DATA_SOURCE_MARKDOWN,
        "index_section": _markdown_list(indexes, default="- 暂无索引"),
        "field_table": _markdown_table(
            headers=["字段名", "类型", "可空", "默认值", "注释", "详情"],
            rows=field_rows,
            default="- 暂无字段",
        ),
        "usage_links": _markdown_links(page_usage_links, default="- 暂无使用页面"),
    }


def _dictionary_db_field_context(task: TaskRecord, index_data: Mapping[str, Any]) -> Mapping[str, str]:
    field_id = str(task.task_id or "").replace("dictionary.db_field.", "", 1)
    field = (_rows_by_key(index_data.get("db_fields") or (), "field_id").get(field_id) or {})
    table_id = str(field.get("table_id") or "").strip()
    usage_links = [
        (
            str(row.get("page_id") or "").strip(),
            f"../../{_page_doc_path(str(row.get('page_id') or '').strip(), index_data)}",
        )
        for row in (index_data.get("page_db_field_links") or [])
        if str(row.get("db_field_id") or "").strip() == field_id
        and str(row.get("page_id") or "").strip()
    ]
    return {
        "title": field_id,
        "field_id": field_id,
        "field_name": str(field.get("name") or "").strip(),
        "field_type": str(field.get("type") or field.get("field_type") or "").strip(),
        "comment": str(field.get("comment") or "").strip(),
        "nullable": _format_nullable(field.get("nullable")),
        "default_value": str(field.get("default") or "").strip(),
        "data_source": _MYSQL_DATA_SOURCE_INLINE,
        "table_link": _markdown_links(
            [(table_id, f"../tables/{table_id}.md")] if table_id else [],
            default="- 暂无所属数据表",
        ),
        "usage_links": _markdown_links(usage_links, default="- 暂无使用页面"),
    }


def _dictionary_form_field_context(task: TaskRecord, index_data: Mapping[str, Any]) -> Mapping[str, str]:
    field = _find_dictionary_row(index_data.get("form_fields") or (), "field_id", task.task_id, "dictionary.form_field.")
    field_id = str(field.get("field_id") or _dictionary_original_id(task.task_id, "dictionary.form_field.") or "").strip()
    page_id = str(field.get("page_id") or "").strip()
    mapping_links = [
        (
            str(row.get("target_id") or "").strip(),
            f"../db-fields/{row.get('target_id')}.md",
        )
        for row in (index_data.get("field_mappings") or [])
        if str(row.get("source_kind") or "").strip() == "form_field"
        and str(row.get("source_id") or "").strip() == field_id
        and str(row.get("target_id") or "").strip()
    ]
    return {
        "title": field_id,
        "field_id": field_id,
        "prop": str(field.get("prop") or "").strip(),
        "label": str(field.get("label") or "").strip(),
        "source_file": str(field.get("source_file") or "").strip(),
        "page_link": _markdown_links(
            [(page_id, f"../../{_page_doc_path(page_id, index_data)}")] if page_id else [],
            default="- 暂无所属页面",
        ),
        "mapping_links": _markdown_links(mapping_links, default="- 暂无字段映射"),
    }


def _dictionary_grid_column_context(task: TaskRecord, index_data: Mapping[str, Any]) -> Mapping[str, str]:
    column = _find_dictionary_row(index_data.get("grid_columns") or (), "column_id", task.task_id, "dictionary.grid_column.")
    column_id = str(column.get("column_id") or _dictionary_original_id(task.task_id, "dictionary.grid_column.") or "").strip()
    page_id = str(column.get("page_id") or "").strip()
    mapping_links = [
        (
            str(row.get("target_id") or "").strip(),
            f"../db-fields/{row.get('target_id')}.md",
        )
        for row in (index_data.get("field_mappings") or [])
        if str(row.get("source_kind") or "").strip() == "grid_column"
        and str(row.get("source_id") or "").strip() == column_id
        and str(row.get("target_id") or "").strip()
    ]
    return {
        "title": column_id,
        "column_id": column_id,
        "prop": str(column.get("prop") or "").strip(),
        "label": str(column.get("label") or "").strip(),
        "source_file": str(column.get("source_file") or "").strip(),
        "page_link": _markdown_links(
            [(page_id, f"../../{_page_doc_path(page_id, index_data)}")] if page_id else [],
            default="- 暂无所属页面",
        ),
        "mapping_links": _markdown_links(mapping_links, default="- 暂无字段映射"),
    }


def _dictionary_model_context(task: TaskRecord, index_data: Mapping[str, Any]) -> Mapping[str, str]:
    model_id = str(task.task_id or "").replace("dictionary.model.", "", 1)
    model = (_rows_by_key(index_data.get("go_models") or (), "model_id").get(model_id) or {})
    fields = []
    for field in model.get("fields") or []:
        if not isinstance(field, Mapping):
            continue
        field_name = str(field.get("field_name") or "").strip()
        column = str(field.get("column") or "").strip()
        field_type = str(field.get("type") or "").strip()
        if field_name:
            fields.append(f"`{field_name}` -> `{column}` ({field_type})".strip())
    return {
        "title": str(model.get("model_name") or model_id),
        "model_id": model_id,
        "model_name": str(model.get("model_name") or model_id),
        "source_file": str(model.get("source_file") or "").strip(),
        "field_section": _markdown_list(fields, default="- 暂无模型字段"),
    }


def _flow_context(task: TaskRecord) -> Mapping[str, str]:
    page_id = _extract_flow_page(task.task_id)
    return {
        # 模板自身会追加“主流程”，这里避免在上下文里重复拼接导致标题重复。
        "title": f"{page_id or task.task_id}",
        "summary": "自动生成的主流程",
        "mermaid": f"flowchart LR\n    start_{page_id or 'page'} --> end_{page_id or 'page'}",
    }


def _subflow_context(task: TaskRecord) -> Mapping[str, str]:
    parts = task.task_id.split(".")
    page_id = parts[2] if len(parts) > 2 else ""
    subflow_name = parts[3] if len(parts) > 3 else "subflow"
    return {
        "title": page_id or task.task_id,
        "subflow_name": subflow_name,
        "description": "自动生成的子流程",
        "mermaid": f"flowchart LR\n    {subflow_name}_start --> {subflow_name}_end",
    }


def _feature_context(task: TaskRecord, index_data: Mapping[str, Any]) -> Mapping[str, str]:
    feature_id = str(task.task_id or "")
    feature = (index_data.get("features") or {}).get(feature_id) or {}

    title = (feature.get("title") or "").strip() or feature_id
    page_ids = list(feature.get("page_ids") or [])
    api_ids = list(feature.get("api_ids") or [])

    page_links = _build_feature_page_links(page_ids, index_data)
    api_links = _build_feature_api_links(api_ids, index_data)
    mermaid = _build_feature_mermaid(page_ids, api_ids, index_data)

    return {
        "title": title,
        "summary": "基于跨页跳转关系自动归并的端到端主线文档。",
        "page_links": page_links,
        "api_links": api_links,
        "mermaid": mermaid,
    }


def _build_feature_page_links(page_ids: Iterable[str], index_data: Mapping[str, Any]) -> str:
    rows: list[str] = []
    for page_id in page_ids:
        if page_id not in (index_data.get("pages") or {}):
            continue
        title = _page_link_title(page_id, index_data)
        rows.append(f"- [{title}](../{_page_doc_path(page_id, index_data)})")
    return "\n".join(rows) if rows else "- 暂无关联页面"


def _build_feature_api_links(api_ids: Iterable[str], index_data: Mapping[str, Any]) -> str:
    apis = index_data.get("apis") or {}
    rows: list[str] = []
    for api_id in api_ids:
        api_info = apis.get(api_id) or {}
        method = (api_info.get("method") or "").upper()
        path = api_info.get("path") or ""
        # markdown link label 内不要嵌 code span（反引号会降低可读性，也容易出现嵌套语法问题）。
        label = f"{method} {path}".strip() if method and path else api_id
        rows.append(f"- [{label}](../{_api_doc_path(api_id, api_info)})")
    return "\n".join(rows) if rows else "- 暂无关联接口"


def _api_doc_path(api_id: str, api_info: Mapping[str, Any]) -> str:
    module = api_info.get("module") or "api"
    name = api_info.get("name")
    if name:
        return f"backend/apis/{module}/{name}.md"
    sanitized = api_id.replace(":", ".").replace("/", "_")
    return f"backend/apis/{module}/{sanitized}.md"


def _build_feature_mermaid(
    page_ids: Iterable[str], api_ids: Iterable[str], index_data: Mapping[str, Any]
) -> str:
    pages = index_data.get("pages") or {}
    apis = index_data.get("apis") or {}

    lines = ["flowchart LR"]
    page_nodes: list[str] = []
    api_nodes: list[str] = []

    for page_id in page_ids:
        if page_id not in pages:
            continue
        node_id = f"page_{_sanitize_mermaid_id(page_id)}"
        label = _sanitize_mermaid_label(page_id)
        page_nodes.append(node_id)
        lines.append(f"    {node_id}[\"{label}\"]")

    for api_id in api_ids:
        api_info = apis.get(api_id) or {}
        method = (api_info.get("method") or "").upper()
        path = api_info.get("path") or ""
        label = _sanitize_mermaid_label(f"{method} {path}".strip() or api_id)
        node_id = f"api_{_sanitize_mermaid_id(api_id)}"
        api_nodes.append(node_id)
        lines.append(f"    {node_id}[\"{label}\"]")

    if page_nodes and api_nodes:
        # 最小可读关系：每个页面节点连到所有接口节点（避免过度推断调用顺序）。
        for page_node in page_nodes:
            for api_node in api_nodes:
                lines.append(f"    {page_node} --> {api_node}")

    return "\n".join(lines)


def _sanitize_mermaid_id(raw: str) -> str:
    # Mermaid 节点 ID 需要稳定且安全：
    # - 仅保留 ASCII 字母数字与下划线
    # - 非 ASCII 字符转为 _uXXXX_ 形式，避免丢失区分度并确保纯 ASCII
    cleaned: list[str] = []
    for ch in str(raw or ""):
        if ("0" <= ch <= "9") or ("a" <= ch <= "z") or ("A" <= ch <= "Z") or ch == "_":
            cleaned.append(ch)
            continue
        codepoint = ord(ch)
        if codepoint <= 0x7F:
            cleaned.append("_")
        else:
            cleaned.append(f"_u{codepoint:04x}_")
    result = re.sub(r"_+", "_", "".join(cleaned)).strip("_") or "node"
    if result[0].isdigit():
        return f"n_{result}"
    return result


def _sanitize_mermaid_label(raw: str) -> str:
    # 最小转义：避免引号与换行破坏 Mermaid 语法。
    text = str(raw or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\"", "\\\"")
    text = text.replace("\n", "\\n")
    return text


def _build_api_section(api_ids: Iterable[str], index_data: Mapping[str, Any]) -> str:
    entries: list[str] = []
    api_map = index_data.get("apis") or {}
    for api_id in api_ids:
        api_info = api_map.get(api_id) or {}
        method = (api_info.get("method") or "GET").upper()
        path = api_info.get("path") or ""
        if path:
            entries.append(f"- `{method} {path}`")
        else:
            entries.append(f"- {api_id}")
    return "\n".join(entries) if entries else "- 暂无接口"


def _build_page_go_handler_links(
    page_id: str, page_info: Mapping[str, Any], index_data: Mapping[str, Any]
) -> str:
    api_ids = set(_as_string_items(page_info.get("api_ids") or []))
    handlers = [
        str(row.get("handler_id") or "").strip()
        for row in (index_data.get("api_handler_links") or [])
        if str(row.get("api_id") or "").strip() in api_ids and str(row.get("handler_id") or "").strip()
    ]
    return _markdown_list([f"`{handler_id}`" for handler_id in sorted(set(handlers))], default="- 未关联 Go Handler")


def _build_page_go_service_links(page_id: str, index_data: Mapping[str, Any]) -> str:
    del page_id
    handler_ids = {
        str(row.get("handler_id") or "").strip()
        for row in (index_data.get("api_handler_links") or [])
        if str(row.get("handler_id") or "").strip()
    }
    services = [
        str(row.get("service_id") or "").strip()
        for row in (index_data.get("handler_service_links") or [])
        if str(row.get("handler_id") or "").strip() in handler_ids and str(row.get("service_id") or "").strip()
    ]
    return _markdown_list([f"`{service_id}`" for service_id in sorted(set(services))], default="- 未关联 Go Service")


def _build_page_table_links(page_id: str, index_data: Mapping[str, Any]) -> str:
    links = [
        (
            str(row.get("table_id") or "").strip(),
            f"../../../dictionary/tables/{row.get('table_id')}.md",
        )
        for row in (index_data.get("page_table_links") or [])
        if str(row.get("page_id") or "").strip() == page_id and str(row.get("table_id") or "").strip()
    ]
    return _markdown_links(links, default="- 未关联数据表")


def _build_page_db_field_links(page_id: str, index_data: Mapping[str, Any]) -> str:
    links = [
        (
            str(row.get("db_field_id") or "").strip(),
            f"../../../dictionary/db-fields/{row.get('db_field_id')}.md",
        )
        for row in (index_data.get("page_db_field_links") or [])
        if str(row.get("page_id") or "").strip() == page_id and str(row.get("db_field_id") or "").strip()
    ]
    return _markdown_links(links, default="- 未关联关键字段")


def _build_page_form_field_links(page_id: str, index_data: Mapping[str, Any]) -> str:
    links = [
        (
            str(row.get("field_id") or "").strip(),
            f"../../../dictionary/form-fields/{_dictionary_suffix(row.get('field_id'))}.md",
        )
        for row in (_rows_by_key(index_data.get("form_fields") or (), "field_id").values())
        if str(row.get("page_id") or "").strip() == page_id and str(row.get("field_id") or "").strip()
    ]
    return _markdown_links(links, default="- 未关联表单字段")


def _build_page_grid_column_links(page_id: str, index_data: Mapping[str, Any]) -> str:
    links = [
        (
            str(row.get("column_id") or "").strip(),
            f"../../../dictionary/grid-columns/{_dictionary_suffix(row.get('column_id'))}.md",
        )
        for row in (_rows_by_key(index_data.get("grid_columns") or (), "column_id").values())
        if str(row.get("page_id") or "").strip() == page_id and str(row.get("column_id") or "").strip()
    ]
    return _markdown_links(links, default="- 未关联表格列")


def _build_page_dictionary_links(page_id: str, index_data: Mapping[str, Any]) -> str:
    entries = [("字段字典", "../../../dictionary/README.md")]
    page_links = []
    form_rows = _rows_by_key(index_data.get("form_fields") or (), "field_id").values()
    grid_rows = _rows_by_key(index_data.get("grid_columns") or (), "column_id").values()
    for row in form_rows:
        field_id = str(row.get("field_id") or "").strip()
        if str(row.get("page_id") or "").strip() == page_id and field_id:
            page_links.append((field_id, f"../../../dictionary/form-fields/{_dictionary_suffix(field_id)}.md"))
    for row in grid_rows:
        column_id = str(row.get("column_id") or "").strip()
        if str(row.get("page_id") or "").strip() == page_id and column_id:
            page_links.append((column_id, f"../../../dictionary/grid-columns/{_dictionary_suffix(column_id)}.md"))
    return _markdown_links(entries + sorted(page_links), default="- 暂无字典入口")


def _build_page_backend_mermaid(page_id: str, index_data: Mapping[str, Any]) -> str:
    page_node = f"page_{_sanitize_mermaid_id(page_id or 'page')}"
    lines = ["flowchart LR", f"    {page_node}[\"{_sanitize_mermaid_label(page_id or 'page')}\"]"]
    handler_nodes: set[str] = set()
    service_nodes: set[str] = set()
    repository_nodes: set[str] = set()
    api_ids = {
        str(row.get("api_id") or "").strip()
        for row in (index_data.get("api_handler_links") or [])
        if str(row.get("handler_id") or "").strip()
    }
    for row in (index_data.get("api_handler_links") or []):
        api_id = str(row.get("api_id") or "").strip()
        handler_id = str(row.get("handler_id") or "").strip()
        if not api_id or not handler_id or api_id not in api_ids:
            continue
        api_node = f"api_{_sanitize_mermaid_id(api_id)}"
        handler_node = f"handler_{_sanitize_mermaid_id(handler_id)}"
        if api_node not in handler_nodes:
            lines.append(f"    {api_node}[\"{_sanitize_mermaid_label(api_id)}\"]")
            handler_nodes.add(api_node)
        lines.append(f"    {page_node} --> {api_node}")
        lines.append(f"    {api_node} --> {handler_node}")
        lines.append(f"    {handler_node}[\"{_sanitize_mermaid_label(handler_id)}\"]")
        for service_row in (index_data.get("handler_service_links") or []):
            if str(service_row.get("handler_id") or "").strip() != handler_id:
                continue
            service_id = str(service_row.get("service_id") or "").strip()
            if not service_id:
                continue
            service_node = f"service_{_sanitize_mermaid_id(service_id)}"
            if service_node not in service_nodes:
                lines.append(f"    {service_node}[\"{_sanitize_mermaid_label(service_id)}\"]")
                service_nodes.add(service_node)
            lines.append(f"    {handler_node} --> {service_node}")
            for repo_row in (index_data.get("service_repository_links") or []):
                if str(repo_row.get("service_id") or "").strip() != service_id:
                    continue
                repository_id = str(repo_row.get("repository_id") or "").strip()
                if not repository_id:
                    continue
                repository_node = f"repo_{_sanitize_mermaid_id(repository_id)}"
                if repository_node not in repository_nodes:
                    lines.append(f"    {repository_node}[\"{_sanitize_mermaid_label(repository_id)}\"]")
                    repository_nodes.add(repository_node)
                lines.append(f"    {service_node} --> {repository_node}")
    return "\n".join(lines)


def _build_page_topic_section(page_id: str, index_data: Mapping[str, Any]) -> str:
    topics = index_data.get("topics") or {}
    entries: list[str] = []
    for topic_id in sorted(topics):
        topic = topics.get(topic_id) or {}
        if page_id not in (topic.get("page_ids") or []):
            continue
        title = topic.get("title") or topic_id
        entries.append(f"- [{title}](../../../topics/{topic_id}.md)")
    return "\n".join(entries) if entries else "- 暂无专题"


def _build_page_reference_section(page_id: str, index_data: Mapping[str, Any]) -> str:
    entries: list[str] = []
    related_card_ids: list[str] = []
    knowledge_cards = index_data.get("knowledge_cards") or {}
    for card_id in sorted(knowledge_cards):
        card = knowledge_cards.get(card_id) or {}
        if page_id not in _as_string_items(card.get("page_ids") or []):
            continue
        title = card.get("title") or card_id
        slug = card.get("slug") or _knowledge_slug(card_id)
        entries.append(f"- [{title}](../../../knowledge/{slug}.md)")
        related_card_ids.append(card_id)

    reference_ids: list[str] = []
    for card_id in related_card_ids:
        card = knowledge_cards.get(card_id) or {}
        reference_ids.extend(_as_string_items(card.get("reference_ids") or []))

    reference_section = _build_reference_links(
        reference_ids,
        index_data,
        prefix="../../../",
        default="",
    )
    if reference_section:
        entries.append(reference_section)
    return "\n".join(entries) if entries else "- 暂无延伸阅读"


def _build_volume_page_links(page_ids: Iterable[str], index_data: Mapping[str, Any]) -> str:
    entries = [f"- [{_page_link_title(page_id, index_data)}](./pages/{page_id}.md)" for page_id in page_ids]
    return "\n".join(entries) if entries else "- 暂无页面"


def _build_volume_topic_links(page_ids: Iterable[str], index_data: Mapping[str, Any]) -> str:
    topics = index_data.get("topics") or {}
    matched_topics: list[str] = []
    page_id_set = set(page_ids)
    for topic_id in sorted(topics):
        topic = topics.get(topic_id) or {}
        if page_id_set.intersection(topic.get("page_ids") or []):
            title = topic.get("title") or topic_id
            matched_topics.append(f"- [{title}](../../topics/{topic_id}.md)")
    return "\n".join(matched_topics) if matched_topics else "- 暂无专题"


def _build_topic_page_links(page_ids: Iterable[str], index_data: Mapping[str, Any]) -> str:
    entries = [f"- [{_page_link_title(page_id, index_data)}](../{_page_doc_path(page_id, index_data)})" for page_id in page_ids]
    return "\n".join(entries) if entries else "- 暂无关联页面"


def _page_link_title(page_id: str, index_data: Mapping[str, Any]) -> str:
    page_info = (index_data.get("pages") or {}).get(page_id) or {}
    return str(page_info.get("title") or page_id).strip() or page_id


def _build_topic_reference_links(index_data: Mapping[str, Any]) -> str:
    return _build_reference_links(
        sorted((index_data.get("references") or {}).keys()),
        index_data,
        prefix="../",
        default="- 暂无参考资料",
    )


def _build_topic_knowledge_links(
    topic_id: str, page_ids: Iterable[str], index_data: Mapping[str, Any]
) -> str:
    knowledge_cards = index_data.get("knowledge_cards") or {}
    page_id_set = set(page_ids)
    entries: list[str] = []
    for card_id in sorted(knowledge_cards):
        card = knowledge_cards.get(card_id) or {}
        topic_ids = set(_as_string_items(card.get("topic_ids") or []))
        related_page_ids = set(_as_string_items(card.get("page_ids") or []))
        if topic_id not in topic_ids and not page_id_set.intersection(related_page_ids):
            continue
        title = card.get("title") or card_id
        slug = card.get("slug") or _knowledge_slug(card_id)
        entries.append(f"- [{title}](../knowledge/{slug}.md)")
    return "\n".join(entries) if entries else "- 暂无知识卡片"


def _build_reference_links(
    reference_ids: Iterable[str],
    index_data: Mapping[str, Any],
    prefix: str,
    default: str,
) -> str:
    references = index_data.get("references") or {}
    entries: list[str] = []
    for reference_id in _as_string_items(reference_ids):
        reference = references.get(reference_id) or {}
        if not reference:
            continue
        title = reference.get("title") or reference_id
        entries.append(f"- [{title}]({prefix}{_reference_doc_path(reference_id, reference)})")
    return "\n".join(entries) if entries else default


def _reference_slug(reference_id: str) -> str:
    return str(reference_id or "").replace("reference.", "").replace("_", "-") or "reference"


def _knowledge_slug(card_id: str) -> str:
    return str(card_id or "").replace("knowledge.", "").replace("_", "-") or "knowledge"


def _reference_doc_path(reference_id: str, reference: Mapping[str, Any] | None = None) -> str:
    reference = reference or {}
    slug = reference.get("slug") or _reference_slug(reference_id)
    return f"references/{slug}.md"


def _page_doc_path(page_id: str, index_data: Mapping[str, Any]) -> str:
    page_info = (index_data.get("pages") or {}).get(page_id) or {}
    volume_id = str(page_info.get("volume_id") or "root").strip() or "root"
    return f"volumes/{volume_id}/pages/{page_id}.md"


def _to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _markdown_list(items: Iterable[Any], default: str) -> str:
    rows: list[str] = []
    for item in _as_string_items(items):
        text = str(item or "").strip()
        if text:
            rows.append(f"- {text}")
    return "\n".join(rows) if rows else default


def _as_string_items(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            result.append(text)
    return result


def _extract_page_id(task_id: str) -> str:
    parts = task_id.split(".")
    if len(parts) >= 2 and parts[0] == "page":
        return parts[1]
    return ""


def _extract_leaf_page_id(task_id: str) -> str:
    parts = task_id.split(".")
    if len(parts) >= 3 and parts[0] == "page_leaf":
        return parts[1]
    return ""


def _is_page_task_kind(kind: str) -> bool:
    return kind in {"page", "page_leaf_finalize"}


def _is_leaf_page_stage_kind(kind: str) -> bool:
    return kind in {"page_leaf_draft", "page_leaf_review", "page_leaf_enrich"}


def _extract_task_page_id(task: TaskRecord) -> str:
    if task.kind == "page":
        return _extract_page_id(task.task_id)
    if task.kind == "page_leaf_finalize" or _is_leaf_page_stage_kind(task.kind):
        return _extract_leaf_page_id(task.task_id)
    return ""


def _extract_volume_id(task_id: str) -> str:
    parts = task_id.split(".")
    if len(parts) >= 2 and parts[0] == "volume":
        return parts[1]
    return ""


def _extract_knowledge_card_id(task_id: str) -> str:
    return str(task_id or "").replace("knowledge.", "") or "knowledge"


def _extract_flow_page(task_id: str) -> str:
    parts = task_id.split(".")
    if len(parts) >= 3 and parts[0] == "flow" and parts[1] == "page":
        return parts[2]
    return ""


def _build_book_context(progress: ProgressState) -> Mapping[str, Any]:
    inputs = progress.inputs if isinstance(progress.inputs, Mapping) else {}
    catalog = inputs.get("catalog") if isinstance(inputs.get("catalog"), Mapping) else {}
    page_catalog = catalog.get("pages") if isinstance(catalog.get("pages"), Mapping) else {}
    volume_catalog = catalog.get("volumes") if isinstance(catalog.get("volumes"), Mapping) else {}
    menu_tree = catalog.get("menu_tree") if isinstance(catalog.get("menu_tree"), list) else []
    volumes: list[Mapping[str, str]] = []
    pages: list[Mapping[str, str]] = []
    apis: list[Mapping[str, str]] = []
    features: list[Mapping[str, str]] = []
    topics: list[Mapping[str, str]] = []
    knowledge_cards: list[Mapping[str, str]] = []
    references: list[Mapping[str, str]] = []
    dictionary_books: list[Mapping[str, str]] = []
    indexes: list[Mapping[str, str]] = []
    page_status_map = _build_page_status_map(progress.page_review_state)
    for task in sorted(progress.tasks.values(), key=lambda record: record.task_id):
        if task.kind == "volume":
            volume_id = _extract_volume_id(task.task_id)
            volume_title = str(
                ((volume_catalog.get(volume_id) or {}).get("title"))
                or task.task_id
            ).strip()
            volumes.append({"title": volume_title, "path": task.output})
        if _is_page_task_kind(task.kind):
            page_id = _extract_task_page_id(task)
            page_title = str(
                ((page_catalog.get(page_id) or {}).get("title"))
                or page_id
                or task.task_id
            ).strip()
            pages.append({"title": page_title, "path": task.output})
        if task.kind == "api":
            apis.append({"title": task.task_id, "path": task.output})
        if task.kind == "flow" and str(task.task_id or "").startswith("feature."):
            features.append({"title": task.task_id, "path": task.output})
        if task.kind == "topic":
            topics.append({"title": task.task_id, "path": task.output})
        if task.kind == "knowledge_card":
            knowledge_cards.append({"title": task.task_id, "path": task.output})
        if task.kind == "reference":
            references.append({"title": task.task_id, "path": task.output})
        if task.kind == "dictionary_index":
            dictionary_books.append({"title": "字段字典", "path": task.output})
        if task.kind == "index":
            indexes.append({"title": task.task_id, "path": task.output})
    completed = sum(1 for entry in progress.tasks.values() if entry.status == "done")
    total = len(progress.tasks)
    return {
        "volumes": volumes,
        "pages": pages,
        "apis": apis,
        "features": features,
        "topics": topics,
        "knowledge_cards": knowledge_cards,
        "references": references,
        "dictionary_books": dictionary_books,
        "indexes": indexes,
        "page_links_markdown": _build_menu_links(progress, page_catalog, menu_tree),
        "menu_links": _build_menu_links(progress, page_catalog, menu_tree),
        "page_review_summary": _build_page_review_summary(progress, page_catalog),
        "scan_summary": inputs.get("scan_summary") if isinstance(inputs.get("scan_summary"), Mapping) else {},
        "warnings": _as_string_items(inputs.get("warnings") or []),
        "stats": {"completed_tasks": completed, "total_tasks": total},
    }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _build_scan_summary(index_data: Mapping[str, Any]) -> Mapping[str, int]:
    return {
        "page_count": len(index_data.get("pages") or {}),
        "api_count": len(index_data.get("apis") or {}),
        "topic_count": len(index_data.get("topics") or {}),
    }


def _build_catalog_snapshot(index_data: Mapping[str, Any]) -> Mapping[str, Any]:
    pages = index_data.get("pages") or {}
    volumes = index_data.get("volumes") or {}
    return {
        "pages": {
            str(page_id): {
                "title": str((page or {}).get("title") or "").strip(),
                "parent_page_id": str((page or {}).get("parent_page_id") or "").strip(),
            }
            for page_id, page in pages.items()
        },
        "volumes": {
            str(volume_id): {
                "title": str((volume or {}).get("title") or "").strip(),
            }
            for volume_id, volume in volumes.items()
        },
        "menu_tree": index_data.get("navigation", {}).get("menu_tree") or [],
    }


def _build_menu_links(
    progress: ProgressState,
    page_catalog: Mapping[str, Any],
    menu_tree: list[Mapping[str, Any]] | None = None,
) -> str:
    page_status_map = _build_page_status_map(progress.page_review_state)
    page_entries: dict[str, dict[str, str]] = {}
    for task in sorted(progress.tasks.values(), key=lambda record: record.task_id):
        if not _is_page_task_kind(task.kind):
            continue
        page_id = _extract_task_page_id(task)
        if not page_id:
            continue
        catalog_row = page_catalog.get(page_id) if isinstance(page_catalog, Mapping) else {}
        title = str((catalog_row or {}).get("title") or page_id).strip() or page_id
        parent_page_id = str((catalog_row or {}).get("parent_page_id") or "").strip()
        page_entries[page_id] = {
            "title": _page_title_with_status(title, page_status_map.get(page_id, "")),
            "path": task.output,
            "parent_page_id": parent_page_id,
        }
    if not page_entries:
        return "- 暂无页面导航"

    if menu_tree:
        rendered = _render_menu_tree_links(menu_tree, page_entries)
        if rendered:
            return rendered

    children_by_parent: dict[str, list[str]] = {}
    root_ids: list[str] = []
    for page_id in sorted(page_entries):
        parent_page_id = page_entries[page_id]["parent_page_id"]
        if parent_page_id and parent_page_id in page_entries:
            children_by_parent.setdefault(parent_page_id, []).append(page_id)
        else:
            root_ids.append(page_id)

    lines: list[str] = []

    def render(page_id: str, depth: int) -> None:
        entry = page_entries[page_id]
        indent = "  " * depth
        lines.append(f"{indent}- [{entry['title']}]({entry['path']})")
        for child_id in children_by_parent.get(page_id, []):
            render(child_id, depth + 1)

    for root_id in root_ids:
        render(root_id, 0)
    return "\n".join(lines) if lines else "- 暂无页面导航"


def _render_menu_tree_links(menu_tree: Iterable[Mapping[str, Any]], page_entries: Mapping[str, Mapping[str, str]]) -> str:
    lines: list[str] = []

    def render(nodes: Iterable[Mapping[str, Any]], depth: int) -> None:
        for node in nodes:
            if not isinstance(node, Mapping):
                continue
            page_id = str(node.get("page_id") or "").strip()
            if not page_id:
                continue
            entry = page_entries.get(page_id)
            if entry:
                indent = "  " * depth
                lines.append(f"{indent}- [{entry['title']}]({entry['path']})")
            render(node.get("children") or [], depth + 1)

    render(menu_tree, 0)
    return "\n".join(lines)


def _build_page_status_map(page_review_state: Mapping[str, Any] | None) -> dict[str, str]:
    if not isinstance(page_review_state, Mapping):
        return {}
    status_map: dict[str, str] = {}
    for page_id, raw_state in page_review_state.items():
        if not isinstance(raw_state, Mapping):
            continue
        status = str(raw_state.get("final_status") or "").strip()
        if status:
            status_map[str(page_id)] = status
    return status_map


def _build_page_review_summary(
    progress: ProgressState,
    page_catalog: Mapping[str, Any],
) -> Mapping[str, list[Mapping[str, str]]]:
    summary: dict[str, list[Mapping[str, str]]] = {"complete": [], "partial": [], "blocked": []}
    page_tasks = {
        _extract_task_page_id(task): task
        for task in progress.tasks.values()
        if _is_page_task_kind(task.kind)
    }
    for page_id, raw_state in sorted((progress.page_review_state or {}).items()):
        if not isinstance(raw_state, Mapping):
            continue
        status = str(raw_state.get("final_status") or "").strip()
        if status not in summary:
            continue
        task = page_tasks.get(str(page_id))
        if task is None:
            continue
        catalog_row = page_catalog.get(page_id) if isinstance(page_catalog, Mapping) else {}
        title = str((catalog_row or {}).get("title") or page_id).strip() or str(page_id)
        summary[status].append(
            {
                "title": title,
                "path": task.output,
                "status": status,
                "page_id": str(page_id),
            }
        )
    return summary


def _page_title_with_status(title: str, status: str) -> str:
    normalized_title = str(title or "").strip()
    normalized_status = str(status or "").strip()
    if normalized_title and normalized_status:
        return f"{normalized_title}（{normalized_status}）"
    return normalized_title


def _build_review_checklist_markdown() -> str:
    return _markdown_list(PromptBuilder.LEAF_REVIEW_CHECK_ITEMS, default="- 无")


def _build_entry_conditions_section(page_info: Mapping[str, Any]) -> str:
    conditions: list[str] = []
    if str(page_info.get("requires_auth") or "").strip():
        conditions.append("需要登录后访问")
    route_path = str(page_info.get("route_path") or "").strip()
    if route_path:
        conditions.append(f"可通过路由 `{route_path}` 进入")
    return _markdown_list(conditions, default="- 未在当前静态扫描中确认")


def _build_steps_section(page_info: Mapping[str, Any], frontend_evidence: Mapping[str, Any]) -> str:
    steps: list[str] = []
    for call in frontend_evidence.get("frontend_calls") or []:
        if not isinstance(call, Mapping):
            continue
        for method_name in _as_string_items(call.get("method_names") or []):
            steps.append(f"触发前端方法 `{method_name}`")
    if not steps:
        title = str(page_info.get("title") or page_info.get("page_id") or "").strip()
        if title:
            steps.append(f"进入“{title}”页面后执行页面默认加载逻辑")
    return _markdown_list(steps, default="- 未在当前静态扫描中确认")


def _build_request_response_section(page_info: Mapping[str, Any], index_data: Mapping[str, Any]) -> str:
    rows: list[str] = []
    apis = index_data.get("apis") or {}
    for api_id in _as_string_items(page_info.get("api_ids") or []):
        api = apis.get(api_id) if isinstance(apis, Mapping) else {}
        path = str((api or {}).get("path") or "").strip()
        method = str((api or {}).get("method") or "").upper().strip()
        if path:
            rows.append(f"{method} {path}：请求/响应字段未在当前静态扫描中确认")
    return _markdown_list(rows, default="- 未在当前静态扫描中确认")


def _build_table_field_section(table_links: str, db_field_links: str) -> str:
    rows: list[str] = []
    if str(table_links or "").strip() and str(table_links).strip() != "- 暂无关联数据表":
        rows.append(f"关联数据表：{table_links}")
    if str(db_field_links or "").strip() and str(db_field_links).strip() != "- 暂无关联数据库字段":
        rows.append(f"关联字段：{db_field_links}")
    return _markdown_list(rows, default="- 未在当前静态扫描中确认")


def _build_permission_section(frontend_evidence: Mapping[str, Any]) -> str:
    rows = [f"权限点：`{item}`" for item in _as_string_items(frontend_evidence.get("permission_points") or [])]
    return _markdown_list(rows, default="- 暂无权限点")


def _build_exception_section(frontend_evidence: Mapping[str, Any]) -> str:
    rows = [f"异常分支：{item}" for item in _as_string_items(frontend_evidence.get("exception_flows") or [])]
    return _markdown_list(rows, default="- 暂无异常分支")


def _build_related_pages_section(
    page_id: str,
    index_data: Mapping[str, Any],
    frontend_evidence: Mapping[str, Any],
) -> str:
    related_page_ids = set(_as_string_items(frontend_evidence.get("related_page_ids") or []))
    for row in (index_data.get("relations") or {}).get("page_transitions", []):
        from_page_id = str(row.get("from_page_id") or row.get("from") or "").strip()
        to_page_id = str(row.get("to_page_id") or row.get("to") or "").strip()
        if from_page_id == page_id and to_page_id:
            related_page_ids.add(to_page_id)
        if to_page_id == page_id and from_page_id:
            related_page_ids.add(from_page_id)
    related_page_ids.discard(page_id)
    rows: list[str] = []
    for related_page_id in sorted(related_page_ids):
        title = _page_link_title(related_page_id, index_data)
        rows.append(f"关联页面：[{title}](../../../{_page_doc_path(related_page_id, index_data)})")
    return _markdown_list(rows, default="- 暂无关联页面")


def _build_business_logic_section(
    page_info: Mapping[str, Any],
    coverage: Mapping[str, Any],
) -> str:
    title = str(page_info.get("title") or page_info.get("page_id") or "当前页面").strip()
    if coverage and any(bool(value) for value in coverage.values()):
        return f"- {title} 的业务逻辑已进入叶子页复审闭环，终稿需按 11 项清单补齐。"
    return "- 未在当前静态扫描中确认"


def _build_flow_section(page_id: str, index_data: Mapping[str, Any]) -> str:
    mermaid = _build_page_backend_mermaid(page_id, index_data)
    return f"```mermaid\n{mermaid}\n```" if mermaid.strip() else "- 暂无流程图"


def _rows_by_key(rows: Any, key_field: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if isinstance(rows, Mapping):
        for key, value in rows.items():
            if isinstance(value, Mapping):
                row = dict(value)
            else:
                row = {key_field: key, "value": value}
            row.setdefault(key_field, key)
            normalized_key = str(row.get(key_field) or "").strip()
            if normalized_key:
                result[normalized_key] = row
        return result
    for value in rows:
        if not isinstance(value, Mapping):
            continue
        row = dict(value)
        normalized_key = str(row.get(key_field) or "").strip()
        if normalized_key:
            result[normalized_key] = row
    return result


def _table_belongs_to_database(table: Mapping[str, Any], database_id: str) -> bool:
    if str(table.get("database_id") or "").strip() == database_id:
        return True
    return str(table.get("schema") or "").strip() == database_id


def _dictionary_suffix(raw_id: Any) -> str:
    return str(raw_id or "").strip().replace(":", ".")


def _dictionary_original_id(task_id: str, prefix: str) -> str:
    return str(task_id or "").replace(prefix, "", 1).replace(".", ":", 1)


def _find_dictionary_row(
    rows: Any,
    key_field: str,
    task_id: str,
    prefix: str,
) -> Mapping[str, Any]:
    suffix = str(task_id or "").replace(prefix, "", 1)
    for row in _rows_by_key(rows, key_field).values():
        row_id = str(row.get(key_field) or "").strip()
        if _dictionary_suffix(row_id) == suffix:
            return row
    return {}


def _markdown_links(entries: Iterable[tuple[str, str]], default: str) -> str:
    rows = [f"- [{title}]({path})" for title, path in entries if title and path]
    return "\n".join(rows) if rows else default


def _markdown_code_lines(items: Iterable[Any], default: str) -> str:
    rows = []
    for item in items:
        text = str(item or "").strip()
        if text:
            rows.append(f"- `{text}`")
    return "\n".join(rows) if rows else default


def _format_nullable(value: Any) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    text = str(value or "").strip().lower()
    if text in {"true", "yes", "y", "1", "是"}:
        return "是"
    if text in {"false", "no", "n", "0", "否"}:
        return "否"
    return text


def _escape_markdown_cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", "<br>")


def _markdown_table(headers: list[str], rows: list[list[str]], default: str) -> str:
    if not rows:
        return default
    header_line = "| " + " | ".join(_escape_markdown_cell(header) for header in headers) + " |"
    separator_line = "| " + " | ".join(["---"] * len(headers)) + " |"
    body_lines = [
        "| " + " | ".join(_escape_markdown_cell(cell) for cell in row) + " |"
        for row in rows
    ]
    return "\n".join([header_line, separator_line, *body_lines])
