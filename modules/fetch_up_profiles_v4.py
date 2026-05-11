"""
UP主资料抓取工具 v4（通过视频详情 API）
直接写入 v4 authors_base + authors_stats 表

与 v3 的区别：
  - 使用 upsert_author_base + upsert_author_stats 替代 update_author_profile
  - 查询待抓取作者时使用 v4 authors_stats 表
  - 视频详情 API 交互层不变

用法:
  dytool_v4.py fetch profiles           # 抓取缺少资料的UP主
  dytool_v4.py fetch profiles --force   # 强制全部重新抓取
  dytool_v4.py fetch profiles -n 50     # 最多50个
  dytool_v4.py fetch profiles -m 3      # 只抓>=3条被赞视频的UP主
  dytool_v4.py fetch profiles -d        # 预览
"""

import os
import sys
import json
import time
import random
import argparse
import io
from datetime import datetime
from urllib.parse import urlencode, quote

import requests

from .db_v4 import (
    get_conn_v4, init_db_v4,
    upsert_author_base, upsert_author_stats,
    get_author_count,
)
from .signer import Signer
from .meta import load_cookie, get_cookie_path, get_db_path, get_data_root

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Referer": "https://www.douyin.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": "https://www.douyin.com",
    "Connection": "keep-alive",
}

DETAIL_URL = "https://www.douyin.com/aweme/v1/web/aweme/detail/"

_COOKIE_EXPIRED_MSG_KEYS = ["登录", "cookie", "token", "auth", "credential", "失效", "过期"]


def _get_v4_db_path() -> str:
    return os.path.join(get_data_root(), "douku_v4.db")


def _is_auth_error_msg(msg: str) -> bool:
    msg_lower = msg.lower()
    return any(k in msg_lower or k in msg for k in _COOKIE_EXPIRED_MSG_KEYS)


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


def get_a_bogus(params_dict, signer):
    try:
        a_bogus = signer.get_a_bogus(params_dict)
        return quote(a_bogus, safe='')
    except RuntimeError:
        return ""


