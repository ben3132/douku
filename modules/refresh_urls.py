"""
抖音视频 URL 刷新工具
通过 aweme_id 逐条调用详情 API，获取最新的下载链接并更新到数据库
下载前必须刷新，否则旧链接过期导致下载失败

用法:
  python refresh_urls.py                    # 刷新所有未下载视频的 URL
  python refresh_urls.py --source likes     # 只刷新点赞来源
  python refresh_urls.py --tag 二次元        # 只刷新指定标签
  python refresh_urls.py --limit 50         # 最多刷新50条
  python refresh_urls.py --failed           # 只刷新之前下载失败的
"""

import os
import sys
import json
import time
import random
import argparse
import requests
import io
import re
from datetime import datetime
from urllib.parse import urlencode, quote

if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from abogus import ABogus
from db_utils import (
    init_db, get_conn, get_video_count, get_author_count,
    upsert_author, upsert_video, VALID_SOURCES,
)

try:
    from config import SESSION_ID, SEC_USER_ID, TTWID
except ImportError:
    print("\n❌ 配置文件不存在！请先复制 config.example.py 为 config.py 并填写")
    sys.exit(1)

# ============ 配置 ============
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Referer": "https://www.douyin.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": "https://www.douyin.com",
    "Connection": "keep-alive",
}

COOKIES = {
    "sessionid": SESSION_ID,
    "sid_tt": SESSION_ID,
    "sid_guard": f"{SESSION_ID}|1777066427|51840000|Tue, 23-Jun-2026 21:33:47 GMT",
    "ttwid": TTWID,
}

DETAIL_URL = "https://www.douyin.com/aweme/v1/web/aweme/detail/"


