"""
UP主资料抓取工具（通过视频详情 API）
抖音的用户资料 API 已失效（404），改用视频详情 API 间接获取：
  每个UP主取一条视频 → 调 detail API → 返回的 author 字段含粉丝数等

用法:
  python fetch_up_profiles.py                 # 抓取所有缺少详细资料的UP主
  python fetch_up_profiles.py --force         # 强制全部重新抓取
  python fetch_up_profiles.py --limit 50      # 最多50个
  python fetch_up_profiles.py --min-videos 3  # 只抓取有3条以上被点赞视频的UP主
  python fetch_up_profiles.py --dry-run       # 预览
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
    init_db, get_conn, update_author_profile, get_author_count,
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

DETAIL_URL = "https://www.douyin.com/aweme/v1/web/aweme/detail/"


def build_detail_params(aweme_id):
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


def fetch_author_via_detail(aweme_id):
    """通过视频详情API获取作者资料，返回 (profile_dict, message)"""
    params = build_detail_params(aweme_id)
    a_bogus = get_a_bogus(params)
    params["a_bogus"] = a_bogus
    url = DETAIL_URL + "?" + urlencode(params)

    try:
        resp = requests.get(url, headers=HEADERS, cookies=COOKIES, timeout=20)
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"

        try:
            data = resp.json()
        except json.JSONDecodeError:
            return None, "非JSON响应(可能限流)"

        if data.get("status_code") != 0:
            return None, f"API: {data.get('status_msg', '未知')}"

        aweme = data.get("aweme_detail") or {}
        author = aweme.get("author") or {}
        if not author:
            return None, "author字段为空"

        # 提取作者资料
        profile = {
            "nickname": author.get("nickname", ""),
            "avatar": "",
            "signature": author.get("signature", ""),
            "ip_location": author.get("ip_location", ""),
            "follower_count": author.get("follower_count", 0) or 0,
            "following_count": author.get("following_count", 0) or 0,
            "aweme_count": author.get("aweme_count", 0) or 0,
            "favoriting_count": author.get("favoriting_count", 0) or 0,
            "verification_type": 0,
            "verification_label": "",
            "is_gov_media_vip": bool(author.get("is_gov_media_vip")),
        }

        # 头像
        avatar_larger = author.get("avatar_larger") or {}
        avatar_thumb = author.get("avatar_thumb") or {}
        if avatar_larger.get("url_list"):
            profile["avatar"] = avatar_larger["url_list"][0]
        elif avatar_thumb.get("url_list"):
            profile["avatar"] = avatar_thumb["url_list"][0]

        # 认证
        verify_enterprise = author.get("enterprise_verify_reason") or ""
        verify_custom = author.get("custom_verify") or ""
        if verify_enterprise:
            profile["verification_type"] = 2
            profile["verification_label"] = verify_enterprise
        elif verify_custom:
            profile["verification_type"] = 1
            profile["verification_label"] = verify_custom

        return profile, "OK"

    except requests.exceptions.Timeout:
        return None, "超时"
    except Exception as e:
        return None, str(e)[:80]


def query_authors_needing_profile(conn, min_videos=0, force=False, limit=0):
    """查询需要抓取资料的UP主，同时返回每个UP主的一条视频aweme_id"""
    if force:
        where = "1=1"
    else:
        where = "a.follower_count = 0"

    sql = f"""
        SELECT a.id, a.sec_uid, a.nickname, a.follower_count,
               COUNT(v.id) as video_count,
               MIN(v.aweme_id) as sample_aweme_id
        FROM authors a
        JOIN videos v ON v.author_id = a.id
        WHERE {where}
        GROUP BY a.id
        HAVING 1=1
    """
    params = []

    if min_videos > 0:
        sql += f" AND video_count >= {min_videos}"

    sql += " ORDER BY video_count DESC"

    if limit > 0:
        sql += f" LIMIT {limit}"

    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def format_count(n):
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    elif n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def main():
    parser = argparse.ArgumentParser(description="UP主资料抓取工具 (通过视频详情API)")
    parser.add_argument("--force", "-f", action="store_true",
                        help="强制重新抓取所有UP主")
    parser.add_argument("--limit", "-n", type=int, default=0,
                        help="最多抓取N个UP主 (0=不限)")
    parser.add_argument("--min-videos", "-m", type=int, default=0,
                        help="只抓取有N条以上被点赞视频的UP主")
    parser.add_argument("--dry-run", "-d", action="store_true",
                        help="只预览不实际抓取")
    args = parser.parse_args()

    init_db()
    conn = get_conn()

    authors = query_authors_needing_profile(
        conn,
        min_videos=args.min_videos,
        force=args.force,
        limit=args.limit,
    )

    if not authors:
        print("所有UP主资料已抓取，无需更新")
        conn.close()
        return

    print("=" * 60)
    print("👤 UP主资料抓取 (通过视频详情API)")
    print("=" * 60)
    print(f"待抓取: {len(authors)} 位UP主")
    if args.force:
        print("模式: 强制全部重新抓取")
    if args.min_videos:
        print(f"筛选: 至少 {args.min_videos} 条被点赞视频")
    if args.limit:
        print(f"上限: {args.limit}")
    print()

    if args.dry_run:
        print("[预览模式]\n")
        for i, a in enumerate(authors[:30], 1):
            status = "✅已抓取" if a["follower_count"] > 0 else "⬜未抓取"
            print(f"  {i}. [{status}] {a['nickname']} ({a['video_count']}条) aweme={a['sample_aweme_id']}")
        if len(authors) > 30:
            print(f"  ... 还有 {len(authors) - 30} 位")
        conn.close()
        return

    # 实际抓取
    success = 0
    not_found = 0
    api_fail = 0
    rate_limit = 0

    for i, a in enumerate(authors, 1):
        sec_uid = a["sec_uid"]
        nickname = a["nickname"]
        vc = a["video_count"]
        aweme_id = a["sample_aweme_id"]

        print(f"[{i}/{len(authors)}] {nickname} ({vc}条视频, aweme={aweme_id})")

        profile, err = fetch_author_via_detail(aweme_id)

        if profile is not None:
            update_author_profile(conn, sec_uid, profile)
            conn.commit()
            success += 1
            fc = format_count(profile["follower_count"])
            sig = profile["signature"][:30] if profile["signature"] else "无简介"
            ip = profile["ip_location"] or ""
            print(f"  ✅ 粉丝:{fc} 作品:{profile['aweme_count']} {ip} {sig}")
        else:
            if "限流" in err:
                rate_limit += 1
                print(f"  ⚠️  {err} — 等待5秒...")
                time.sleep(5)
                continue
            elif "API" in err or "HTTP" in err:
                api_fail += 1
            else:
                not_found += 1
            print(f"  ❌ {err}")

        # 请求间隔
        delay = random.uniform(1.0, 2.0)
        time.sleep(delay)

        # 每50个提交
        if i % 50 == 0:
            conn.commit()

    conn.commit()

    print("\n" + "=" * 60)
    print(f"抓取完成!")
    print(f"  ✅ 成功: {success}")
    print(f"  ⚠️  未找到: {not_found}")
    print(f"  ❌ API失败: {api_fail}")
    if rate_limit:
        print(f"  🔒 限流: {rate_limit}")
    print("=" * 60)

    conn.close()


if __name__ == "__main__":
    main()
