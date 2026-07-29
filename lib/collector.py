"""通过已登录的真实浏览器采集当前账号的个人抖音数据。

请求在 douyin.com 页面内发出，复用浏览器 Cookie、风控参数和国内网络，
不依赖易失效的手写 a_bogus 算法。
"""

from __future__ import annotations

import os
import time
from typing import Any

from lib.db.db_v4 import (
    get_conn,
    init_db,
    set_bookmark,
    upsert_aweme,
    upsert_comment,
)
from lib.utils.auth import extract_cookie, has_session, load_auth, save_auth
from lib.utils.meta import get_browser_profile_dir

HOME_URL = "https://www.douyin.com/"
TAB_URL = "https://www.douyin.com/user/self?showTab=favorite_collection"
RESPONSE_SOURCES = {
    "/aweme/v1/web/aweme/listcollection/": "favorites",
    "/aweme/v1/web/aweme/favorite/": "likes",
}


def _launch_persistent_context(playwright: Any, headless: bool) -> Any:
    channel = os.environ.get("DOUKU_BROWSER_CHANNEL", "msedge")
    profile = get_browser_profile_dir()
    options = {
        "user_data_dir": str(profile),
        "headless": headless,
        "locale": "zh-CN",
        "accept_downloads": False,
    }
    try:
        return playwright.chromium.launch_persistent_context(
            channel=channel,
            **options,
        )
    except Exception as first_error:
        try:
            return playwright.chromium.launch_persistent_context(**options)
        except Exception as fallback_error:
            detail = str(fallback_error or first_error)
            if "ProcessSingleton" in detail or "user data directory" in detail.lower():
                raise RuntimeError(
                    "DouKU 专用 Edge 目录正在被另一个进程使用，请关闭已打开的 "
                    "DouKU Edge 窗口后重试。"
                ) from fallback_error
            raise


def _restore_auth_backup(context: Any) -> bool:
    """仅在专用目录没有会话时导入旧 storage_state Cookie。"""
    current = {"cookies": context.cookies(), "origins": []}
    if has_session(current):
        return False
    backup = load_auth()
    if not has_session(backup):
        return False
    cookies = backup.get("cookies") or []
    if cookies:
        context.add_cookies(cookies)
        return True
    return False


def _context_page(context: Any) -> Any:
    pages = context.pages
    return pages[0] if pages else context.new_page()


def login(timeout_seconds: int = 180) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        context = _launch_persistent_context(playwright, headless=False)
        imported_backup = _restore_auth_backup(context)
        page = _context_page(context)
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
        print(
            "请在 DouKU 专用 Edge 窗口中登录抖音。"
            "检测到登录成功后，登录态会保存在专用用户目录。"
        )
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            cookies = context.cookies()
            state = {"cookies": cookies, "origins": []}
            if has_session(state):
                storage_state = context.storage_state()
                sec_uid = extract_cookie(storage_state, "sec_user_id")
                path = save_auth(storage_state, sec_uid)
                context.close()
                return {
                    "success": True,
                    "mode": "persistent_edge_profile",
                    "profile_dir": str(get_browser_profile_dir()),
                    "state_backup": str(path),
                    "imported_previous_state": imported_backup,
                }
            page.wait_for_timeout(1_000)
        context.close()
    raise RuntimeError("等待登录超时，请重新运行 login")


def _list_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    return (
        data.get("aweme_list")
        or data.get("aweme_list_data")
        or data.get("item_list")
        or []
    )


