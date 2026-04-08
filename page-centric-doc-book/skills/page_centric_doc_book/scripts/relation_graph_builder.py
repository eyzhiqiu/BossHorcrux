from __future__ import annotations

from itertools import combinations
import re
from typing import Any, Iterable, Mapping


class RelationGraphBuilder:
    """Build conservative links between pages, APIs, tables and fields."""

    def build(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        pages = self._normalize_rows(snapshot.get("pages") or (), key_field="page_id")
        apis = self._normalize_rows(snapshot.get("apis") or (), key_field="api_id")
        tables = self._normalize_rows(snapshot.get("tables") or (), key_field="table_id")
        db_fields = self._normalize_db_fields(snapshot.get("db_fields") or ())
        form_fields = self._normalize_rows(snapshot.get("form_fields") or ())
        grid_columns = self._normalize_rows(snapshot.get("grid_columns") or ())
        page_transitions = self._normalize_rows(snapshot.get("page_transitions") or ())

        page_api_ids = self._build_page_api_ids(pages, apis, snapshot.get("page_api_links") or ())
        db_field_candidates = self._index_db_fields_by_name(db_fields)
        tables_by_id = dict(tables)
        apis_by_id = dict(apis)

        page_db_field_links: list[dict[str, Any]] = []
        field_mappings: list[dict[str, Any]] = []
        page_table_map: dict[tuple[str, str], set[str]] = {}

        for source_kind, source_rows, id_field in (
            ("form_field", form_fields, "field_id"),
            ("grid_column", grid_columns, "column_id"),
        ):
            for row in source_rows:
                page_id = str(row.get("page_id") or "").strip()
                prop = str(row.get("prop") or "").strip()
                source_id = str(row.get(id_field) or "").strip()
                if not page_id or not prop or not source_id:
                    continue
                matched_fields = self._select_db_fields(
                    page=pages.get(page_id) or {},
                    api_ids=page_api_ids.get(page_id) or set(),
                    apis_by_id=apis_by_id,
                    tables_by_id=tables_by_id,
                    candidates=db_field_candidates.get(self._normalize_name(prop), []),
                )
                for db_field in matched_fields:
                    db_field_id = str(db_field.get("field_id") or "").strip()
                    table_id = str(db_field.get("table_id") or "").strip()
                    if not db_field_id or not table_id:
                        continue
                    field_mappings.append(
                        {
                            "mapping_id": f"{source_kind}:{source_id}->{db_field_id}",
                            "page_id": page_id,
                            "prop": prop,
                            "source_id": source_id,
                            "source_kind": source_kind,
                            "table_id": table_id,
                            "target_id": db_field_id,
                            "target_kind": "db_field",
                        }
                    )
                    page_db_field_links.append(
                        {
                            "page_id": page_id,
                            "db_field_id": db_field_id,
                            "table_id": table_id,
                            "source_id": source_id,
                            "source_kind": source_kind,
                        }
                    )
                    page_table_map.setdefault((page_id, table_id), set()).add(db_field_id)

        page_table_links = [
            {
                "page_id": page_id,
                "table_id": table_id,
                "db_field_ids": sorted(field_ids),
            }
            for (page_id, table_id), field_ids in sorted(page_table_map.items())
        ]
        page_db_field_links = self._sorted_rows(page_db_field_links)
        field_mappings = self._sorted_rows(field_mappings)
        business_domains = self._build_business_domains(
            pages=pages,
            page_api_ids=page_api_ids,
            apis_by_id=apis_by_id,
            page_table_links=page_table_links,
            page_db_field_links=page_db_field_links,
        )
        implicit_flows = self._build_implicit_flows(
            page_table_links=page_table_links,
            page_transitions=page_transitions,
        )
        page_frontend_calls = self._normalize_page_frontend_calls(
            snapshot.get("page_call_chains") or {}
        )
        return {
            "page_table_links": page_table_links,
            "page_db_field_links": page_db_field_links,
            "field_mappings": field_mappings,
            "business_domains": business_domains,
            "implicit_flows": implicit_flows,
            "page_frontend_calls": page_frontend_calls,
        }

    def _normalize_rows(
        self,
        rows: Iterable[Any] | Mapping[str, Any],
        *,
        key_field: str | None = None,
    ) -> list[dict[str, Any]] | dict[str, dict[str, Any]]:
        normalized_rows: list[dict[str, Any]] = []
        if isinstance(rows, Mapping):
            for key, value in rows.items():
                if isinstance(value, Mapping):
                    row = dict(value)
                else:
                    row = {key_field or "id": key, "value": value}
                if key_field and not row.get(key_field):
                    row[key_field] = str(key)
                normalized_rows.append(row)
        else:
            for value in rows:
                if isinstance(value, Mapping):
                    normalized_rows.append(dict(value))
                else:
                    normalized_rows.append({key_field or "id": value})
        normalized_rows = self._sorted_rows(normalized_rows)
        if not key_field:
            return normalized_rows
        keyed_rows: dict[str, dict[str, Any]] = {}
        for row in normalized_rows:
            key_value = str(row.get(key_field) or "").strip()
            if key_value:
                keyed_rows[key_value] = row
        return keyed_rows

    def _normalize_db_fields(self, rows: Iterable[Any] | Mapping[str, Any]) -> list[dict[str, Any]]:
        normalized = self._normalize_rows(rows)
        db_fields: list[dict[str, Any]] = []
        for row in normalized:
            field = dict(row)
            table_id = str(field.get("table_id") or "").strip()
            name = str(field.get("name") or "").strip()
            field_id = str(field.get("field_id") or "").strip()
            if not field_id and table_id and name:
                field["field_id"] = f"{table_id}.{name}"
            if field.get("field_id"):
                db_fields.append(field)
        return self._sorted_rows(db_fields)

    def _build_page_api_ids(
        self,
        pages: Mapping[str, Mapping[str, Any]],
        apis: Mapping[str, Mapping[str, Any]],
        page_api_links: Iterable[Any] | Mapping[str, Any],
    ) -> dict[str, set[str]]:
        result = {
            page_id: {
                str(api_id).strip()
                for api_id in (page.get("api_ids") or [])
                if str(api_id).strip() in apis
            }
            for page_id, page in pages.items()
        }
        for row in self._normalize_rows(page_api_links):
            page_id = str(row.get("page_id") or "").strip()
            api_id = str(row.get("api_id") or "").strip()
            if page_id in result and api_id in apis:
                result[page_id].add(api_id)
        return result

    def _index_db_fields_by_name(
        self, db_fields: Iterable[Mapping[str, Any]]
    ) -> dict[str, list[dict[str, Any]]]:
        indexed: dict[str, list[dict[str, Any]]] = {}
        for row in db_fields:
            name = self._normalize_name(row.get("name"))
            if not name:
                continue
            indexed.setdefault(name, []).append(dict(row))
        for name in indexed:
            indexed[name] = self._sorted_rows(indexed[name])
        return indexed

    def _select_db_fields(
        self,
        *,
        page: Mapping[str, Any],
        api_ids: set[str],
        apis_by_id: Mapping[str, Mapping[str, Any]],
        tables_by_id: Mapping[str, Mapping[str, Any]],
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not candidates:
            return []
        if len(candidates) == 1:
            return [dict(candidates[0])]

        context_tokens = self._collect_context_tokens(page=page, api_ids=api_ids, apis_by_id=apis_by_id)
        scored: list[tuple[int, dict[str, Any]]] = []
        for candidate in candidates:
            table = tables_by_id.get(str(candidate.get("table_id") or "").strip()) or {}
            score = self._score_candidate(table=table, candidate=candidate, context_tokens=context_tokens)
            scored.append((score, dict(candidate)))
        scored.sort(key=lambda item: (-item[0], item[1].get("field_id", "")))
        best_score = scored[0][0]
        if best_score <= 0:
            return []
        best_candidates = [row for score, row in scored if score == best_score]
        if len({str(row.get("table_id") or "").strip() for row in best_candidates}) > 1:
            return []
        return self._sorted_rows(best_candidates)

    def _collect_context_tokens(
        self,
        *,
        page: Mapping[str, Any],
        api_ids: Iterable[str],
        apis_by_id: Mapping[str, Mapping[str, Any]],
    ) -> set[str]:
        tokens: set[str] = set()
        page_id = str(page.get("page_id") or "").strip()
        route_path = str(page.get("route_path") or "").strip()
        tokens.update(self._tokenize(page_id))
        tokens.update(self._tokenize(route_path))
        for api_id in api_ids:
            api = apis_by_id.get(api_id) or {}
            tokens.update(self._tokenize(api.get("path")))
            tokens.update(self._tokenize(api_id))
        expanded_tokens = set(tokens)
        for token in list(tokens):
            singular = self._singularize(token)
            plural = self._pluralize(token)
            if singular:
                expanded_tokens.add(singular)
            if plural:
                expanded_tokens.add(plural)
        return {token for token in expanded_tokens if token}

    def _score_candidate(
        self,
        *,
        table: Mapping[str, Any],
        candidate: Mapping[str, Any],
        context_tokens: set[str],
    ) -> int:
        haystacks = [
            str(table.get("table_name") or ""),
            str(table.get("table_id") or ""),
            str(candidate.get("field_id") or ""),
        ]
        score = 0
        for token in context_tokens:
            if any(token in haystack.lower() for haystack in haystacks):
                score += 1
        return score

    def _build_business_domains(
        self,
        *,
        pages: Mapping[str, Mapping[str, Any]],
        page_api_ids: Mapping[str, set[str]],
        apis_by_id: Mapping[str, Mapping[str, Any]],
        page_table_links: Iterable[Mapping[str, Any]],
        page_db_field_links: Iterable[Mapping[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        domain_map: dict[str, dict[str, set[str]]] = {}
        page_table_ids: dict[str, set[str]] = {}
        for row in page_table_links:
            page_id = str(row.get("page_id") or "").strip()
            table_id = str(row.get("table_id") or "").strip()
            if page_id and table_id:
                page_table_ids.setdefault(page_id, set()).add(table_id)
        page_db_field_ids: dict[str, set[str]] = {}
        for row in page_db_field_links:
            page_id = str(row.get("page_id") or "").strip()
            db_field_id = str(row.get("db_field_id") or "").strip()
            if page_id and db_field_id:
                page_db_field_ids.setdefault(page_id, set()).add(db_field_id)

        for page_id, page in pages.items():
            domain_id = self._derive_page_domain(page)
            if not domain_id:
                continue
            domain = domain_map.setdefault(
                domain_id,
                {
                    "page_ids": set(),
                    "api_ids": set(),
                    "table_ids": set(),
                    "db_field_ids": set(),
                },
            )
            domain["page_ids"].add(page_id)
            domain["api_ids"].update(page_api_ids.get(page_id) or set())
            domain["table_ids"].update(page_table_ids.get(page_id) or set())
            domain["db_field_ids"].update(page_db_field_ids.get(page_id) or set())

        for api_id, api in apis_by_id.items():
            domain_id = self._derive_api_domain(api)
            if not domain_id:
                continue
            domain = domain_map.setdefault(
                domain_id,
                {
                    "page_ids": set(),
                    "api_ids": set(),
                    "table_ids": set(),
                    "db_field_ids": set(),
                },
            )
            domain["api_ids"].add(api_id)

        return {
            domain_id: {
                "domain_id": domain_id,
                "page_ids": sorted(values["page_ids"]),
                "api_ids": sorted(values["api_ids"]),
                "table_ids": sorted(values["table_ids"]),
                "db_field_ids": sorted(values["db_field_ids"]),
            }
            for domain_id, values in sorted(domain_map.items())
        }

    def _build_implicit_flows(
        self,
        *,
        page_table_links: Iterable[Mapping[str, Any]],
        page_transitions: Iterable[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        explicit_pairs: set[tuple[str, str]] = set()
        for row in page_transitions:
            from_page_id = str(row.get("from_page_id") or row.get("from") or "").strip()
            to_page_id = str(row.get("to_page_id") or row.get("to") or "").strip()
            if from_page_id and to_page_id and from_page_id != to_page_id:
                explicit_pairs.add(tuple(sorted((from_page_id, to_page_id))))

        table_pages: dict[str, set[str]] = {}
        for row in page_table_links:
            table_id = str(row.get("table_id") or "").strip()
            page_id = str(row.get("page_id") or "").strip()
            if table_id and page_id:
                table_pages.setdefault(table_id, set()).add(page_id)

        pair_tables: dict[tuple[str, str], set[str]] = {}
        for table_id, page_ids in table_pages.items():
            if len(page_ids) < 2:
                continue
            for page_pair in combinations(sorted(page_ids), 2):
                if page_pair in explicit_pairs:
                    continue
                pair_tables.setdefault(page_pair, set()).add(table_id)

        return [
            {
                "flow_id": f"implicit:{page_pair[0]}__{page_pair[1]}",
                "page_ids": [page_pair[0], page_pair[1]],
                "reason": "shared_table",
                "table_ids": sorted(table_ids),
            }
            for page_pair, table_ids in sorted(pair_tables.items())
        ]

    def _normalize_page_frontend_calls(
        self, raw: Iterable[Any] | Mapping[str, Any]
    ) -> dict[str, dict[str, list[str]]]:
        entries: dict[str, Mapping[str, Any]] = {}
        if isinstance(raw, Mapping):
            for page_id, value in raw.items():
                if not page_id or not isinstance(value, Mapping):
                    continue
                entries[str(page_id).strip()] = value
        else:
            for row in self._normalize_rows(raw):
                page_id = str(row.get("page_id") or "").strip()
                if not page_id:
                    continue
                entries[page_id] = row
        normalized: dict[str, dict[str, list[str]]] = {}
        for page_id in sorted(entries):
            normalized[page_id] = self._normalize_frontend_call_entry(entries[page_id])
        return normalized

    def _normalize_frontend_call_entry(self, entry: Mapping[str, Any]) -> dict[str, list[str]]:
        return {
            "method_names": self._normalize_string_collection(
                entry.get("methods") or entry.get("method_names")
            ),
            "import_paths": self._normalize_string_collection(
                entry.get("imports") or entry.get("import_paths")
            ),
            "request_paths": self._normalize_string_collection(entry.get("request_paths")),
        }

    def _normalize_string_collection(self, values: Any) -> list[str]:
        tokens: set[str] = set()
        if not values:
            return []
        if isinstance(values, str):
            token = values.strip()
            return [token] if token else []
        if isinstance(values, Mapping):
            return []
        try:
            iterator = iter(values)
        except TypeError:
            return []
        for item in iterator:
            token = str(item or "").strip()
            if token:
                tokens.add(token)
        return sorted(tokens)

    def _derive_page_domain(self, page: Mapping[str, Any]) -> str:
        route_path = str(page.get("route_path") or "").strip()
        if route_path:
            segments = [segment for segment in route_path.split("/") if segment]
            if segments:
                return self._normalize_name(segments[0])
        return self._normalize_name(page.get("page_id"))

    def _derive_api_domain(self, api: Mapping[str, Any]) -> str:
        path = str(api.get("path") or "").strip()
        segments = [segment for segment in path.split("/") if segment]
        if not segments:
            return ""
        if segments[0].lower() == "api" and len(segments) > 1:
            return self._normalize_name(segments[1])
        return self._normalize_name(segments[0])

    def _tokenize(self, value: Any) -> set[str]:
        text = str(value or "").strip().lower()
        if not text:
            return set()
        return {token for token in re.split(r"[^a-z0-9]+", text) if token}

    def _normalize_name(self, value: Any) -> str:
        tokens = sorted(self._tokenize(value))
        if not tokens:
            return ""
        return "_".join(tokens)

    def _singularize(self, token: str) -> str:
        if token.endswith("ies") and len(token) > 3:
            return f"{token[:-3]}y"
        if token.endswith("s") and len(token) > 1:
            return token[:-1]
        return token

    def _pluralize(self, token: str) -> str:
        if token.endswith("y") and len(token) > 1:
            return f"{token[:-1]}ies"
        if token.endswith("s"):
            return token
        return f"{token}s"

    def _sorted_rows(self, rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        normalized_rows = [dict(row) for row in rows]
        normalized_rows.sort(
            key=lambda row: tuple((str(key), str(row.get(key, ""))) for key in sorted(row))
        )
        return normalized_rows
