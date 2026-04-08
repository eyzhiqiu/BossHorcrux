from __future__ import annotations

from collections.abc import Mapping


class RecoveryManager:
    @staticmethod
    def diff_stale_nodes(previous: Mapping | None, current: Mapping | None) -> set[str]:
        stale = set()
        prev_pages = RecoveryManager._normalize_pages(previous)
        curr_pages = RecoveryManager._normalize_pages(current)

        all_page_ids = set(prev_pages.keys()) | set(curr_pages.keys())
        for page_id in all_page_ids:
            prev_meta = prev_pages.get(page_id)
            curr_meta = curr_pages.get(page_id)
            prev_fp = prev_meta.get("fingerprint") if isinstance(prev_meta, Mapping) else None
            curr_fp = curr_meta.get("fingerprint") if isinstance(curr_meta, Mapping) else None

            if prev_fp != curr_fp:
                stale.add(f"page.{page_id}")

        return stale

    @staticmethod
    def _normalize_pages(source: Mapping | None) -> Mapping:
        if not isinstance(source, Mapping):
            return {}
        pages = source.get("pages")
        return pages if isinstance(pages, Mapping) else {}
