"""抖音浏览器登录态的读取、迁移和状态检查。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .meta import get_auth_path, get_auth_store_key, get_browser_profile_dir
from .secret_store import read_secret, secret_dir, write_secret


def load_auth() -> dict[str, Any]:
    encrypted = read_secret(f"douyin_auth_{get_auth_store_key()}")
    if encrypted:
        try:
            return json.loads(encrypted.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return {}
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
    encrypted_path = write_secret(
        f"douyin_auth_{get_auth_store_key()}",
        json.dumps(state, ensure_ascii=False).encode("utf-8"),
    )
    if path.exists():
        path.unlink()
    return encrypted_path


def normalize_imported_cookies(value: Any) -> list[dict[str, Any]]:
    cookies = value.get("cookies") if isinstance(value, dict) else value
    if not isinstance(cookies, list):
        raise ValueError("Cookie 文件必须是 JSON 数组或包含 cookies 数组的对象")
    result: list[dict[str, Any]] = []
    for item in cookies:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        domain = str(item.get("domain") or "")
        if "douyin.com" not in domain:
            continue
        cookie: dict[str, Any] = {
            "name": str(item["name"]),
            "value": str(item.get("value") or ""),
            "domain": domain,
            "path": str(item.get("path") or "/"),
            "httpOnly": bool(item.get("httpOnly")),
            "secure": bool(item.get("secure")),
        }
        expires = item.get("expires", item.get("expirationDate"))
        if expires not in (None, "", 0, -1):
            cookie["expires"] = float(expires)
        same_site = str(item.get("sameSite") or "").lower()
        mapping = {
            "lax": "Lax",
            "strict": "Strict",
            "none": "None",
            "no_restriction": "None",
        }
        if same_site in mapping:
            cookie["sameSite"] = mapping[same_site]
        result.append(cookie)
    if not result:
        raise ValueError("文件中没有找到 douyin.com Cookie")
    return result


def import_cookie_file(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    cookies = normalize_imported_cookies(value)
    saved = save_auth({"cookies": cookies, "origins": []})
    names = {cookie["name"] for cookie in cookies}
    return {
        "success": True,
        "cookie_count": len(cookies),
        "has_session": bool({"sessionid", "sessionid_ss"} & names),
        "encrypted": True,
        "store": str(secret_dir()),
        "source_deleted": False,
        "saved_file": saved.name,
    }


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
        "state_backup": "Windows DPAPI encrypted store",
        "source": auth.get("migrated_from", "persistent_edge_profile"),
        "cookie_count": len(auth.get("cookies") or []),
        "sec_user_id_configured": bool(
            auth.get("sec_user_id") or extract_cookie(auth, "sec_user_id")
        ),
    }
