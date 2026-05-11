"""
视频热评抓取工具 v4
调用抖音评论 API，抓取视频热门评论存入 v4 comments 表

与 v3 的区别：
  - 使用 upsert_comment 替代 insert_comments
  - 查询待抓评论视频时使用 v4 videos_base + videos_stats 表
  - 评论抓取完成后更新 v4 状态

用法:
  dytool_v4.py fetch comments              # 抓取所有未抓评论的视频
  dytool_v4.py fetch comments -n 50        # 最多50个视频
  dytool_v4.py fetch comments -a AWEME_ID  # 抓指定视频
  dytool_v4.py fetch comments -p 3         # 每视频抓3页评论(约60条)
  dytool_v4.py fetch comments --force      # 重新抓取(含已抓过的)
  dytool_v4.py fetch comments --min-digg 1000  # 只抓点赞数>1000的视频
"""

import json
import time
import random
import os
import urllib.parse
from datetime import datetime

import requests

from .db_v4 import (
    get_conn_v4, init_db_v4,
    upsert_comment,
)
from .meta import get_cookie_path, get_data_root, load_cookie
from .signer import sign_request

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Referer": "https://www.douyin.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": "https://www.douyin.com",
    "Connection": "keep-alive",
}

COMMENT_API = "https://www.douyin.com/aweme/v1/web/comment/list/"

_COOKIE_EXPIRED_MSG_KEYS = ["登录", "cookie", "token", "auth", "credential", "失效", "过期"]


def _get_v4_db_path() -> str:
    return os.path.join(get_data_root(), "douku_v4.db")


def _is_auth_error_msg(msg: str) -> bool:
    msg_lower = msg.lower()
    return any(k in msg_lower or k in msg for k in _COOKIE_EXPIRED_MSG_KEYS)


def build_params(aweme_id: str, cursor: str = "0", count: int = 20) -> dict:
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
        "count": str(count),
        "comment_style": "2",
    }


def fetch_comments_page(aweme_id: str, cookies: dict, cursor: str = "0", user_agent: str = None) -> tuple:
    """抓取一页评论，返回 (comments_list, has_more, next_cursor, error)"""
    params = build_params(aweme_id, cursor)
    raw_url = COMMENT_API + "?" + urllib.parse.urlencode(params)
    ua = user_agent or HEADERS["User-Agent"]
    signed_url = sign_request(raw_url, ua, sign_type="a")

    try:
        resp = requests.get(signed_url, headers=HEADERS, cookies=cookies, timeout=20)
    except requests.exceptions.Timeout:
        return [], 0, "0", "超时"
    except requests.exceptions.RequestException as e:
        return [], 0, "0", str(e)[:80]

    if resp.status_code == 401 or resp.status_code == 403:
        return [], 0, "0", "COOKIE_EXPIRED"

    if resp.status_code != 200:
        return [], 0, "0", f"HTTP {resp.status_code}"

    try:
        data = resp.json()
    except json.JSONDecodeError:
        return [], 0, "0", "非JSON响应(可能限流)"

    if data.get("status_code") != 0:
        code = data.get("status_code", -1)
        msg = data.get("status_msg", "未知")
        if code == 8 or code == 210 or _is_auth_error_msg(msg):
            return [], 0, "0", "COOKIE_EXPIRED"
        return [], 0, "0", f"API: {msg}"

    comments = data.get("comments") or []
    has_more = data.get("has_more", 0)
    next_cursor = str(data.get("cursor", 0))

    return comments, has_more, next_cursor, None


def fetch_and_store(conn, aweme_id: str, cookies: dict, max_pages: int = 1, user_agent: str = None) -> tuple:
    """抓取并存储单个视频的评论到 v4 comments 表，返回 (新增评论数, 错误信息)"""
    all_comments = []
    cursor = "0"

    for _ in range(max_pages):
        comments, has_more, next_cursor, err = fetch_comments_page(aweme_id, cookies, cursor, user_agent)

        if err:
            if not all_comments:
                return 0, err
            break

        all_comments.extend(comments)

        if not has_more:
            break

        cursor = next_cursor
        time.sleep(random.uniform(0.5, 1.0))

    new_count = 0
    if all_comments:
        for c in all_comments:
            upsert_comment(conn, {
                "aweme_id": aweme_id,
                "cid": str(c.get("cid", "")),
                "content": c.get("text", ""),
                "user_name": c.get("user", {}).get("nickname", "匿名"),
                "digg_count": c.get("digg_count", 0) or 0,
                "reply_count": c.get("reply_comment_total", 0) or 0,
                "is_hot": 1 if c.get("is_hot", False) else 0,
                "create_time": datetime.fromtimestamp(c.get("create_time", 0)).strftime("%Y-%m-%d %H:%M") if c.get("create_time") else "",
                "ip_location": c.get("ip_label", "") or "",
            })
            new_count += 1

        conn.execute(
            "INSERT INTO videos_download (aweme_id, status, updated_at) VALUES (?, 99, datetime('now')) ON CONFLICT(aweme_id) DO NOTHING",
            (aweme_id,)
        )
        conn.commit()

    return new_count, None


