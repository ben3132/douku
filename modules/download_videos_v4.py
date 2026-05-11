# -*- coding: utf-8 -*-
"""
download_videos_v4.py - 抖音视频下载器 (v4)
使用 v4 多表查询 + 5 态下载状态机

与 v3 的区别：
  - 查询改为 video_base/urls/meta/classification/authors_base JOIN
  - 下载状态使用 DL_PENDING/DL_DONE/DL_FAILED/DL_URL_EXPIRED/DL_IN_PROGRESS
  - URL 刷新写入 videos_urls 表
  - show_status 改为 v4 多表聚合
"""

import os
import sys
import json
import time
import random
import re
from datetime import datetime
from urllib.parse import urlencode
from pathlib import Path
from typing import Optional

import requests

from .db_v4 import (
    get_conn_v4, init_db_v4, get_download_queue, get_download_stats,
    set_download_status, upsert_video_urls,
    DL_PENDING, DL_DONE, DL_FAILED, DL_URL_EXPIRED, DL_IN_PROGRESS,
)
from . import signer as signer_module
from .meta import get_downloads_dir, get_data_root, load_cookie


# ============================================================
# 常量
# ============================================================

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Referer": "https://www.douyin.com/",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Connection": "keep-alive",
}

DOWNLOAD_DELAY_MIN = 1.0
DOWNLOAD_DELAY_MAX = 3.0
MAX_RETRIES = 3


# ============================================================
# 文件名处理
# ============================================================

def sanitize_filename(name: str, max_len: int = 50) -> str:
    """清理文件名"""
    if not name:
        return "untitled"
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f#]', '_', name)
    name = re.sub(r'[\U00010000-\U0010ffff]', '', name)
    name = name.replace('\n', ' ').replace('\r', '').replace('\t', ' ')
    name = re.sub(r'\s+', ' ', name).strip()
    name = name.strip(' .')
    if len(name) > max_len:
        name = name[:max_len]
    return name or "untitled"


def extract_primary_tag(video_tags_json: str) -> str:
    if not video_tags_json:
        return "未分类"
    try:
        tags = json.loads(video_tags_json)
        for t in tags:
            if t.get("level") == 1:
                return t.get("tag_name", "未分类")
        if tags:
            return tags[0].get("tag_name", "未分类")
    except (json.JSONDecodeError, TypeError):
        pass
    return "未分类"


# ============================================================
# URL 刷新
# ============================================================

def _build_refresh_params(aweme_id: str) -> dict:
    return {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "aweme_id": aweme_id,
        "publish_video_strategy_type": "2",
        "update_version_code": "170400",
        "version_name": "17.4.0",
        "cookie_enabled": "true",
        "screen_width": "1920",
        "screen_height": "1080",
        "browser_language": "zh-CN",
        "browser_platform": "Win32",
        "browser_name": "Chrome",
        "browser_version": "130.0.0.0",
        "browser_online": "true",
        "engine_name": "Blink",
        "engine_version": "130.0.0.0",
        "os_name": "Windows",
        "os_version": "10",
        "device_memory": "16",
        "platform": "PC",
        "downlink": "10",
        "effective_type": "4g",
        "round_trip_time": "200",
    }


def _get_refresh_cookies() -> dict:
    cookie = load_cookie()
    return {
        "sessionid": cookie.get("sessionid", ""),
        "sid_tt": cookie.get("sessionid", ""),
        "ttwid": cookie.get("ttwid", ""),
    }


