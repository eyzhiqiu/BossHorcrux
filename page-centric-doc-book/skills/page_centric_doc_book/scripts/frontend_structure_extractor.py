from __future__ import annotations

from collections import deque
import re
from pathlib import Path
from typing import Any, Mapping


_IMPORT_PATTERN = re.compile(
    r"import\s+(?:[\w\W]*?)\s+from\s+['\"](?P<path>[^'\"]+)['\"]", re.IGNORECASE
)
_DYNAMIC_IMPORT_PATTERN = re.compile(
    r"import\s*\([^)]*?['\"](?P<path>[^'\"]+)['\"]", re.IGNORECASE
)
_REQUEST_CALL_PATTERN = re.compile(
    r"(?:request|axios\.[A-Za-z_]+|fetch|uni\.request)\s*\(\s*['\"](?P<path>/[^'\"]+)['\"]",
    re.IGNORECASE,
)
_REQUEST_OBJECT_PATTERN = re.compile(
    r"(?:request|axios(?:\.[A-Za-z_]+)?|uni\.request)\s*\(\s*{[^)]*?\burl\s*:\s*['\"](?P<path>/[^'\"]+)['\"]",
    re.IGNORECASE | re.DOTALL,
)
_ALLOWED_FRONTEND_EXTENSIONS = (
    ".vue",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
)


