from __future__ import annotations

from typing import Any, Iterable, Mapping

from .models import TaskRecord


class TaskPlanner:
    """把 index_builder 的输出拆成可追踪任务。"""

    def build(self, index_data: Mapping[str, Any]) -> list[TaskRecord]:
        tasks: list[TaskRecord] = []
        volumes = index_data.get("volumes") or {}
        pages = index_data.get("pages") or {}
        apis_map = index_data.get("apis") or {}
        features = index_data.get("features") or {}
        topics = index_data.get("topics") or {}
        knowledge_cards = index_data.get("knowledge_cards") or {}
        references = index_data.get("references") or {}
        databases = self._rows_by_key(index_data.get("databases") or (), "database_id")
        tables = self._rows_by_key(index_data.get("tables") or (), "table_id")
        db_fields = self._rows_by_key(index_data.get("db_fields") or (), "field_id")
        go_models = self._rows_by_key(index_data.get("go_models") or (), "model_id")
        form_fields = self._rows_by_key(index_data.get("form_fields") or (), "field_id")
        grid_columns = self._rows_by_key(index_data.get("grid_columns") or (), "column_id")
        known_api_ids = set(apis_map)
        task_ids: set[str] = set()
        volume_task_ids: list[str] = []
        for volume_id in sorted(volumes):
            volume = volumes[volume_id] or {}
            page_deps = self._page_dependencies(volume.get("page_ids") or [], pages)
            volume_task_id = f"volume.{volume_id}"
            self._ensure_unique_task_id(volume_task_id, task_ids)
            tasks.append(
                TaskRecord(
                    task_id=volume_task_id,
                    kind="volume",
                    output=f"volumes/{volume_id}/index.md",
                    depends_on=page_deps,
                )
            )
            volume_task_ids.append(volume_task_id)

        page_task_ids: list[str] = []
        for page_id in sorted(pages):
            self._validate_page_id(page_id)
            page = pages[page_id] or {}
            volume_id = str(page.get("volume_id") or "root").strip() or "root"
            final_page_task_id = self._final_page_task_id(page_id, page)

            if bool(page.get("is_leaf")):
                stage_output_prefix = f"volumes/{volume_id}/pages/{page_id}"
                previous_stage: str | None = None
                for stage, stage_kind in (
                    ("draft", "page_leaf_draft"),
                    ("review.round1", "page_leaf_review"),
                    ("enrich.round1", "page_leaf_enrich"),
                    ("finalize", "page_leaf_finalize"),
                ):
                    stage_task_id = f"page_leaf.{page_id}.{stage}"
                    self._ensure_unique_task_id(stage_task_id, task_ids)
                    depends_on = [previous_stage] if previous_stage else []
                    output = (
                        f"{stage_output_prefix}.md"
                        if stage == "finalize"
                        else f"{stage_output_prefix}.{stage}.md"
                    )
                    tasks.append(
                        TaskRecord(
                            task_id=stage_task_id,
                            kind=stage_kind,
                            output=output,
                            depends_on=depends_on,
                        )
                    )
                    previous_stage = stage_task_id
                page_task_ids.append(final_page_task_id)
            else:
                self._ensure_unique_task_id(final_page_task_id, task_ids)
                tasks.append(
                    TaskRecord(
                        task_id=final_page_task_id,
                        kind="page",
                        output=f"volumes/{volume_id}/pages/{page_id}.md",
                    )
                )
                page_task_ids.append(final_page_task_id)

            subflows: list[str] = []
            for subfeature in self._unique_in_order(page.get("subfeatures") or []):
                task_id = f"subflow.page.{page_id}.{subfeature}"
                self._ensure_unique_task_id(task_id, task_ids)
                tasks.append(
                    TaskRecord(
                        task_id=task_id,
                        kind="subflow",
                        output=f"pages/{page_id}/subflows/{subfeature}.md",
                    )
                )
                subflows.append(task_id)

            api_ids = self._unique_in_order(page.get("api_ids") or [])
            flow_task_id = f"flow.page.{page_id}.main"
            self._ensure_unique_task_id(flow_task_id, task_ids)
            tasks.append(
                TaskRecord(
                    task_id=flow_task_id,
                    kind="flow",
                    output=f"pages/{page_id}/flows/page-main-flow.md",
                    depends_on=subflows + [api_id for api_id in api_ids if api_id in known_api_ids],
                )
            )

        api_task_ids: list[str] = []
        # 最后再按稳定顺序追加 api 任务
        for api_id in sorted(apis_map):
            api = apis_map[api_id] or {}
            module = api.get("module") or "api"
            name = api.get("name")
            if name:
                output = f"backend/apis/{module}/{name}.md"
            else:
                sanitized = api_id.replace(":", ".").replace("/", "_")
                output = f"backend/apis/{module}/{sanitized}.md"

            self._ensure_unique_task_id(api_id, task_ids)
            tasks.append(
                TaskRecord(
                    task_id=api_id,
                    kind="api",
                    output=output,
                )
            )
            api_task_ids.append(api_id)

        feature_task_ids: list[str] = []
        for feature_id in sorted(features):
            feature = features[feature_id] or {}
            page_deps = self._page_dependencies(feature.get("page_ids") or [], pages)
            self._ensure_unique_task_id(feature_id, task_ids)
            tasks.append(
                TaskRecord(
                    task_id=feature_id,
                    kind="flow",
                    output=f"features/{feature_id}.md",
                    depends_on=page_deps,
                )
            )
            feature_task_ids.append(feature_id)

        topic_task_ids: list[str] = []
        for topic_id in sorted(topics):
            topic = topics[topic_id] or {}
            page_deps = self._page_dependencies(topic.get("page_ids") or [], pages)
            api_deps = [
                api_id
                for api_id in self._unique_in_order(topic.get("api_ids") or [])
                if api_id in known_api_ids
            ]
            self._ensure_unique_task_id(topic_id, task_ids)
            tasks.append(
                TaskRecord(
                    task_id=topic_id,
                    kind="topic",
                    output=f"topics/{topic_id}.md",
                    depends_on=page_deps + api_deps,
                )
            )
            topic_task_ids.append(topic_id)

        knowledge_task_ids: list[str] = []
        for card_id in sorted(knowledge_cards):
            card = knowledge_cards[card_id] or {}
            slug = str(card.get("slug") or self._knowledge_slug(card_id)).strip() or self._knowledge_slug(card_id)
            self._ensure_unique_task_id(card_id, task_ids)
            tasks.append(
                TaskRecord(
                    task_id=card_id,
                    kind="knowledge_card",
                    output=f"knowledge/{slug}.md",
                )
            )
            knowledge_task_ids.append(card_id)

        reference_task_ids: list[str] = []
        for reference_id in sorted(references):
            reference = references[reference_id] or {}
            slug = str(reference.get("slug") or self._reference_slug(reference_id)).strip() or self._reference_slug(reference_id)
            self._ensure_unique_task_id(reference_id, task_ids)
            tasks.append(
                TaskRecord(
                    task_id=reference_id,
                    kind="reference",
                    output=f"references/{slug}.md",
                )
            )
            reference_task_ids.append(reference_id)

        dictionary_detail_task_ids: list[str] = []
        database_task_ids: list[str] = []
        for database_id in sorted(databases):
            task_id = f"dictionary.database.{database_id}"
            self._ensure_unique_task_id(task_id, task_ids)
            table_deps = [
                f"dictionary.table.{table_id}"
                for table_id, table in sorted(tables.items())
                if self._table_belongs_to_database(table, database_id)
            ]
            tasks.append(
                TaskRecord(
                    task_id=task_id,
                    kind="dictionary_database",
                    output=f"dictionary/databases/{database_id}.md",
                    depends_on=table_deps,
                )
            )
            database_task_ids.append(task_id)
            dictionary_detail_task_ids.append(task_id)

        table_task_ids: list[str] = []
        for table_id in sorted(tables):
            task_id = f"dictionary.table.{table_id}"
            self._ensure_unique_task_id(task_id, task_ids)
            field_deps = [
                f"dictionary.db_field.{field_id}"
                for field_id, field in sorted(db_fields.items())
                if str(field.get("table_id") or "").strip() == table_id
            ]
            tasks.append(
                TaskRecord(
                    task_id=task_id,
                    kind="dictionary_table",
                    output=f"dictionary/tables/{table_id}.md",
                    depends_on=field_deps,
                )
            )
            table_task_ids.append(task_id)
            dictionary_detail_task_ids.append(task_id)

        db_field_task_ids: list[str] = []
        for field_id in sorted(db_fields):
            task_id = f"dictionary.db_field.{field_id}"
            self._ensure_unique_task_id(task_id, task_ids)
            tasks.append(
                TaskRecord(
                    task_id=task_id,
                    kind="dictionary_db_field",
                    output=f"dictionary/db-fields/{field_id}.md",
                )
            )
            db_field_task_ids.append(task_id)
            dictionary_detail_task_ids.append(task_id)

        form_field_task_ids: list[str] = []
        for field_id in sorted(form_fields):
            task_suffix = self._dictionary_suffix(field_id)
            task_id = f"dictionary.form_field.{task_suffix}"
            self._ensure_unique_task_id(task_id, task_ids)
            tasks.append(
                TaskRecord(
                    task_id=task_id,
                    kind="dictionary_form_field",
                    output=f"dictionary/form-fields/{task_suffix}.md",
                )
            )
            form_field_task_ids.append(task_id)
            dictionary_detail_task_ids.append(task_id)

        grid_column_task_ids: list[str] = []
        for column_id in sorted(grid_columns):
            task_suffix = self._dictionary_suffix(column_id)
            task_id = f"dictionary.grid_column.{task_suffix}"
            self._ensure_unique_task_id(task_id, task_ids)
            tasks.append(
                TaskRecord(
                    task_id=task_id,
                    kind="dictionary_grid_column",
                    output=f"dictionary/grid-columns/{task_suffix}.md",
                )
            )
            grid_column_task_ids.append(task_id)
            dictionary_detail_task_ids.append(task_id)

        model_task_ids: list[str] = []
        for model_id in sorted(go_models):
            task_id = f"dictionary.model.{model_id}"
            self._ensure_unique_task_id(task_id, task_ids)
            tasks.append(
                TaskRecord(
                    task_id=task_id,
                    kind="dictionary_model",
                    output=f"dictionary/models/{model_id}.md",
                )
            )
            model_task_ids.append(task_id)
            dictionary_detail_task_ids.append(task_id)

        dictionary_task_ids: list[str] = []
        dictionary_task_id = "dictionary.readme"
        self._ensure_unique_task_id(dictionary_task_id, task_ids)
        tasks.append(
            TaskRecord(
                task_id=dictionary_task_id,
                kind="dictionary_index",
                output="dictionary/README.md",
                depends_on=self._unique_in_order(
                    database_task_ids
                    + table_task_ids
                    + form_field_task_ids
                    + grid_column_task_ids
                    + model_task_ids
                ),
            )
        )
        dictionary_task_ids.append(dictionary_task_id)

        index_task_ids: list[str] = []
        for name in ("book", "relations", "navigation"):
            index_task_id = f"index.{name}"
            self._ensure_unique_task_id(index_task_id, task_ids)
            output_name = "book_index.json" if name == "book" else f"{name}.json"
            tasks.append(
                TaskRecord(
                    task_id=index_task_id,
                    kind="index",
                    output=f"indexes/{output_name}",
                )
            )
            index_task_ids.append(index_task_id)

        book_dependencies = self._unique_in_order(
            volume_task_ids
            + page_task_ids
            + api_task_ids
            + feature_task_ids
            + topic_task_ids
            + knowledge_task_ids
            + reference_task_ids
            + dictionary_task_ids
            + index_task_ids
        )
        book_task_id = "book.assembly"
        self._ensure_unique_task_id(book_task_id, task_ids)
        tasks.append(
            TaskRecord(
                task_id=book_task_id,
                kind="flow",
                output="book/assembly.md",
                depends_on=book_dependencies,
            )
        )
        release_task_id = "release.snapshot"
        self._ensure_unique_task_id(release_task_id, task_ids)
        tasks.append(
            TaskRecord(
                task_id=release_task_id,
                kind="flow",
                output="release/snapshot.md",
                depends_on=[book_task_id],
            )
        )

        return tasks

    @staticmethod
    def _unique_in_order(items: Iterable[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for item in items:
            if item in seen:
                continue
            seen.add(item)
            result.append(item)
        return result

    @staticmethod
    def _ensure_unique_task_id(task_id: str, registry: set[str]) -> None:
        if task_id in registry:
            raise ValueError(f"任务 ID 冲突：{task_id}")
        registry.add(task_id)

    @staticmethod
    def _knowledge_slug(card_id: str) -> str:
        return str(card_id or "").replace("knowledge.", "").replace("_", "-") or "knowledge"

    @staticmethod
    def _reference_slug(reference_id: str) -> str:
        return str(reference_id or "").replace("reference.", "").replace("_", "-") or "reference"

    @staticmethod
    def _validate_page_id(page_id: str) -> None:
        if "." in str(page_id or ""):
            raise ValueError(f"页面 ID 不能包含 '.'：{page_id}")

    def _page_dependencies(
        self, page_ids: Iterable[str], pages: Mapping[str, Any]
    ) -> list[str]:
        result: list[str] = []
        for page_id in self._unique_in_order(page_ids):
            page = pages.get(page_id)
            if page is None:
                continue
            page_record = page if isinstance(page, Mapping) else {}
            result.append(self._final_page_task_id(page_id, page_record))
        return result

    @staticmethod
    def _final_page_task_id(page_id: str, page: Mapping[str, Any]) -> str:
        if bool(page.get("is_leaf")):
            return f"page_leaf.{page_id}.finalize"
        return f"page.{page_id}"

    @staticmethod
    def _rows_by_key(rows: Any, key_field: str) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        if isinstance(rows, Mapping):
            iterable = rows.items()
            for key, value in iterable:
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

    @staticmethod
    def _table_belongs_to_database(table: Mapping[str, Any], database_id: str) -> bool:
        if str(table.get("database_id") or "").strip() == database_id:
            return True
        return str(table.get("schema") or "").strip() == database_id

    @staticmethod
    def _dictionary_suffix(raw_id: Any) -> str:
        return str(raw_id or "").strip().replace(":", ".")
