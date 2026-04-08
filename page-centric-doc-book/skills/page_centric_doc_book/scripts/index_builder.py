from __future__ import annotations

from typing import Any, Iterable, Mapping

from .relation_graph_builder import RelationGraphBuilder

Snapshot = Mapping[str, Any]


class IndexBuilder:
    COVERAGE_FIELDS = (
        "page_summary",
        "entry_conditions",
        "steps",
        "api_complete",
        "request_response_fields",
        "table_field_complete",
        "permission_complete",
        "exception_complete",
        "flow_complete",
        "related_pages_complete",
        "business_logic_complete",
    )
    EVIDENCE_FIELDS = (
        "frontend_calls",
        "api_ids",
        "table_ids",
        "db_field_ids",
        "related_page_ids",
        "permission_points",
        "exception_flows",
    )

    def build(self, snapshot: Snapshot) -> Mapping[str, Any]:
        pages = self._build_pages(snapshot)
        apis = self._build_apis(snapshot)
        snapshot_menu_tree = self._normalize_menu_tree(snapshot.get("menu_tree") or [], pages)
        menu_tree = snapshot_menu_tree or self._build_menu_tree(snapshot, pages)
        databases = self._build_rows(snapshot.get("databases") or ())
        tables = self._build_rows(snapshot.get("tables") or ())
        db_fields = self._build_rows(snapshot.get("db_fields") or ())
        go_models = self._build_rows(snapshot.get("go_models") or ())
        components = self._build_rows(snapshot.get("components") or ())
        form_fields = self._build_rows(snapshot.get("form_fields") or ())
        grid_columns = self._build_rows(snapshot.get("grid_columns") or ())
        handlers = self._build_handler_ids(snapshot.get("handlers") or ())
        api_handler_links = self._build_rows(snapshot.get("api_handler_links") or ())
        handler_service_links = self._build_rows(
            snapshot.get("handler_service_links") or ()
        )
        service_repository_links = self._build_rows(
            snapshot.get("service_repository_links") or ()
        )
        self._attach_page_apis(pages, apis, snapshot)
        relation_graph = RelationGraphBuilder().build(
            {
                "pages": pages,
                "apis": apis,
                "page_api_links": snapshot.get("page_api_links") or (),
                "page_transitions": snapshot.get("page_transitions") or (),
                "tables": tables,
                "db_fields": db_fields,
                "form_fields": form_fields,
                "grid_columns": grid_columns,
                "page_call_chains": snapshot.get("page_call_chains") or {},
            }
        )
        page_evidence = self._build_page_evidence(pages)
        self._attach_page_frontend_calls(
            page_evidence, relation_graph.get("page_frontend_calls") or {}
        )
        volumes = self._build_volumes(pages, snapshot_menu_tree)
        topics = self._build_topics(pages, apis, snapshot)
        knowledge_cards = self._build_knowledge_cards(pages, topics)
        references = self._build_references(knowledge_cards)
        features = self._build_features(pages, snapshot)
        relations = self._build_relations(pages, apis, topics, snapshot, relation_graph)
        navigation = self._build_navigation(volumes, pages, topics, menu_tree)
        warnings = self._build_warnings(snapshot)
        return {
            "pages": pages,
            "apis": apis,
            "page_evidence": page_evidence,
            "features": features,
            "volumes": volumes,
            "topics": topics,
            "knowledge_cards": knowledge_cards,
            "references": references,
            "databases": databases,
            "tables": tables,
            "db_fields": db_fields,
            "go_models": go_models,
            "components": components,
            "form_fields": form_fields,
            "grid_columns": grid_columns,
            "handlers": handlers,
            "api_handler_links": api_handler_links,
            "handler_service_links": handler_service_links,
            "service_repository_links": service_repository_links,
            "page_table_links": relation_graph.get("page_table_links", []),
            "page_db_field_links": relation_graph.get("page_db_field_links", []),
            "field_mappings": relation_graph.get("field_mappings", []),
            "business_domains": relation_graph.get("business_domains", {}),
            "implicit_flows": relation_graph.get("implicit_flows", []),
            "relations": relations,
            "navigation": navigation,
            "warnings": warnings,
        }

    def _build_pages(self, snapshot: Snapshot) -> dict[str, dict[str, Any]]:
        pages_map: dict[str, dict[str, Any]] = {}
        for page in snapshot.get("pages") or ():
            page_id = page.get("page_id")
            if not page_id:
                continue
            pages_map[page_id] = {
                "page_id": page_id,
                "route_path": page.get("route_path", ""),
                "requires_auth": page.get("requires_auth", ""),
                "title": page.get("title", ""),
                "parent_page_id": page.get("parent_page_id", ""),
                "component_path": page.get("component_path", ""),
            }
        ordered_pages: dict[str, dict[str, Any]] = {}
        for page_id in sorted(pages_map):
            ordered_pages[page_id] = {
                "page_id": page_id,
                "route_path": pages_map[page_id].get("route_path", ""),
                "requires_auth": pages_map[page_id].get("requires_auth", ""),
                "title": pages_map[page_id].get("title", ""),
                "parent_page_id": pages_map[page_id].get("parent_page_id", ""),
                "component_path": pages_map[page_id].get("component_path", ""),
                "api_ids": [],
            }
        self._apply_menu_metadata(ordered_pages, snapshot)
        return ordered_pages

    def _build_page_evidence(
        self,
        pages: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        evidence_pool: dict[str, dict[str, Any]] = {}
        for page_id in sorted(pages):
            page = pages.get(page_id) or {}
            if not page.get("is_leaf"):
                continue
            evidence_pool[page_id] = {
                "page_id": page_id,
                "status": "draft",
                "round": 0,
                "gaps": [],
                "resolved_gaps": [],
                "coverage": {field: False for field in self.COVERAGE_FIELDS},
                "evidence": {field: [] for field in self.EVIDENCE_FIELDS},
            }
        return evidence_pool

    def _attach_page_frontend_calls(
        self,
        page_evidence: Mapping[str, Mapping[str, Any]],
        page_frontend_calls: Mapping[str, Mapping[str, Any]],
    ) -> None:
        for page_id, calls in page_frontend_calls.items():
            evidence_entry = page_evidence.get(page_id)
            if not evidence_entry or not isinstance(calls, Mapping):
                continue
            frontend_calls_list = evidence_entry.get("evidence", {}).get("frontend_calls")
            if isinstance(frontend_calls_list, list):
                frontend_calls_list.append(dict(calls))

    def _apply_menu_metadata(
        self,
        pages: dict[str, dict[str, Any]],
        snapshot: Snapshot,
    ) -> None:
        leaf_page_ids = {
            str(page_id or "").strip()
            for page_id in snapshot.get("leaf_page_ids") or ()
            if str(page_id or "").strip()
        }
        for page_id, page in pages.items():
            page["is_leaf"] = page_id in leaf_page_ids
            page["menu_ancestors"] = self._build_menu_ancestors(pages, page_id)

    def _build_menu_ancestors(
        self,
        pages: Mapping[str, Mapping[str, Any]],
        page_id: str,
    ) -> list[str]:
        ancestors: list[str] = []
        visited: set[str] = set()
        current = page_id
        while True:
            parent_id = str(
                (pages.get(current) or {}).get("parent_page_id") or ""
            ).strip()
            if not parent_id or parent_id in visited:
                break
            visited.add(parent_id)
            ancestors.insert(0, parent_id)
            if parent_id not in pages:
                break
            current = parent_id
        return ancestors

    def _build_apis(self, snapshot: Snapshot) -> dict[str, dict[str, Any]]:
        apis_map: dict[str, dict[str, Any]] = {}
        for api in snapshot.get("apis") or ():
            api_id = api.get("api_id")
            if not api_id:
                continue
            apis_map[api_id] = dict(api)
        return {api_id: apis_map[api_id] for api_id in sorted(apis_map)}

    def _attach_page_apis(
        self,
        pages: dict[str, dict[str, Any]],
        apis: Mapping[str, Mapping[str, Any]],
        snapshot: Snapshot,
    ) -> None:
        api_ids_by_page: dict[str, set[str]] = {page_id: set() for page_id in pages}
        for link in snapshot.get("page_api_links") or ():
            page_id = link.get("page_id")
            api_id = link.get("api_id")
            if not page_id or not api_id:
                continue
            if page_id not in api_ids_by_page or api_id not in apis:
                continue
            api_ids_by_page[page_id].add(api_id)

        for page_id in pages:
            pages[page_id]["api_ids"] = sorted(api_ids_by_page.get(page_id, []))

    def _build_features(
        self,
        pages: Mapping[str, Mapping[str, Any]],
        snapshot: Snapshot,
    ) -> dict[str, dict[str, Any]]:
        features: dict[str, dict[str, Any]] = {}
        adjacency = self._build_transition_graph(pages, snapshot.get("page_transitions") or ())
        visited: set[str] = set()
        for page_id in sorted(pages):
            if page_id in visited:
                continue
            component = self._collect_transition_component(page_id, adjacency)
            visited.update(component)
            if len(component) < 2:
                continue
            feature_id = f"feature.{page_id}.journey"
            api_ids = self._collect_group_api_ids(component, pages)
            title_seed = str((pages.get(page_id) or {}).get("title") or page_id).strip() or page_id
            features[feature_id] = {
                "feature_id": feature_id,
                "title": f"{title_seed} 端到端主线",
                "page_ids": component,
                "api_ids": api_ids,
            }
        return features

    def _build_transition_graph(
        self,
        pages: Mapping[str, Mapping[str, Any]],
        transitions: Iterable[Mapping[str, Any]],
    ) -> dict[str, set[str]]:
        adjacency: dict[str, set[str]] = {page_id: set() for page_id in pages}
        for row in transitions:
            from_page_id = str(row.get("from_page_id") or row.get("from") or "").strip()
            to_page_id = str(row.get("to_page_id") or row.get("to") or "").strip()
            if from_page_id not in adjacency or to_page_id not in adjacency:
                continue
            if from_page_id == to_page_id:
                continue
            adjacency[from_page_id].add(to_page_id)
            adjacency[to_page_id].add(from_page_id)
        return adjacency

    def _collect_transition_component(
        self, seed_page_id: str, adjacency: Mapping[str, set[str]]
    ) -> list[str]:
        visited: set[str] = set()
        ordered: list[str] = []
        pending = [seed_page_id]
        while pending:
            current = pending.pop(0)
            if current in visited:
                continue
            visited.add(current)
            ordered.append(current)
            for neighbor in sorted(adjacency.get(current) or ()):
                if neighbor not in visited:
                    pending.append(neighbor)
        return ordered

    def _collect_group_api_ids(
        self,
        page_ids: Iterable[str],
        pages: Mapping[str, Mapping[str, Any]],
    ) -> list[str]:
        api_ids: set[str] = set()
        for page_id in page_ids:
            page = pages.get(page_id) or {}
            for api_id in page.get("api_ids") or []:
                api_ids.add(str(api_id))
        return sorted(api_ids)

    def _build_volumes(
        self,
        pages: Mapping[str, Mapping[str, Any]],
        menu_tree: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        volumes: dict[str, dict[str, Any]] = {}
        assigned_page_ids: set[str] = set()
        for root in menu_tree:
            volume_id = str(root.get("page_id") or "").strip()
            if not volume_id or volume_id not in pages:
                continue
            volume_title = str(root.get("title") or (pages.get(volume_id) or {}).get("title") or volume_id).strip() or volume_id
            volume = volumes.setdefault(
                volume_id,
                {"volume_id": volume_id, "title": volume_title, "page_ids": []},
            )
            for page_id in self._collect_menu_page_ids(root):
                if page_id not in pages or page_id in assigned_page_ids:
                    continue
                volume["page_ids"].append(page_id)
                pages[page_id]["volume_id"] = volume_id
                assigned_page_ids.add(page_id)

        for page_id in sorted(pages):
            if page_id in assigned_page_ids:
                continue
            page = pages[page_id]
            route_path = page.get("route_path", "")
            volume_id = self._derive_volume_id(route_path)
            volume = volumes.setdefault(
                volume_id,
                {"volume_id": volume_id, "title": volume_id, "page_ids": []},
            )
            if (
                str(page.get("route_path") or "").strip() == f"/{volume_id}"
                and str(page.get("title") or "").strip()
            ):
                volume["title"] = str(page.get("title") or "").strip()
            volume["page_ids"].append(page_id)
            page["volume_id"] = volume_id
        return volumes

    def _collect_menu_page_ids(self, node: Mapping[str, Any]) -> list[str]:
        page_ids: list[str] = []
        page_id = str(node.get("page_id") or "").strip()
        if page_id:
            page_ids.append(page_id)
        for child in node.get("children") or []:
            if isinstance(child, Mapping):
                page_ids.extend(self._collect_menu_page_ids(child))
        return page_ids

    def _derive_volume_id(self, route_path: Any) -> str:
        if not isinstance(route_path, str):
            return "root"
        segments = [segment for segment in route_path.split("/") if segment]
        if not segments:
            return "root"
        return segments[0]

    def _build_topics(
        self,
        pages: Mapping[str, Mapping[str, Any]],
        apis: Mapping[str, Mapping[str, Any]],
        snapshot: Snapshot,
    ) -> dict[str, dict[str, Any]]:
        # 预留 apis/snapshot 参数，后续可按 API 语义与发现上下文扩展专题归并规则。
        del apis, snapshot
        topics: dict[str, dict[str, Any]] = {}
        for page_id in sorted(pages):
            page = pages[page_id]
            api_ids = list(page.get("api_ids") or [])
            if not api_ids:
                continue
            topic_id = f"topic.{page_id}.mainline"
            topics[topic_id] = {
                "topic_id": topic_id,
                "title": f"{page.get('title') or page_id} 主线专题",
                "page_ids": [page_id],
                "api_ids": api_ids,
            }
        return topics

    def _build_knowledge_cards(
        self,
        pages: Mapping[str, Mapping[str, Any]],
        topics: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        auth_pages = [
            page_id
            for page_id in sorted(pages)
            if self._is_truthy((pages.get(page_id) or {}).get("requires_auth"))
        ]
        if not auth_pages:
            return {}

        related_topics = [
            topic_id
            for topic_id in sorted(topics)
            if set(auth_pages).intersection((topics.get(topic_id) or {}).get("page_ids") or [])
        ]
        return {
            "knowledge.auth_access": {
                "card_id": "auth_access",
                "title": "认证访问规则",
                "slug": "auth-access",
                "summary": "记录需要登录态才能访问的页面与相关阅读线索。",
                "points": [f"页面 {page_id} 需要登录后访问" for page_id in auth_pages],
                "page_ids": auth_pages,
                "topic_ids": related_topics,
                "reference_ids": ["reference.permissions_book"],
            }
        }

    def _build_references(
        self, knowledge_cards: Mapping[str, Mapping[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        if "knowledge.auth_access" not in knowledge_cards:
            return {}
        return {
            "reference.permissions_book": {
                "reference_id": "reference.permissions_book",
                "title": "权限体系书",
                "slug": "permissions-book",
                "target_book": "permissions-book",
                "target_path": "README.md",
                "reference_type": "book",
                "reason": "存在需要登录态控制的页面，建议延伸阅读权限与认证体系。",
                "source_node_id": "knowledge.auth_access",
                "source": "跨书引用",
                "summary": "延伸阅读权限、认证与访问控制的完整说明。",
                "links": ["book://permissions-book/README.md"],
            }
        }

    def _build_relations(
        self,
        pages: Mapping[str, Mapping[str, Any]],
        apis: Mapping[str, Mapping[str, Any]],
        topics: Mapping[str, Mapping[str, Any]],
        snapshot: Snapshot,
        relation_graph: Mapping[str, Any],
    ) -> dict[str, Any]:
        del pages, apis
        route_relations = self._sorted_rows(snapshot.get("route_relations") or ())
        page_transitions = self._sorted_rows(snapshot.get("page_transitions") or ())
        topic_bindings: list[dict[str, Any]] = []
        for topic_id in sorted(topics):
            topic = topics[topic_id] or {}
            topic_bindings.append(
                {
                    "topic_id": topic_id,
                    "page_ids": list(topic.get("page_ids") or []),
                    "api_ids": list(topic.get("api_ids") or []),
                }
            )
        return {
            "route_relations": route_relations,
            "page_transitions": page_transitions,
            "topic_bindings": topic_bindings,
            "page_table_links": self._sorted_rows(relation_graph.get("page_table_links") or ()),
            "page_db_field_links": self._sorted_rows(relation_graph.get("page_db_field_links") or ()),
            "field_mappings": self._sorted_rows(relation_graph.get("field_mappings") or ()),
            "implicit_flows": self._sorted_rows(relation_graph.get("implicit_flows") or ()),
            "business_domains": {
                str(domain_id): dict(domain)
                for domain_id, domain in sorted(
                    (relation_graph.get("business_domains") or {}).items()
                )
            },
        }

    def _build_navigation(
        self,
        volumes: Mapping[str, Mapping[str, Any]],
        pages: Mapping[str, Mapping[str, Any]],
        topics: Mapping[str, Mapping[str, Any]],
        menu_tree: list[dict[str, Any]],
    ) -> dict[str, Any]:
        volume_pages: dict[str, list[str]] = {}
        for volume_id in volumes:
            volume = volumes[volume_id] or {}
            volume_pages[volume_id] = list(volume.get("page_ids") or [])
        page_topics: dict[str, list[str]] = {page_id: [] for page_id in sorted(pages)}
        for topic_id in sorted(topics):
            topic = topics[topic_id] or {}
            for page_id in topic.get("page_ids") or []:
                if page_id in page_topics:
                    page_topics[page_id].append(topic_id)
        return {
            "volume_order": list(volumes.keys()),
            "volume_pages": volume_pages,
            "page_topics": page_topics,
            "menu_tree": menu_tree,
        }

    def _build_menu_tree(
        self,
        snapshot: Snapshot,
        pages: Mapping[str, Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        snapshot_menu_tree = snapshot.get("menu_tree") or []
        if snapshot_menu_tree:
            normalized_menu_tree = self._normalize_menu_tree(snapshot_menu_tree, pages)
            if normalized_menu_tree:
                return normalized_menu_tree
        nodes: dict[str, dict[str, Any]] = {}
        for page_id in sorted(pages):
            page = pages[page_id] or {}
            nodes[page_id] = {
                "page_id": page_id,
                "title": str(page.get("title") or page_id),
                "parent_page_id": str(page.get("parent_page_id") or "").strip(),
                "children": [],
            }
        roots: list[dict[str, Any]] = []
        for page_id in sorted(nodes):
            node = nodes[page_id]
            parent_page_id = node["parent_page_id"]
            if parent_page_id and parent_page_id in nodes:
                nodes[parent_page_id]["children"].append(node)
            else:
                roots.append(node)
        return roots

    def _normalize_menu_tree(
        self,
        nodes: Iterable[Any],
        pages: Mapping[str, Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        normalized_nodes: list[dict[str, Any]] = []
        for raw_node in nodes:
            if not isinstance(raw_node, Mapping):
                continue
            page_id = str(raw_node.get("page_id") or "").strip()
            if not page_id:
                continue
            page = pages.get(page_id) or {}
            children = self._normalize_menu_tree(raw_node.get("children") or [], pages)
            normalized_nodes.append(
                {
                    "page_id": page_id,
                    "title": str(raw_node.get("title") or page.get("title") or page_id).strip() or page_id,
                    "parent_page_id": str(raw_node.get("parent_page_id") or page.get("parent_page_id") or "").strip(),
                    "route_path": str(raw_node.get("route_path") or page.get("route_path") or "").strip(),
                    "component_path": str(raw_node.get("component_path") or page.get("component_path") or "").strip(),
                    "children": children,
                }
            )
        return normalized_nodes

    def _build_warnings(self, snapshot: Snapshot) -> list[str]:
        warnings: list[str] = []
        for raw_warning in snapshot.get("warnings") or ():
            warning_text = str(raw_warning or "").strip()
            if warning_text and warning_text not in warnings:
                warnings.append(warning_text)
        return warnings

    def _build_rows(self, rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return self._sorted_rows(rows)

    def _build_handler_ids(self, handlers: Iterable[Any]) -> list[str]:
        normalized = {
            str(handler_id).strip()
            for handler_id in handlers
            if str(handler_id).strip()
        }
        return sorted(normalized)

    def _sorted_rows(self, rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        normalized_rows = [dict(row) for row in rows]
        normalized_rows.sort(key=lambda row: tuple((str(k), str(v)) for k, v in sorted(row.items())))
        return normalized_rows

    def _is_truthy(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}