def refresh_video_url(conn, aweme_id: str):
    """刷新视频下载 URL。返回 (success, url_or_error)"""
    if not signer_module.HAS_GMSSL:
        return False, "gmssl 未安装，无法刷新 URL"

    params = _build_refresh_params(aweme_id)

    try:
        signer = signer_module.Signer()
        a_bogus = signer.get_a_bogus(params)
        params["a_bogus"] = a_bogus
    except Exception as e:
        return False, f"签名失败: {e}"

    url = "https://www.douyin.com/aweme/v1/web/aweme/detail/?" + urlencode(params)

    try:
        resp = requests.get(
            url,
            headers=DEFAULT_HEADERS,
            cookies=_get_refresh_cookies(),
            timeout=20,
        )
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}"

        data = resp.json()
        if data.get("status_code") != 0:
            return False, f"API: {data.get('status_msg', '')}"

        aweme = data.get("aweme_detail")
        if not aweme:
            return False, "无详情"

        video_info = aweme.get("video") or {}
        new_url = ""

        for key in ["play_addr_265", "play_addr_h264"]:
            alt = video_info.get(key) or {}
            if alt.get("url_list"):
                new_url = alt["url_list"][0]
                break

        if not new_url:
            play_addr = video_info.get("play_addr") or {}
            if play_addr.get("url_list"):
                new_url = play_addr["url_list"][0]

        prevent = 1 if aweme.get("prevent_download", False) else 0
        if new_url:
            upsert_video_urls(conn, aweme_id, {
                "video_url": new_url,
            })
            conn.commit()

        if new_url:
            return True, new_url
        return False, "无下载链接"

    except requests.exceptions.Timeout:
        return False, "请求超时"
    except Exception as e:
        return False, str(e)[:80]


# ============================================================
# 下载逻辑
# ============================================================

