from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from string import Template
from typing import Any, Mapping


class BookAssembler:
    """Render the README and BOOK landing pages from a simple context."""

    def __init__(self, template_root: Path | str, output_root: Path | str) -> None:
        self.template_root = Path(template_root).resolve()
        self.output_root = Path(output_root).resolve()
        self._template_cache: dict[Path, Template] = {}

    def build(self, context: Mapping[str, Any] | None) -> None:
        context = context or {}
        volume_links = self._format_links(context.get("volumes"), default="- 暂无卷目录")
        page_links = str(context.get("page_links_markdown") or "").strip() or self._format_links(
            context.get("pages"), default="- 暂无页面章节"
        )
        topic_links = self._format_links(context.get("topics"), default="- 暂无专题链路")
        knowledge_links = self._format_links(context.get("knowledge_cards"), default="- 暂无知识卡")
        reference_links = self._format_links(context.get("references"), default="- 暂无跨书引用")
        api_links = self._format_links(context.get("apis"), default="- 暂无接口章节")
        dictionary_links = self._format_links(context.get("dictionary_books"), default="- 暂无字典书")
        menu_links = str(context.get("menu_links") or "").strip() or "- 暂无页面导航"
        review_summary_links = self._format_review_summary(context.get("page_review_summary"))
        scan_summary = context.get("scan_summary") if isinstance(context.get("scan_summary"), Mapping) else {}
        warnings_section = self._format_text_lines(context.get("warnings"), default="- 暂无扫描告警")
        # 规则：
        # 1) 如果调用方显式提供了 features（即便为空），必须以本次任务集为准，禁止回退扫描 output_root/features。
        # 2) 仅当调用方未提供 features 时，才做磁盘扫描兼容旧行为。
        if "features" in context:
            feature_links = self._format_links(context.get("features"), default="- 暂无完整功能主线")
        else:
            discovered = self._discover_feature_entries()
            feature_links = self._format_links(discovered, default="- 暂无完整功能主线")
        stats = context.get("stats") or {}

        readme_tpl = self._load_template("book/README.md.tmpl")
        book_tpl = self._load_template("book/BOOK.md.tmpl")

        payload = {
            "completed_tasks": stats.get("completed_tasks", 0),
            "total_tasks": stats.get("total_tasks", 0),
            "volume_links": volume_links,
            "page_links": page_links,
            "topic_links": topic_links,
            "knowledge_links": knowledge_links,
            "reference_links": reference_links,
            "api_links": api_links,
            "page_count": self._resolve_count(scan_summary.get("page_count"), context.get("pages")),
            "api_count": self._resolve_count(scan_summary.get("api_count"), context.get("apis")),
            "topic_count": self._resolve_count(scan_summary.get("topic_count"), context.get("topics")),
            "warnings_section": warnings_section,
        }
        book_payload = {
            "volume_links": volume_links,
            "page_links": page_links,
            "topic_links": topic_links,
            "knowledge_links": knowledge_links,
            "reference_links": reference_links,
            "api_links": api_links,
            "page_count": self._resolve_count(scan_summary.get("page_count"), context.get("pages")),
            "api_count": self._resolve_count(scan_summary.get("api_count"), context.get("apis")),
            "topic_count": self._resolve_count(scan_summary.get("topic_count"), context.get("topics")),
            "warnings_section": warnings_section,
        }

        readme_content = self._append_extra_sections(
            readme_tpl.substitute(payload),
            menu_links=menu_links,
            dictionary_links=dictionary_links,
            feature_links=feature_links,
            review_summary_links=review_summary_links,
        )
        book_content = self._append_extra_sections(
            book_tpl.substitute(book_payload),
            menu_links=menu_links,
            dictionary_links=dictionary_links,
            feature_links=feature_links,
            review_summary_links=review_summary_links,
        )

        self.output_root.mkdir(parents=True, exist_ok=True)
        (self.output_root / "README.md").write_text(readme_content, encoding="utf-8")
        (self.output_root / "BOOK.md").write_text(book_content, encoding="utf-8")

    def _format_links(self, entries: Any, default: str) -> str:
        rows = []
        for entry in self._normalize_entries(entries):
            title = str(entry.get("title") or entry.get("name") or "").strip()
            path = str(entry.get("path") or "").strip()
            if title and path:
                rows.append(f"- [{title}]({path})")
            elif path:
                rows.append(f"- {path}")
            elif title:
                rows.append(f"- {title}")
        return "\n".join(rows) if rows else default

    def _append_extra_sections(
        self,
        content: str,
        *,
        menu_links: str,
        dictionary_links: str,
        feature_links: str,
        review_summary_links: str,
    ) -> str:
        return (
            f"{content.rstrip()}\n\n"
            f"## 页面导航\n{menu_links}\n\n"
            f"## 待复核页面\n{review_summary_links}\n\n"
            f"## 字典书\n{dictionary_links}\n\n"
            f"## 完整功能主线\n{feature_links}\n"
        )

    def _format_text_lines(self, values: Any, default: str) -> str:
        rows: list[str] = []
        if isinstance(values, str):
            text = values.strip()
            rows = [f"- {text}"] if text else []
        elif isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
            for value in values:
                text = str(value or "").strip()
                if text:
                    rows.append(f"- {text}")
        return "\n".join(rows) if rows else default

    def _resolve_count(self, raw_count: Any, entries: Any) -> int:
        try:
            if raw_count is not None:
                return int(raw_count)
        except (TypeError, ValueError):
            pass
        return len(self._normalize_entries(entries))

    def _discover_feature_entries(self) -> list[Mapping[str, Any]]:
        base_dir = self.output_root / "features"
        if not base_dir.exists() or not base_dir.is_dir():
            return []
        entries: list[Mapping[str, Any]] = []
        for file_path in sorted(base_dir.glob("*.md")):
            title = self._extract_markdown_title(file_path) or file_path.stem
            rel_path = file_path.relative_to(self.output_root).as_posix()
            entries.append({"title": title, "path": rel_path})
        return entries

    @staticmethod
    def _extract_markdown_title(path: Path) -> str:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return ""
        for line in text.splitlines():
            candidate = line.strip()
            if not candidate:
                continue
            if candidate.startswith("#"):
                return candidate.lstrip("#").strip()
            return ""
        return ""

    def _normalize_entries(self, value: Any) -> list[Mapping[str, Any]]:
        if value is None:
            return []
        if isinstance(value, Mapping):
            return [value]
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return [entry for entry in value if isinstance(entry, Mapping)]
        return []

    def _format_review_summary(self, summary: Any) -> str:
        if not isinstance(summary, Mapping):
            return "- 暂无待复核页面"
        rows: list[str] = []
        for status in ("partial", "blocked"):
            for entry in self._normalize_entries(summary.get(status)):
                title = str(entry.get("title") or entry.get("name") or "").strip()
                path = str(entry.get("path") or "").strip()
                status_label = str(entry.get("status") or status).strip()
                display = f"{title}（{status_label}）" if title else status_label
                if path:
                    rows.append(f"- [{display}]({path})")
                elif display:
                    rows.append(f"- {display}")
        return "\n".join(rows) if rows else "- 暂无待复核页面"

    def _load_template(self, relative_path: str) -> Template:
        target = (self.template_root / relative_path).resolve()
        if target not in self._template_cache:
            self._template_cache[target] = Template(target.read_text(encoding="utf-8"))
        return self._template_cache[target]
