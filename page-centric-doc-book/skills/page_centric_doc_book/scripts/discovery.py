from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, List, Mapping

from skills.page_centric_doc_book.scripts.database_schema_loader import DatabaseSchemaLoader
from skills.page_centric_doc_book.scripts.frontend_structure_extractor import (
    FrontendStructureExtractor,
)
from skills.page_centric_doc_book.scripts.go_structure_extractor import GoStructureExtractor


class ProjectDiscovery:
    """Minimal static scanner that finds pages, APIs and their links."""

    def __init__(self, backend_path: Path | str, frontend_path: Path | str) -> None:
        self.backend_path = Path(backend_path)
        self.frontend_path = Path(frontend_path)
        self.schema_loader = DatabaseSchemaLoader()

    def scan(self) -> Mapping[str, object]:
        """Return discovered pages, APIs and relation signals."""
        route_snapshot = self._discover_routes()
        pages = list(route_snapshot.get("pages") or [])
        menu_tree = list(route_snapshot.get("menu_tree") or [])
        leaf_page_ids = sorted(self._collect_leaf_page_ids_from_menu_tree(menu_tree))
        frontend_structure = FrontendStructureExtractor(self.frontend_path).extract()
        pages = self._merge_pages_with_frontend_structure(pages, frontend_structure)
        apis = list(self._discover_backend_apis())
        links = self._build_page_api_links(pages, apis)
        route_relations = self._build_route_relations(pages)
        page_transitions = self._build_page_transitions(pages)
        api_domains = self._build_api_domains(apis)
        warnings = self._build_warnings(pages, page_transitions)
        schema_snapshot = self.schema_loader.load(self.backend_path)
        go_structure = GoStructureExtractor(self.backend_path).extract()
        return {
            "pages": pages,
            "apis": apis,
            "page_api_links": links,
            "handlers": list(go_structure.get("handlers") or []),
            "api_handler_links": list(go_structure.get("api_handler_links") or []),
            "handler_service_links": list(
                go_structure.get("handler_service_links") or []
            ),
            "service_repository_links": list(
                go_structure.get("service_repository_links") or []
            ),
            "route_relations": route_relations,
            "page_transitions": page_transitions,
            "api_domains": api_domains,
            "menu_tree": menu_tree,
            "leaf_page_ids": leaf_page_ids,
            "warnings": warnings,
            "databases": schema_snapshot.get("databases", []),
            "tables": schema_snapshot.get("tables", []),
            "db_fields": schema_snapshot.get("db_fields", []),
            "go_models": schema_snapshot.get("go_models", []),
            "components": list(frontend_structure.get("components") or []),
            "form_fields": list(frontend_structure.get("form_fields") or []),
            "grid_columns": list(frontend_structure.get("grid_columns") or []),
            "page_call_chains": frontend_structure.get("page_call_chains") or {},
        }

    def _merge_pages_with_frontend_structure(
        self,
        pages: List[Mapping[str, str]],
        frontend_structure: Mapping[str, object],
    ) -> List[Mapping[str, str]]:
        page_map: dict[str, dict[str, str]] = {
            str(page.get("page_id") or "").strip(): dict(page)
            for page in pages
            if str(page.get("page_id") or "").strip()
        }
        for key in ("components", "form_fields", "grid_columns"):
            for row in frontend_structure.get(key) or ():
                page_id = str(getattr(row, "get", lambda *_: "")("page_id") or "").strip()
                if not page_id or page_id in page_map:
                    continue
                page_map[page_id] = {"page_id": page_id, "route_path": ""}
        return [page_map[page_id] for page_id in sorted(page_map)]

    def _discover_routes(self) -> Mapping[str, Any]:
        router_dir = self.frontend_path / "src" / "router"
        if not router_dir.exists():
            return {"pages": [], "menu_tree": []}

        pages: dict[str, dict[str, str]] = {}
        menu_tree: list[dict[str, Any]] = []
        router_files = sorted(
            [*router_dir.rglob("*.js"), *router_dir.rglob("*.ts")],
            key=lambda path: path.as_posix(),
        )
        for route_file in router_files:
            text = route_file.read_text(encoding="utf-8", errors="ignore")
            route_entries, route_menu_tree = self._extract_route_snapshot(text)
            menu_tree.extend(route_menu_tree)
            for route_entry in route_entries:
                path = route_entry.get("path", "").strip()
                name = route_entry.get("name", "").strip()
                page_id = self._normalize_page_id(name, path)
                if not page_id:
                    continue
                page_record = {"page_id": page_id, "route_path": path}
                title = str(route_entry.get("title") or "").strip()
                parent_page_id = str(route_entry.get("parent_page_id") or "").strip()
                component_path = str(route_entry.get("component_path") or "").strip()
                if title:
                    page_record["title"] = title
                if parent_page_id:
                    page_record["parent_page_id"] = parent_page_id
                if component_path:
                    page_record["component_path"] = component_path
                if route_entry.get("requires_auth") == "true":
                    page_record["requires_auth"] = "true"
                existing = pages.setdefault(page_id, page_record)
                for key, value in page_record.items():
                    if value and not existing.get(key):
                        existing[key] = value
        return {
            "pages": [pages[key] for key in sorted(pages.keys())],
            "menu_tree": menu_tree,
        }

    def _extract_route_snapshot(self, text: str) -> tuple[list[Mapping[str, str]], list[dict[str, Any]]]:
        entries: list[Mapping[str, str]] = []
        menu_tree: list[dict[str, Any]] = []
        for array_body in self._extract_route_array_bodies(text):
            array_entries, array_menu_tree = self._extract_route_snapshot_from_array_body(array_body)
            entries.extend(array_entries)
            menu_tree.extend(array_menu_tree)
        return entries, menu_tree

    def _collect_leaf_page_ids_from_menu_tree(
        self, nodes: Iterable[Any]
    ) -> set[str]:
        leaf_ids: set[str] = set()
        for node in nodes:
            if not isinstance(node, Mapping):
                continue
            children = node.get("children") or []
            if children:
                leaf_ids.update(
                    self._collect_leaf_page_ids_from_menu_tree(children)
                )
                continue
            page_id = str(node.get("page_id") or "").strip()
            if page_id:
                leaf_ids.add(page_id)
        return leaf_ids

    def _extract_route_entries(self, text: str) -> List[Mapping[str, str]]:
        entries = []
        for array_body in self._extract_route_array_bodies(text):
            entries.extend(self._extract_route_entries_from_array_body(array_body))
        return entries

    def _extract_route_array_bodies(self, text: str) -> Iterable[str]:
        seen_brackets: set[int] = set()
        route_array_patterns = [
            re.compile(r"\broutes\s*:\s*\[", re.IGNORECASE),
            re.compile(r"\b(?:const|let|var)\s+routes\s*=\s*\[", re.IGNORECASE),
            re.compile(
                r"\b(?:const|let|var)\s+(?:routes|asyncRoutes|allAsyncRoutes|constantRoutes)\s*=\s*\[",
                re.IGNORECASE,
            ),
            re.compile(
                r"export\s+(?:const|let|var)\s+[A-Za-z_][A-Za-z0-9_]*routes[A-Za-z0-9_]*\s*=\s*\[",
                re.IGNORECASE,
            ),
            re.compile(r"export\s+default\s*\[", re.IGNORECASE),
        ]
        for pattern in route_array_patterns:
            for matched in pattern.finditer(text):
                bracket_idx = matched.end() - 1
                if bracket_idx in seen_brackets:
                    continue
                end_idx = self._find_matching_bracket(text, bracket_idx, "[", "]")
                if end_idx == -1:
                    continue
                seen_brackets.add(bracket_idx)
                yield text[bracket_idx + 1 : end_idx]

    def _extract_route_snapshot_from_array_body(
        self,
        array_body: str,
        *,
        parent_path: str = "",
        parent_page_id: str = "",
    ) -> tuple[list[Mapping[str, str]], list[dict[str, Any]]]:
        entries: list[Mapping[str, str]] = []
        menu_nodes: list[dict[str, Any]] = []
        for block_body in self._iterate_top_level_brace_blocks(array_body):
            filtered_block_body = self._strip_children_arrays(block_body)
            raw_path = self._extract_field(filtered_block_body, "path").strip()
            if not raw_path:
                continue
            resolved_path = self._resolve_route_path(parent_path, raw_path)
            if not resolved_path.startswith("/"):
                continue
            name_value = self._extract_field(filtered_block_body, "name")
            page_id = self._normalize_page_id(name_value or resolved_path, resolved_path)
            if not page_id:
                continue
            title = self._extract_field(filtered_block_body, "title").strip()
            component_path = self._extract_component_path(filtered_block_body)

            route_entry: dict[str, str] = {"path": resolved_path}
            if name_value:
                route_entry["name"] = name_value
            if title:
                route_entry["title"] = title
            if parent_page_id:
                route_entry["parent_page_id"] = parent_page_id
            if component_path:
                route_entry["component_path"] = component_path
            if self._has_boolean_flag(filtered_block_body, "requiresAuth"):
                route_entry["requires_auth"] = "true"
            entries.append(route_entry)

            child_entries: list[Mapping[str, str]] = []
            child_menu_nodes: list[dict[str, Any]] = []
            for children_body in self._extract_children_array_bodies(block_body):
                nested_entries, nested_menu_nodes = self._extract_route_snapshot_from_array_body(
                    children_body,
                    parent_path=resolved_path,
                    parent_page_id=page_id,
                )
                child_entries.extend(nested_entries)
                child_menu_nodes.extend(nested_menu_nodes)
            entries.extend(child_entries)

            if title:
                menu_nodes.append(
                    {
                        "page_id": page_id,
                        "title": title,
                        "route_path": resolved_path,
                        "component_path": component_path,
                        "parent_page_id": parent_page_id,
                        "children": child_menu_nodes,
                    }
                )
            else:
                menu_nodes.extend(child_menu_nodes)
        return entries, menu_nodes

    def _extract_route_entries_from_array_body(
        self,
        array_body: str,
        *,
        parent_path: str = "",
        parent_page_id: str = "",
    ) -> List[Mapping[str, str]]:
        entries: list[Mapping[str, str]] = []
        for block_body in self._iterate_top_level_brace_blocks(array_body):
            filtered_block_body = self._strip_children_arrays(block_body)
            raw_path = self._extract_field(filtered_block_body, "path").strip()
            if not raw_path:
                continue
            resolved_path = self._resolve_route_path(parent_path, raw_path)
            if not resolved_path.startswith("/"):
                continue
            name_value = self._extract_field(filtered_block_body, "name")
            page_id = self._normalize_page_id(name_value or resolved_path, resolved_path)
            if not page_id:
                continue
            route_entry: dict[str, str] = {"path": resolved_path}
            if name_value:
                route_entry["name"] = name_value
            title = self._extract_field(filtered_block_body, "title").strip()
            if title:
                route_entry["title"] = title
            if parent_page_id:
                route_entry["parent_page_id"] = parent_page_id
            if self._has_boolean_flag(filtered_block_body, "requiresAuth"):
                route_entry["requires_auth"] = "true"
            entries.append(route_entry)
            for children_body in self._extract_children_array_bodies(block_body):
                entries.extend(
                    self._extract_route_entries_from_array_body(
                        children_body,
                        parent_path=resolved_path,
                        parent_page_id=page_id,
                    )
                )
        return entries

    def _extract_children_array_bodies(self, block_body: str) -> List[str]:
        bodies: list[str] = []
        index = 0
        depth = 0
        while index < len(block_body):
            skipped_index = self._skip_quoted_or_comment_region(block_body, index)
            if skipped_index != index:
                index = skipped_index
                continue
            char = block_body[index]
            if char in "{[(":
                depth += 1
                index += 1
                continue
            if char in "}])":
                depth = max(0, depth - 1)
                index += 1
                continue
            if depth == 0:
                matched = re.match(r"children\s*:\s*\[", block_body[index:], re.IGNORECASE)
                if matched:
                    bracket_idx = index + matched.end() - 1
                    end_idx = self._find_matching_bracket(block_body, bracket_idx, "[", "]")
                    if end_idx != -1:
                        bodies.append(block_body[bracket_idx + 1 : end_idx])
                        index = end_idx + 1
                        continue
            index += 1
        return bodies

    def _strip_children_arrays(self, block_body: str) -> str:
        cleaned: list[str] = []
        index = 0
        depth = 0
        while index < len(block_body):
            skipped_index = self._skip_quoted_or_comment_region(block_body, index)
            if skipped_index != index:
                cleaned.append(block_body[index:skipped_index])
                index = skipped_index
                continue
            char = block_body[index]
            if char in "{[(":
                depth += 1
                cleaned.append(char)
                index += 1
                continue
            if char in "}])":
                depth = max(0, depth - 1)
                cleaned.append(char)
                index += 1
                continue
            if depth == 0:
                matched = re.match(r"children\s*:\s*\[", block_body[index:], re.IGNORECASE)
                if matched:
                    bracket_idx = index + matched.end() - 1
                    end_idx = self._find_matching_bracket(block_body, bracket_idx, "[", "]")
                    if end_idx != -1:
                        index = end_idx + 1
                        continue
            cleaned.append(char)
            index += 1
        return "".join(cleaned)

    def _resolve_route_path(self, parent_path: str, raw_path: str) -> str:
        normalized = (raw_path or "").strip()
        if not normalized:
            return ""
        if normalized.startswith("/"):
            return normalized
        base = (parent_path or "").rstrip("/")
        if not base:
            return f"/{normalized.lstrip('/')}"
        return f"{base}/{normalized.lstrip('/')}"

    def _iterate_top_level_brace_blocks(self, text: str) -> Iterable[str]:
        depth = 0
        start = None
        index = 0
        while index < len(text):
            skipped_index = self._skip_quoted_or_comment_region(text, index)
            if skipped_index != index:
                index = skipped_index
                continue
            char = text[index]
            if char == "{":
                if depth == 0:
                    start = index
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0 and start is not None:
                    yield text[start + 1 : index]
                    start = None
            index += 1

    def _iterate_brace_blocks(self, text: str) -> Iterable[str]:
        depth = 0
        start = None
        index = 0
        while index < len(text):
            skipped_index = self._skip_quoted_or_comment_region(text, index)
            if skipped_index != index:
                index = skipped_index
                continue
            char = text[index]
            if char == "{":
                if depth == 0:
                    start = index
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0 and start is not None:
                    yield text[start + 1 : index]
            index += 1

    def _find_matching_bracket(
        self, text: str, start_index: int, open_char: str, close_char: str
    ) -> int:
        depth = 0
        index = start_index
        while index < len(text):
            skipped_index = self._skip_quoted_or_comment_region(text, index)
            if skipped_index != index:
                index = skipped_index
                continue
            char = text[index]
            if char == open_char:
                depth += 1
                index += 1
                continue
            elif char == close_char:
                depth -= 1
                if depth == 0:
                    return index
            index += 1
        return -1

    def _skip_quoted_or_comment_region(self, text: str, index: int) -> int:
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if text[index] == "/" and next_char == "/":
            newline_index = text.find("\n", index + 2)
            return len(text) if newline_index == -1 else newline_index + 1
        if text[index] == "/" and next_char == "*":
            comment_end = text.find("*/", index + 2)
            return len(text) if comment_end == -1 else comment_end + 2
        if text[index] not in {"'", '"', "`"}:
            return index

        quote_char = text[index]
        cursor = index + 1
        while cursor < len(text):
            if quote_char in {"'", '"'} and text[cursor] == "\\":
                cursor += 2
                continue
            if text[cursor] == quote_char:
                return cursor + 1
            cursor += 1
        return len(text)

    def _extract_field(self, text: str, field: str) -> str:
        pattern = re.compile(
            rf"{field}\s*:\s*['\"](?P<value>[^'\"]+)['\"]", re.IGNORECASE
        )
        matched = pattern.search(text)
        return matched.group("value").strip() if matched else ""

    def _has_boolean_flag(self, text: str, field: str) -> bool:
        pattern = re.compile(rf"\b{field}\s*:\s*true\b", re.IGNORECASE)
        return bool(pattern.search(text))

    def _extract_component_path(self, text: str) -> str:
        component_ref_pattern = re.compile(
            r"\bcomponent\s*:\s*(?P<ref>[A-Za-z_][A-Za-z0-9_]*)",
            re.IGNORECASE,
        )
        matched = component_ref_pattern.search(text)
        if matched:
            return str(matched.group("ref") or "").strip()

        dynamic_import_pattern = re.compile(
            r"\bcomponent\s*:\s*\(\s*\)\s*=>\s*import\((?P<body>.*?)\)",
            re.IGNORECASE | re.DOTALL,
        )
        matched = dynamic_import_pattern.search(text)
        if matched:
            import_path = self._extract_last_string_literal(matched.group("body"))
            return import_path.strip()
        return ""

    def _extract_last_string_literal(self, text: str) -> str:
        matches = re.findall(r"['\"](?P<value>[^'\"]+)['\"]", text or "")
        return str(matches[-1] if matches else "")

    def _normalize_page_id(self, name: str | None, path: str | None) -> str:
        candidate = (name or "").strip() or (path or "").strip()
        candidate = candidate.strip("/")
        if not candidate:
            return ""
        return candidate.split("/")[-1].lower()

    def _discover_backend_apis(self) -> Iterable[Mapping[str, str]]:
        go_files = sorted(self.backend_path.rglob("*.go"))
        if not go_files:
            return []

        api_pattern = re.compile(
            r"\b[\w\.]+\.(?P<method>Get|Post|Put|Delete|Patch|Options|Head)\s*\(\s*['\"](?P<path>[^'\"]+)['\"]",
            re.IGNORECASE,
        )
        apis: List[Mapping[str, str]] = []
        seen_signatures: set[tuple[str, str]] = set()
        for go_file in go_files:
            text = go_file.read_text(encoding="utf-8", errors="ignore")
            for match in api_pattern.finditer(text):
                method = match.group("method").upper()
                path_value = match.group("path")
                signature = (method, path_value)
                if signature in seen_signatures:
                    continue
                seen_signatures.add(signature)
                apis.append(
                    {
                        "api_id": f"{method.lower()}:{path_value}",
                        "path": path_value,
                        "method": method,
                    }
                )
        return sorted(apis, key=lambda api: api["api_id"])

    def _build_page_api_links(
        self, pages: List[Mapping[str, str]], apis: List[Mapping[str, str]]
    ) -> List[Mapping[str, str]]:
        links: List[Mapping[str, str]] = []
        seen: set[tuple[str, str]] = set()
        sorted_pages = sorted(pages, key=lambda page: page.get("page_id", ""))
        sorted_apis = sorted(apis, key=lambda api: api.get("api_id", ""))

        for page in sorted_pages:
            page_id = page.get("page_id", "")
            if not page_id:
                continue
            page_segments = self._extract_path_segments(page.get("route_path", ""))
            if not page_segments:
                page_segments = [page_id]
            if any(segment.startswith(":") for segment in page_segments):
                continue

            for api in sorted_apis:
                api_id = api.get("api_id")
                if not api_id:
                    continue
                api_path = (api.get("path") or "").strip()
                if not api_path.lower().startswith("/api"):
                    continue
                api_segments = self._extract_path_segments(api_path)
                if not api_segments or any(segment.startswith(":") for segment in api_segments):
                    continue
                if len(api_segments) < len(page_segments):
                    continue
                if len(api_segments) > len(page_segments) + 1:
                    continue
                if api_segments[-len(page_segments) :] != page_segments:
                    continue

                key = (page_id, api_id)
                if key in seen:
                    continue
                seen.add(key)
                links.append({"page_id": page_id, "api_id": api_id})

        links.sort(key=lambda entry: (entry["page_id"], entry["api_id"]))
        return links

    def _build_route_relations(
        self, pages: List[Mapping[str, str]]
    ) -> List[Mapping[str, str]]:
        relations: List[Mapping[str, str]] = []
        for page in pages:
            route_path = (page.get("route_path") or "").strip()
            page_id = page.get("page_id", "")
            if not route_path or not page_id:
                continue
            parent = "/".join(route_path.rstrip("/").split("/")[:-1]).strip()
            if not parent or parent == "":
                continue
            relations.append(
                {
                    "page_id": page_id,
                    "relation": "route_parent",
                    "route_parent": parent,
                }
            )
        return relations

    def _build_page_transitions(
        self, pages: List[Mapping[str, str]]
    ) -> List[Mapping[str, str]]:
        page_ids = {str(page.get("page_id") or "").strip() for page in pages}
        page_ids.discard("")
        page_id_by_path = {
            str(page.get("route_path") or "").strip(): str(page.get("page_id") or "").strip()
            for page in pages
            if page.get("route_path") and page.get("page_id")
        }
        transitions: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()

        for source_file in sorted(self.frontend_path.rglob("*")):
            if source_file.suffix.lower() not in {".js", ".vue"}:
                continue
            source_page_ids = self._infer_source_page_ids(source_file, page_ids)
            if not source_page_ids:
                continue
            text = source_file.read_text(encoding="utf-8", errors="ignore")
            for target_path, target_page_id in self._extract_transition_targets(text, page_ids, page_id_by_path):
                for source_page_id in source_page_ids:
                    if source_page_id == target_page_id:
                        continue
                    key = (source_page_id, target_page_id, target_path)
                    if key in seen:
                        continue
                    seen.add(key)
                    transitions.append(
                        {
                            "from_page_id": source_page_id,
                            "to_page_id": target_page_id,
                            "target_path": target_path,
                        }
                    )

        transitions.sort(
            key=lambda row: (
                row.get("from_page_id", ""),
                row.get("to_page_id", ""),
                row.get("target_path", ""),
            )
        )
        return transitions

    def _build_warnings(
        self, pages: List[Mapping[str, str]], page_transitions: List[Mapping[str, str]]
    ) -> List[str]:
        if pages and not page_transitions:
            return ["未检测到跨页跳转，完整功能主线章节将被跳过。"]
        return []

    def _infer_source_page_ids(self, source_file: Path, page_ids: set[str]) -> list[str]:
        relative_path = source_file.relative_to(self.frontend_path)
        tokens = {
            segment.lower()
            for segment in relative_path.parts
            if isinstance(segment, str) and segment
        }
        stem = source_file.stem.lower()
        tokens.add(stem)
        return sorted(page_id for page_id in page_ids if page_id.lower() in tokens)

    def _extract_transition_targets(
        self,
        text: str,
        page_ids: set[str],
        page_id_by_path: Mapping[str, str],
    ) -> List[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        push_patterns = [
            re.compile(r"(?:this\.)?(?:\$)?router\.push\(\s*['\"](?P<path>/[^'\"]+)['\"]\s*\)"),
            re.compile(r"(?:this\.)?(?:\$)?router\.push\(\s*\{\s*path\s*:\s*['\"](?P<path>/[^'\"]+)['\"]"),
            re.compile(r"(?:this\.)?(?:\$)?router\.push\(\s*\{\s*name\s*:\s*['\"](?P<name>[^'\"]+)['\"]"),
        ]
        for pattern in push_patterns:
            for match in pattern.finditer(text):
                target_path, target_page_id = self._resolve_transition_target(
                    page_ids=page_ids,
                    page_id_by_path=page_id_by_path,
                    path_value=match.groupdict().get("path"),
                    name_value=match.groupdict().get("name"),
                )
                if target_page_id:
                    rows.append((target_path, target_page_id))
        return rows

    def _resolve_transition_target(
        self,
        *,
        page_ids: set[str],
        page_id_by_path: Mapping[str, str],
        path_value: str | None,
        name_value: str | None,
    ) -> tuple[str, str]:
        route_path = (path_value or "").strip()
        if route_path:
            target_page_id = page_id_by_path.get(route_path, "")
            return route_path, target_page_id

        page_name = self._normalize_page_id(name_value, name_value)
        if page_name in page_ids:
            for known_path, known_page_id in page_id_by_path.items():
                if known_page_id == page_name:
                    return known_path, known_page_id
            return f"/{page_name}", page_name
        return "", ""

    def _build_api_domains(
        self, apis: List[Mapping[str, str]]
    ) -> List[Mapping[str, str]]:
        rows: List[Mapping[str, str]] = []
        for api in apis:
            path = (api.get("path") or "").strip()
            api_id = api.get("api_id", "")
            domain = self._extract_api_domain(path)
            rows.append({"api_id": api_id, "domain": domain})
        return rows

    def _extract_api_domain(self, path: str) -> str:
        segments = self._extract_path_segments(path)
        if not segments:
            return ""
        if segments[0].lower() == "api":
            for segment in segments[1:]:
                if segment and not segment.startswith(":"):
                    return segment.lower()
            return "api"
        for segment in segments:
            if segment and not segment.startswith(":"):
                return segment.lower()
        return ""

    def _extract_path_segments(self, path: str) -> List[str]:
        cleaned = (path or "").strip()
        if not cleaned:
            return []
        segments = [segment for segment in cleaned.split("/") if segment]
        return segments