def download_file(url: str, save_path: Path, retries: int = MAX_RETRIES):
    """下载文件到本地。返回 (success, message)"""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=60, stream=True)
            if resp.status_code == 200:
                save_path.parent.mkdir(parents=True, exist_ok=True)
                with open(save_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                size_mb = save_path.stat().st_size / 1024 / 1024
                return True, f"OK ({size_mb:.1f}MB)"
            elif resp.status_code in (401, 403):
                return False, f"HTTP {resp.status_code} (链接已过期)"
            else:
                if attempt < retries:
                    time.sleep(2)
                    continue
                return False, f"HTTP {resp.status_code}"
        except requests.exceptions.Timeout:
            if attempt < retries:
                time.sleep(3)
                continue
            return False, "超时"
        except Exception as e:
            if attempt < retries:
                time.sleep(2)
                continue
            return False, str(e)[:80]
    return False, "重试耗尽"


def source_label(in_likes: bool, in_favorites: bool) -> str:
    parts = []
    if in_likes:
        parts.append("likes")
    if in_favorites:
        parts.append("favs")
    return "+".join(parts) if parts else "?"


# ============================================================
# 主运行函数 (v4)
# ============================================================

def run(
    source: Optional[str] = None,
    category: Optional[str] = None,
    tag: Optional[str] = None,
    author: Optional[str] = None,
    limit: int = 0,
    dry_run: bool = False,
    refresh: bool = False,
) -> dict:
    """
    核心下载函数 (v4 版)。

    Returns:
        {"success": int, "failed": int, "skipped": int, "total": int}
    """
    data_dir = str(get_data_root())
    db_path = os.path.join(data_dir, "douku_v4.db")
    conn = get_conn_v4(db_path)

    # ── v4 多表查询待下载视频 ──
    sql = """
        SELECT vb.aweme_id, vb.title, vb.desc, vb.duration_sec, vb.video_tags,
               vu.video_url,
               COALESCE(vm.in_likes, 0) as in_likes,
               COALESCE(vm.in_favorites, 0) as in_favorites,
               COALESCE(vm.is_downloaded, 0) as is_downloaded,
               vc.category as content_category,
               ab.nickname, ab.sec_uid,
               vd.status as dl_status
        FROM videos_base vb
        JOIN videos_urls vu ON vb.aweme_id = vu.aweme_id
        LEFT JOIN videos_meta vm ON vb.aweme_id = vm.aweme_id
        LEFT JOIN videos_classification vc ON vb.aweme_id = vc.aweme_id
        LEFT JOIN authors_base ab ON vb.author_sec_uid = ab.sec_uid
        LEFT JOIN videos_download vd ON vb.aweme_id = vd.aweme_id
        WHERE (vd.status IS NULL OR vd.status = 0 OR vd.status = 3)
          AND vu.video_url != ''
    """
    params: list = []

    if source == "likes":
        sql += " AND vm.in_likes = 1"
    elif source == "favorites":
        sql += " AND vm.in_favorites = 1"

    if category:
        sql += " AND vc.category = ?"
        params.append(category)

    if tag:
        sql += " AND vb.video_tags LIKE ?"
        params.append(f"%{tag}%")

    if author:
        sql += " AND ab.nickname LIKE ?"
        params.append(f"%{author}%")

    sql += " ORDER BY vb.create_time DESC"
    if limit > 0:
        sql += f" LIMIT {limit}"

    rows = conn.execute(sql, params).fetchall()
    videos = [dict(r) for r in rows]

    if not videos:
        print("没有待下载的视频")
        conn.close()
        return {"success": 0, "failed": 0, "skipped": 0, "total": 0}

    # 打印概览
    print("=" * 60)
    print(f"待下载: {len(videos)} 条视频 (v4)")
    if source:
        print(f"来源: {'likes' if source == 'likes' else 'favorites'}")
    if category:
        print(f"分类: {category}")
    if tag:
        print(f"标签: {tag}")
    if author:
        print(f"作者: {author}")
    if refresh:
        print("URL刷新: 开启")
    if limit:
        print(f"本次上限: {limit}")
    print("=" * 60)

    if dry_run:
        print("\n[预览模式] 不实际下载:\n")
        for i, v in enumerate(videos[:20], 1):
            tag_name = extract_primary_tag(v.get("video_tags", ""))
            src = source_label(v.get("in_likes"), v.get("in_favorites"))
            title = v.get("title") or v.get("desc") or v["aweme_id"]
            print(f"  {i}. [{src}][{tag_name}] {v['nickname']} - {title[:40]}")
        if len(videos) > 20:
            print(f"  ... 还有 {len(videos) - 20} 条")
        conn.close()
        return {"success": 0, "failed": 0, "skipped": 0, "total": len(videos)}

    success = 0
    failed = 0
    skipped = 0

    for i, v in enumerate(videos, 1):
        aweme_id = v["aweme_id"]
        title = v.get("title") or v.get("desc") or str(aweme_id)
        nickname = v.get("nickname", "")
        video_url = v.get("video_url", "")
        src = source_label(v.get("in_likes"), v.get("in_favorites"))

        print(f"\n[{i}/{len(videos)}] [{src}] {nickname}: {title[:40]}")

        safe_tag = sanitize_filename(extract_primary_tag(v.get("video_tags", "")))
        safe_title = sanitize_filename(title, max_len=40)
        filename = f"{aweme_id}_{safe_title}.mp4"
        save_dir = get_downloads_dir(safe_tag)
        save_path = save_dir / filename

        # 已存在
        if save_path.exists() and save_path.stat().st_size > 1024:
            size_mb = save_path.stat().st_size / 1024 / 1024
            print(f"  OK already ({size_mb:.1f}MB)")
            set_download_status(conn, aweme_id, DL_DONE, str(save_path))
            success += 1
            time.sleep(random.uniform(DOWNLOAD_DELAY_MIN, DOWNLOAD_DELAY_MAX))
            continue

        # 刷新 URL
        actual_url = video_url
        if refresh or not actual_url:
            ok, result = refresh_video_url(conn, aweme_id)
            if ok:
                actual_url = result
                print(f"  URL refreshed")
            else:
                print(f"  refresh fail: {result}")
                if not actual_url:
                    set_download_status(conn, aweme_id, DL_URL_EXPIRED, error=f"URL refresh fail: {result}")
                    failed += 1
                    time.sleep(random.uniform(0.5, 1.0))
                    continue
            time.sleep(random.uniform(0.5, 1.0))

        # 下载
        set_download_status(conn, aweme_id, DL_IN_PROGRESS)
        print(f"  downloading... -> {safe_tag}/{filename[:50]}")
        ok, msg = download_file(actual_url, save_path)

        if ok:
            print(f"  OK {msg}")
            set_download_status(conn, aweme_id, DL_DONE, str(save_path))
            success += 1
        else:
            # URL 刷新重试（未开启 refresh 时）
            if not refresh:
                ok2, result2 = refresh_video_url(conn, aweme_id)
                if ok2:
                    time.sleep(0.5)
                    ok3, msg3 = download_file(result2, save_path)
                    if ok3:
                        print(f"  OK retry {msg3}")
                        set_download_status(conn, aweme_id, DL_DONE, str(save_path))
                        success += 1
                        time.sleep(random.uniform(DOWNLOAD_DELAY_MIN, DOWNLOAD_DELAY_MAX))
                        continue
                time.sleep(0.5)

            error_status = DL_URL_EXPIRED if "过期" in msg or "403" in msg else DL_FAILED
            print(f"  FAIL {msg}")
            set_download_status(conn, aweme_id, error_status, error=msg)
            failed += 1

        time.sleep(random.uniform(DOWNLOAD_DELAY_MIN, DOWNLOAD_DELAY_MAX))

    print("\n" + "=" * 60)
    print(f"Complete! OK={success}, FAIL={failed}, SKIP={skipped}")
    print(f"Dir: {get_downloads_dir()}")
    print("=" * 60)

    conn.close()
    return {"success": success, "failed": failed, "skipped": skipped, "total": len(videos)}


# ============================================================
# 状态显示 (v4)
# ============================================================

def show_status(source: Optional[str] = None) -> None:
    """显示 v4 下载状态概览"""
    data_dir = str(get_data_root())
    db_path = os.path.join(data_dir, "douku_v4.db")
    conn = get_conn_v4(db_path)

    print("=" * 60)
    print("Download Status (v4)")
    print("=" * 60)

    total = conn.execute("SELECT COUNT(*) FROM videos_base").fetchone()[0]
    total_done = conn.execute(
        "SELECT COUNT(*) FROM videos_download WHERE status=1"
    ).fetchone()[0]
    total_fail = conn.execute(
        "SELECT COUNT(*) FROM videos_download WHERE status=2"
    ).fetchone()[0]
    total_expired = conn.execute(
        "SELECT COUNT(*) FROM videos_download WHERE status=3"
    ).fetchone()[0]
    total_pending = conn.execute("""
        SELECT COUNT(*) FROM videos_base vb
        LEFT JOIN videos_download vd ON vb.aweme_id = vd.aweme_id
        WHERE (vd.status IS NULL OR vd.status = 0)
    """).fetchone()[0]

    print(f"\n  Total: {total}")
    print(f"  Done:    {total_done}")
    print(f"  Pending: {total_pending}")
    print(f"  Failed:  {total_fail}")
    print(f"  Expired: {total_expired}")

    # 按分类
    print("\n  By category:")
    rows = conn.execute("""
        SELECT vc.category,
               COUNT(DISTINCT vb.aweme_id) as total,
               COUNT(DISTINCT CASE WHEN vd.status = 1 THEN vb.aweme_id END) as done
        FROM videos_base vb
        LEFT JOIN videos_classification vc ON vb.aweme_id = vc.aweme_id
        LEFT JOIN videos_download vd ON vb.aweme_id = vd.aweme_id
        WHERE vc.category IS NOT NULL AND vc.category != ''
        GROUP BY vc.category
        ORDER BY total DESC
    """).fetchall()

    for row in rows:
        cat, cnt, done = row
        pct = done / cnt * 100 if cnt > 0 else 0
        bar = "=" * int(pct / 5) + " " * (20 - int(pct / 5))
        print(f"  {cat or '?' :8s} [{bar}] {done}/{cnt} ({pct:.0f}%)")

    conn.close()


def run_from_cli(args):
    """供 dytool_v4.py CLI 调用"""
    if getattr(args, 'status', False):
        show_status(source=getattr(args, 'source', None))
        return

    run(
        source=getattr(args, 'source', None),
        category=getattr(args, 'category', None),
        tag=getattr(args, 'tag', None),
        author=getattr(args, 'author', None),
        limit=getattr(args, 'limit', 0),
        dry_run=getattr(args, 'dry_run', False),
        refresh=getattr(args, 'refresh', False),
    )