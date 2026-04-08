import os
from pathlib import Path
from typing import Union

PathLike = Union[Path, str]


class ArchiveManager:
    def __init__(self, *, doc_repo: PathLike, backend_root: PathLike, frontend_root: PathLike):
        doc_repo_path = self._normalize_path(doc_repo)
        backend_path = self._normalize_path(backend_root)
        frontend_path = self._normalize_path(frontend_root)

        if self._is_inside_or_equal(doc_repo_path, backend_path):
            raise ValueError(self._isolation_error_message("backend_path"))

        if self._is_inside_or_equal(doc_repo_path, frontend_path):
            raise ValueError(self._isolation_error_message("frontend_path"))

        self.doc_repo = doc_repo_path
        self.backend_root = backend_path
        self.frontend_root = frontend_path

    @staticmethod
    def _normalize_path(value: PathLike) -> Path:
        expanded = Path(value).expanduser()
        absolute = os.path.abspath(expanded)
        resolved = os.path.realpath(absolute)
        normalized = os.path.normcase(os.path.normpath(resolved))
        return Path(normalized)

    @staticmethod
    def _is_inside_or_equal(candidate: Path, ancestor: Path) -> bool:
        candidate_str = str(candidate)
        ancestor_str = str(ancestor)
        if candidate_str == ancestor_str:
            return True

        try:
            return os.path.commonpath([candidate_str, ancestor_str]) == ancestor_str
        except ValueError:
            return False

    @staticmethod
    def _isolation_error_message(conflict_label: str) -> str:
        return (
            f"参数冲突：output_path 不能位于或等于 {conflict_label}。"
            "请将 output_path 调整为 backend_path/frontend_path 之外的独立文档目录后重试。"
        )
