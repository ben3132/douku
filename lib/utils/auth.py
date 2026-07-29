"""抖音浏览器登录态的读取、迁移和状态检查。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .meta import get_auth_path, get_browser_profile_dir


def load_auth() -> dict[str, Any]:
    path = get_auth_path()
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    return {}


def save_auth(storage_state: dict[str, Any], sec_user_id: str = "") -> Path:
    path = get_auth_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    state = dict(storage_state)
    previous_sec_uid = ""
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
            previous_sec_uid = str(previous.get("sec_user_id") or "")
        except (OSError, ValueError):
            pass
    state["sec_user_id"] = (
        sec_user_id
        or extract_cookie(state, "sec_user_id")
        or previous_sec_uid
    )
    state["saved_at"] = datetime.now().isoformat(timespec="seconds")
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def extract_cookie(auth: dict[str, Any], name: str) -> str:
    for cookie in auth.get("cookies") or []:
        if cookie.get("name") == name:
            return str(cookie.get("value") or "")
    return ""


def has_session(auth: dict[str, Any] | None = None) -> bool:
    auth = auth or load_auth()
    return bool(extract_cookie(auth, "sessionid") or extract_cookie(auth, "sessionid_ss"))


def auth_status() -> dict[str, Any]:
    auth = load_auth()
    profile = get_browser_profile_dir()
    profile_entries = list(profile.iterdir()) if profile.exists() else []
    return {
        "configured": has_session(auth),
        "mode": "persistent_edge_profile",
        "profile_initialized": bool(profile_entries),
        "profile_path": str(profile),
        "saved_at": auth.get("saved_at"),
        "state_backup": str(get_auth_path()),
        "source": auth.get("migrated_from", "persistent_edge_profile"),
        "cookie_count": len(auth.get("cookies") or []),
        "sec_user_id_configured": bool(
            auth.get("sec_user_id") or extract_cookie(auth, "sec_user_id")
        ),
    }