def _collect_tabs(
    page: Any, conn: Any, wanted: set[str], max_pages: int
) -> tuple[dict[str, dict[str, int]], str]:
    # 个人页经常预加载相邻标签；全部接收可减少重复打开浏览器触发风控。
    results = {
        source: {"pages": 0, "saved": 0}
        for source in {"favorites", "likes"}
    }
    errors: list[str] = []
    account_sec_uid = ""

    def handle_response(response: Any) -> None:
        nonlocal account_sec_uid
        path = response.request.url.split("?", 1)[0]
        source = next(
            (value for suffix, value in RESPONSE_SOURCES.items() if path.endswith(suffix)),
            None,
        )
        if source is None or results[source]["pages"] >= max_pages:
            return
        try:
            data = response.json()
            if response.status != 200 or data.get("status_code") not in (None, 0):
                errors.append(f"{source}: HTTP {response.status}")
                return
            account_sec_uid = str(
                data.get("sec_uid") or data.get("sec_user_id") or account_sec_uid
            )
            items = _list_items(data)
            page_offset = results[source]["pages"] * len(items)
            for item_position, item in enumerate(items, 1):
                results[source]["saved"] += int(
                    upsert_aweme(
                        conn,
                        item,
                        source,
                        position=page_offset + item_position,
                    )
                )
            results[source]["pages"] += 1
            cursor = data.get("cursor") or data.get("max_cursor") or "0"
            set_bookmark(conn, source, str(cursor), len(items))
            conn.commit()
        except Exception as exc:
            errors.append(f"{source}: {exc}")

    page.on("response", handle_response)
    page.goto(TAB_URL, wait_until="domcontentloaded", timeout=60_000)
    # 页面可能预加载相邻标签；滚动负责触发后续分页及懒加载。
    page.wait_for_timeout(10_000)
    if "likes" in wanted and results["likes"]["pages"] == 0:
        if account_sec_uid:
            page.goto(
                f"https://www.douyin.com/user/{account_sec_uid}?showTab=like",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            page.wait_for_timeout(10_000)
        else:
            likes_tab = page.get_by_text("喜欢", exact=True)
            if likes_tab.count() == 1:
                likes_tab.click()
                page.wait_for_timeout(6_000)
    for _ in range(max_pages + 2):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(3_000)
        if all(value["pages"] >= max_pages for value in results.values()):
            break
    page.remove_listener("response", handle_response)
    missing = [
        source for source in wanted if results[source]["pages"] == 0
    ]
    if missing:
        detail = "; ".join(errors[-3:]) if errors else "页面没有产生对应响应"
        for source in missing:
            results[source]["error"] = detail
    selected = {source: results[source] for source in wanted | {
        source for source, value in results.items() if value["pages"] > 0
    }}
    return selected, account_sec_uid


def _candidate_ids(conn: Any, limit: int, missing_url: bool = False) -> list[str]:
    query = """
        SELECT vb.aweme_id
        FROM videos_base vb
        LEFT JOIN video_urls vu ON vu.aweme_id=vb.aweme_id
    """
    if missing_url:
        query += " WHERE COALESCE(vu.video_url,'')='' "
    query += " ORDER BY vb.updated_at DESC LIMIT ?"
    return [row["aweme_id"] for row in conn.execute(query, (limit,))]


def _fetch_video_pages(
    page: Any, conn: Any, limit: int, want_details: bool, want_comments: bool
) -> dict[str, dict[str, int]]:
    results = {
        "details": {"saved": 0, "failed": 0},
        "comments": {"saved": 0, "failed_videos": 0},
    }
    ids = _candidate_ids(conn, limit, missing_url=want_details and not want_comments)
    for aweme_id in ids:
        seen_detail = False
        seen_comments = False

        def handle_response(response: Any) -> None:
            nonlocal seen_detail, seen_comments
            path = response.request.url.split("?", 1)[0]
            try:
                if want_details and path.endswith("/aweme/v1/web/aweme/detail/"):
                    data = response.json()
                    item = data.get("aweme_detail") or data.get("aweme") or {}
                    if item:
                        results["details"]["saved"] += int(
                            upsert_aweme(conn, item, "details")
                        )
                        seen_detail = True
                if want_comments and path.endswith("/aweme/v1/web/comment/list/"):
                    data = response.json()
                    for comment in data.get("comments") or []:
                        results["comments"]["saved"] += int(
                            upsert_comment(conn, aweme_id, comment)
                        )
                    seen_comments = True
                conn.commit()
            except Exception:
                return

        page.on("response", handle_response)
        try:
            page.goto(
                f"https://www.douyin.com/video/{aweme_id}",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            page.wait_for_timeout(3_000)
        finally:
            page.remove_listener("response", handle_response)
        if want_details and not seen_detail:
            results["details"]["failed"] += 1
        if want_comments and not seen_comments:
            results["comments"]["failed_videos"] += 1
    return results


def collect(
    source: str,
    max_pages: int = 3,
    limit: int = 20,
    headless: bool = False,
) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    tasks = (
        ["favorites", "likes", "details", "comments"]
        if source == "all"
        else [source]
    )
    results: dict[str, Any] = {}
    with sync_playwright() as playwright:
        context = _launch_persistent_context(playwright, headless=headless)
        _restore_auth_backup(context)
        active_state = context.storage_state()
        if not has_session(active_state):
            context.close()
            raise RuntimeError(
                "DouKU 专用 Edge 目录中没有可用登录态，请先运行: "
                "python douku.py login"
            )
        auth = load_auth()
        sec_user_id = str(
            extract_cookie(active_state, "sec_user_id")
            or auth.get("sec_user_id")
            or extract_cookie(auth, "sec_user_id")
        )
        page = _context_page(context)
        with get_conn() as conn:
            init_db(conn)
            list_tasks = {task for task in tasks if task in {"favorites", "likes"}}
            if list_tasks:
                tab_results, discovered_sec_uid = _collect_tabs(
                    page, conn, list_tasks, max_pages
                )
                results.update(tab_results)
                if discovered_sec_uid:
                    sec_user_id = discovered_sec_uid
            page_tasks = {task for task in tasks if task in {"details", "comments"}}
            if page_tasks:
                page_results = _fetch_video_pages(
                    page,
                    conn,
                    limit,
                    want_details="details" in page_tasks,
                    want_comments="comments" in page_tasks,
                )
                results.update({task: page_results[task] for task in page_tasks})
        # 登录过程中站点可能刷新 Cookie，顺便持久化。
        save_auth(context.storage_state(), sec_user_id)
        context.close()
    missing = [
        task
        for task in tasks
        if task in {"favorites", "likes"}
        and results.get(task, {}).get("pages", 0) == 0
    ]
    if missing:
        detail = "; ".join(
            str(results[task].get("error") or "") for task in missing
        )
        raise RuntimeError(
            f"未采集到 {','.join(missing)}：{detail or '页面没有产生对应响应'}。"
            "若页面空白或出现验证，请稍后重试或重新运行 login。"
        )
    return results
