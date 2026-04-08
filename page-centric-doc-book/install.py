import argparse
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple


DEFAULT_SKILLS_TARGET = Path.home() / ".codex" / "skills"
EXPECTED_SKILL_DIRS = ("page-centric-doc-book", "page_centric_doc_book")


def parse_version(version: str) -> Tuple[int, int, int]:
    """解析 x.y.z 格式的版本号并返回可比较的数字元组。"""
    parts = [segment.strip() for segment in version.split(".")]
    if len(parts) != 3:
        raise ValueError("版本号必须为 x.y.z 格式")

    try:
        return tuple(int(part) for part in parts)
    except ValueError as exc:
        raise ValueError("版本号每一段必须是整数") from exc


def _sanitize_version_label(version_label: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", version_label)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or "unknown"


def _resolve_backup_base_dir(target_root: Path) -> Path:
    resolved_target = target_root.resolve()
    resolved_default = DEFAULT_SKILLS_TARGET.resolve()
    if resolved_target == resolved_default:
        return resolved_target.parent / "skill-backups"
    return resolved_target / "backups"


def _backup_existing_install(target_root: Path, version_label: str) -> None:
    sanitized_label = _sanitize_version_label(version_label)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    base_name = f"page-centric-doc-book-{sanitized_label}-{timestamp}"
    backup_base_dir = _resolve_backup_base_dir(target_root)
    backup_root = backup_base_dir / base_name
    counter = 1
    while backup_root.exists():
        backup_root = backup_base_dir / f"{base_name}-{counter}"
        counter += 1
    backup_root.mkdir(parents=True, exist_ok=True)

    for skill_dir in EXPECTED_SKILL_DIRS:
        src = target_root / skill_dir
        if src.exists():
            shutil.copytree(src, backup_root / skill_dir)

    manifest_path = target_root / "page-centric-doc-book.manifest.json"
    if manifest_path.exists():
        shutil.copy2(manifest_path, backup_root / manifest_path.name)


def resolve_install_target(custom_target: Optional[str] = None) -> Path:
    """返回安装目标目录，自定义则取其，否则使用默认目录。"""
    if custom_target:
        return Path(custom_target).expanduser().resolve()
    return DEFAULT_SKILLS_TARGET


def install_package(
    package_root: Path, target_root: Path, force: bool, no_backup: bool
) -> None:
    """复制技能目录并写入安装 manifest。"""
    skills_root = package_root / "skills"
    manifest_path = package_root / "manifest.json"
    target_root.mkdir(parents=True, exist_ok=True)

    if not manifest_path.exists():
        raise FileNotFoundError(
            f"缺失 manifest.json，路径：{manifest_path.resolve()}"
        )

    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    new_version = manifest_data.get("version")
    if not isinstance(new_version, str):
        raise ValueError("manifest 中缺失 version 字段")

    new_version_tuple = parse_version(new_version)

    target_manifest = target_root / "page-centric-doc-book.manifest.json"
    existing_skill_dir = any(
        (target_root / skill_dir).exists() for skill_dir in EXPECTED_SKILL_DIRS
    )
    existing_install_present = target_manifest.exists() or existing_skill_dir

    installed_version = None
    installed_version_tuple = None
    if target_manifest.exists():
        installed_manifest = json.loads(
            target_manifest.read_text(encoding="utf-8")
        )
        installed_version = installed_manifest.get("version")
        if isinstance(installed_version, str):
            try:
                installed_version_tuple = parse_version(installed_version)
            except ValueError:
                installed_version_tuple = None

    if installed_version_tuple is not None:
        if not force:
            if installed_version_tuple > new_version_tuple:
                raise RuntimeError(
                    "目标已安装版本高于当前版本，使用 force=True 才能降级。"
                )
            if installed_version_tuple == new_version_tuple:
                return
    elif existing_install_present and not force:
        raise FileExistsError(
            "目标存在旧安装但无法识别版本，使用 force=True 绕过。"
        )

    if not no_backup and existing_install_present:
        _backup_existing_install(target_root, installed_version or "unknown")

    for skill_dir in EXPECTED_SKILL_DIRS:
        source_dir = skills_root / skill_dir
        if not source_dir.exists():
            raise FileNotFoundError(f"缺失 skill 目录：{source_dir.resolve()}")

        destination = target_root / skill_dir
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source_dir, destination)

    manifest_data["installed_at"] = datetime.now(timezone.utc).isoformat()
    manifest_data["installed_from"] = str(package_root.resolve())
    manifest_data["install_target"] = str(target_root.resolve())

    target_manifest.write_text(
        json.dumps(manifest_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="安装 page-centric-doc-book 发布包到 Codex skills 目录。"
    )
    parser.add_argument(
        "--package-root",
        default=str(Path(__file__).resolve().parent),
        help="发布包根目录，默认使用 install.py 所在目录。",
    )
    parser.add_argument(
        "--target",
        default=None,
        help="安装目标目录，默认使用 ~/.codex/skills。",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="强制覆盖或降级安装。",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="升级时不创建旧版本备份。",
    )
    return parser


def _format_cli_error_message(exc: Exception) -> str:
    message = str(exc)
    replacements = {
        "force=True": "--force",
        "no_backup=True": "--no-backup",
    }
    for old, new in replacements.items():
        message = message.replace(old, new)
    return message


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    package_root = Path(args.package_root).expanduser().resolve()
    target_root = resolve_install_target(args.target)

    print(f"[install] package_root={package_root}")
    print(f"[install] target_root={target_root}")

    try:
        install_package(
            package_root=package_root,
            target_root=target_root,
            force=args.force,
            no_backup=args.no_backup,
        )
    except Exception as exc:
        print(f"[install] failed: {_format_cli_error_message(exc)}", file=sys.stderr)
        return 1

    print("[install] done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
