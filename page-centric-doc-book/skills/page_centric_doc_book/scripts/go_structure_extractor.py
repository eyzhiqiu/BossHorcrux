from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Iterable, List, Mapping


class GoStructureExtractor:
    """Lightweight Go text scanner for router->handler->service->repository links."""

    _ROUTE_CALL_PATTERN = re.compile(
        r"\.\s*(?P<method>Get|Post|Put|Delete|Patch|Options|Head)\s*"
        r"\(\s*['\"](?P<path>/[^'\"]+)['\"]\s*,\s*(?P<receiver>\w+)\.(?P<handler>\w+)\s*\)",
        re.IGNORECASE,
    )
    _FUNCTION_SIGNATURE_PATTERN = re.compile(
        r"func\s+(?P<name>\w+)\s*\((?P<params>[^)]*)\)\s*[^{]*\{",
        re.DOTALL,
    )
    _STRUCT_PATTERN = re.compile(
        r"type\s+(?P<name>\w+)\s+struct\s*\{(?P<body>.*?)\}",
        re.DOTALL,
    )
    _METHOD_SIGNATURE_PATTERN = re.compile(
        r"func\s*\(\s*(?P<receiver_var>\w+)\s+\*?(?P<receiver_type>\w+)\s*\)\s*"
        r"(?P<method>\w+)\s*\([^)]*\)\s*[^{]*\{",
        re.DOTALL,
    )
    _FIELD_PATTERN = re.compile(r"\b(?P<field>\w+)\s+\*?(?P<dependency>\w+)\b")
    _SERVICE_TYPE_SUFFIXES = ("service", "svc", "usecase")
    _REPOSITORY_TYPE_SUFFIXES = ("repo", "repository", "dao", "store")

    def __init__(self, backend_root: Path | str) -> None:
        self.backend_root = Path(backend_root)

    def extract(self) -> Mapping[str, object]:
        files = sorted(self.backend_root.rglob("*.go"))
        if not files:
            return self._empty_result()

        source = "\n".join(
            file.read_text(encoding="utf-8", errors="ignore") for file in files
        )
        struct_dependencies = self._parse_struct_dependencies(source)
        route_snapshot = self._parse_route_links(source)
        method_calls = self._parse_method_calls(source, struct_dependencies)

        handler_service_links = self._build_handler_service_links(
            route_snapshot["handlers"],
            method_calls,
        )
        service_repository_links = self._build_service_repository_links(
            handler_service_links,
            method_calls,
        )

        return {
            "apis": sorted(route_snapshot["apis"]),
            "handlers": sorted(route_snapshot["handlers"]),
            "api_handler_links": self._sorted_links(route_snapshot["api_handler_links"]),
            "handler_service_links": self._sorted_links(handler_service_links),
            "service_repository_links": self._sorted_links(service_repository_links),
        }

    def _parse_route_links(self, source: str) -> Mapping[str, object]:
        apis: set[str] = set()
        handlers: set[str] = set()
        api_handler_links: list[dict[str, str]] = []
        seen_links: set[tuple[str, str]] = set()

        for function_block in self._iter_function_blocks(source):
            param_types = self._parse_param_types(function_block["params"])
            body = function_block["body"]
            for route_call in self._ROUTE_CALL_PATTERN.finditer(body):
                receiver_name = route_call.group("receiver")
                handler_struct = param_types.get(receiver_name, "")
                if not handler_struct:
                    continue
                api_id = f"{route_call.group('method').lower()}:{route_call.group('path')}"
                handler_id = f"{handler_struct}.{route_call.group('handler')}"
                link_key = (api_id, handler_id)
                if link_key in seen_links:
                    continue
                seen_links.add(link_key)
                apis.add(api_id)
                handlers.add(handler_id)
                api_handler_links.append(
                    {"api_id": api_id, "handler_id": handler_id}
                )

        return {
            "apis": apis,
            "handlers": handlers,
            "api_handler_links": api_handler_links,
        }

    def _parse_param_types(self, params_text: str) -> Dict[str, str]:
        bindings: Dict[str, str] = {}
        for raw_param in params_text.split(","):
            token = raw_param.strip()
            if not token:
                continue
            match = re.match(r"(?P<name>\w+)\s+\*?(?P<type>\w+)$", token)
            if not match:
                continue
            bindings[match.group("name")] = match.group("type")
        return bindings

    def _parse_struct_dependencies(self, source: str) -> Mapping[str, Mapping[str, str]]:
        dependencies: dict[str, dict[str, str]] = {}
        for struct_match in self._STRUCT_PATTERN.finditer(source):
            struct_name = struct_match.group("name")
            fields: dict[str, str] = {}
            for field_match in self._FIELD_PATTERN.finditer(struct_match.group("body")):
                fields[field_match.group("field")] = field_match.group("dependency")
            dependencies[struct_name] = fields
        return dependencies

    def _parse_method_calls(
        self,
        source: str,
        struct_dependencies: Mapping[str, Mapping[str, str]],
    ) -> Mapping[str, list[tuple[str, str]]]:
        method_calls: dict[str, list[tuple[str, str]]] = {}
        for method_block in self._iter_method_blocks(source):
            receiver_var = method_block["receiver_var"]
            receiver_type = method_block["receiver_type"]
            method_name = method_block["method"]
            method_id = f"{receiver_type}.{method_name}"
            body = method_block["body"]
            call_pattern = re.compile(
                rf"\b{receiver_var}\.(?P<field>\w+)\.(?P<callee>\w+)\s*\("
            )
            calls: list[tuple[str, str]] = []
            seen_calls: set[tuple[str, str]] = set()
            for call_match in call_pattern.finditer(body):
                field_name = call_match.group("field")
                dependency_type = (struct_dependencies.get(receiver_type) or {}).get(
                    field_name, ""
                )
                if not dependency_type:
                    continue
                call_key = (dependency_type, call_match.group("callee"))
                if call_key in seen_calls:
                    continue
                seen_calls.add(call_key)
                calls.append(call_key)
            if calls:
                method_calls[method_id] = calls
        return method_calls

    def _build_handler_service_links(
        self,
        handler_ids: Iterable[str],
        method_calls: Mapping[str, List[tuple[str, str]]],
    ) -> list[dict[str, str]]:
        links: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for handler_id in sorted(handler_ids):
            for dependency_type, method_name in method_calls.get(handler_id, []):
                if not self._is_service_dependency(dependency_type):
                    continue
                service_id = f"{dependency_type}.{method_name}"
                key = (handler_id, service_id)
                if key in seen:
                    continue
                seen.add(key)
                links.append({"handler_id": handler_id, "service_id": service_id})
        return links

    def _build_service_repository_links(
        self,
        handler_service_links: Iterable[Mapping[str, str]],
        method_calls: Mapping[str, List[tuple[str, str]]],
    ) -> list[dict[str, str]]:
        links: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        service_ids = sorted(
            {
                str(link.get("service_id") or "").strip()
                for link in handler_service_links
                if str(link.get("service_id") or "").strip()
            }
        )
        for service_id in service_ids:
            for dependency_type, method_name in method_calls.get(service_id, []):
                if not self._is_repository_dependency(dependency_type):
                    continue
                repository_id = f"{dependency_type}.{method_name}"
                key = (service_id, repository_id)
                if key in seen:
                    continue
                seen.add(key)
                links.append(
                    {"service_id": service_id, "repository_id": repository_id}
                )
        return links

    def _sorted_links(self, links: Iterable[Mapping[str, str]]) -> list[dict[str, str]]:
        rows = [dict(link) for link in links]
        rows.sort(key=lambda row: tuple((key, row.get(key, "")) for key in sorted(row)))
        return rows

    def _iter_function_blocks(self, source: str) -> Iterable[dict[str, str]]:
        for function_match in self._FUNCTION_SIGNATURE_PATTERN.finditer(source):
            block_start = function_match.end() - 1
            block_end = self._find_matching_brace(source, block_start)
            if block_end < 0:
                continue
            yield {
                "name": function_match.group("name"),
                "params": function_match.group("params"),
                "body": source[block_start + 1 : block_end],
            }

    def _iter_method_blocks(self, source: str) -> Iterable[dict[str, str]]:
        for method_match in self._METHOD_SIGNATURE_PATTERN.finditer(source):
            block_start = method_match.end() - 1
            block_end = self._find_matching_brace(source, block_start)
            if block_end < 0:
                continue
            yield {
                "receiver_var": method_match.group("receiver_var"),
                "receiver_type": method_match.group("receiver_type"),
                "method": method_match.group("method"),
                "body": source[block_start + 1 : block_end],
            }

    def _find_matching_brace(self, source: str, start: int) -> int:
        depth = 0
        in_line_comment = False
        in_block_comment = False
        in_double_quote = False
        in_single_quote = False
        in_raw_quote = False
        index = start

        while index < len(source):
            char = source[index]
            next_char = source[index + 1] if index + 1 < len(source) else ""

            if in_line_comment:
                if char == "\n":
                    in_line_comment = False
                index += 1
                continue

            if in_block_comment:
                if char == "*" and next_char == "/":
                    in_block_comment = False
                    index += 2
                    continue
                index += 1
                continue

            if in_double_quote:
                if char == "\\":
                    index += 2
                    continue
                if char == '"':
                    in_double_quote = False
                index += 1
                continue

            if in_single_quote:
                if char == "\\":
                    index += 2
                    continue
                if char == "'":
                    in_single_quote = False
                index += 1
                continue

            if in_raw_quote:
                if char == "`":
                    in_raw_quote = False
                index += 1
                continue

            if char == "/" and next_char == "/":
                in_line_comment = True
                index += 2
                continue
            if char == "/" and next_char == "*":
                in_block_comment = True
                index += 2
                continue
            if char == '"':
                in_double_quote = True
                index += 1
                continue
            if char == "'":
                in_single_quote = True
                index += 1
                continue
            if char == "`":
                in_raw_quote = True
                index += 1
                continue

            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return index
            index += 1
        return -1

    def _is_service_dependency(self, dependency_type: str) -> bool:
        normalized = dependency_type.strip().lower()
        return normalized.endswith(self._SERVICE_TYPE_SUFFIXES)

    def _is_repository_dependency(self, dependency_type: str) -> bool:
        normalized = dependency_type.strip().lower()
        return normalized.endswith(self._REPOSITORY_TYPE_SUFFIXES)

    def _empty_result(self) -> Mapping[str, object]:
        return {
            "apis": [],
            "handlers": [],
            "api_handler_links": [],
            "handler_service_links": [],
            "service_repository_links": [],
        }
