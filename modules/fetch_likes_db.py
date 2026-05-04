"""
抖音点赞列表 - SQLite 版 (增量抓取)
核心: requests + a_bogus 签名 + 自动翻页 + SQLite 存储 + 断点续扫
支持 --count N 控制抓取数量，--reset 重置书签重新抓取
"""

import json
import time
import random
import sys
import os
import re
import argparse
from datetime import datetime
from urllib.parse import urlencode, quote
import requests
import io

if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from abogus import ABogus
from db_utils import (
    init_db, get_conn, upsert_author, upsert_video,
    update_bookmark, get_bookmark, get_video_count,
    get_author_count, get_download_stats, get_tag_distribution,
)

try:
    from config import SESSION_ID, SEC_USER_ID, TTWID
except ImportError:
    print("\n❌ 配置文件不存在！请先复制 config.example.py 为 config.py 并填写")
    sys.exit(1)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Referer": "https://www.douyin.com/user/self?showTab=like",
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

BASE_URL = "https://www.douyin.com/aweme/v1/web/aweme/favorite/"


def build_params(max_cursor=0, count=20):
    params = {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "sec_user_id": SEC_USER_ID,
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
    return params


def get_a_bogus(params_dict):
    bogus = ABogus()
    params_str = urlencode(params_dict)
    a_bogus = bogus.get_value(params_str, method="GET")
    return quote(a_bogus, safe='')


def fetch_page(max_cursor=0, count=20):
    params = build_params(max_cursor, count)
    a_bogus = get_a_bogus(params)
    params["a_bogus"] = a_bogus
    url = BASE_URL + "?" + urlencode(params)
    try:
        response = requests.get(url, headers=HEADERS, cookies=COOKIES, timeout=30)
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


def parse_aweme(aweme):
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
    interact_data = aweme.get("interact_data") or {}
    liked_time = interact_data.get("liked_time", 0)
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


def main():
    parser = argparse.ArgumentParser(description="抖音点赞列表 - SQLite 增量抓取")
    parser.add_argument("--count", "-n", type=int, default=0,
                        help="本次最多抓取N条新视频 (0=不限制)")
    parser.add_argument("--reset", "-r", action="store_true",
                        help="重置书签，从最新开始重新抓取")
    args = parser.parse_args()

    source = "likes"
    count_limit = args.count

    print("=" * 60)
    print("抖音点赞列表 - SQLite 增量抓取")
    print("=" * 60)

    init_db()
    conn = get_conn()

    if args.reset:
        from db_utils import reset_bookmark
        reset_bookmark(conn, source=source)
        conn.commit()
        print("🔄 已重置书签")

    bm = get_bookmark(conn, source=source)
    start_cursor = int(bm["last_cursor"]) if bm and bm["last_cursor"] else 0
    prev_total = bm["total_fetched"] if bm else 0

    existing_count = get_video_count(conn, source=source)
    author_count = get_author_count(conn)

    print(f"用户 ID: {SEC_USER_ID[:20]}...")
    print(f"来源: 点赞 (in_likes)")
    print(f"数据库已有: {existing_count} 条点赞视频, {author_count} 位作者")
    print(f"书签 cursor: {start_cursor}, 历史累计: {prev_total}")
    if count_limit > 0:
        print(f"本次上限: {count_limit} 条新视频")
    else:
        print("本次上限: 无限制")
    print()

    max_cursor = start_cursor
    page_num = 0
    new_count = 0
    update_count = 0
    has_more = True
    empty_count = 0

    while has_more:
        if count_limit > 0 and new_count >= count_limit:
            print(f"\n✅ 已达到本次抓取上限 ({count_limit} 条)")
            break

        page_num += 1
        print(f"\n--- 第 {page_num} 页 (cursor={max_cursor}) ---")

        data = fetch_page(max_cursor=max_cursor, count=20)

        if data is None:
            print("  请求失败, 等待重试...")
            time.sleep(3)
            continue

        status_code = data.get("status_code", -1)
        print(f"  status_code={status_code}")

        if status_code != 0:
            print(f"  API 错误: {data.get('status_msg', '未知')}")
            if status_code == 8:
                print("\n⚠️  登录已失效，请更新 config.py 中的 sessionid")
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
            author_id = upsert_author(
                conn,
                sec_uid=parsed["author"]["sec_uid"],
                nickname=parsed["author"]["nickname"],
                avatar=parsed["author"]["avatar"],
            )
            is_new = upsert_video(conn, parsed, author_id, source=source)
            if is_new:
                new_count += 1
            else:
                update_count += 1
            last_liked_time = parsed["liked_time"] or parsed["create_time"]

        conn.commit()

        print(f"  +{len(aweme_list)} 条 (新增{new_count}, 更新{update_count})")
        for aweme in aweme_list[:3]:
            parsed = parse_aweme(aweme)
            tags = ", ".join([t['tag_name'] for t in parsed['video_tags']]) or "无标签"
            print(f"    [{parsed['create_time']}] [{tags}] {parsed['author']['nickname']}")
            print(f"       {parsed['desc'][:50]} | {parsed['duration_human']} | 👍{parsed['stats']['digg']} 💬{parsed['stats']['comment']}")
        if len(aweme_list) > 3:
            print(f"    ... +{len(aweme_list) - 3} 条")

        if new_cursor != max_cursor:
            max_cursor = new_cursor
        else:
            if has_more:
                max_cursor += 1

        update_bookmark(conn, source, max_cursor, last_liked_time, new_count)
        conn.commit()

        delay = random.uniform(1.0, 2.0)
        time.sleep(delay)

        if page_num >= 200:
            print("\n已达到最大页数限制 (200 页)")
            break

    conn.commit()
    total_videos = get_video_count(conn, source=source)
    total_authors = get_author_count(conn)
    dl_stats = get_download_stats(conn, source=source)

    print("\n" + "=" * 60)
    print(f"抓取完成! 本轮新增 {new_count} 条, 更新 {update_count} 条")
    print(f"点赞视频: {total_videos} 条, 作者: {total_authors} 位")
    print("=" * 60)

    pending = dl_stats.get("pending", 0) or 0
    done = dl_stats.get("done", 0) or 0
    failed = dl_stats.get("failed", 0) or 0
    print(f"\n📥 下载状态 [点赞]:")
    print(f"  ⬜ 未下载: {pending}")
    print(f"  ✅ 已下载: {done}")
    print(f"  ❌ 失败:   {failed}")

    print(f"\n📊 标签分布 [点赞] (Top 20):")
    for tag_name, count in get_tag_distribution(conn, source=source, limit=20):
        print(f"  {tag_name}: {count} 条")

    print(f"\n📋 最新入库 10 条 [点赞]:")
    print("-" * 70)
    rows = conn.execute("""
        SELECT v.aweme_id, v.title, v.create_time, v.type, v.duration_sec,
               v.video_tags, a.nickname, v.stats, v.in_likes, v.in_favorites
        FROM videos v JOIN authors a ON v.author_id = a.id
        WHERE v.in_likes=1
        ORDER BY v.added_at DESC, v.id DESC
        LIMIT 10
    """).fetchall()
    for i, r in enumerate(rows, 1):
        tags = ", ".join([t["tag_name"] for t in json.loads(r["video_tags"])]) or "无标签"
        stats = json.loads(r["stats"])
        desc = (r["title"] or "")[:40]
        dur = r["duration_sec"]
        dur_str = f"{dur // 60}:{dur % 60:02d}" if dur > 60 else f"{dur}秒"
        src_tags = []
        if r["in_likes"]: src_tags.append("👍")
        if r["in_favorites"]: src_tags.append("⭐")
        print(f"{i}. [{''.join(src_tags)}] [{r['create_time']}] [{tags}] {r['nickname']}")
        print(f"   {desc} | {dur_str} | 👍{stats.get('digg',0)} 💬{stats.get('comment',0)}")

    conn.close()
    print(f"\n数据库路径: E:\\xn\\ai_xm\\DY_huoqu_ag\\two\\data\\likes.db")


if __name__ == "__main__":
    main()