def fetch_author_via_detail(aweme_id, signer, cookies, headers):
    """通过视频详情API获取作者资料，返回 (profile_dict, error)"""
    params = build_detail_params(aweme_id)
    a_bogus = get_a_bogus(params, signer)
    if a_bogus:
        params["a_bogus"] = a_bogus
    url = DETAIL_URL + "?" + urlencode(params)

    try:
        resp = requests.get(url, headers=headers, cookies=cookies, timeout=20)
        if resp.status_code == 401 or resp.status_code == 403:
            return None, "COOKIE_EXPIRED"
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"

        try:
            data = resp.json()
        except json.JSONDecodeError:
            return None, "非JSON响应(可能限流)"

        status_code = data.get("status_code", -1)
        if status_code != 0:
            msg = data.get('status_msg', '未知')
            if status_code == 8 or status_code == 210 or _is_auth_error_msg(msg):
                return None, "COOKIE_EXPIRED"
            return None, f"API: {msg}"

        aweme = data.get("aweme_detail") or {}
        author = aweme.get("author") or {}
        if not author:
            return None, "author字段为空"

        profile = {
            "sec_uid": author.get("sec_uid", ""),
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
    """查询需要抓取资料的UP主（v4: 使用 authors_stats 表）"""
    if force:
        where = "1=1"
    else:
        where = "COALESCE(s.follower_count, 0) = 0"

    sql = f"""
        SELECT b.sec_uid, b.nickname,
               COALESCE(s.follower_count, 0) as follower_count,
               COUNT(v.aweme_id) as video_count,
               MIN(v.aweme_id) as sample_aweme_id
        FROM authors_base b
        LEFT JOIN authors_stats s ON b.sec_uid = s.sec_uid
        JOIN videos_base v ON v.author_sec_uid = b.sec_uid
        WHERE {where}
        GROUP BY b.sec_uid
        HAVING COUNT(v.aweme_id) >= ?
        ORDER BY video_count DESC
    """
    params = [min_videos]

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


# ============================================================
# v4 存储层
# ============================================================

def save_author_to_v4(conn, sec_uid: str, profile: dict) -> None:
    """将作者资料写入 v4（base + stats）"""
    # 基础信息
    upsert_author_base(conn, {
        "sec_uid": sec_uid,
        "nickname": profile.get("nickname", ""),
        "avatar": profile.get("avatar", ""),
        "signature": profile.get("signature", ""),
        "ip_location": profile.get("ip_location", ""),
        "verification_type": profile.get("verification_type", 0),
        "verification_label": profile.get("verification_label", ""),
        "is_gov_media_vip": profile.get("is_gov_media_vip", False),
    })

    # 统计数据
    upsert_author_stats(conn, {
        "sec_uid": sec_uid,
        "follower_count": profile.get("follower_count", 0),
        "following_count": profile.get("following_count", 0),
        "aweme_count": profile.get("aweme_count", 0),
        "favoriting_count": profile.get("favoriting_count", 0),
    })


# ============================================================
# 主函数
# ============================================================

def run(force=False, limit=0, min_videos=0, dry_run=False):
    """主入口"""
    cookie_data = load_cookie()
    cookies = {
        "sessionid": cookie_data.get("sessionid", ""),
        "sid_tt": cookie_data.get("sessionid", ""),
        "ttwid": cookie_data.get("ttwid", ""),
    }
    headers = HEADERS.copy()
    signer = Signer(headers["User-Agent"])

    db_path = _get_v4_db_path()
    init_db_v4(db_path)
    conn = get_conn_v4(db_path)

    authors = query_authors_needing_profile(conn, min_videos=min_videos, force=force, limit=limit)

    if not authors:
        print("所有UP主资料已抓取，无需更新")
        conn.close()
        return {"success": 0, "failed": 0, "total": 0}

    print("=" * 60)
    print("UP主资料抓取 (v4 · 多表分层)")
    print("=" * 60)
    print(f"待抓取: {len(authors)} 位UP主")
    if force:
        print("模式: 强制全部重新抓取")
    if min_videos:
        print(f"筛选: 至少 {min_videos} 条被点赞视频")
    if limit:
        print(f"上限: {limit}")
    print()

    if dry_run:
        print("[预览模式]\n")
        for i, a in enumerate(authors[:30], 1):
            status = "已抓取" if a["follower_count"] > 0 else "未抓取"
            print(f"  {i}. [{status}] {a['nickname']} ({a['video_count']}条) aweme={a['sample_aweme_id']}")
        if len(authors) > 30:
            print(f"  ... 还有 {len(authors) - 30} 位")
        conn.close()
        return {"success": 0, "failed": 0, "total": len(authors)}

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

        profile, err = fetch_author_via_detail(aweme_id, signer, cookies, headers)

        if profile is not None:
            save_author_to_v4(conn, sec_uid, profile)
            conn.commit()
            success += 1
            fc = format_count(profile["follower_count"])
            sig = profile["signature"][:30] if profile["signature"] else "无简介"
            ip = profile["ip_location"] or ""
            print(f"  OK 粉丝:{fc} 作品:{profile['aweme_count']} {ip} {sig}")
        else:
            if err == "COOKIE_EXPIRED":
                print("\nCookie 已过期！请运行: dytool.py cookie")
                conn.close()
                return {"error": "cookie_expired", "success": success, "failed": api_fail}
            if "限流" in err:
                rate_limit += 1
                print(f"  限流: {err} — 等待5秒...")
                time.sleep(5)
                continue
            elif "API" in err or "HTTP" in err:
                api_fail += 1
            else:
                not_found += 1
            print(f"  ERR {err}")

        delay = random.uniform(1.0, 2.0)
        time.sleep(delay)

        if i % 50 == 0:
            conn.commit()

    conn.commit()

    print()
    print("=" * 60)
    print(f"抓取完成!")
    print(f"  成功: {success}")
    print(f"  未找到: {not_found}")
    print(f"  API失败: {api_fail}")
    if rate_limit:
        print(f"  限流: {rate_limit}")
    print("=" * 60)

    conn.close()
    return {"success": success, "failed": api_fail, "total": len(authors)}


# ============================================================
# CLI 入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="UP主资料抓取工具 v4")
    parser.add_argument("--force", "-f", action="store_true", help="强制重新抓取所有UP主")
    parser.add_argument("--limit", "-n", type=int, default=0, help="最多抓取N个UP主")
    parser.add_argument("--min-videos", "-m", type=int, default=0, help="只抓>=N条视频的UP主")
    parser.add_argument("--dry-run", "-d", action="store_true", help="预览")
    parser.add_argument("--data-dir", type=str, help="数据目录")
    args = parser.parse_args()

    if args.data_dir:
        from .meta import set_data_dir
        set_data_dir(args.data_dir)

    try:
        result = run(
            force=args.force, limit=args.limit,
            min_videos=args.min_videos, dry_run=args.dry_run
        )
        if "error" not in result:
            print(f"\n完成: 成功={result['success']}, 失败={result['failed']}")
    except Exception as e:
        print(f"\n错误: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
