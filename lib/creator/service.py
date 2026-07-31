"""Archive and incrementally synchronize public creator works."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from lib.collector import (
    _context_page,
    _launch_persistent_context,
    _restore_auth_backup,
)
from lib.db.db_v4 import DL_DONE, DL_FAILED, get_conn, init_db, now
from lib.download.download_videos import (
    _download_url,
    _extension,
    _has_expected_media_header,
    _music_extension,
    _new_session,
    media_stem,
)
from lib.link.resolver import detect_platform, validate_url
from lib.utils.meta import get_creator_downloads_dir

DOUYIN_POST_ENDPOINT = "/aweme/v1/web/aweme/post/"
DOUYIN_CREATOR_SEARCH_PLACEHOLDER = "搜索 Ta 的作品"


def _douyin_creator_id(url: str) -> str:
    path = urlparse(url).path
    if "/user/" not in path:
        raise ValueError("请提供抖音创作者主页链接，而不是单个作品链接")
    value = path.split("/user/", 1)[1].split("/", 1)[0]
    if not value or value == "self":
        raise ValueError("创作者主页必须包含明确的 sec_user_id")
    return value


def _bilibili_creator_id(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    parts = [part for part in urlparse(url).path.split("/") if part]
    if host != "space.bilibili.com" or not parts or not parts[0].isdigit():
        raise ValueError("请提供形如 https://space.bilibili.com/数字ID 的B站空间链接")
    return parts[0]


def _ensure_creator(
    conn: Any,
    platform: str,
    creator_key: str,
    profile_url: str,
    nickname: str = "",
) -> int:
    timestamp = now()
    conn.execute(
        """
        INSERT INTO creator_profiles
          (platform,platform_creator_id,nickname,profile_url,
           created_at,updated_at)
        VALUES (?,?,?,?,?,?)
        ON DUPLICATE KEY UPDATE
          nickname=IF(VALUES(nickname)!='',VALUES(nickname),nickname),
          profile_url=VALUES(profile_url),updated_at=VALUES(updated_at)
        """,
        (platform, creator_key, nickname, profile_url, timestamp, timestamp),
    )
    return int(
        conn.execute(
            """
            SELECT id FROM creator_profiles
            WHERE platform=? AND platform_creator_id=?
            """,
            (platform, creator_key),
        ).fetchone()["id"]
    )


def _next_creator_code(conn: Any, creator_id: int) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(MAX(local_code),0)+1 AS next_code
        FROM creator_works WHERE creator_id=?
        """,
        (creator_id,),
    ).fetchone()
    code = int(row["next_code"])
    if code > 99999:
        raise RuntimeError("该创作者本地作品编号已经超过五位数")
    return code


