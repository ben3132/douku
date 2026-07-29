"""项目路径和数据目录管理。"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

_DATA_ROOT: Path | None = None


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
    return get_data_root() / "private" / "douyin_state.json"


def get_browser_profile_dir() -> Path:
    """DouKU 专用 Edge 用户目录，不与用户日常 Edge 配置混用。"""
    path = get_data_root() / "private" / "edge_profile"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_downloads_dir(category: str | None = None) -> Path:
    path = get_data_root() / "downloads"
    if category:
        path /= sanitize_dirname(category)
    path.mkdir(parents=True, exist_ok=True)
    return path


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
        "version": "2.1",
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
