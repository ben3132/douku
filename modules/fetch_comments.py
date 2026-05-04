"""
视频热评抓取工具
调用抖音评论 API，抓取视频热门评论存入 comments 表

用法:
  python fetch_comments.py                       # 抓取所有未抓评论的视频
  python fetch_comments.py --limit 50            # 最多50个视频
  python fetch_comments.py --aweme_id XXX        # 抓指定视频
  python fetch_comments.py --pages 3             # 每个视频抓3页评论(约60条)
  python fetch_comments.py --force               # 重新抓取(包括已抓过的)
  python fetch_comments.py --hot-only            # 只抓热评(每视频约20条)
  python fetch_comments.py --min-digg 1000       # 只抓点赞数>1000的视频
"""

import os
import sys
import json
import time
import random
import argparse
import requests
import io
from datetime import datetime
from urllib.parse import urlencode, quote

if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from abogus import ABogus
from db_utils import (
    init_db, get_conn, insert_comments, get_video_count,
)

try:
    from config import SESSION_ID, TTWID
except ImportError:
    print("\n❌ 配置文件不存在！请先复制 config.example.py 为 config.py 并填写")
    sys.exit(1)

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
    "ttwid": TTWID,
}

COMMENT_URL = "https://www.douyin.com/aweme/v1/web/comment/list/"


def build_comment_params(aweme_id, cursor="0"):
    return {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "aweme_id": aweme_id,
        "cursor": str(cursor),
        "item_type": "0",
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
        "whale_cut_token": "",
        "cut_version": "1",
        "count": "20",
        "comment_style": "2",
    }


def get_a_bogus(params_dict):
    bogus = ABogus()
    params_str = urlencode(params_dict)
    a_bogus = bogus.get_value(params_str, method="GET")
    return quote(a_bogus, safe='')


def fetch_and_store_comments(conn, aweme_id, max_pages=1):
    """抓取并存储单个视频的评论，返回 (新增评论数, 错误信息)"""
    all_comments = []
    cursor = "0"
    err = None

    for page in range(max_pages):
        params = build_comment_params(aweme_id, cursor)
        a_bogus = get_a_bogus(params)
        params["a_bogus"] = a_bogus
        url = COMMENT_URL + "?" + urlencode(params)

        try:
            resp = requests.get(url, headers=HEADERS, cookies=COOKIES, timeout=20)
            if resp.status_code != 200:
                err = f"HTTP {resp.status_code}"
                break

            try:
                data = resp.json()
            except json.JSONDecodeError:
                err = "非JSON(可能限流)"
                break

            if data.get("status_code") != 0:
                err = f"API: {data.get('status_msg', '未知')}"
                break

            comments = data.get("comments") or []
            all_comments.extend(comments)

            has_more = data.get("has_more", 0)
            next_cursor = str(data.get("cursor", 0))

            if not has_more:
                break

            cursor = next_cursor
            time.sleep(random.uniform(0.5, 1.0))

        except requests.exceptions.Timeout:
            err = "超时"
            break
        except Exception as e:
            err = str(e)[:80]
            break

    # 存入数据库
    new_count = 0
    if all_comments:
        new_count = insert_comments(conn, aweme_id, all_comments)
        # 标记已抓取
        conn.execute("UPDATE videos SET comment_fetched=1 WHERE aweme_id=?", (aweme_id,))
        conn.commit()

    return new_count, err


def query_videos_needing_comments(conn, min_digg=0, force=False, limit=0, aweme_id=None):
    """查询需要抓评论的视频"""
    if aweme_id:
        return [{"aweme_id": aweme_id, "title": "", "digg": 0}]

    where = "1=1" if force else "comment_fetched = 0"

    sql = f"""
        SELECT v.aweme_id,
               COALESCE(json_extract(v.stats, '$.digg'), 0) as digg,
               COALESCE(v.title, v.desc, '') as title
        FROM videos v
        WHERE {where}
    """

    if min_digg > 0:
        sql += f" AND json_extract(v.stats, '$.digg') >= {min_digg}"

    # 优先抓高赞视频
    sql += " ORDER BY json_extract(v.stats, '$.digg') DESC"

    if limit > 0:
        sql += f" LIMIT {limit}"

    rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def format_count(n):
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    elif n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def main():
    parser = argparse.ArgumentParser(description="视频热评抓取工具")
    parser.add_argument("--aweme_id", "-a", help="抓取指定视频的评论")
    parser.add_argument("--force", "-f", action="store_true",
                        help="重新抓取(包括已抓过的)")
    parser.add_argument("--limit", "-n", type=int, default=0,
                        help="最多抓取N个视频 (0=不限)")
    parser.add_argument("--pages", "-p", type=int, default=1,
                        help="每个视频抓几页评论 (默认1页≈20条)")
    parser.add_argument("--hot-only", action="store_true",
                        help="仅热评模式(同 --pages 1)")
    parser.add_argument("--min-digg", "-d", type=int, default=0,
                        help="只抓视频点赞数>N的视频的评论")
    args = parser.parse_args()

    if args.hot_only:
        args.pages = 1

    init_db()
    conn = get_conn()

    videos = query_videos_needing_comments(
        conn,
        min_digg=args.min_digg,
        force=args.force,
        limit=args.limit,
        aweme_id=args.aweme_id,
    )

    if not videos:
        print("所有视频评论已抓取，无需更新")
        conn.close()
        return

    print("=" * 60)
    print("💬 视频热评抓取工具")
    print("=" * 60)
    print(f"待抓取: {len(videos)} 个视频")
    print(f"每视频: {args.pages} 页 (约{args.pages * 20}条评论)")
    if args.min_digg:
        print(f"筛选: 视频点赞 ≥ {args.min_digg}")
    print()

    success = 0
    total_new = 0
    api_fail = 0
    rate_limit = 0

    for i, v in enumerate(videos, 1):
        aweme_id = v["aweme_id"]
        digg = v.get("digg", 0)
        title = (v.get("title") or "")[:20]

        print(f"[{i}/{len(videos)}] {aweme_id} (赞:{format_count(digg)}) {title}")

        new_count, err = fetch_and_store_comments(conn, aweme_id, max_pages=args.pages)

        if err and new_count == 0:
            if "限流" in err:
                rate_limit += 1
                print(f"  ⚠️  {err} — 等待8秒...")
                time.sleep(8)
                continue
            api_fail += 1
            print(f"  ❌ {err}")
        else:
            success += 1
            total_new += new_count
            status = f"✅ +{new_count} 条"
            if err:
                status += f" (部分失败: {err})"
            print(f"  {status}")

        # 请求间隔
        delay = random.uniform(1.0, 2.0)
        time.sleep(delay)

        # 每20个提交
        if i % 20 == 0:
            conn.commit()

    conn.commit()

    print("\n" + "=" * 60)
    print(f"抓取完成!")
    print(f"  ✅ 成功: {success}")
    print(f"  💬 新增评论: {total_new}")
    print(f"  ❌ API失败: {api_fail}")
    if rate_limit:
        print(f"  🔒 限流: {rate_limit}")
    print("=" * 60)

    conn.close()


if __name__ == "__main__":
    main()