def _store_douyin_work(
    conn: Any,
    creator_id: int,
    item: dict[str, Any],
) -> bool:
    work_id = str(item.get("aweme_id") or "")
    if not work_id:
        return False
    existing = conn.execute(
        """
        SELECT local_code FROM creator_works
        WHERE creator_id=? AND platform_work_id=?
        """,
        (creator_id, work_id),
    ).fetchone()
    local_code = (
        int(existing["local_code"])
        if existing
        else _next_creator_code(conn, creator_id)
    )
    author = item.get("author") or {}
    nickname = str(author.get("nickname") or "")
    if nickname:
        conn.execute(
            """
            UPDATE creator_profiles SET nickname=?,updated_at=? WHERE id=?
            """,
            (nickname, now(), creator_id),
        )
    content_type = "image" if item.get("images") else "video"
    statistics = item.get("statistics") or {}
    video = item.get("video") or {}
    duration_seconds = max(
        0, int((video.get("duration") or item.get("duration") or 0) / 1000)
    )
    is_pinned = bool(
        item.get("is_top")
        or item.get("is_pinned")
        or item.get("is_sticky")
    )
    created = int(item.get("create_time") or 0)
    published_at = (
        datetime.fromtimestamp(created) if created > 0 else None
    )
    timestamp = now()
    conn.execute(
        """
        INSERT INTO creator_works
          (creator_id,platform_work_id,local_code,title,published_at,
           content_type,like_count,play_count,comment_count,share_count,
           favorite_count,duration_seconds,is_pinned,webpage_url,
           metadata_json,first_seen_at,last_seen_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON DUPLICATE KEY UPDATE title=VALUES(title),
          published_at=VALUES(published_at),
          content_type=VALUES(content_type),
          like_count=VALUES(like_count),play_count=VALUES(play_count),
          comment_count=VALUES(comment_count),share_count=VALUES(share_count),
          favorite_count=VALUES(favorite_count),
          duration_seconds=VALUES(duration_seconds),
          is_pinned=VALUES(is_pinned),
          webpage_url=VALUES(webpage_url),
          metadata_json=VALUES(metadata_json),
          last_seen_at=VALUES(last_seen_at),is_removed=0
        """,
        (
            creator_id,
            work_id,
            local_code,
            str(item.get("desc") or "")[:512],
            published_at,
            content_type,
            int(statistics.get("digg_count") or 0),
            int(statistics.get("play_count") or 0),
            int(statistics.get("comment_count") or 0),
            int(statistics.get("share_count") or 0),
            int(statistics.get("collect_count") or 0),
            duration_seconds,
            is_pinned,
            str(item.get("share_url") or f"https://www.douyin.com/video/{work_id}"),
            json.dumps(item, ensure_ascii=False),
            timestamp,
            timestamp,
        ),
    )
    urls: list[tuple[str, int, str]] = []
    video_urls = (video.get("play_addr") or {}).get("url_list") or []
    cover_urls = (
        video.get("cover") or video.get("origin_cover") or {}
    ).get("url_list") or []
    if content_type == "video" and video_urls:
        urls.append(("video", 0, video_urls[0]))
    if cover_urls:
        urls.append(("cover", 0, cover_urls[0]))
    for position, image in enumerate(item.get("images") or [], 1):
        image_urls = (
            image.get("url_list")
            or (image.get("display_image") or {}).get("url_list")
            or []
        )
        if image_urls:
            urls.append(("image", position, image_urls[0]))
    music_urls = (
        ((item.get("music") or {}).get("play_url") or {}).get("url_list")
        or []
    )
    if content_type == "image" and music_urls:
        urls.append(("music", 0, music_urls[0]))
    for media_type, position, remote_url in urls:
        conn.execute(
            """
            INSERT INTO creator_media_urls
              (creator_id,platform_work_id,media_type,position_no,
               remote_url,refreshed_at)
            VALUES (?,?,?,?,?,?)
            ON DUPLICATE KEY UPDATE remote_url=VALUES(remote_url),
              refreshed_at=VALUES(refreshed_at)
            """,
            (
                creator_id,
                work_id,
                media_type,
                position,
                remote_url,
                timestamp,
            ),
        )
    conn.execute(
        """
        INSERT INTO creator_download_tasks
          (creator_id,platform_work_id,status,updated_at)
        VALUES (?,?,0,?)
        ON DUPLICATE KEY UPDATE platform_work_id=VALUES(platform_work_id)
        """,
        (creator_id, work_id, timestamp),
    )
    return existing is None


def _store_flat_work(
    conn: Any,
    creator_id: int,
    entry: dict[str, Any],
    platform: str,
) -> bool:
    work_id = str(entry.get("id") or "")
    if not work_id:
        return False
    existing = conn.execute(
        """
        SELECT local_code FROM creator_works
        WHERE creator_id=? AND platform_work_id=?
        """,
        (creator_id, work_id),
    ).fetchone()
    local_code = (
        int(existing["local_code"])
        if existing
        else _next_creator_code(conn, creator_id)
    )
    timestamp_value = entry.get("timestamp") or entry.get("release_timestamp")
    published_at = (
        datetime.fromtimestamp(int(timestamp_value))
        if timestamp_value
        else None
    )
    webpage_url = str(entry.get("webpage_url") or entry.get("url") or "")
    if platform == "bilibili" and not webpage_url.startswith("http"):
        webpage_url = f"https://www.bilibili.com/video/{work_id}"
    timestamp = now()
    duration = entry.get("duration") or 0
    duration_seconds = max(0, int(float(duration))) if duration else 0
    conn.execute(
        """
        INSERT INTO creator_works
          (creator_id,platform_work_id,local_code,title,published_at,
           content_type,like_count,play_count,comment_count,share_count,
           favorite_count,duration_seconds,is_pinned,webpage_url,
           metadata_json,first_seen_at,last_seen_at)
        VALUES (?,?,?,?,?,'video',?,?,?,?,?,?,?,?,?,?,?)
        ON DUPLICATE KEY UPDATE title=VALUES(title),
          published_at=COALESCE(VALUES(published_at),published_at),
          like_count=VALUES(like_count),play_count=VALUES(play_count),
          comment_count=VALUES(comment_count),share_count=VALUES(share_count),
          favorite_count=VALUES(favorite_count),
          duration_seconds=VALUES(duration_seconds),
          is_pinned=VALUES(is_pinned),
          webpage_url=VALUES(webpage_url),
          metadata_json=VALUES(metadata_json),last_seen_at=VALUES(last_seen_at),
          is_removed=0
        """,
        (
            creator_id,
            work_id,
            local_code,
            str(entry.get("title") or "")[:512],
            published_at,
            int(entry.get("like_count") or 0),
            int(entry.get("view_count") or entry.get("play_count") or 0),
            int(entry.get("comment_count") or 0),
            int(entry.get("repost_count") or entry.get("share_count") or 0),
            int(entry.get("favorite_count") or 0),
            duration_seconds,
            bool(entry.get("is_pinned") or entry.get("is_top")),
            webpage_url,
            json.dumps(entry, ensure_ascii=False),
            timestamp,
            timestamp,
        ),
    )
    conn.execute(
        """
        INSERT INTO creator_download_tasks
          (creator_id,platform_work_id,status,updated_at)
        VALUES (?,?,0,?)
        ON DUPLICATE KEY UPDATE platform_work_id=VALUES(platform_work_id)
        """,
        (creator_id, work_id, timestamp),
    )
    return existing is None


