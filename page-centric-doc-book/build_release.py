from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path


EXCLUDED_DIR_NAMES = {"tests", "__pycache__"}
EXCLUDED_FILE_SUFFIXES = {".pyc"}


def _copy_filtered_tree(source: Path, destination: Path) -> None:
    for root, dirs, files in os.walk(source):
        rel_root = Path(root).relative_to(source)
        target_root = destination / rel_root
        target_root.mkdir(parents=True, exist_ok=True)

        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIR_NAMES]

        for file in files:
            if Path(file).suffix.lower() in EXCLUDED_FILE_SUFFIXES:
                continue
            shutil.copy2(Path(root) / file, target_root / file)


def _copy_release_assets(repo_root: Path, release_root: Path) -> None:
    release_src = repo_root / "release" / "page-centric-doc-book"
    for file_name in ("README.md", "install.py", "build_release.py"):
        src = release_src / file_name
        if src.exists():
            shutil.copy2(src, release_root / file_name)


def build_release(repo_root: Path, output_root: Path) -> dict[str, Path]:
    release_root = output_root / "page-centric-doc-book"
    if release_root.exists():
        shutil.rmtree(release_root)
    release_root.mkdir(parents=True, exist_ok=True)

    manifest_src = repo_root / "release" / "page-centric-doc-book" / "manifest.json"
    if not manifest_src.exists():
        raise FileNotFoundError(f"找不到发行 manifest: {manifest_src}")
    manifest = json.loads(manifest_src.read_text(encoding="utf-8"))

    skills_root = release_root / "skills"
    if skills_root.exists():
        shutil.rmtree(skills_root)
    skills_root.mkdir()

    for skill_dir in ("page-centric-doc-book", "page_centric_doc_book"):
        source = repo_root / "skills" / skill_dir
        if not source.exists():
            raise FileNotFoundError(f"缺少 skill 目录: {source}")
        _copy_filtered_tree(source, skills_root / skill_dir)

    _copy_release_assets(repo_root, release_root)
    shutil.copy2(manifest_src, release_root / "manifest.json")

    zip_path = output_root / f"page-centric-doc-book-{manifest['version']}.zip"
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for file_path in release_root.rglob("*"):
            archive.write(file_path, file_path.relative_to(output_root))

    return {"release_dir": release_root, "zip_path": zip_path}


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="构建 page-centric-doc-book 发布目录与 zip 文件。"
    )
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parents[2]),
        help="源码仓库根目录，默认根据脚本路径推导。",
    )
    parser.add_argument(
        "--output-root",
        default=str((Path(__file__).resolve().parents[2] / "release" / "dist")),
        help="发布产物输出目录。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    try:
        repo_root = Path(args.repo_root).expanduser().resolve()
        output_root = Path(args.output_root).expanduser().resolve()
        output_root.mkdir(parents=True, exist_ok=True)

        print(f"[build_release] repo_root={repo_root}")
        print(f"[build_release] output_root={output_root}")

        result = build_release(repo_root=repo_root, output_root=output_root)
    except Exception as exc:
        print(f"[build_release] failed: {exc}", file=sys.stderr)
        return 1

    print(f"[build_release] release_dir={result['release_dir']}")
    print(f"[build_release] zip_path={result['zip_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
