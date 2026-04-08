from __future__ import annotations

from pathlib import Path
from string import Template
from typing import Any, Mapping

from .models import TaskRecord


class DocumentGenerator:
    """最小的模板驱动文档生成器。"""

    TEMPLATE_FILES = {
        "api": Path("api/api.md.tmpl"),
        "flow": Path("flow/page-main-flow.md.tmpl"),
        "subflow": Path("flow/subflow.md.tmpl"),
        "page": Path("page/index.md.tmpl"),
        "page_interfaces": Path("page/interfaces.md.tmpl"),
        "volume": Path("volume/index.md.tmpl"),
        "topic": Path("topic/topic.md.tmpl"),
        "knowledge_card": Path("knowledge/card.md.tmpl"),
        "reference": Path("reference/reference.md.tmpl"),
        "index": Path("index/book_index.json.tmpl"),
        "dictionary_index": Path("dictionary/README.md.tmpl"),
        "dictionary_database": Path("dictionary/database_index.md.tmpl"),
        "dictionary_table": Path("dictionary/table.md.tmpl"),
        "dictionary_db_field": Path("dictionary/db_field.md.tmpl"),
        "dictionary_form_field": Path("dictionary/form_field.md.tmpl"),
        "dictionary_grid_column": Path("dictionary/grid_column.md.tmpl"),
        "dictionary_model": Path("dictionary/model.md.tmpl"),
    }

    _FEATURE_FLOW_TEMPLATE = """# 完整功能主线：${title}

${summary}

```mermaid
${mermaid}
```

## 关联页面
${page_links}

## 关联接口
${api_links}

> 自动生成的文档（task_id=${task_id}）
"""

    def __init__(self, template_root: Path | str, output_root: Path | str) -> None:
        self.template_root = Path(template_root).resolve()
        self.output_root = Path(output_root).resolve()
        self._template_cache: dict[Path, str] = {}

    def generate(self, task: TaskRecord, context: Mapping[str, Any] | None = None) -> Path:
        template_text = self._get_template_text(task)
        template_data = self._build_template_data(task, context)
        try:
            rendered = Template(template_text).substitute(template_data)
        except KeyError as exc:
            raise ValueError(f"模板字段缺失：{exc.args[0]}，kind={task.kind}") from exc
        return self.write_markdown(task, rendered)

    def write_markdown(self, task: TaskRecord, markdown_text: str) -> Path:
        if not isinstance(markdown_text, str):
            raise TypeError("Markdown 内容必须是字符串")
        if not markdown_text.strip():
            raise ValueError("Markdown 内容不能为空")
        output_path = self._resolve_output_path(task.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(markdown_text, encoding="utf-8")
        return output_path

    def _get_template_text(self, task: TaskRecord) -> str:
        if self._is_feature_flow(task):
            return self._FEATURE_FLOW_TEMPLATE
        if task.kind == "index":
            if task.task_id == "index.book":
                return self._get_template_text_from(Path("index/book_index.json.tmpl"))
            if task.task_id == "index.relations":
                return self._get_template_text_from(Path("index/relations.json.tmpl"))
            if task.task_id == "index.navigation":
                return self._get_template_text_from(Path("index/navigation.json.tmpl"))
            raise ValueError(f"不支持的索引任务：{task.task_id}")
        return self._get_template_text_for_kind(task.kind)

    def _get_template_text_for_kind(self, kind: str) -> str:
        normalized_kind = self._normalize_template_kind(kind)
        template_path = self.TEMPLATE_FILES.get(normalized_kind)
        if not template_path:
            raise ValueError(f"不支持的任务类型：{kind}")
        return self._get_template_text_from(template_path)

    def _get_template_text_from(self, template_path: Path) -> str:
        resolved_path = self.template_root / template_path
        if resolved_path not in self._template_cache:
            self._template_cache[resolved_path] = resolved_path.read_text(encoding="utf-8")
        return self._template_cache[resolved_path]

    def _build_template_data(
        self, task: TaskRecord, context: Mapping[str, Any] | None
    ) -> dict[str, str]:
        template_data: dict[str, str] = {"task_id": task.task_id, "task_kind": task.kind}
        if context:
            for key, raw_value in context.items():
                template_data[key] = self._flatten_value(raw_value)
        if self._is_feature_flow(task):
            self._apply_feature_defaults(task, template_data)
        if task.kind == "api":
            self._apply_api_defaults(template_data)
        if self._normalize_template_kind(task.kind) == "page":
            self._apply_page_defaults(template_data)
        if task.kind.startswith("dictionary_"):
            self._apply_dictionary_defaults(task.kind, template_data)
        return template_data

    def _resolve_output_path(self, raw_output: str) -> Path:
        if not raw_output:
            raise ValueError("输出路径不能为空")
        candidate = Path(raw_output)
        if candidate.is_absolute():
            raise ValueError("输出路径不能是绝对路径")
        target_path = (self.output_root / candidate).resolve()
        try:
            target_path.relative_to(self.output_root)
        except ValueError as exc:
            raise ValueError("输出路径必须位于 output_root 内") from exc
        return target_path

    def _is_feature_flow(self, task: TaskRecord) -> bool:
        # Task 2 兼容方案：feature 节点暂时复用 flow kind 承载，这里用 task_id 模式做分流。
        return task.kind == "flow" and str(task.task_id or "").startswith("feature.")

    def _normalize_template_kind(self, kind: str) -> str:
        if str(kind or "").startswith("page_leaf_"):
            return "page"
        return kind

    def _apply_feature_defaults(self, task: TaskRecord, template_data: dict[str, str]) -> None:
        raw_title = (template_data.get("title") or "").strip()
        if not raw_title:
            raw_title = task.task_id
        normalized = raw_title
        if normalized.endswith(" 主流程"):
            normalized = normalized[: -len(" 主流程")].strip()
        template_data["title"] = normalized
        template_data.setdefault("summary", "自动生成的完整功能主线文档。")
        template_data.setdefault("mermaid", "flowchart LR\n    Start --> End")
        template_data.setdefault("page_links", "- 暂无关联页面")
        template_data.setdefault("api_links", "- 暂无关联接口")

    def _apply_page_defaults(self, template_data: dict[str, str]) -> None:
        template_data.setdefault("description", "自动生成的页面摘要；当前版本仅保证页面 ID、路由和关联 API 信息可靠。")
        template_data.setdefault("page_status", "draft")
        template_data.setdefault("review_checklist", "- 页面说明")
        template_data.setdefault("remaining_gaps", "- 无")
        template_data.setdefault("resolved_gaps", "- 无")
        template_data.setdefault("entry_conditions_section", "- 未在当前静态扫描中确认")
        template_data.setdefault("steps_section", "- 未在当前静态扫描中确认")
        template_data.setdefault("request_response_section", "- 未在当前静态扫描中确认")
        template_data.setdefault("table_field_section", "- 未在当前静态扫描中确认")
        template_data.setdefault("permission_section", "- 暂无权限点")
        template_data.setdefault("exception_section", "- 暂无异常分支")
        template_data.setdefault("related_pages_section", "- 暂无关联页面")
        template_data.setdefault("business_logic_section", "- 未在当前静态扫描中确认")
        template_data.setdefault("flow_section", "- 暂无流程图")
        template_data.setdefault("api_section", "- 暂无 API")
        template_data.setdefault("subflow_section", "- 暂无子流程")
        template_data.setdefault("topic_section", "- 暂无专题")
        template_data.setdefault("note_section", "- 暂无批注")
        template_data.setdefault("reference_section", "- 暂无延伸阅读")
        template_data.setdefault(
            "coverage_note",
            "- 能力边界：当前版本主要基于静态扫描结果，暂未恢复组件状态与表单约束。",
        )

    def _apply_api_defaults(self, template_data: dict[str, str]) -> None:
        template_data.setdefault("description", "自动生成的 API 摘要；当前版本仅保证方法、路径和模块信息可靠。")
        template_data.setdefault("request_params", "- 当前版本未解析请求参数结构")
        template_data.setdefault("response_fields", "- 当前版本未解析响应字段结构")
        template_data.setdefault(
            "coverage_note",
            "- 能力边界：当前版本暂未解析鉴权、请求体 schema 与响应字段细节。",
        )

    def _apply_dictionary_defaults(self, kind: str, template_data: dict[str, str]) -> None:
        if kind == "dictionary_index":
            template_data.setdefault("title", "字段字典")
            template_data.setdefault("database_links", "- 暂无数据库")
            template_data.setdefault("table_links", "- 暂无数据表")
            template_data.setdefault("db_field_links", "- 暂无数据库字段")
            template_data.setdefault("form_field_links", "- 暂无表单字段")
            template_data.setdefault("grid_column_links", "- 暂无表格列")
            template_data.setdefault("model_links", "- 暂无 Go Model")
            return
        if kind == "dictionary_database":
            template_data.setdefault("title", template_data.get("database_id", "数据库"))
            template_data.setdefault("table_links", "- 暂无数据表")
            template_data.setdefault("data_source", "- `MySQL information_schema`")
            return
        if kind == "dictionary_table":
            template_data.setdefault("title", template_data.get("table_id", "数据表"))
            template_data.setdefault("schema", "")
            template_data.setdefault("table_name", "")
            template_data.setdefault("data_source", "- `MySQL information_schema`")
            template_data.setdefault("index_section", "- 暂无索引")
            template_data.setdefault("field_table", "- 暂无字段")
            template_data.setdefault("usage_links", "- 暂无使用页面")
            return
        if kind == "dictionary_db_field":
            template_data.setdefault("title", template_data.get("field_id", "数据库字段"))
            template_data.setdefault("table_link", "- 暂无所属数据表")
            template_data.setdefault("field_name", "")
            template_data.setdefault("field_type", "")
            template_data.setdefault("comment", "")
            template_data.setdefault("nullable", "")
            template_data.setdefault("default_value", "")
            template_data.setdefault("data_source", "MySQL information_schema")
            template_data.setdefault("usage_links", "- 暂无使用页面")
            return
        if kind == "dictionary_form_field":
            template_data.setdefault("title", template_data.get("field_id", "表单字段"))
            template_data.setdefault("page_link", "- 暂无所属页面")
            template_data.setdefault("prop", "")
            template_data.setdefault("label", "")
            template_data.setdefault("source_file", "")
            template_data.setdefault("mapping_links", "- 暂无字段映射")
            return
        if kind == "dictionary_grid_column":
            template_data.setdefault("title", template_data.get("column_id", "表格列"))
            template_data.setdefault("page_link", "- 暂无所属页面")
            template_data.setdefault("prop", "")
            template_data.setdefault("label", "")
            template_data.setdefault("source_file", "")
            template_data.setdefault("mapping_links", "- 暂无字段映射")
            return
        if kind == "dictionary_model":
            template_data.setdefault("title", template_data.get("model_name", "Go Model"))
            template_data.setdefault("model_id", "")
            template_data.setdefault("source_file", "")
            template_data.setdefault("field_section", "- 暂无模型字段")

    def _flatten_value(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float, bool)):
            return str(value)
        if isinstance(value, Mapping):
            return ", ".join(f"{key}: {self._flatten_value(val)}" for key, val in value.items())
        if isinstance(value, (list, tuple)):
            lines = []
            for item in value:
                flattened = self._flatten_value(item)
                lines.append(f"- {flattened}")
            return "\n".join(lines)
        if isinstance(value, set):
            try:
                return ", ".join(str(item) for item in sorted(value))
            except TypeError:
                return ", ".join(str(item) for item in value)
        return str(value)