def _fetch_bilibili_creator(
    profile_url: str,
    max_pages: int,
    latest: int,
    after: datetime | None,
    mode: str,
) -> dict[str, Any]:
    from yt_dlp import YoutubeDL

    creator_key = _bilibili_creator_id(profile_url)
    normalized_url = f"https://space.bilibili.com/{creator_key}/video"
    with get_conn() as conn:
        init_db(conn)
        creator_id = _ensure_creator(
            conn, "bilibili", creator_key, normalized_url
        )
        started = now()
        cursor = conn.execute(
            """
            INSERT INTO creator_sync_jobs
              (creator_id,mode,status,started_at)
            VALUES (?,?,0,?)
            """,
            (creator_id, mode, started),
        )
        job_id = int(cursor.lastrowid)
        conn.commit()
        try:
            page_cap = 0 if max_pages == 0 else max_pages * 30
            entry_cap = latest or page_cap
            options: dict[str, Any] = {
                "quiet": True,
                "no_warnings": True,
                "extract_flat": True,
                "skip_download": True,
            }
            if entry_cap:
                options["playlistend"] = entry_cap
            try:
                with YoutubeDL(options) as ydl:
                    info = ydl.extract_info(normalized_url, download=False)
                    clean = YoutubeDL.sanitize_info(info)
            except Exception:
                clean = _fetch_bilibili_space_in_browser(
                    normalized_url,
                    page_limit=(
                        max_pages if max_pages > 0 else 500
                    ),
                    latest=latest,
                )
            nickname = str(
                clean.get("uploader")
                or clean.get("channel")
                or clean.get("title")
                or f"B站用户_{creator_key}"
            ).removesuffix("的视频")
            conn.execute(
                """
                UPDATE creator_profiles SET nickname=?,profile_url=?,
                  last_sync_at=?,updated_at=? WHERE id=?
                """,
                (nickname, normalized_url, now(), now(), creator_id),
            )
            seen = new_works = consecutive_known = 0
            for entry in clean.get("entries") or []:
                timestamp_value = entry.get("timestamp") or entry.get(
                    "release_timestamp"
                )
                published = (
                    datetime.fromtimestamp(int(timestamp_value))
                    if timestamp_value
                    else None
                )
                if after and published and published < after:
                    break
                is_new = _store_flat_work(
                    conn, creator_id, entry, "bilibili"
                )
                seen += 1
                new_works += int(is_new)
                consecutive_known = 0 if is_new else consecutive_known + 1
                if mode == "sync" and consecutive_known >= 30:
                    break
                if latest and seen >= latest:
                    break
            conn.execute(
                """
                UPDATE creator_sync_jobs SET status=2,pages_fetched=?,
                  works_seen=?,new_works=?,finished_at=? WHERE id=?
                """,
                ((seen + 29) // 30, seen, new_works, now(), job_id),
            )
            conn.commit()
            return {
                "success": True,
                "creator_id": creator_id,
                "nickname": nickname,
                "platform": "bilibili",
                "pages": (seen + 29) // 30,
                "works_seen": seen,
                "new_works": new_works,
                "job_id": job_id,
            }
        except Exception as exc:
            conn.execute(
                """
                UPDATE creator_sync_jobs SET status=3,error_message=?,
                  finished_at=? WHERE id=?
                """,
                (str(exc)[:1000], now(), job_id),
            )
            conn.commit()
            raise


def _fetch_bilibili_space_in_browser(
    normalized_url: str,
    page_limit: int,
    latest: int,
) -> dict[str, Any]:
    """Let the real page produce WBI-signed space-list requests."""
    from playwright.sync_api import sync_playwright

    entries: list[dict[str, Any]] = []
    nickname = ""
    pages = 0
    total_pages: int | None = None

    def handle_response(response: Any) -> None:
        nonlocal nickname, pages, total_pages
        path = response.url.split("?", 1)[0]
        if not path.endswith("/x/space/wbi/arc/search"):
            return
        try:
            payload = response.json()
            if int(payload.get("code") or 0) != 0:
                return
            data = payload.get("data") or {}
            page_data = data.get("page") or {}
            count = int(page_data.get("count") or 0)
            page_size = int(page_data.get("ps") or 30)
            total_pages = (
                (count + page_size - 1) // page_size if page_size else 1
            )
            works = ((data.get("list") or {}).get("vlist") or [])
            if works:
                pages += 1
            for item in works:
                bvid = str(item.get("bvid") or "")
                if not bvid or any(entry["id"] == bvid for entry in entries):
                    continue
                nickname = nickname or str(item.get("author") or "")
                entries.append(
                    {
                        "id": bvid,
                        "title": item.get("title") or "",
                        "timestamp": item.get("created"),
                        "webpage_url": f"https://www.bilibili.com/video/{bvid}",
                    }
                )
        except Exception:
            return

    def collect_dom(page: Any) -> int:
        nonlocal nickname
        before = len(entries)
        title = page.title().strip()
        if title and title not in {"哔哩哔哩", "bilibili"}:
            nickname = nickname or title.split("的个人空间", 1)[0].strip()
        anchors = page.locator('a[href*="/video/BV"]')
        for index in range(min(anchors.count(), 200)):
            anchor = anchors.nth(index)
            href = str(anchor.get_attribute("href") or "")
            marker = "/video/"
            if marker not in href:
                continue
            bvid = href.split(marker, 1)[1].split("?", 1)[0].split("/", 1)[0]
            if not bvid.startswith("BV") or any(
                entry["id"] == bvid for entry in entries
            ):
                continue
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = "https://www.bilibili.com" + href
            title_text = (
                anchor.get_attribute("title")
                or anchor.inner_text()
                or bvid
            )
            entries.append(
                {
                    "id": bvid,
                    "title": title_text.strip(),
                    "webpage_url": href,
                }
            )
        return len(entries) - before

    with sync_playwright() as playwright:
        context = _launch_persistent_context(playwright, headless=False)
        page = _context_page(context)
        page.on("response", handle_response)
        page.goto(
            normalized_url,
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        page.wait_for_timeout(8_000)
        if not entries:
            if collect_dom(page):
                pages = max(pages, 1)
        idle = 0
        while pages < page_limit:
            if latest and len(entries) >= latest:
                break
            if total_pages is not None and pages >= total_pages:
                break
            previous = pages
            candidates = (
                page.locator(".be-pager-next").first,
                page.locator(
                    'button:has-text("下一页"),'
                    '.vui_pagenation--btn-side:has-text("下一页")'
                ).first,
                page.get_by_text("下一页", exact=True).first,
            )
            clicked = False
            for candidate in candidates:
                if candidate.count() and candidate.is_visible():
                    candidate.click()
                    clicked = True
                    break
            if not clicked:
                page.mouse.wheel(0, 5000)
            page.wait_for_timeout(3_000)
            if collect_dom(page):
                pages = max(pages, previous + 1)
            idle = idle + 1 if pages == previous else 0
            if idle >= 5:
                break
        page.remove_listener("response", handle_response)
        context.close()
    if not entries:
        raise RuntimeError("B站空间页面没有返回公开投稿列表")
    return {
        "title": nickname or "B站创作者",
        "uploader": nickname,
        "entries": entries[:latest] if latest else entries,
    }


def fetch_creator(
    profile_url: str,
    max_pages: int = 0,
    latest: int = 0,
    after: datetime | None = None,
    mode: str = "fetch",
    headless: bool = False,
) -> dict[str, Any]:
    profile_url = validate_url(profile_url)
    platform = detect_platform(profile_url)
    if platform == "bilibili":
        return _fetch_bilibili_creator(
            profile_url, max_pages, latest, after, mode
        )
    if platform != "douyin":
        raise NotImplementedError("当前支持抖音和B站创作者主页")
    creator_key = _douyin_creator_id(profile_url)
    page_limit = 500 if max_pages == 0 else max(1, max_pages)
    with get_conn() as conn:
        init_db(conn)
        creator_id = _ensure_creator(
            conn, platform, creator_key, profile_url
        )
        cursor = conn.execute(
            """
            INSERT INTO creator_sync_jobs
              (creator_id,mode,status,started_at)
            VALUES (?,?,0,?)
            """,
            (creator_id, mode, now()),
        )
        job_id = int(cursor.lastrowid)
        conn.commit()
        pages = seen = new_works = consecutive_known = 0
        has_more: bool | None = None
        oldest_seen: datetime | None = None

        def handle_response(response: Any) -> None:
            nonlocal pages, seen, new_works, consecutive_known
            nonlocal has_more, oldest_seen
            path = response.url.split("?", 1)[0]
            if not path.endswith(DOUYIN_POST_ENDPOINT) or pages >= page_limit:
                return
            try:
                data = response.json()
                if data.get("status_code") not in (None, 0):
                    return
                items = data.get("aweme_list") or []
                if not items:
                    has_more = bool(data.get("has_more"))
                    return
                pages += 1
                has_more = bool(data.get("has_more"))
                for item in items:
                    created = int(item.get("create_time") or 0)
                    published = (
                        datetime.fromtimestamp(created) if created else None
                    )
                    if published and (
                        oldest_seen is None or published < oldest_seen
                    ):
                        oldest_seen = published
                    is_new = _store_douyin_work(conn, creator_id, item)
                    seen += 1
                    new_works += int(is_new)
                    consecutive_known = 0 if is_new else consecutive_known + 1
                conn.commit()
            except Exception:
                return

        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                context = _launch_persistent_context(
                    playwright, headless=headless
                )
                _restore_auth_backup(context)
                page = _context_page(context)
                page.on("response", handle_response)
                page.goto(
                    profile_url,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                page.wait_for_timeout(8_000)
                idle_rounds = 0
                while pages < page_limit and has_more is not False:
                    if latest and seen >= latest:
                        break
                    if after and oldest_seen and oldest_seen < after:
                        break
                    if mode == "sync" and consecutive_known >= 30:
                        break
                    previous = pages
                    page.mouse.wheel(0, 5000)
                    page.wait_for_timeout(2_500)
                    idle_rounds = (
                        idle_rounds + 1 if pages == previous else 0
                    )
                    if idle_rounds >= 6:
                        break
                page.remove_listener("response", handle_response)
                context.close()
            conn.execute(
                """
                UPDATE creator_profiles SET last_sync_at=?,updated_at=?
                WHERE id=?
                """,
                (now(), now(), creator_id),
            )
            conn.execute(
                """
                UPDATE creator_sync_jobs SET status=2,pages_fetched=?,
                  works_seen=?,new_works=?,finished_at=? WHERE id=?
                """,
                (pages, seen, new_works, now(), job_id),
            )
            conn.commit()
            profile = conn.execute(
                "SELECT nickname FROM creator_profiles WHERE id=?",
                (creator_id,),
            ).fetchone()
            return {
                "success": True,
                "creator_id": creator_id,
                "nickname": profile["nickname"],
                "platform": platform,
                "pages": pages,
                "works_seen": seen,
                "new_works": new_works,
                "has_more": has_more,
                "job_id": job_id,
            }
        except Exception as exc:
            conn.execute(
                """
                UPDATE creator_sync_jobs SET status=3,error_message=?,
                  finished_at=? WHERE id=?
                """,
                (str(exc)[:1000], now(), job_id),
            )
            conn.commit()
            raise


def search_creator_works(
    profile_url: str,
    keyword: str,
    max_pages: int = 3,
    headless: bool = False,
) -> dict[str, Any]:
    """Use Douyin's own creator-page search and return canonical works."""
    creator_key = _douyin_creator_id(profile_url)
    keyword = keyword.strip()
    if not keyword:
        raise ValueError("搜索关键词不能为空")
    captured: dict[str, dict[str, Any]] = {}

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            work_id = str(value.get("aweme_id") or "")
            if work_id and (
                value.get("desc") is not None
                or value.get("video")
                or value.get("images")
            ):
                captured[work_id] = value
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    def handle_response(response: Any) -> None:
        if "douyin.com" not in response.url:
            return
        if "json" not in response.headers.get("content-type", ""):
            return
        try:
            walk(response.json())
        except Exception:
            return

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        context = _launch_persistent_context(playwright, headless=headless)
        _restore_auth_backup(context)
        page = _context_page(context)
        page.on("response", handle_response)
        page.goto(
            f"https://www.douyin.com/user/{creator_key}",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        page.wait_for_timeout(6_000)
        search = page.locator(
            f'input[placeholder="{DOUYIN_CREATOR_SEARCH_PLACEHOLDER}"]'
        ).first
        if search.count() != 1:
            context.close()
            raise RuntimeError("没有找到“搜索 Ta 的作品”输入框")
        search.fill(keyword)
        search.press("Enter")
        search.evaluate("element => element.blur()")
        page.wait_for_timeout(4_000)
        for _ in range(max(1, max_pages)):
            page.mouse.wheel(0, 5000)
            page.wait_for_timeout(2_000)
        dom_results = page.locator('a[href*="/video/"]').evaluate_all(
            """links => links.slice(0, 200).map(a => ({
                url: a.href,
                title: (a.innerText || a.getAttribute('aria-label') || '').trim()
            }))"""
        )
        page.remove_listener("response", handle_response)
        context.close()

    with get_conn() as conn:
        init_db(conn)
        creator_id = _ensure_creator(
            conn, "douyin", creator_key, profile_url
        )
        for item in captured.values():
            author_key = str((item.get("author") or {}).get("sec_uid") or "")
            title = str(item.get("desc") or "")
            if (
                (author_key and author_key != creator_key)
                or keyword.lower() not in title.lower()
            ):
                continue
            _store_douyin_work(conn, creator_id, item)
        conn.commit()
        profile = conn.execute(
            "SELECT nickname FROM creator_profiles WHERE id=?",
            (creator_id,),
        ).fetchone()

    results: dict[str, dict[str, Any]] = {}
    for item in captured.values():
        author_key = str((item.get("author") or {}).get("sec_uid") or "")
        if author_key and author_key != creator_key:
            continue
        work_id = str(item.get("aweme_id") or "")
        title = str(item.get("desc") or "")
        if work_id and keyword.lower() in title.lower():
            results[work_id] = {
                "platform_work_id": work_id,
                "title": title,
                "url": str(
                    item.get("share_url")
                    or f"https://www.douyin.com/video/{work_id}"
                ),
            }
    for item in dom_results:
        url = str(item.get("url") or "")
        title = str(item.get("title") or "")
        work_id = url.split("/video/", 1)[1].split("?", 1)[0] if "/video/" in url else ""
        if work_id and (not title or keyword.lower() in title.lower()):
            results.setdefault(
                work_id,
                {
                    "platform_work_id": work_id,
                    "title": title,
                    "url": url,
                },
            )
    return {
        "success": True,
        "creator_id": creator_id,
        "nickname": str((profile or {}).get("nickname") or ""),
        "keyword": keyword,
        "results": list(results.values()),
        "count": len(results),
    }


def _creator_profile(conn: Any, creator: str) -> dict[str, Any]:
    if creator.isdigit():
        row = conn.execute(
            "SELECT * FROM creator_profiles WHERE id=?",
            (int(creator),),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT * FROM creator_profiles
            WHERE nickname=? OR profile_url=? ORDER BY updated_at DESC LIMIT 1
            """,
            (creator, creator),
        ).fetchone()
    if not row:
        raise RuntimeError(f"没有找到创作者档案: {creator}")
    return dict(row)


def download_creator(
    creator: str,
    latest: int = 0,
    after: datetime | None = None,
    before: datetime | None = None,
    content_type: str = "all",
    sort_by: str = "published",
    order: str = "desc",
    exclude_pinned: bool = False,
    limit: int = 0,
    dry_run: bool = False,
    work_ids: list[str] | None = None,
    retry_failed: bool = False,
    retries: int = 2,
) -> dict[str, Any]:
    sort_columns = {
        "published": "published_at",
        "likes": "like_count",
        "views": "play_count",
        "comments": "comment_count",
        "shares": "share_count",
        "favorites": "favorite_count",
        "duration": "duration_seconds",
    }
    if content_type not in {"all", "video", "image"}:
        raise ValueError("content_type 必须是 all、video 或 image")
    if sort_by not in sort_columns:
        raise ValueError(f"不支持的排序字段: {sort_by}")
    if order not in {"asc", "desc"}:
        raise ValueError("order 必须是 asc 或 desc")
    effective_limit = max(0, limit or latest)
    with get_conn() as conn:
        init_db(conn)
        profile = _creator_profile(conn, creator)
        statuses = [0, 3] if retry_failed else [0]
        placeholders = ",".join("?" for _ in statuses)
        selection_sql = """
          SELECT platform_work_id FROM creator_works
          WHERE creator_id=?
        """
        selection_params: list[Any] = [profile["id"]]
        cleaned_work_ids = [
            value.strip()
            for value in (work_ids or [])
            if value and value.strip()
        ]
        if cleaned_work_ids:
            work_placeholders = ",".join("?" for _ in cleaned_work_ids)
            selection_sql += (
                f" AND platform_work_id IN ({work_placeholders})"
            )
            selection_params.extend(cleaned_work_ids)
        if after:
            selection_sql += " AND published_at>=?"
            selection_params.append(after)
        if before:
            selection_sql += " AND published_at<?"
            selection_params.append(before)
        if content_type != "all":
            selection_sql += " AND content_type=?"
            selection_params.append(content_type)
        if exclude_pinned:
            selection_sql += " AND is_pinned=FALSE"
        sort_column = sort_columns[sort_by]
        direction = order.upper()
        selection_sql += (
            f" ORDER BY {sort_column} {direction},"
            f"published_at {direction},platform_work_id {direction}"
        )
        if effective_limit:
            selection_sql += " LIMIT ?"
            selection_params.append(effective_limit)
        selected_ids = [
            row["platform_work_id"]
            for row in conn.execute(selection_sql, selection_params)
        ]
        if dry_run:
            if not selected_ids:
                selected_works = []
            else:
                preview_placeholders = ",".join("?" for _ in selected_ids)
                preview_sql = f"""
                    SELECT platform_work_id,local_code,title,published_at,
                           content_type,like_count,play_count,comment_count,
                           share_count,favorite_count,duration_seconds,
                           is_pinned,webpage_url
                    FROM creator_works
                    WHERE creator_id=?
                      AND platform_work_id IN ({preview_placeholders})
                    ORDER BY FIELD(platform_work_id,{preview_placeholders})
                """
                selected_works = [
                    dict(row)
                    for row in conn.execute(
                        preview_sql,
                        [profile["id"], *selected_ids, *selected_ids],
                    )
                ]
                for work in selected_works:
                    if work["published_at"]:
                        work["published_at"] = work[
                            "published_at"
                        ].isoformat()
                    work["is_pinned"] = bool(work["is_pinned"])
            return {
                "creator_id": profile["id"],
                "nickname": profile["nickname"],
                "selected": len(selected_works),
                "works": selected_works,
            }
        if not selected_ids:
            works = []
        else:
            id_placeholders = ",".join("?" for _ in selected_ids)
            sql = f"""
              SELECT cw.*,cdt.status
              FROM creator_works cw
              JOIN creator_download_tasks cdt
                ON cdt.creator_id=cw.creator_id
               AND cdt.platform_work_id=cw.platform_work_id
              WHERE cw.creator_id=? AND cdt.status IN ({placeholders})
                AND cw.platform_work_id IN ({id_placeholders})
              ORDER BY FIELD(cw.platform_work_id,{id_placeholders})
            """
            params: list[Any] = [
                profile["id"],
                *statuses,
                *selected_ids,
                *selected_ids,
            ]
            works = [dict(row) for row in conn.execute(sql, params)]
        root = get_creator_downloads_dir(
            profile["platform"], profile["nickname"] or str(profile["id"])
        )
        session = _new_session()
        success = failed = 0
        for work in works:
            stem = media_stem(
                int(work["local_code"]),
                profile["nickname"],
                work["title"],
            )
            if profile["platform"] == "bilibili":
                from yt_dlp import YoutubeDL

                from lib.link.resolver import _yt_option_scope

                video_dir = root / "videos"
                video_dir.mkdir(parents=True, exist_ok=True)
                existing = [
                    path
                    for path in video_dir.glob(f"{stem}.*")
                    if path.is_file()
                    and path.suffix.lower() in {".mp4", ".mov", ".m4v"}
                    and _has_expected_media_header(path, "video")
                ]
                try:
                    if existing:
                        output_path = existing[0]
                    else:
                        template = str(video_dir / f"{stem}.%(ext)s")
                        with _yt_option_scope(
                            "bilibili", True, template
                        ) as options:
                            with YoutubeDL(options) as ydl:
                                ydl.extract_info(
                                    work["webpage_url"], download=True
                                )
                        outputs = [
                            path
                            for path in video_dir.glob(f"{stem}.*")
                            if path.is_file()
                            and path.suffix not in {".part", ".ytdl"}
                        ]
                        output_path = next(
                            (
                                path
                                for path in outputs
                                if path.suffix.lower()
                                in {".mp4", ".mov", ".m4v"}
                                and _has_expected_media_header(path, "video")
                            ),
                            None,
                        )
                        if output_path is None:
                            raise RuntimeError("B站下载后没有生成有效视频文件")
                    conn.execute(
                        """
                        UPDATE creator_download_tasks SET status=2,
                          local_path=?,download_error='',downloaded_at=?,
                          updated_at=?
                        WHERE creator_id=? AND platform_work_id=?
                        """,
                        (
                            str(output_path),
                            now(),
                            now(),
                            profile["id"],
                            work["platform_work_id"],
                        ),
                    )
                    success += 1
                except Exception as exc:
                    conn.execute(
                        """
                        UPDATE creator_download_tasks SET status=3,
                          download_error=?,retry_count=retry_count+1,
                          updated_at=?
                        WHERE creator_id=? AND platform_work_id=?
                        """,
                        (
                            str(exc)[:1000],
                            now(),
                            profile["id"],
                            work["platform_work_id"],
                        ),
                    )
                    failed += 1
                conn.commit()
                continue
            is_image = work["content_type"] == "image"
            video_dir = root / "videos"
            image_root = root / "image_posts"
            directories = {
                "video": video_dir,
                "cover": image_root / "covers" if is_image else root / "covers",
                "image": image_root / "images",
                "music": image_root / "music",
            }
            for directory in directories.values():
                directory.mkdir(parents=True, exist_ok=True)
            errors: list[str] = []
            main_path = ""
            media = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT cmu.*,COALESCE(cmf.local_path,'') AS local_path
                    FROM creator_media_urls cmu
                    LEFT JOIN creator_media_files cmf
                      ON cmf.media_url_id=cmu.id
                    WHERE cmu.creator_id=? AND cmu.platform_work_id=?
                    ORDER BY FIELD(cmu.media_type,'video','cover','image','music'),
                             cmu.position_no
                    """,
                    (profile["id"], work["platform_work_id"]),
                )
            ]
            for asset in media:
                kind = asset["media_type"]
                if kind == "video":
                    target = directories[kind] / f"{stem}.mp4"
                    expected = "video"
                elif kind == "cover":
                    target = directories[kind] / (
                        stem + _extension(asset["remote_url"], ".jpg")
                    )
                    expected = ""
                elif kind == "image":
                    target = directories[kind] / (
                        f"{stem}_{int(asset['position_no']):02d}"
                        + _extension(asset["remote_url"], ".jpg")
                    )
                    expected = ""
                else:
                    target = directories[kind] / (
                        stem + _music_extension(asset["remote_url"])
                    )
                    expected = "music"
                valid = target.exists() and target.stat().st_size > 256
                if expected and valid:
                    valid = _has_expected_media_header(target, expected)
                if valid:
                    ok, error = True, ""
                else:
                    ok, error = _download_url(
                        session,
                        asset["remote_url"],
                        target,
                        minimum_size=1024 if expected else 256,
                        expected_type=expected,
                        retries=retries,
                    )
                if ok and kind == "music":
                    header = target.read_bytes()[:8]
                    if target.suffix.lower() != ".m4a" and header[4:8] == b"ftyp":
                        renamed = target.with_suffix(".m4a")
                        if not renamed.exists():
                            target.replace(renamed)
                        target = renamed
                conn.execute(
                    """
                    INSERT INTO creator_media_files
                      (media_url_id,local_path,status,download_error,updated_at)
                    VALUES (?,?,?,?,?)
                    ON DUPLICATE KEY UPDATE local_path=VALUES(local_path),
                      status=VALUES(status),
                      download_error=VALUES(download_error),
                      updated_at=VALUES(updated_at)
                    """,
                    (
                        asset["id"],
                        str(target) if ok else "",
                        DL_DONE if ok else DL_FAILED,
                        error[:1000],
                        now(),
                    ),
                )
                if ok and kind == "video":
                    main_path = str(target)
                if not ok:
                    errors.append(f"{kind}[{asset['position_no']}]: {error}")
            status = DL_FAILED if errors else DL_DONE
            conn.execute(
                """
                UPDATE creator_download_tasks SET status=?,local_path=?,
                  download_error=?,retry_count=retry_count+?,
                  downloaded_at=?,updated_at=?
                WHERE creator_id=? AND platform_work_id=?
                """,
                (
                    status,
                    main_path,
                    "; ".join(errors)[:1000],
                    int(bool(errors)),
                    now() if status == DL_DONE else None,
                    now(),
                    profile["id"],
                    work["platform_work_id"],
                ),
            )
            conn.commit()
            success += int(status == DL_DONE)
            failed += int(status == DL_FAILED)
        return {
            "creator_id": profile["id"],
            "nickname": profile["nickname"],
            "total": len(works),
            "success": success,
            "failed": failed,
            "selected": len(selected_ids),
            "filters": {
                "content_type": content_type,
                "sort_by": sort_by,
                "order": order,
                "exclude_pinned": exclude_pinned,
                "limit": effective_limit,
                "after": after.isoformat() if after else None,
                "before": before.isoformat() if before else None,
            },
            "root": str(root),
        }
