# -*- coding: utf-8 -*-
"""
refresh_urls_v4.py - 抖音视频 URL 刷新工具 (v4)
通过 aweme_id 逐条调用详情 API，获取最新的下载链接并更新到数据库

与 v3 的区别：
  - 查询改为 v4 多表 JOIN
  - URL 更新写入 videos_urls 表
  - 下载状态使用 v4 5 态状态机（PENDING/FAILED/URL_EXPIRED → PENDING on success）
"""

import os
import sys
import json
import time
import random
import argparse
import io
import re
from datetime import datetime
from urllib.parse import urlencode, quote

from .db_v4 import (
    get_conn_v4, init_db_v4,
    upsert_author_base, upsert_video_base,
    upsert_video_urls, upsert_video_stats,
    set_download_status,
    DL_PENDING, DL_DONE, DL_FAILED, DL_URL_EXPIRED, DL_IN_PROGRESS,
)
from .signer import Signer
from .meta import load_cookie, get_data_root

# ============ 配置（同 v3）============
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Referer": "https://www.douyin.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": "https://www.douyin.com",
    "Connection": "keep-alive",
}

COOKIES = None

DETAIL_URL = "https://www.douyin.com/aweme/v1/web/aweme/detail/"


def build_detail_params(aweme_id):
    return {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "aweme_id": aweme_id,
        "publish_video_strategy_type": 2,
        "update_version_code": "170400",
        "version_name": "17.4.0",
        "cookie_enabled": "true",
        "screen_width": "1616",
        "screen_height": "908",
        "browser_language": "zh-CN",
        "browser_platform": "Win32",
        "browser_name": "Edge",
        "browser_version": "147.0.0.0",
        "browser_online": "true",
        "engine_name": "Blink",
        "engine_version": "147.0.0.0",
        "os_name": "Windows",
        "os_version": "10.0",
        "device_memory": 16,
        "platform": "PC",
        "downlink": 10,
        "effective_type": "4g",
        "round_trip_time": 200,
    }


def get_a_bogus(params_dict, signer):
    try:
        a_bogus = signer.get_a_bogus(params_dict)
        return quote(a_bogus, safe='')
    except RuntimeError:
        return ""


def fetch_detail(aweme_id, signer, cookies, headers):
    """获取单个视频详情，返回 (aweme, error_msg)"""
    params = build_detail_params(aweme_id)
    a_bogus = get_a_bogus(params, signer)
    if a_bogus:
        params["a_bogus"] = a_bogus
    url = DETAIL_URL + "?" + urlencode(params)

    try:
        resp = requests.get(url, headers=headers, cookies=cookies, timeout=30)
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"
        data = resp.json()
        if data.get("status_code") != 0:
            return None, f"API error: {data.get('status_msg', '未知')}"
        aweme = data.get("aweme_detail")
        if not aweme:
            return None, "aweme_detail 为空"
        return aweme, "OK"
    except requests.exceptions.Timeout:
        return None, "超时"
    except Exception as e:
        return None, str(e)[:80]