class FrontendStructureExtractor:
    """Extract minimal frontend structures from Vue files."""

    VUE_BUILTIN_COMPONENT_DENYLIST = {
        "teleport",
        "router-link",
        "router-view",
        "routerlink",
        "routerview",
        "transition",
        "transition-group",
        "transitiongroup",
        "keep-alive",
        "keepalive",
        "suspense",
    }

    def __init__(self, frontend_path: Path | str) -> None:
        self.frontend_path = Path(frontend_path)
        try:
            self.frontend_root = self.frontend_path.resolve()
        except OSError:
            self.frontend_root = self.frontend_path

    def extract(self) -> Mapping[str, Any]:
        components: list[dict[str, Any]] = []
        form_fields: list[dict[str, Any]] = []
        grid_columns: list[dict[str, Any]] = []
        route_page_ids_by_source_file = self._build_route_page_id_by_source_file()

        component_seen: set[tuple[str, str, str]] = set()
        form_field_seen: set[tuple[str, str, str]] = set()
        grid_column_seen: set[tuple[str, str, str]] = set()
        page_call_chains_by_page: dict[str, dict[str, set[str]]] = {}

        for vue_file in self._iter_candidate_vue_files(
            route_page_ids_by_source_file=route_page_ids_by_source_file
        ):
            source_file = vue_file.relative_to(self.frontend_path).as_posix()
            page_ids = (
                route_page_ids_by_source_file.get(source_file)
                or route_page_ids_by_source_file.get(source_file.lower())
                or {self._normalize_page_id(vue_file.stem)}
            )
            page_ids = {page_id for page_id in page_ids if page_id}
            if not page_ids:
                continue
            text = vue_file.read_text(encoding="utf-8", errors="ignore")
            template_text = self._extract_template(text)
            chain_data = self._build_page_call_chain(vue_file, text)

            for page_id in sorted(page_ids):
                page_chain = page_call_chains_by_page.setdefault(
                    page_id,
                    {
                        "methods": set(),
                        "imports": set(),
                        "request_paths": set(),
                    },
                )
                page_chain["methods"].update(chain_data.get("methods") or ())
                page_chain["imports"].update(chain_data.get("imports") or ())
                page_chain["request_paths"].update(chain_data.get("request_paths") or ())

                if not template_text:
                    continue

                for component_name in self._extract_components(template_text):
                    key = (page_id, component_name, source_file)
                    if key in component_seen:
                        continue
                    component_seen.add(key)
                    components.append(
                        {
                            "page_id": page_id,
                            "component_name": component_name,
                            "source_file": source_file,
                        }
                    )

                for prop, label in self._extract_form_fields(template_text):
                    key = (page_id, prop, source_file)
                    if key in form_field_seen:
                        continue
                    form_field_seen.add(key)
                    form_fields.append(
                        {
                            "field_id": f"{page_id}:{prop}",
                            "page_id": page_id,
                            "prop": prop,
                            "label": label,
                            "source_file": source_file,
                        }
                    )

                for prop, label in self._extract_grid_columns(template_text):
                    key = (page_id, prop, source_file)
                    if key in grid_column_seen:
                        continue
                    grid_column_seen.add(key)
                    grid_columns.append(
                        {
                            "column_id": f"{page_id}:{prop}",
                            "page_id": page_id,
                            "prop": prop,
                            "label": label,
                            "source_file": source_file,
                        }
                    )

        components.sort(
            key=lambda row: (
                str(row.get("page_id", "")),
                str(row.get("component_name", "")),
                str(row.get("source_file", "")),
            )
        )
        form_fields.sort(
            key=lambda row: (
                str(row.get("field_id", "")),
                str(row.get("source_file", "")),
            )
        )
        grid_columns.sort(
            key=lambda row: (
                str(row.get("column_id", "")),
                str(row.get("source_file", "")),
            )
        )
        page_call_chains = {
            page_id: {
                "methods": sorted(entry["methods"]),
                "imports": sorted(entry["imports"]),
                "request_paths": sorted(entry["request_paths"]),
            }
            for page_id, entry in sorted(page_call_chains_by_page.items())
        }
        return {
            "components": components,
            "form_fields": form_fields,
            "grid_columns": grid_columns,
            "page_call_chains": page_call_chains,
        }

    def _iter_candidate_vue_files(
        self, *, route_page_ids_by_source_file: Mapping[str, set[str]] | None = None
    ) -> list[Path]:
        fallback_candidates = self._iter_fallback_vue_files()

        if route_page_ids_by_source_file:
            mapped_candidates: list[Path] = []
            seen_paths: set[str] = set()
            for source_file in sorted(route_page_ids_by_source_file.keys()):
                vue_file = self.frontend_path / source_file
                normalized_source_file = source_file.lower()
                if normalized_source_file in seen_paths:
                    continue
                if not vue_file.is_file() or vue_file.suffix.lower() != ".vue":
                    continue
                seen_paths.add(normalized_source_file)
                mapped_candidates.append(vue_file)
            return self._merge_candidate_files(mapped_candidates, fallback_candidates)

        return fallback_candidates

    def _iter_fallback_vue_files(self) -> list[Path]:
        views_root = self.frontend_path / "src" / "views"
        if views_root.is_dir():
            candidates: list[Path] = []
            for vue_file in views_root.rglob("*.vue"):
                relative_parts = {part.lower() for part in vue_file.relative_to(views_root).parts}
                if "components" in relative_parts:
                    continue
                candidates.append(vue_file)
            return sorted(candidates)

        src_root = self.frontend_path / "src"
        if src_root.is_dir():
            candidates: list[Path] = []
            for vue_file in src_root.rglob("*.vue"):
                relative_parts = {part.lower() for part in vue_file.relative_to(src_root).parts}
                if "components" in relative_parts:
                    continue
                candidates.append(vue_file)
            return sorted(candidates)

        candidates: list[Path] = []
        for vue_file in self.frontend_path.rglob("*.vue"):
            relative_parts = {part.lower() for part in vue_file.relative_to(self.frontend_path).parts}
            if "components" in relative_parts:
                continue
            candidates.append(vue_file)
        return sorted(candidates)

    def _merge_candidate_files(
        self, mapped_candidates: list[Path], fallback_candidates: list[Path]
    ) -> list[Path]:
        merged_candidates: list[Path] = []
        seen_paths: set[str] = set()
        for vue_file in [*sorted(mapped_candidates), *fallback_candidates]:
            normalized_source_file = str(vue_file.relative_to(self.frontend_path).as_posix()).lower()
            if normalized_source_file in seen_paths:
                continue
            seen_paths.add(normalized_source_file)
            merged_candidates.append(vue_file)
        return merged_candidates

    def _extract_template(self, vue_text: str) -> str:
        scannable_text = re.sub(r"<!--.*?-->", lambda matched: " " * len(matched.group(0)), vue_text, flags=re.DOTALL)
        opening_match = self._find_top_level_template_opening(scannable_text)
        if not opening_match:
            return ""
        content_start = opening_match.end()
        depth = 1
        template_tag_pattern = re.compile(
            r"<\s*/?\s*template\b[^>]*>",
            re.IGNORECASE | re.DOTALL,
        )
        for matched in template_tag_pattern.finditer(scannable_text, pos=content_start):
            matched_text = matched.group(0) or ""
            is_closing_tag = re.match(r"<\s*/\s*template\b", matched_text, re.IGNORECASE)
            if is_closing_tag:
                depth -= 1
                if depth == 0:
                    template_text = vue_text[content_start : matched.start()]
                    return re.sub(r"<!--.*?-->", "", template_text, flags=re.DOTALL)
                continue
            is_self_closing_tag = bool(re.search(r"/\s*>$", matched_text))
            if is_self_closing_tag:
                continue
            depth += 1
        return ""

    def _find_top_level_template_opening(self, vue_text: str) -> re.Match[str] | None:
        tag_pattern = re.compile(
            r"<\s*(?P<closing>/?)\s*(?P<tag>template|script|style)\b[^>]*>",
            re.IGNORECASE | re.DOTALL,
        )
        search_from = 0
        while True:
            comment_index = vue_text.find("<!--", search_from)
            matched = tag_pattern.search(vue_text, pos=search_from)
            if comment_index != -1 and (not matched or comment_index < matched.start()):
                comment_end = vue_text.find("-->", comment_index + 4)
                if comment_end == -1:
                    return None
                search_from = comment_end + 3
                continue
            if not matched:
                return None
            if matched.group("closing"):
                search_from = matched.end()
                continue

            tag_name = (matched.group("tag") or "").lower()
            if tag_name == "template":
                return matched

            closing_match = self._find_closing_block_tag(vue_text, tag_name, matched.end())
            if closing_match < 0:
                return None
            search_from = closing_match

    def _find_closing_block_tag(self, vue_text: str, tag_name: str, start_index: int) -> int:
        closing_pattern = re.compile(
            rf"<\s*/\s*{tag_name}\b[^>]*>",
            re.IGNORECASE | re.DOTALL,
        )
        index = start_index
        while index < len(vue_text):
            skipped_index = self._skip_quoted_or_comment_region(vue_text, index)
            if skipped_index != index:
                index = skipped_index
                continue
            matched = closing_pattern.match(vue_text, pos=index)
            if matched:
                return matched.end()
            index += 1
        return -1

    def _extract_components(self, text: str) -> list[str]:
        component_names: list[str] = []
        index = 0
        while index < len(text):
            skipped_index = self._skip_quoted_or_comment_region(text, index)
            if skipped_index != index:
                index = skipped_index
                continue
            if text[index] != "<":
                index += 1
                continue
            match = re.match(r"<\s*(?P<name>[A-Za-z][A-Za-z0-9_-]*)\b", text[index:])
            if not match:
                index += 1
                continue
            component_name = match.group("name")
            normalized_name = component_name.lower()
            if normalized_name in self.VUE_BUILTIN_COMPONENT_DENYLIST:
                index += max(1, match.end())
                continue
            if component_name[:1].isupper():
                component_names.append(component_name)
            elif "-" in normalized_name and not normalized_name.startswith("el-"):
                component_names.append(normalized_name)
            index += max(1, match.end())
        return component_names

    def _extract_form_fields(self, text: str) -> list[tuple[str, str]]:
        pattern = re.compile(r"<\s*el-form-item\b(?P<attrs>[^>]*)>", re.IGNORECASE | re.DOTALL)
        fields: list[tuple[str, str]] = []
        for match in pattern.finditer(text):
            attrs = match.group("attrs") or ""
            prop = self._extract_attr(attrs, "prop")
            if not prop:
                continue
            label = self._extract_attr(attrs, "label")
            fields.append((prop, label))
        return fields

    def _extract_grid_columns(self, text: str) -> list[tuple[str, str]]:
        pattern = re.compile(
            r"<\s*el-table-column\b(?P<attrs>[^>]*)/?>",
            re.IGNORECASE | re.DOTALL,
        )
        columns: list[tuple[str, str]] = []
        for match in pattern.finditer(text):
            attrs = match.group("attrs") or ""
            prop = self._extract_attr(attrs, "prop")
            if not prop:
                continue
            label = self._extract_attr(attrs, "label")
            columns.append((prop, label))
        return columns

    def _extract_attr(self, attrs: str, name: str) -> str:
        pattern = re.compile(
            rf"(?<![:\w-]){name}\s*=\s*['\"](?P<value>[^'\"]+)['\"]",
            re.IGNORECASE,
        )
        matched = pattern.search(attrs)
        return matched.group("value").strip() if matched else ""

    def _build_route_page_id_by_source_file(self) -> dict[str, set[str]]:
        router_dir = self.frontend_path / "src" / "router"
        if not router_dir.is_dir():
            return {}

        route_page_id_by_source_file: dict[str, set[str]] = {}
        router_files = sorted(
            [*router_dir.rglob("*.js"), *router_dir.rglob("*.ts")],
            key=lambda path: path.as_posix(),
        )
        for router_file in router_files:
            text = router_file.read_text(encoding="utf-8", errors="ignore")
            vue_imports = self._extract_vue_imports(text, router_file)
            for block in self._iterate_route_blocks(text):
                path_value = self._extract_field(block, "path")
                if not path_value or not path_value.startswith("/"):
                    continue
                page_id = self._normalize_route_page_id(
                    self._extract_field(block, "name"), path_value
                )
                if not page_id:
                    continue
                source_file = self._extract_component_source_file(
                    block=block,
                    vue_imports=vue_imports,
                    router_file=router_file,
                )
                if not source_file:
                    continue
                route_page_id_by_source_file.setdefault(source_file, set()).add(page_id)
                route_page_id_by_source_file.setdefault(source_file.lower(), set()).add(page_id)
        return route_page_id_by_source_file

    def _iterate_route_blocks(self, text: str) -> list[str]:
        blocks: list[str] = []
        for array_body in self._extract_route_array_bodies(text):
            blocks.extend(self._iterate_brace_blocks(array_body))
        return blocks

    def _extract_route_array_bodies(self, text: str) -> list[str]:
        array_bodies: list[str] = []
        seen_brackets: set[int] = set()
        route_array_patterns = [
            re.compile(r"\broutes\s*:\s*\[", re.IGNORECASE),
            re.compile(r"\b(?:const|let|var)\s+routes\s*=\s*\[", re.IGNORECASE),
            re.compile(
                r"export\s+(?:const|let|var)\s+[A-Za-z_][A-Za-z0-9_]*routes[A-Za-z0-9_]*\s*=\s*\[",
                re.IGNORECASE,
            ),
            re.compile(r"export\s+default\s*\[", re.IGNORECASE),
        ]
        for pattern in route_array_patterns:
            for matched in pattern.finditer(text):
                bracket_index = matched.end() - 1
                end_index = self._find_matching_bracket(text, bracket_index, "[", "]")
                if end_index < 0 or bracket_index in seen_brackets:
                    continue
                seen_brackets.add(bracket_index)
                array_bodies.append(text[bracket_index + 1 : end_index])
        return array_bodies

    def _extract_vue_imports(self, text: str, router_file: Path) -> dict[str, str]:
        pattern = re.compile(
            r"import\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s+from\s+['\"](?P<path>[^'\"]+)['\"]"
        )
        imports: dict[str, str] = {}
        for matched in pattern.finditer(text):
            source_file = self._resolve_source_file_from_import_path(
                router_file=router_file,
                import_path=matched.group("path"),
            )
            if not source_file:
                continue
            imports[matched.group("name")] = source_file
        return imports

    def _iterate_brace_blocks(self, text: str) -> list[str]:
        blocks: list[str] = []
        depth = 0
        start_index = -1
        index = 0
        while index < len(text):
            skipped_index = self._skip_quoted_or_comment_region(text, index)
            if skipped_index != index:
                index = skipped_index
                continue
            char = text[index]
            if char == "{":
                if depth == 0:
                    start_index = index
                depth += 1
            elif char == "}":
                if depth == 0:
                    index += 1
                    continue
                depth -= 1
                if depth == 0 and start_index >= 0:
                    blocks.append(text[start_index + 1 : index])
                    start_index = -1
            index += 1
        return blocks

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
            if char == close_char:
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
            rf"{field}\s*:\s*['\"](?P<value>[^'\"]+)['\"]",
            re.IGNORECASE,
        )
        matched = pattern.search(text)
        return matched.group("value").strip() if matched else ""

    def _extract_component_source_file(
        self,
        *,
        block: str,
        vue_imports: Mapping[str, str],
        router_file: Path,
    ) -> str:
        dynamic_import_match = re.search(
            r"component\s*:\s*(?:\([^)]*\)\s*=>\s*)?import\(\s*(?:/\*.*?\*/\s*)*['\"](?P<path>[^'\"]+)['\"]\s*\)",
            block,
            re.IGNORECASE | re.DOTALL,
        )
        if dynamic_import_match:
            return self._resolve_source_file_from_import_path(
                router_file=router_file,
                import_path=dynamic_import_match.group("path"),
            )

        component_match = re.search(
            r"component\s*:\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\b",
            block,
            re.IGNORECASE,
        )
        if not component_match:
            return ""
        return str(vue_imports.get(component_match.group("name")) or "")

    def _resolve_source_file_from_import_path(
        self, *, router_file: Path, import_path: str
    ) -> str:
        cleaned_path = self._strip_import_suffix(import_path)
        if not cleaned_path:
            return ""
        candidate_paths = [cleaned_path]
        if not cleaned_path.lower().endswith(".vue"):
            candidate_paths.append(f"{cleaned_path}.vue")
        frontend_root = self.frontend_root

        for candidate_path in candidate_paths:
            if candidate_path.startswith("@/"):
                absolute_path = (frontend_root / "src" / candidate_path[2:]).resolve()
            elif candidate_path.startswith("/"):
                absolute_path = (frontend_root / candidate_path.lstrip("/")).resolve()
            elif candidate_path.startswith("src/"):
                absolute_path = (frontend_root / candidate_path).resolve()
            else:
                absolute_path = (router_file.parent / candidate_path).resolve()
            if absolute_path.suffix.lower() != ".vue":
                continue
            if not absolute_path.exists():
                continue
            try:
                return absolute_path.relative_to(frontend_root).as_posix()
            except ValueError:
                continue
        return ""

    def _strip_import_suffix(self, path_value: str) -> str:
        stripped = (path_value or "").strip()
        if not stripped:
            return ""
        return stripped.split("?", maxsplit=1)[0].split("#", maxsplit=1)[0]

    def _build_page_call_chain(self, entry_file: Path, entry_text: str) -> dict[str, set[str]]:
        imports, request_paths = self._collect_frontend_imports_and_requests(entry_file, entry_text)
        return {
            "methods": self._extract_methods_from_vue_text(entry_text),
            "imports": imports,
            "request_paths": request_paths,
        }

    def _extract_methods_from_vue_text(self, text: str) -> set[str]:
        methods: set[str] = set()
        for script_text in self._extract_script_blocks(text):
            methods.update(self._extract_methods_from_script(script_text))
        return methods

    def _extract_methods_from_script(self, script_text: str) -> set[str]:
        names: set[str] = set()
        for match in re.finditer(r"methods\s*:\s*{", script_text, re.IGNORECASE):
            start_index = match.end() - 1
            end_index = self._find_matching_bracket(script_text, start_index, "{", "}")
            if end_index <= start_index:
                continue
            body = script_text[match.end() : end_index]
            for method_name in re.findall(
                r"\b(?:async\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(|:)", body
            ):
                names.add(method_name)
        return names

    def _collect_frontend_imports_and_requests(
        self, entry_file: Path, entry_text: str
    ) -> tuple[set[str], set[str]]:
        imports: set[str] = set()
        request_paths: set[str] = set()
        normalized_entry_file = self._resolve_path(entry_file)
        queue = deque([(normalized_entry_file, entry_text)])
        visited: set[Path] = set()
        while queue:
            module_path, module_text = queue.popleft()
            module_path = self._resolve_path(module_path)
            if module_path in visited:
                continue
            visited.add(module_path)
            for script_text in self._get_script_segments(module_path, module_text):
                request_paths.update(self._extract_request_paths(script_text))
                for raw_import in self._extract_import_paths(script_text):
                    resolved_import = self._resolve_local_import(module_path, raw_import)
                    if not resolved_import:
                        continue
                    resolved_import = self._resolve_path(resolved_import)
                    normalized = self._normalize_frontend_path(resolved_import)
                    if normalized:
                        imports.add(normalized)
                    if resolved_import in visited:
                        continue
                    try:
                        next_text = resolved_import.read_text(encoding="utf-8", errors="ignore")
                    except OSError:
                        continue
                    queue.append((resolved_import, next_text))
        return imports, request_paths

    def _get_script_segments(self, module_path: Path, module_text: str) -> list[str]:
        if module_path.suffix.lower() == ".vue":
            return self._extract_script_blocks(module_text)
        return [module_text]

    def _extract_script_blocks(self, text: str) -> list[str]:
        scripts: list[str] = []
        pattern = re.compile(r"<script\b[^>]*>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL)
        for match in pattern.finditer(text):
            scripts.append(match.group(1) or "")
        return scripts

    def _extract_import_paths(self, script_text: str) -> list[str]:
        paths: list[str] = []
        for pattern in (_IMPORT_PATTERN, _DYNAMIC_IMPORT_PATTERN):
            for match in pattern.finditer(script_text):
                path_value = self._strip_import_suffix(match.group("path") or "")
                if not path_value or not self._is_local_import_path(path_value):
                    continue
                paths.append(path_value)
        return paths

    def _is_local_import_path(self, path_value: str) -> bool:
        normalized = (path_value or "").strip()
        if not normalized:
            return False
        return normalized.startswith((".", "/", "@/")) or normalized.startswith("src/")

    def _resolve_local_import(self, base_file: Path, import_path: str) -> Path | None:
        cleaned = self._strip_import_suffix(import_path)
        if not cleaned:
            return None
        if cleaned.startswith("@/"):
            candidate_base = self.frontend_root / "src" / cleaned[2:]
        elif cleaned.startswith("/"):
            candidate_base = self.frontend_root / cleaned.lstrip("/")
        elif cleaned.startswith("src/"):
            candidate_base = self.frontend_root / cleaned
        else:
            candidate_base = base_file.parent / cleaned

        candidates: list[Path] = []
        if candidate_base.suffix.lower() in _ALLOWED_FRONTEND_EXTENSIONS:
            candidates.append(candidate_base)
        else:
            for ext in _ALLOWED_FRONTEND_EXTENSIONS:
                candidates.append(candidate_base.with_suffix(ext))
                candidates.append(candidate_base / f"index{ext}")

        for candidate in candidates:
            try:
                resolved_candidate = candidate.resolve()
            except OSError:
                continue
            if not resolved_candidate.exists() or not resolved_candidate.is_file():
                continue
            try:
                resolved_candidate.relative_to(self.frontend_root)
            except ValueError:
                continue
            if "node_modules" in resolved_candidate.parts:
                continue
            return resolved_candidate
        return None

    def _normalize_frontend_path(self, path: Path) -> str:
        try:
            relative = path.relative_to(self.frontend_root)
        except ValueError:
            return ""
        if "node_modules" in relative.parts:
            return ""
        normalized = relative.as_posix()
        suffix = path.suffix.lower()
        if suffix in _ALLOWED_FRONTEND_EXTENSIONS:
            normalized = normalized[: -len(suffix)]
        return normalized

    def _extract_request_paths(self, script_text: str) -> set[str]:
        paths: set[str] = {
            match.group("path") for match in _REQUEST_CALL_PATTERN.finditer(script_text)
        }
        paths.update(
            match.group("path") for match in _REQUEST_OBJECT_PATTERN.finditer(script_text)
        )
        return paths

    def _resolve_path(self, path: Path) -> Path:
        try:
            return path.resolve(strict=False)
        except OSError:
            return path

    def _normalize_route_page_id(self, name: str | None, path: str | None) -> str:
        candidate = (name or "").strip() or (path or "").strip()
        candidate = candidate.strip("/")
        if not candidate:
            return ""
        return candidate.split("/")[-1].lower()

    def _normalize_page_id(self, raw_page_id: str) -> str:
        page_id = str(raw_page_id or "").strip().lower()
        if not page_id:
            return ""
        return page_id