def query_videos_needing_comments(conn, min_digg: int = 0, force: bool = False,
                                   limit: int = 0, aweme_id: str = None) -> list:
    """查询需要抓评论的视频列表（v4: 使用 videos_base + videos_stats）"""
    if aweme_id:
        return [{"aweme_id": aweme_id, "title": "", "digg": 0}]

    if force:
        sql = """
            SELECT vb.aweme_id,
                   COALESCE(vs.digg_count, 0) as digg,
                   COALESCE(vb.title, vb.desc, '') as title
            FROM videos_base vb
            LEFT JOIN videos_stats vs ON vb.aweme_id = vs.aweme_id
            WHERE 1=1
        """
    else:
        # 没有评论的视频 = comments 表中不存在的 aweme_id
        sql = """
            SELECT vb.aweme_id,
                   COALESCE(vs.digg_count, 0) as digg,
                   COALESCE(vb.title, vb.desc, '') as title
            FROM videos_base vb
            LEFT JOIN videos_stats vs ON vb.aweme_id = vs.aweme_id
            WHERE vb.aweme_id NOT IN (SELECT DISTINCT aweme_id FROM comments)
        """

    if min_digg > 0:
        sql += f" AND COALESCE(vs.digg_count, 0) >= {min_digg}"

    sql += " ORDER BY digg DESC"

    if limit > 0:
        sql += f" LIMIT {limit}"

    return [dict(r) for r in conn.execute(sql).fetchall()]


def _load_config() -> dict:
    cookie = load_cookie()
    if not cookie:
        return {"error": "Cookie 未设置，请运行 dytool.py cookie"}
    return {"SESSION_ID": cookie.get("sessionid", ""), "TTWID": cookie.get("ttwid", "")}


def run(aweme_id: str = None, min_digg: int = 0, force: bool = False,
        limit: int = 0, max_pages: int = 1) -> dict:
    """主入口，返回统计结果"""
    config = _load_config()
    session_id = config.get("SESSION_ID", "") or config.get("session_id", "")
    ttwid = config.get("TTWID", "") or config.get("ttwid", "")
    user_agent = config.get("USER_AGENT", "") or HEADERS["User-Agent"]

    cookies = {
        "sessionid": session_id,
        "sid_tt": session_id,
        "ttwid": ttwid,
    }

    if not session_id:
        return {"error": "config 中未设置 SESSION_ID"}

    db_path = _get_v4_db_path()
    init_db_v4(db_path)
    conn = get_conn_v4(db_path)

    videos = query_videos_needing_comments(conn, min_digg, force, limit, aweme_id)
    if not videos:
        conn.close()
        return {"success": 0, "new_comments": 0, "api_fail": 0, "skipped": 0}

    success = 0
    total_new = 0
    api_fail = 0
    skipped = 0

    for i, v in enumerate(videos, 1):
        aweme_id = v["aweme_id"]
        digg = v.get("digg", 0)
        title = (v.get("title") or "")[:20]

        print(f"[{i}/{len(videos)}] {aweme_id} (赞:{_fmt(digg)}) {title}")

        new_count, err = fetch_and_store(conn, aweme_id, cookies, max_pages, user_agent)

        if err and new_count == 0:
            if err == "COOKIE_EXPIRED":
                print("\nCookie 已过期！请运行: dytool.py cookie")
                conn.close()
                return {"error": "cookie_expired", "success": success, "new_comments": total_new}
            if "限流" in err:
                print(f"  [限流] 等待8秒...")
                time.sleep(8)
                skipped += 1
                continue
            api_fail += 1
            print(f"  [失败] {err}")
        else:
            success += 1
            total_new += new_count
            print(f"  [成功] +{new_count} 条")

        time.sleep(random.uniform(1.0, 2.0))

        if i % 20 == 0:
            conn.commit()

    conn.commit()
    conn.close()

    return {
        "success": success,
        "new_comments": total_new,
        "api_fail": api_fail,
        "skipped": skipped,
    }


def _fmt(n: int) -> str:
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    elif n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)
