"""
抖音点赞列表抓取 (v4)
特性: 直接写入 v4 多表分层架构 + 增量抓取 + 签名算法

与 v3 的区别:
  - 使用 db_v4 替代 db_utils（多表 upsert 替代单表写入）
  - 输出数据被解析为 v4 结构: authors_base/stats → videos_base/stats/urls/meta/download
  - 书签使用 v4 bookmark 表
  - parse_aweme 与 v3 完全一致（数据解析层不变）
"""

import json
import time
import random
import re
import argparse
import os
from datetime import datetime
from urllib.parse import urlencode, quote
from typing import Dict, Any, List, Optional

import requests

from .db_v4 import (
    get_conn_v4, init_db_v4,
    upsert_author_base, upsert_author_stats,
    upsert_video_base, upsert_video_stats, upsert_video_urls, upsert_video_meta,
    init_download_status,
    set_bookmark, get_bookmark,
    get_video_count, get_author_count,
    get_download_stats, get_summary,
)
from .signer import Signer
from .meta import get_cookie_path, get_data_root, load_cookie


# ============================================================
# v4 路径
# ============================================================

def _get_v4_db_path() -> str:
    return os.path.join(get_data_root(), "douku_v4.db")


# ============================================================
# API 参数构建
# ============================================================

def build_params(sec_user_id: str, max_cursor: int = 0, count: int = 20) -> Dict[str, Any]:
    return {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "sec_user_id": sec_user_id,
        "max_cursor": max_cursor,
        "min_cursor": 0,
        "whale_cut_token": "",
        "count": count,
        "publish_video_strategy_type": 2,
        "update_version_code": 170400,
        "version_name": "17.4.0",
        "cookie_enabled": "true",
        "screen_width": 1616,
        "screen_height": 908,
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


# ============================================================
# 数据解析（与 v3 完全一致，不涉及存储层）
# ============================================================

def parse_aweme(aweme: Dict) -> Dict[str, Any]:
    """解析单条视频数据（v4 增强：提取 liked_time 给 videos_meta）"""
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
    author_signature = author_info.get("signature", "")
    author_ip_location = author_info.get("ip_location", "")
    author_avatar = ""
    avatar_thumb = author_info.get("avatar_thumb") or {}
    if avatar_thumb.get("url_list"):
        author_avatar = avatar_thumb["url_list"][0]

    # 认证信息
    verification_type = 0
    verification_label = ""
    if author_info.get("enterprise_verify_reason"):
        verification_type = 2
        verification_label = author_info["enterprise_verify_reason"]
    elif author_info.get("custom_verify"):
        verification_type = 1
        verification_label = author_info["custom_verify"]
    is_gov_media_vip = bool(author_info.get("is_gov_media_vip"))

    # 作者统计
    author_follower_count = author_info.get("follower_count", 0) or 0
    author_following_count = author_info.get("following_count", 0) or 0
    author_aweme_count = author_info.get("aweme_count", 0) or 0
    author_favoriting_count = author_info.get("favoriting_count", 0) or 0

    # 视频标签
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

    # 统计数据
    stats = aweme.get("statistics") or {}
    digg_count = stats.get("digg_count", 0)
    comment_count = stats.get("comment_count", 0)
    share_count = stats.get("share_count", 0)
    collect_count = stats.get("collect_count", 0)
    play_count = stats.get("play_count", 0)

    # 视频 URL
    video_info = aweme.get("video") or {}
    play_addr = video_info.get("play_addr") or {}
    video_url = ""
    if play_addr.get("url_list"):
        video_url = play_addr["url_list"][0]

    # 封面
    cover = ""
    origin_cover = aweme.get("origin_cover") or {}
    if origin_cover.get("url_list"):
        cover = origin_cover["url_list"][0]
    elif video_info.get("cover") and video_info["cover"].get("url_list"):
        cover = video_info["cover"]["url_list"][0]

    # 音乐
    music_info = aweme.get("music") or {}
    music_title = music_info.get("title", "")
    music_author = music_info.get("author", "") or ""
    music_url = ""
    music_play = music_info.get("play_addr") or {}
    if music_play.get("url_list"):
        music_url = music_play["url_list"][0]

    # 分享
    share_info = aweme.get("share_info") or {}
    share_url = share_info.get("share_url", "")

    is_top = aweme.get("is_top", 0)
    prevent_download = aweme.get("prevent_download", False)

    # 点赞时间
    interact_data = aweme.get("interact_data") or {}
    liked_time = interact_data.get("liked_time", 0)
    if not liked_time:
        liked_time = aweme.get("liked_time", 0) or aweme.get("like_time", 0)
    if not liked_time:
        liked_time = create_time
    liked_ts = datetime.fromtimestamp(liked_time).strftime("%Y-%m-%d %H:%M") if liked_time else ""

    return {
        "aweme_id": aweme_id,
        "item_title": item_title,
        "desc": desc,
        "create_time": ts,
        "liked_time": liked_ts,
        "type": type_label,
        "aweme_type_raw": aweme_type,
        "duration_sec": duration_sec,
        "duration_human": f"{duration_sec // 60}:{duration_sec % 60:02d}" if duration_sec > 60 else f"{duration_sec}秒",
        # v4: 完整的作者信息（base + stats）
        "author": {
            "uid": author_uid,
            "sec_uid": author_sec_uid,
            "nickname": author_nickname,
            "avatar": author_avatar,
            "signature": author_signature,
            "ip_location": author_ip_location,
            "verification_type": verification_type,
            "verification_label": verification_label,
            "is_gov_media_vip": is_gov_media_vip,
            "follower_count": author_follower_count,
            "following_count": author_following_count,
            "aweme_count": author_aweme_count,
            "favoriting_count": author_favoriting_count,
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
        "music": {
            "title": music_title,
            "author": music_author,
        },
        "is_top": bool(is_top),
        "prevent_download": bool(prevent_download),
    }


# ============================================================
# v4 存储层（对应老版的 upsert_author + upsert_video）
# ============================================================

def save_to_v4(conn, parsed: dict, source: str = "likes") -> bool:
    """
    将解析后的视频数据写入 v4 多表架构，返回是否为新视频。

    写入的表:
      - authors_base  (sec_uid, nickname, avatar, signature, ...)
      - authors_stats  (follower_count, ...)
      - videos_base    (aweme_id, title, desc, ...)
      - videos_stats   (digg_count, comment_count, ...)
      - videos_urls    (video_url, cover_url, music_url)
      - videos_meta    (in_likes / in_favorites, liked_time)
      - videos_download (初始化 DL_PENDING)
    """
    author = parsed["author"]
    aweme_id = parsed["aweme_id"]
    sid = author["sec_uid"]

    # 1. 写入作者基础信息
    upsert_author_base(conn, {
        "sec_uid": sid,
        "nickname": author["nickname"],
        "avatar": author["avatar"],
        "signature": author.get("signature", ""),
        "ip_location": author.get("ip_location", ""),
        "verification_type": author.get("verification_type", 0),
        "verification_label": author.get("verification_label", ""),
        "is_gov_media_vip": author.get("is_gov_media_vip", False),
    })

    # 2. 写入作者统计（首次才写，避免覆盖已有数据）
    upsert_author_stats(conn, {
        "sec_uid": sid,
        "follower_count": author.get("follower_count", 0) or 0,
        "following_count": author.get("following_count", 0) or 0,
        "aweme_count": author.get("aweme_count", 0) or 0,
        "favoriting_count": author.get("favoriting_count", 0) or 0,
    })

    # 3. 写入视频基础信息（INSERT OR IGNORE，已存在跳过）
    upsert_video_base(conn, {
        "aweme_id": aweme_id,
        "title": parsed.get("item_title", "") or parsed.get("desc", ""),
        "desc": parsed.get("desc", ""),
        "create_time": parsed.get("create_time", ""),
        "type": parsed.get("type", ""),
        "aweme_type_raw": parsed.get("aweme_type_raw", 0),
        "duration_sec": parsed.get("duration_sec", 0),
        "author_sec_uid": sid,
        "video_tags": parsed.get("video_tags", []),
        "hashtags": parsed.get("hashtags", []),
        "desc_hashtags": parsed.get("desc_hashtags", []),
        "share_url": parsed.get("urls", {}).get("share", ""),
        "is_top": 1 if parsed.get("is_top") else 0,
        "prevent_download": 1 if parsed.get("prevent_download") else 0,
    })

    # 4. 写入视频统计（首次才写）
    stats = parsed.get("stats", {})
    upsert_video_stats(conn, aweme_id, {
        "digg_count": stats.get("digg", 0),
        "comment_count": stats.get("comment", 0),
        "share_count": stats.get("share", 0),
        "collect_count": stats.get("collect", 0),
        "play_count": stats.get("play", 0),
    })

    # 5. 写入视频 URL（首次才写）
    urls = parsed.get("urls", {})
    upsert_video_urls(conn, aweme_id, {
        "video_url": urls.get("video", ""),
        "cover_url": urls.get("cover", ""),
        "music_url": urls.get("music", ""),
        "music_title": parsed.get("music", {}).get("title", ""),
    })

    # 6. 写入来源标记（likes / favorites）
    meta_updates = {"in_likes": 0, "in_favorites": 0, "liked_time": "", "favorited_time": ""}
    if source == "likes":
        meta_updates["in_likes"] = 1
        meta_updates["liked_time"] = parsed.get("liked_time", "")
    elif source == "favorites":
        meta_updates["in_favorites"] = 1
        meta_updates["favorited_time"] = parsed.get("liked_time", "")
    upsert_video_meta(conn, aweme_id, meta_updates)

    # 7. 初始化下载状态
    init_download_status(conn, aweme_id)

    return True  # v4 用 INSERT OR IGNORE，不区分 new/update


# ============================================================
# v4 统计辅助
# ============================================================

def get_v4_likes_count(conn) -> int:
    """查询点赞视频数量"""
    row = conn.execute(
        "SELECT COUNT(*) FROM videos_meta WHERE in_likes = 1"
    ).fetchone()
    return row[0] if row else 0


def get_v4_tag_distribution(conn, source: str = "likes", limit: int = 10):
    """查询标签分布"""
    field = "in_likes" if source == "likes" else "in_favorites"
    rows = conn.execute(f"""
        SELECT vt.value as tag_name, COUNT(*) as cnt
        FROM videos_base vb, json_each(vb.video_tags) vt
        JOIN videos_meta vm ON vb.aweme_id = vm.aweme_id
        WHERE vm.{field} = 1
        GROUP BY vt.value
        ORDER BY cnt DESC
        LIMIT ?
    """, (limit,)).fetchall()
    return [(r["tag_name"], r["cnt"]) for r in rows]


# ============================================================
# API 请求
# ============================================================

BASE_URL = "https://www.douyin.com/aweme/v1/web/aweme/favorite/"

_COOKIE_EXPIRED_CODES = {8, 210}
_COOKIE_EXPIRED_MSG_KEYS = ["登录", "cookie", "token", "auth", "credential", "失效", "过期"]


def _is_auth_error(status_code: int, msg: str) -> bool:
    if status_code in _COOKIE_EXPIRED_CODES:
        return True
    msg_lower = msg.lower()
    return any(key in msg_lower or key in msg for key in _COOKIE_EXPIRED_MSG_KEYS)


def fetch_page(signer: Signer, sec_user_id: str, cookies: Dict, headers: Dict,
               max_cursor: int = 0, count: int = 20) -> Optional[Dict]:
    """抓取单页数据"""
    params = build_params(sec_user_id, max_cursor, count)
    a_bogus = signer.get_a_bogus(params)
    params["a_bogus"] = quote(a_bogus, safe='')
    url = BASE_URL + "?" + urlencode(params)

    try:
        response = requests.get(url, headers=headers, cookies=cookies, timeout=30)
        if response.status_code == 401 or response.status_code == 403:
            print(f"  HTTP {response.status_code} — Cookie 已过期")
            return {"status_code": 999, "_cookie_expired": True}
        if response.status_code != 200:
            print(f"  HTTP 错误: {response.status_code}")
            return None
        return response.json()
    except requests.exceptions.Timeout:
        print("  请求超时")
        return None
    except requests.exceptions.RequestException as e:
        print(f"  请求异常: {e}")
        return None
    except json.JSONDecodeError:
        print("  JSON 解析失败")
        return None


# ============================================================
# 主函数
# ============================================================

def run(count: int = 0, reset: bool = False) -> Dict[str, int]:
    """
    执行抓取（v4 存储层）

    参数:
        count: 本次最多抓取数量 (0=不限制)
        reset: 是否重置书签

    返回:
        {"new": 新增数量, "update": 更新数量}
    """
    cookie = load_cookie()
    session_id = cookie.get("sessionid", "")
    sec_user_id = cookie.get("sec_user_id", "")
    ttwid = cookie.get("ttwid", "")

    if not session_id or not sec_user_id:
        raise ValueError(
            f"Cookie 未设置或缺失\n"
            f"请运行 dytool.py cookie 获取，或确认 {get_cookie_path()} 存在且包含 sessionid 和 sec_user_id"
        )

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Referer": "https://www.douyin.com/user/self?showTab=like",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": "https://www.douyin.com",
    }

    cookies = {
        "sessionid": session_id,
        "sid_tt": session_id,
        "sid_guard": f"{session_id}|1777066427|51840000|Tue, 23-Jun-2026 21:33:47 GMT",
        "ttwid": ttwid,
    }

    source = "likes"
    db_path = _get_v4_db_path()
    init_db_v4(db_path)
    conn = get_conn_v4(db_path)
    signer = Signer(headers["User-Agent"])

    # 重置书签
    if reset:
        set_bookmark(conn, source, cursor="0", liked_time="", total_fetched=0)
        print("已重置书签")

    # 读取书签
    bm = get_bookmark(conn, source)
    start_cursor = int(bm.get("last_cursor", 0)) if bm and bm.get("last_cursor") else 0

    existing_count = get_v4_likes_count(conn)
    author_count = get_author_count(conn)

    print(f"用户 ID: {sec_user_id[:20]}...")
    print(f"来源: 点赞 (in_likes)")
    print(f"v4 数据库已有: {existing_count} 条点赞视频, {author_count} 位作者")
    print(f"书签 cursor: {start_cursor}")
    print(f"本次上限: {count if count > 0 else '无限制'}")
    print()

    max_cursor = start_cursor
    page_num = 0
    new_count = 0
    has_more = True
    empty_count = 0

    while has_more:
        if count > 0 and new_count >= count:
            print(f"\n已达到本次抓取上限 ({count} 条)")
            break

        page_num += 1
        print(f"\n--- 第 {page_num} 页 (cursor={max_cursor}) ---")

        data = fetch_page(signer, sec_user_id, cookies, headers, max_cursor, 20)

        if data is None:
            print("  请求失败, 等待重试...")
            time.sleep(3)
            continue

        if data.get("_cookie_expired"):
            print("\nCookie 已过期！请运行: dytool.py cookie")
            break

        status_code = data.get("status_code", -1)
        print(f"  status_code={status_code}")

        if status_code != 0:
            msg = data.get('status_msg', '未知')
            print(f"  API 错误: {msg}")
            if status_code == 8 or _is_auth_error(status_code, msg):
                print("\nCookie 已过期！请运行: dytool.py cookie")
                break
            continue

        aweme_list = data.get("aweme_list") or []
        has_more = data.get("has_more", False)
        new_cursor = data.get("max_cursor", 0)

        if len(aweme_list) == 0:
            empty_count += 1
            print(f"  空页 (连续 {empty_count} 次)")
            if empty_count >= 3:
                print("  连续 3 次空页, 结束")
                break
            time.sleep(1)
            continue

        empty_count = 0

        last_liked_time = ""
        for aweme in aweme_list:
            parsed = parse_aweme(aweme)
            save_to_v4(conn, parsed, source=source)
            new_count += 1
            last_liked_time = parsed["liked_time"] or parsed["create_time"]

        conn.commit()

        print(f"  +{len(aweme_list)} 条 (累计新增 {new_count})")
        for aweme in aweme_list[:3]:
            parsed = parse_aweme(aweme)
            tags = ", ".join([t['tag_name'] for t in parsed['video_tags']]) or "无标签"
            print(f"    [{parsed['create_time']}] [{tags}] {parsed['author']['nickname']}")
            print(f"       {parsed['desc'][:50]} | {parsed['duration_human']} | 赞{parsed['stats']['digg']} 评{parsed['stats']['comment']}")
        if len(aweme_list) > 3:
            print(f"    ... +{len(aweme_list) - 3} 条")

        if new_cursor != max_cursor:
            max_cursor = new_cursor
        else:
            if has_more:
                max_cursor += 1

        set_bookmark(conn, source, str(max_cursor), last_liked_time, new_count)

        delay = random.uniform(1.0, 2.0)
        time.sleep(delay)

        if page_num >= 200:
            print("\n已达到最大页数限制 (200 页)")
            break

    conn.commit()

    total_likes = get_v4_likes_count(conn)
    total_authors = get_author_count(conn)

    print("\n" + "=" * 60)
    print(f"抓取完成! 本轮新增 {new_count} 条")
    print(f"v4 库: 点赞视频 {total_likes} 条, 作者 {total_authors} 位")
    print("=" * 60)

    # v4 下载状态
    dl = get_download_stats(conn)
    print(f"\n下载状态 (v4):")
    print(f"  待下载: {dl['pending']}")
    print(f"  已下载: {dl['done']}")
    print(f"  失败:   {dl['failed']}")
    print(f"  URL过期: {dl['expired']}")

    # v4 标签分布
    print(f"\n标签分布 [点赞] (Top 10):")
    for tag_name, cnt in get_v4_tag_distribution(conn, source="likes", limit=10):
        print(f"  {tag_name}: {cnt} 条")

    conn.close()
    return {"new": new_count, "update": 0}


# ============================================================
# CLI 入口
# ============================================================

def main():
    import sys
    import io

    if sys.platform == 'win32':
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

    parser = argparse.ArgumentParser(description="抖音点赞列表抓取 (v4)")
    parser.add_argument("--count", "-n", type=int, default=0,
                        help="本次最多抓取N条新视频 (0=不限制)")
    parser.add_argument("--reset", "-r", action="store_true",
                        help="重置书签，从最新开始重新抓取")
    parser.add_argument("--data-dir", type=str, help="数据目录路径")

    args = parser.parse_args()

    if args.data_dir:
        from .meta import set_data_dir
        set_data_dir(args.data_dir)

    print("=" * 60)
    print("抖音点赞列表抓取 (v4 · 多表分层)")
    print(f"数据目录: {get_data_root()}")
    print("=" * 60)

    try:
        result = run(count=args.count, reset=args.reset)
        print(f"\n完成: 新增 {result['new']} 条")
    except Exception as e:
        print(f"\n错误: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