def parse_aweme(aweme):
    """解析视频详情（与 v3 完全一致）"""
    aweme_id = aweme.get("aweme_id", "")
    desc = (aweme.get("desc") or "").strip() or "(无描述)"
    item_title = aweme.get("item_title", "") or ""
    create_time = aweme.get("create_time", 0)
    ts = datetime.fromtimestamp(create_time).strftime("%Y-%m-%d %H:%M") if create_time else "N/A"

    aweme_type = aweme.get("aweme_type", 0)
    type_label = "视频" if aweme_type in (0, 4) else ("图集" if aweme_type in (2, 68) else f"类型{aweme_type}")
    duration = aweme.get("duration", 0)
    duration_sec = round(duration / 1000) if duration else 0

    author_info = aweme.get("author") or {}
    author_nickname = author_info.get("nickname", "N/A")
    author_uid = author_info.get("uid", "")
    author_sec_uid = author_info.get("sec_uid", "")
    author_avatar = ""
    avatar_thumb = author_info.get("avatar_thumb") or {}
    if avatar_thumb.get("url_list"):
        author_avatar = avatar_thumb["url_list"][0]

    video_tag_list = []
    for tag in (aweme.get("video_tag") or []):
        video_tag_list.append({
            "tag_id": tag.get("tag_id", ""),
            "tag_name": tag.get("tag_name", ""),
            "level": tag.get("level", 0),
        })

    hashtag_list = []
    for te in (aweme.get("text_extra") or []):
        if te.get("hashtag_name"):
            hashtag_list.append({
                "name": te["hashtag_name"],
                "id": te.get("sec_uid", ""),
            })

    desc_hashtags = re.findall(r'#([^#\s]+)', desc)

    stats = aweme.get("statistics") or {}
    digg_count = stats.get("digg_count", 0)
    comment_count = stats.get("comment_count", 0)
    share_count = stats.get("share_count", 0)
    collect_count = stats.get("collect_count", 0)
    play_count = stats.get("play_count", 0)

    video_info = aweme.get("video") or {}
    play_addr = video_info.get("play_addr") or {}
    video_url = ""
    if play_addr.get("url_list"):
        video_url = play_addr["url_list"][0]
    for key in ["play_addr_265", "play_addr_h264"]:
        alt = video_info.get(key) or {}
        if alt.get("url_list"):
            video_url = alt["url_list"][0]
            break

    cover = ""
    origin_cover = aweme.get("origin_cover") or {}
    if origin_cover.get("url_list"):
        cover = origin_cover["url_list"][0]
    elif video_info.get("cover") and video_info["cover"].get("url_list"):
        cover = video_info["cover"]["url_list"][0]

    music_info = aweme.get("music") or {}
    music_title = music_info.get("title", "")
    music_author = music_info.get("author", "") or ""
    music_url = ""
    music_play = music_info.get("play_addr") or {}
    if music_play.get("url_list"):
        music_url = music_play["url_list"][0]

    share_info = aweme.get("share_info") or {}
    share_url = share_info.get("share_url", "")

    prevent_download = aweme.get("prevent_download", False)

    return {
        "aweme_id": aweme_id,
        "item_title": item_title,
        "desc": desc,
        "create_time": ts,
        "type": type_label,
        "duration_sec": duration_sec,
        "author": {
            "sec_uid": author_sec_uid,
            "nickname": author_nickname,
            "avatar": author_avatar,
        },
        "video_tags": video_tag_list,
        "hashtags": hashtag_list,
        "desc_hashtags": desc_hashtags,
        "stats": {
            "digg": digg_count,
            "comment": comment_count,
            "share": share_count,
            "collect": collect_count,
            "play": play_count,
        },
        "urls": {
            "video": video_url,
            "cover": cover,
            "music": music_url,
            "share": share_url,
        },
        "prevent_download": bool(prevent_download),
    }


def refresh_video(conn, aweme_id, signer, cookies, headers):
    """
    刷新单个视频的 URL，写入 v4 多表。
    返回 (success, message)
    """
    aweme, err = fetch_detail(aweme_id, signer, cookies, headers)
    if aweme is None:
        return False, err

    parsed = parse_aweme(aweme)
    sec_uid = parsed["author"]["sec_uid"]

    # ── authors_base ──
    upsert_author_base(conn, {
        "sec_uid": sec_uid,
        "nickname": parsed["author"]["nickname"],
        "avatar": parsed["author"]["avatar"],
    })

    # ── videos_base ──
    upsert_video_base(conn, {
        "aweme_id": parsed["aweme_id"],
        "title": parsed["item_title"],
        "desc": parsed["desc"],
        "create_time": parsed["create_time"],
        "duration_sec": parsed["duration_sec"],
        "author_sec_uid": sec_uid,
        "share_url": parsed["urls"]["share"],
    })

    # ── videos_urls（核心：更新 video_url）──
    upsert_video_urls(conn, parsed["aweme_id"], {
        "video_url": parsed["urls"]["video"],
        "cover_url": parsed["urls"]["cover"],
    })

    # ── videos_stats ──
    upsert_video_stats(conn, parsed["aweme_id"], parsed["stats"])

    new_url = parsed["urls"]["video"]
    if new_url:
        # 刷新成功 → 状态回到 PENDING（可以下载了）
        set_download_status(conn, aweme_id, DL_PENDING)
        conn.commit()
        return True, f"URL 已更新 ({new_url[:50]}...)"
    else:
        return False, "无视频链接"


