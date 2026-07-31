"""项目路径和数据目录管理。"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

_DATA_ROOT: Path | None = None
_ACCOUNT_KEY = "auto"


def get_project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def get_user_config_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = (
        Path(local_app_data)
        if local_app_data
        else Path.home() / "AppData" / "Local"
    )
    return base / "DouKU" / "config.json"


def load_config() -> dict[str, Any]:
    explicit = os.environ.get("DOUKU_CONFIG")
    candidates = [
        Path(explicit).expanduser() if explicit else None,
        get_user_config_path(),
        # 仅保留旧项目的平滑迁移兼容；公开仓库不包含此文件。
        get_project_root() / "douku_config.json",
    ]
    for config_path in candidates:
        if not config_path or not config_path.exists():
            continue
        try:
            return json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return {}


def set_data_dir(data_dir: str | Path) -> Path:
    global _DATA_ROOT
    _DATA_ROOT = Path(data_dir).expanduser().resolve()
    return ensure_directories(_DATA_ROOT)


def set_account(account_key: str) -> str:
    global _ACCOUNT_KEY
    _ACCOUNT_KEY = sanitize_dirname(account_key or "auto")
    return _ACCOUNT_KEY


def get_account_key() -> str:
    if _ACCOUNT_KEY == "auto":
        path = get_data_root() / "private" / "active_account.json"
        if path.exists():
            try:
                return sanitize_dirname(
                    json.loads(path.read_text(encoding="utf-8")).get(
                        "account_key", "default"
                    )
                )
            except (OSError, ValueError):
                pass
        return "default"
    return _ACCOUNT_KEY


def get_auth_store_key() -> str:
    return "auto" if is_auto_account() else sanitize_dirname(_ACCOUNT_KEY)


def is_auto_account() -> bool:
    return _ACCOUNT_KEY == "auto"


def remember_active_account(
    account_key: str,
    platform_user_id: str,
    nickname: str,
) -> Path:
    path = get_data_root() / "private" / "active_account.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "account_key": sanitize_dirname(account_key),
                "platform_user_id": platform_user_id,
                "nickname": nickname,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def get_data_root() -> Path:
    global _DATA_ROOT
    if _DATA_ROOT is None:
        configured = os.environ.get("DOUKU_DATA_DIR")
        if not configured:
            configured = load_config().get("data_dir")
        _DATA_ROOT = (
            Path(configured).expanduser().resolve()
            if configured
            else get_project_root() / "data"
        )
    return ensure_directories(_DATA_ROOT)


def ensure_directories(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name in ("private", "downloads", "output", "logs"):
        (root / name).mkdir(exist_ok=True)
    return root


def get_auth_path() -> Path:
    if is_auto_account():
        return get_data_root() / "private" / "douyin_state.json"
    if get_account_key() != "default":
        path = (
            get_data_root()
            / "private"
            / "accounts"
            / get_account_key()
            / "douyin_state.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    return get_data_root() / "private" / "douyin_state.json"


def get_browser_profile_dir() -> Path:
    """DouKU 专用 Edge 用户目录，不与用户日常 Edge 配置混用。"""
    path = (
        get_data_root() / "private" / "edge_profile"
        if is_auto_account() or get_account_key() == "default"
        else get_data_root()
        / "private"
        / "accounts"
        / get_account_key()
        / "edge_profile"
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_downloads_dir(category: str | None = None) -> Path:
    path = get_data_root() / "downloads"
    if category:
        path /= sanitize_dirname(category)
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_account_downloads_dir(account_key: str | None = None) -> Path:
    key = sanitize_dirname(account_key or get_account_key())
    path = get_downloads_dir() / "accounts" / key
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_direct_downloads_dir(platform: str | None = None) -> Path:
    path = get_downloads_dir() / "direct"
    if platform:
        path /= sanitize_dirname(platform)
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_creator_downloads_dir(platform: str, nickname: str) -> Path:
    path = (
        get_downloads_dir()
        / "creators"
        / sanitize_dirname(platform)
        / sanitize_dirname(nickname)
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def migrate_legacy_default_downloads() -> dict[str, Any]:
    """Move pre-2.1 account media into the isolated default-account tree."""
    downloads = get_downloads_dir()
    target_root = get_account_downloads_dir("default")
    moved: list[tuple[str, str]] = []
    conflicts: list[str] = []
    for name in ("videos", "covers", "images", "music", "image_posts"):
        source = downloads / name
        if not source.exists():
            continue
        target = target_root / name
        for item in sorted(source.rglob("*")):
            if not item.is_file():
                continue
            relative = item.relative_to(source)
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                conflicts.append(str(item))
                continue
            item.replace(destination)
            moved.append((str(item), str(destination)))
        for directory in sorted(
            (path for path in source.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass
        try:
            source.rmdir()
        except OSError:
            pass
    return {
        "moved": len(moved),
        "conflicts": conflicts,
        "path_changes": moved,
    }


def get_output_dir() -> Path:
    return get_data_root() / "output"


def sanitize_dirname(value: str) -> str:
    forbidden = '<>:"/\\|?*'
    result = "".join("_" if char in forbidden else char for char in value).strip(" .")
    return result[:80] or "未分类"


def init_project(data_dir: str | Path | None = None) -> dict[str, Any]:
    root = set_data_dir(data_dir) if data_dir else get_data_root()
    meta_path = root / "project_meta.json"
    meta = {
        "name": "DouKU",
        "version": "2.2",
        "data_root": str(root),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    if not meta_path.exists():
        meta["created_at"] = meta["updated_at"]
    else:
        try:
            old = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["created_at"] = old.get("created_at", meta["updated_at"])
        except (OSError, ValueError):
            meta["created_at"] = meta["updated_at"]
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"success": True, **meta}