def build_detail_params(aweme_id):
    """构建视频详情 API 参数"""
    return {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "aweme_id": aweme_id,
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


def get_a_bogus(params_dict):
    bogus = ABogus()
    params_str = urlencode(params_dict)
    a_bogus = bogus.get_value(params_str, method="GET")
    return quote(a_bogus, safe='')


def fetch_detail(aweme_id):
    """获取单个视频详情，返回 aweme 对象或 None"""
    params = build_detail_params(aweme_id)
    a_bogus = get_a_bogus(params)
    params["a_bogus"] = a_bogus
    url = DETAIL_URL + "?" + urlencode(params)

    try:
        resp = requests.get(url, headers=HEADERS, cookies=COOKIES, timeout=30)
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
    """解析视频详情 - 与 fetch_likes_db.py 保持一致"""
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

    # 尝试获取无水印链接
    # play_addr_265 / play_addr_h264 等可能有更好的链接
    for key in ["play_addr_265", "play_addr_h264"]:
        alt = video_info.get(key) or {}
        if alt.get("url_list"):
            # 如果 265/h264 链接可用，优先使用
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

    is_top = aweme.get("is_top", 0)
    prevent_download = aweme.get("prevent_download", False)

    return {
        "aweme_id": aweme_id,
        "item_title": item_title,
        "desc": desc,
        "create_time": ts,
        "liked_time": "",  # 详情 API 不返回 liked_time
        "type": type_label,
        "aweme_type_raw": aweme_type,
        "duration_sec": duration_sec,
        "duration_human": f"{duration_sec // 60}:{duration_sec % 60:02d}" if duration_sec > 60 else f"{duration_sec}秒",
        "author": {
            "uid": author_uid,
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
        "music": {
            "title": music_title,
            "author": music_author,
        },
        "is_top": bool(is_top),
        "prevent_download": bool(prevent_download),
    }


def refresh_video(conn, aweme_id, source="likes"):
    """刷新单个视频的 URL，返回 (success, message)"""
    aweme, err = fetch_detail(aweme_id)
    if aweme is None:
        return False, err

    parsed = parse_aweme(aweme)

    # 更新作者信息
    author_id = upsert_author(
        conn,
        sec_uid=parsed["author"]["sec_uid"],
        nickname=parsed["author"]["nickname"],
        avatar=parsed["author"]["avatar"],
    )

    # upsert_video 会更新 video_url 等字段，但保留 in_likes/in_favorites 标记
    upsert_video(conn, parsed, author_id, source=source)

    new_url = parsed["urls"]["video"]
    if new_url:
        return True, f"URL已更新 ({new_url[:60]}...)"
    else:
        return False, "无视频链接"


def query_pending_videos(conn, source=None, tag=None, failed_only=False, limit=0):
    """查询需要刷新 URL 的视频"""
    sql = """
        SELECT v.id, v.aweme_id, v.title, v.type, v.video_tags,
               v.video_url, v.is_downloaded, v.in_likes, v.in_favorites
        FROM videos v
        WHERE 1=1
    """
    params = []

    if failed_only:
        # 只刷新下载失败的
        sql += " AND v.is_downloaded = 2"
    else:
        # 刷新所有未成功下载的（pending + failed）
        sql += " AND v.is_downloaded != 1"

    if source == "likes":
        sql += " AND v.in_likes = 1"
    elif source == "favorites":
        sql += " AND v.in_favorites = 1"

    if tag:
        sql += " AND v.video_tags LIKE ?"
        params.append(f'%{tag}%')

    sql += " ORDER BY v.create_time DESC"

    if limit > 0:
        sql += f" LIMIT {limit}"

    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def main():
    parser = argparse.ArgumentParser(description="抖音视频 URL 刷新工具")
    parser.add_argument("--source", "-S", choices=list(VALID_SOURCES),
                        help="只刷新指定来源: likes/favorites")
    parser.add_argument("--tag", "-t", help="只刷新指定标签")
    parser.add_argument("--limit", "-n", type=int, default=0,
                        help="最多刷新N条 (0=不限)")
    parser.add_argument("--failed", "-f", action="store_true",
                        help="只刷新下载失败的记录")
    parser.add_argument("--dry-run", "-d", action="store_true",
                        help="只显示需要刷新的列表，不实际刷新")
    args = parser.parse_args()

    init_db()
    conn = get_conn()

    videos = query_pending_videos(
        conn,
        source=args.source,
        tag=args.tag,
        failed_only=args.failed,
        limit=args.limit,
    )

    if not videos:
        print("没有需要刷新 URL 的视频")
        conn.close()
        return

    src_name = {"likes": "👍点赞", "favorites": "⭐收藏"}.get(args.source, "全部")

    print("=" * 60)
    print("🔄 视频URL刷新工具")
    print("=" * 60)
    print(f"来源: {src_name}")
    if args.tag:
        print(f"标签: {args.tag}")
    if args.failed:
        print("模式: 只刷新失败的")
    if args.limit:
        print(f"上限: {args.limit}")
    print(f"待刷新: {len(videos)} 条")
    print()

    if args.dry_run:
        for i, v in enumerate(videos[:30], 1):
            has_url = "✅有链接" if v["video_url"] else "❌无链接"
            status = {0: "⬜未下载", 1: "✅已下载", 2: "❌失败"}.get(v["is_downloaded"], "?")
            src_tags = []
            if v["in_likes"]: src_tags.append("👍")
            if v["in_favorites"]: src_tags.append("⭐")
            title = (v["title"] or v.get("desc", ""))[:40]
            print(f"  {i}. [{status}] [{has_url}] [{''.join(src_tags)}] {title}")
        if len(videos) > 30:
            print(f"  ... 还有 {len(videos) - 30} 条")
        conn.close()
        return

    # 实际刷新
    success = 0
    no_url = 0  # API 返回成功但无下载链接（可能被删除/私密）
    api_fail = 0
    prevent_count = 0

    for i, v in enumerate(videos, 1):
        aweme_id = v["aweme_id"]
        title = (v["title"] or "")[:35]
        has_url_old = "有链接" if v["video_url"] else "无链接"

        print(f"[{i}/{len(videos)}] {aweme_id} ({has_url_old}) {title}")

        # 判断来源标记（用哪个 source 去 upsert）
        source = "likes" if v["in_likes"] else "favorites"

        ok, msg = refresh_video(conn, aweme_id, source=source)

        if ok:
            conn.commit()
            success += 1
            print(f"  ✅ {msg}")
        else:
            if "无视频链接" in msg or "禁止下载" in msg:
                no_url += 1
                # 标记 prevent_download 或下载失败
                if "禁止下载" in msg:
                    prevent_count += 1
                    conn.execute("UPDATE videos SET prevent_download=1 WHERE aweme_id=?", (aweme_id,))
                else:
                    # 可能是已删除的视频
                    no_url += 1
                conn.commit()
                print(f"  ⚠️  {msg}")
            else:
                api_fail += 1
                print(f"  ❌ {msg}")

        # 请求间隔
        delay = random.uniform(0.8, 1.5)
        time.sleep(delay)

    # 最终统计
    print("\n" + "=" * 60)
    print(f"刷新完成!")
    print(f"  ✅ 成功: {success}")
    print(f"  ⚠️  无链接/已删除: {no_url}")
    print(f"  🔒 禁止下载: {prevent_count}")
    print(f"  ❌ API失败: {api_fail}")
    print("=" * 60)

    conn.close()


if __name__ == "__main__":
    main()
