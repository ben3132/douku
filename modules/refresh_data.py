"""
数据分层刷新工具
按数据变动频率分层刷新，保持数据新鲜度

分层策略:
  Tier 1 (最高频): 下载URL — 几小时过期，每次下载前刷新 (由 refresh_urls.py / download_videos.py 处理)
  Tier 2 (中频):   视频动态数据(stats/tags/comments) — 天级变动，用户触发或周期刷新
  Tier 3 (最低频): UP主画像(粉丝数/签名等) — 周级变动

用法:
  python refresh_data.py --tier 2              # 刷新Tier2（视频动态数据：stats+tags+评论）
  python refresh_data.py --tier 3              # 刷新Tier3（UP主画像）
  python refresh_data.py --all                 # 全部刷新
  python refresh_data.py --tier 2 --days 7     # 只刷新7天前更新过的视频
  python refresh_data.py --tier 2 --force      # 忽略时间限制，强制全部刷新
  python refresh_data.py --video AWEME_ID      # 指定视频刷新
  python refresh_data.py --author SEC_UID      # 指定UP主刷新
  python refresh_data.py --tier 2 --no-comments  # Tier2但跳过评论刷新
  python refresh_data.py --tier 2 --limit 50   # 最多刷新50条
  python refresh_data.py --dry-run             # 预览模式
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
    init_db, get_conn, get_video_count, get_author_count,
    upsert_author, update_video_stats_and_tags, update_video_data_refreshed,
    update_author_profile, insert_comments, replace_comments_for_video,
    update_video_comment_tags, query_videos_needing_refresh,
    query_authors_needing_refresh, VALID_SOURCES,
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
COMMENT_URL = "https://www.douyin.com/aweme/v1/web/comment/list/"

# 默认刷新间隔（天）
DEFAULT_TIER2_DAYS = 7   # 视频动态数据：7天刷新一次
DEFAULT_TIER3_DAYS = 30  # UP主画像：30天刷新一次


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


def build_comment_params(aweme_id, cursor="0"):
    """构建评论 API 参数"""
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


def fetch_comments_page(aweme_id, cursor="0"):
    """获取一页评论，返回 (comments_list, next_cursor, has_more, error)"""
    params = build_comment_params(aweme_id, cursor)
    a_bogus = get_a_bogus(params)
    params["a_bogus"] = a_bogus
    url = COMMENT_URL + "?" + urlencode(params)

    try:
        resp = requests.get(url, headers=HEADERS, cookies=COOKIES, timeout=20)
        if resp.status_code != 200:
            return [], "0", 0, f"HTTP {resp.status_code}"

        try:
            data = resp.json()
        except json.JSONDecodeError:
            return [], "0", 0, "非JSON(可能限流)"

        if data.get("status_code") != 0:
            return [], "0", 0, f"API: {data.get('status_msg', '未知')}"

        comments = data.get("comments") or []
        has_more = data.get("has_more", 0)
        next_cursor = str(data.get("cursor", 0))
        return comments, next_cursor, has_more, None

    except requests.exceptions.Timeout:
        return [], "0", 0, "超时"
    except Exception as e:
        return [], "0", 0, str(e)[:80]


def parse_aweme(aweme):
    """解析视频详情 — 与 fetch_likes_db.py / refresh_urls.py 保持一致"""
    import re

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
        "liked_time": "",
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
        },
        "is_top": bool(is_top),
        "prevent_download": bool(prevent_download),
    }


def format_count(n):
    """格式化数字"""
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    elif n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


# ============================================================
# Tier 2: 视频动态数据刷新
# ============================================================

def refresh_tier2_video(conn, aweme_id, refresh_comments=True, comment_pages=1):
    """刷新单个视频的Tier2数据（stats/tags/评论），返回 (success, message)"""
    aweme, err = fetch_detail(aweme_id)
    if aweme is None:
        return False, f"详情API: {err}"

    parsed = parse_aweme(aweme)

    # 更新作者基本信息
    author_id = upsert_author(
        conn,
        sec_uid=parsed["author"]["sec_uid"],
        nickname=parsed["author"]["nickname"],
        avatar=parsed["author"]["avatar"],
    )

    # 更新视频动态数据（不更新下载链接，留给 refresh_urls.py）
    update_video_stats_and_tags(conn, aweme_id, parsed, author_id)
    update_video_data_refreshed(conn, aweme_id)

    result_parts = []

    # stats 更新
    digg = parsed["stats"]["digg"]
    result_parts.append(f"赞:{format_count(digg)}")

    # tags 更新
    tags = [t["tag_name"] for t in parsed["video_tags"]]
    if tags:
        result_parts.append(f"标签:{'/'.join(tags[:3])}")

    # 评论刷新
    comment_msg = ""
    if refresh_comments:
        all_comments = []
        cursor = "0"
        for page in range(comment_pages):
            comments, next_cursor, has_more, err = fetch_comments_page(aweme_id, cursor)
            if err and not all_comments:
                comment_msg = f"评论:{err}"
                break
            all_comments.extend(comments)
            if not has_more:
                break
            cursor = next_cursor
            time.sleep(random.uniform(0.3, 0.6))

        if all_comments:
            new_count = replace_comments_for_video(conn, aweme_id, all_comments)
            comment_msg = f"评论:{new_count}条"
        elif not comment_msg:
            comment_msg = "评论:0条"

    conn.commit()

    msg = " | ".join(result_parts)
    if comment_msg:
        msg += f" | {comment_msg}"
    return True, msg


def run_tier2(conn, args):
    """执行 Tier2 刷新（视频动态数据）"""
    videos = query_videos_needing_refresh(
        conn,
        days=args.days,
        limit=args.limit,
        aweme_id=args.video,
        force=args.force,
    )

    if not videos:
        print("✅ 所有视频动态数据已是最新，无需刷新")
        return

    tier_days = args.days or DEFAULT_TIER2_DAYS
    print(f"📅 刷新阈值: {tier_days}天")
    print(f"📊 待刷新: {len(videos)} 条视频")
    if args.no_comments:
        print("⏭️  跳过评论刷新")
    if args.limit:
        print(f"🔢 上限: {args.limit}")
    print()

    if args.dry_run:
        print("[预览模式]\n")
        for i, v in enumerate(videos[:30], 1):
            upd = v.get("video_data_updated_at") or "从未刷新"
            title = (v.get("title") or "")[:35]
            print(f"  {i}. [{upd[:10]}] (赞:{format_count(v.get('digg', 0))}) {title}")
        if len(videos) > 30:
            print(f"  ... 还有 {len(videos) - 30} 条")
        return

    success = 0
    api_fail = 0
    rate_limit = 0
    comment_pages = args.comment_pages

    for i, v in enumerate(videos, 1):
        aweme_id = v["aweme_id"]
        title = (v.get("title") or "")[:30]

        print(f"[{i}/{len(videos)}] {aweme_id} {title}")

        ok, msg = refresh_tier2_video(
            conn, aweme_id,
            refresh_comments=not args.no_comments,
            comment_pages=comment_pages,
        )

        if ok:
            success += 1
            print(f"  ✅ {msg}")
        else:
            if "限流" in msg:
                rate_limit += 1
                print(f"  ⚠️  {msg} — 等待8秒...")
                time.sleep(8)
            else:
                api_fail += 1
                print(f"  ❌ {msg}")

        # 请求间隔
        delay = random.uniform(1.0, 2.0)
        if refresh_comments := not args.no_comments:
            delay += random.uniform(0.5, 1.0)
        time.sleep(delay)

    print("\n" + "=" * 60)
    print(f"Tier2 刷新完成!")
    print(f"  ✅ 成功: {success}")
    print(f"  ❌ API失败: {api_fail}")
    if rate_limit:
        print(f"  🔒 限流: {rate_limit}")
    print("=" * 60)


# ============================================================
# Tier 3: UP主画像刷新
# ============================================================

def refresh_tier3_author(conn, sec_uid, sample_aweme_id):
    """刷新单个UP主的Tier3数据（通过视频详情API间接获取），返回 (success, message)"""
    aweme, err = fetch_detail(sample_aweme_id)
    if aweme is None:
        return False, f"详情API: {err}"

    author = aweme.get("author") or {}
    if not author or author.get("sec_uid") != sec_uid:
        # sec_uid 不匹配，可能是不同作者的视频
        return False, "作者信息不匹配"

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

    update_author_profile(conn, sec_uid, profile)
    conn.commit()

    fc = format_count(profile["follower_count"])
    sig = profile["signature"][:25] if profile["signature"] else "无简介"
    ip = profile["ip_location"] or ""
    return True, f"粉丝:{fc} 作品:{profile['aweme_count']} {ip} {sig}"


def run_tier3(conn, args):
    """执行 Tier3 刷新（UP主画像）"""
    authors = query_authors_needing_refresh(
        conn,
        days=args.days,
        limit=args.limit,
        sec_uid=args.author,
        force=args.force,
    )

    if not authors:
        print("✅ 所有UP主画像已是最新，无需刷新")
        return

    tier_days = args.days or DEFAULT_TIER3_DAYS
    print(f"📅 刷新阈值: {tier_days}天")
    print(f"👤 待刷新: {len(authors)} 位UP主")
    if args.limit:
        print(f"🔢 上限: {args.limit}")
    print()

    if args.dry_run:
        print("[预览模式]\n")
        for i, a in enumerate(authors[:30], 1):
            upd = a.get("updated_at") or "从未刷新"
            print(f"  {i}. [{upd[:10]}] {a['nickname']} ({a['video_count']}条)")
        if len(authors) > 30:
            print(f"  ... 还有 {len(authors) - 30} 位")
        return

    success = 0
    api_fail = 0
    rate_limit = 0

    for i, a in enumerate(authors, 1):
        sec_uid = a["sec_uid"]
        nickname = a["nickname"]
        aweme_id = a["sample_aweme_id"]

        print(f"[{i}/{len(authors)}] {nickname} (aweme={aweme_id})")

        ok, msg = refresh_tier3_author(conn, sec_uid, aweme_id)

        if ok:
            success += 1
            print(f"  ✅ {msg}")
        else:
            if "限流" in msg:
                rate_limit += 1
                print(f"  ⚠️  {msg} — 等待5秒...")
                time.sleep(5)
            else:
                api_fail += 1
                print(f"  ❌ {msg}")

        # 请求间隔
        delay = random.uniform(1.0, 2.0)
        time.sleep(delay)

    print("\n" + "=" * 60)
    print(f"Tier3 刷新完成!")
    print(f"  ✅ 成功: {success}")
    print(f"  ❌ API失败: {api_fail}")
    if rate_limit:
        print(f"  🔒 限流: {rate_limit}")
    print("=" * 60)


# ============================================================
# 主入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="数据分层刷新工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
分层策略:
  Tier 1: 下载URL (由 refresh_urls.py 处理，每次下载前自动刷新)
  Tier 2: 视频动态数据 (stats/tags/comments)，默认7天刷新一次
  Tier 3: UP主画像 (粉丝数/签名等)，默认30天刷新一次

示例:
  python refresh_data.py --tier 2              # 刷新7天前的视频数据
  python refresh_data.py --tier 2 --force      # 强制刷新所有视频
  python refresh_data.py --tier 3 --days 14    # 刷新14天前的UP主画像
  python refresh_data.py --all                 # Tier2 + Tier3 全部刷新
        """
    )

    # 刷新层级
    tier_group = parser.add_mutually_exclusive_group()
    tier_group.add_argument("--tier", "-T", type=int, choices=[2, 3],
                           help="刷新层级: 2=视频动态数据, 3=UP主画像")
    tier_group.add_argument("--all", "-A", action="store_true",
                           help="刷新全部 (Tier2 + Tier3)")

    # 刷新范围
    parser.add_argument("--days", "-d", type=int, default=0,
                       help="刷新N天前的数据 (默认: Tier2=7天, Tier3=30天)")
    parser.add_argument("--force", "-f", action="store_true",
                       help="忽略时间限制，强制全部刷新")
    parser.add_argument("--limit", "-n", type=int, default=0,
                       help="最多刷新N条 (0=不限)")

    # 指定对象
    parser.add_argument("--video", "-v", help="指定视频 aweme_id 刷新")
    parser.add_argument("--author", "-a", help="指定UP主 sec_uid 刷新")

    # Tier2 评论控制
    parser.add_argument("--no-comments", action="store_true",
                       help="Tier2 跳过评论刷新")
    parser.add_argument("--comment-pages", "-p", type=int, default=1,
                       help="Tier2 每视频抓几页评论 (默认1页≈20条)")

    # 其他
    parser.add_argument("--dry-run", action="store_true",
                       help="预览模式，不实际刷新")

    args = parser.parse_args()

    # 默认执行 Tier2
    if not args.tier and not args.all:
        args.tier = 2

    init_db()
    conn = get_conn()

    # 分隔线
    print("=" * 60)
    tier_name = {2: "🟡 Tier2: 视频动态数据", 3: "🟢 Tier3: UP主画像"}
    if args.all:
        print("🔄 数据分层刷新 (全部)")
    else:
        print(f"🔄 {tier_name.get(args.tier, '未知层级')}")
    print("=" * 60)

    try:
        if args.all or args.tier == 2:
            if args.all:
                print("\n--- 🟡 Tier2: 视频动态数据 ---\n")
            run_tier2(conn, args)

        if args.all or args.tier == 3:
            if args.all:
                print("\n--- 🟢 Tier3: UP主画像 ---\n")
            # Tier3 用自己的默认天数
            if args.all and not args.days:
                args.days = DEFAULT_TIER3_DAYS
            run_tier3(conn, args)
    except KeyboardInterrupt:
        print("\n\n⚠️ 用户中断，已保存进度")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