def query_pending_videos(conn, source=None, tag=None, failed_only=False, limit=0):
    """
    查询需要刷新 URL 的视频 (v4 多表 JOIN)
    """
    sql = """
        SELECT vb.aweme_id, vb.title,
               COALESCE(vb.type, '') as type,
               vu.video_url,
               vd.status as dl_status,
               COALESCE(vm.in_likes, 0) as in_likes,
               COALESCE(vm.in_favorites, 0) as in_favorites
        FROM videos_base vb
        LEFT JOIN videos_urls vu ON vb.aweme_id = vu.aweme_id
        LEFT JOIN videos_meta vm ON vb.aweme_id = vm.aweme_id
        LEFT JOIN videos_download vd ON vb.aweme_id = vd.aweme_id
        WHERE (vd.status IS NULL OR vd.status IN (0, 2, 3))
    """
    params = []

    if source == "likes":
        sql += " AND vm.in_likes = 1"
    elif source == "favorites":
        sql += " AND vm.in_favorites = 1"

    if tag:
        sql += " AND vb.video_tags LIKE ?"
        params.append(f"%{tag}%")

    sql += " ORDER BY vb.create_time DESC"

    if limit > 0:
        sql += f" LIMIT {limit}"

    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def refresh_urls(source=None, tag=None, failed_only=False, limit=0, dry_run=False):
    """CLI entry point (v4)"""
    data_dir = str(get_data_root())
    db_path = os.path.join(data_dir, "douku_v4.db")
    init_db_v4(db_path)
    conn = get_conn_v4(db_path)

    cookie_data = load_cookie()
    cookies = {
        "sessionid": cookie_data.get("sessionid", ""),
        "sid_tt": cookie_data.get("sessionid", ""),
        "ttwid": cookie_data.get("ttwid", ""),
    }
    headers = HEADERS.copy()
    signer = Signer(headers["User-Agent"])

    videos = query_pending_videos(
        conn, source=source, tag=tag, failed_only=failed_only, limit=limit
    )

    if not videos:
        print("[refresh_urls v4] no pending videos")
        conn.close()
        return {"success": 0, "no_url": 0, "api_fail": 0, "prevent": 0, "total": 0}

    src_name = {"likes": "likes", "favorites": "favorites"}.get(source, "all")
    print("=" * 60)
    print("refresh_urls (v4)")
    print("=" * 60)
    print(f"source: {src_name}")
    if tag: print(f"tag: {tag}")
    if failed_only: print("mode: failed only")
    if limit: print(f"limit: {limit}")
    print(f"pending: {len(videos)}")
    print()

    if dry_run:
        for i, v in enumerate(videos[:30], 1):
            has_url = "YES" if v.get("video_url") else "NO"
            status = {0: "pending", 1: "done", 2: "failed", 3: "expired"}.get(
                v.get("dl_status"), "?")
            src_tags = []
            if v.get("in_likes"): src_tags.append("L")
            if v.get("in_favorites"): src_tags.append("F")
            title = (v.get("title") or "")[:40]
            print(f"  {i}. [{status}] [{has_url}] [{'+'.join(src_tags)}] {title}")
        if len(videos) > 30:
            print(f"  ... +{len(videos) - 30} more")
        conn.close()
        return {"success": 0, "no_url": 0, "api_fail": 0, "prevent": 0, "total": len(videos)}

    success = 0
    no_url = 0
    api_fail = 0
    prevent_count = 0

    for i, v in enumerate(videos, 1):
        aweme_id = v["aweme_id"]
        title = (v.get("title") or "")[:35]
        has_url_old = "YES" if v.get("video_url") else "NO"
        print(f"[{i}/{len(videos)}] {aweme_id} ({has_url_old}) {title}")

        ok, msg = refresh_video(conn, aweme_id, signer, cookies, headers)

        if ok:
            success += 1
            print(f"  OK {msg}")
        else:
            if "无视频链接" in msg or "禁止下载" in msg:
                no_url += 1
                if "禁止下载" in msg:
                    prevent_count += 1
                print(f"  SKIP {msg}")
            else:
                api_fail += 1
                print(f"  FAIL {msg}")

        delay = random.uniform(0.8, 1.5)
        time.sleep(delay)

    print()
    print("=" * 60)
    print(f"Done! success={success}, no_url={no_url}, fail={api_fail}, prevent={prevent_count}")
    print("=" * 60)

    conn.close()
    return {"success": success, "no_url": no_url, "api_fail": api_fail, "prevent": prevent_count, "total": len(videos)}


def run_from_cli(args):
    """供 dytool_v4.py CLI 调用"""
    refresh_urls(
        source=getattr(args, 'source', None),
        tag=getattr(args, 'tag', None),
        failed_only=getattr(args, 'failed', False),
        limit=getattr(args, 'limit', 0),
        dry_run=getattr(args, 'dry_run', False),
    )


def main():
    parser = argparse.ArgumentParser(description="抖音视频 URL 刷新工具 (v4)")
    parser.add_argument("--source", "-s", choices=["likes", "favorites"],
                        help="只刷新指定来源")
    parser.add_argument("--tag", "-t", help="只刷新指定标签")
    parser.add_argument("--limit", "-n", type=int, default=0,
                        help="最多刷新N条 (0=不限)")
    parser.add_argument("--failed", "-f", action="store_true",
                        help="只刷新下载失败的记录")
    parser.add_argument("--dry-run", "-d", action="store_true",
                        help="只显示需要刷新的列表，不实际刷新")
    args = parser.parse_args()

    refresh_urls(
        source=args.source,
        tag=args.tag,
        failed_only=args.failed,
        limit=args.limit,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
