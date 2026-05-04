"""
抖音视频下载器 - SQLite 版
从 likes.db 读取视频信息，按标签/类型/作者/来源筛选下载
下载后自动更新数据库中的 is_downloaded 状态
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
from db_utils import get_conn, init_db, VALID_SOURCES, get_source_summary, upsert_author, upsert_video

try:
    from abogus import ABogus
    from config import SESSION_ID, SEC_USER_ID, TTWID
    HAS_REFRESH = True
except ImportError:
    HAS_REFRESH = False


DOWNLOAD_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "downloads"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Referer": "https://www.douyin.com/",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Connection": "keep-alive",
}

DOWNLOAD_DELAY_MIN = 1.0
DOWNLOAD_DELAY_MAX = 3.0
MAX_RETRIES = 3


def sanitize_filename(name, max_len=50):
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


def get_primary_tag(video_tags_json):
    tags = json.loads(video_tags_json) if video_tags_json else []
    for t in tags:
        if t.get("level") == 1:
            return t.get("tag_name", "未分类")
    if tags:
        return tags[0].get("tag_name", "未分类")
    return "未分类"


def source_tags_str(in_likes, in_favorites):
    """生成来源标记字符串"""
    tags = []
    if in_likes: tags.append("👍点赞")
    if in_favorites: tags.append("⭐收藏")
    return "+".join(tags) if tags else "未知"


def query_videos(conn, tag=None, type_=None, author=None, source=None, status="pending", limit=0):
    """从数据库查询待下载视频"""
    sql = """
        SELECT v.id, v.aweme_id, v.title, v.desc, v.type, v.duration_sec,
               v.video_tags, v.video_url, v.prevent_download, v.in_likes, v.in_favorites,
               a.nickname, a.sec_uid
        FROM videos v
        JOIN authors a ON v.author_id = a.id
        WHERE 1=1
    """
    params = []

    # 来源筛选
    if source == "likes":
        sql += " AND v.in_likes = 1"
    elif source == "favorites":
        sql += " AND v.in_favorites = 1"

    # 下载状态筛选
    if status == "pending":
        sql += " AND v.is_downloaded = 0"
    elif status == "done":
        sql += " AND v.is_downloaded = 1"
    elif status == "failed":
        sql += " AND v.is_downloaded = 2"
    elif status == "all":
        pass

    if tag:
        sql += " AND v.video_tags LIKE ?"
        params.append(f'%{tag}%')

    if type_:
        sql += " AND v.type = ?"
        params.append(type_)

    if author:
        sql += " AND a.nickname LIKE ?"
        params.append(f'%{author}%')

    sql += " AND v.video_url != ''"
    sql += " ORDER BY v.create_time DESC"

    if limit > 0:
        sql += f" LIMIT {limit}"

    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def download_video(video_url, save_path, retries=MAX_RETRIES):
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(video_url, headers=HEADERS, timeout=60, stream=True)
            if resp.status_code == 200:
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                with open(save_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                file_size = os.path.getsize(save_path)
                return True, f"OK ({file_size / 1024 / 1024:.1f}MB)"
            else:
                err = f"HTTP {resp.status_code}"
                if attempt < retries:
                    time.sleep(2)
                    continue
                return False, err
        except requests.exceptions.Timeout:
            err = "超时"
            if attempt < retries:
                time.sleep(3)
                continue
            return False, err
        except Exception as e:
            err = str(e)[:80]
            if attempt < retries:
                time.sleep(2)
                continue
            return False, err
    return False, "重试耗尽"


def update_download_status(conn, video_id, status, path="", error=""):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        UPDATE videos SET
            is_downloaded = ?,
            downloaded_at = ?,
            download_path = ?,
            download_error = ?
        WHERE id = ?
    """, (status, now, path, error, video_id))
    conn.commit()


# ============ URL 刷新功能 ============

REFRESH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Referer": "https://www.douyin.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": "https://www.douyin.com",
    "Connection": "keep-alive",
}

REFRESH_COOKIES = {
    "sessionid": SESSION_ID if HAS_REFRESH else "",
    "sid_tt": SESSION_ID if HAS_REFRESH else "",
    "ttwid": TTWID if HAS_REFRESH else "",
}


def _refresh_get_a_bogus(params_dict):
    if not HAS_REFRESH:
        return ""
    bogus = ABogus()
    params_str = urlencode(params_dict)
    a_bogus = bogus.get_value(params_str, method="GET")
    return quote(a_bogus, safe='')


def refresh_single_url(conn, aweme_id, source="likes"):
    """刷新单个视频的下载URL，返回 (success, new_url_or_error)"""
    from urllib.parse import urlencode

    params = {
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
    a_bogus = _refresh_get_a_bogus(params)
    params["a_bogus"] = a_bogus
    url = "https://www.douyin.com/aweme/v1/web/aweme/detail/" + "?" + urlencode(params)

    try:
        resp = requests.get(url, headers=REFRESH_HEADERS, cookies=REFRESH_COOKIES, timeout=20)
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}"
        data = resp.json()
        if data.get("status_code") != 0:
            return False, f"API: {data.get('status_msg', '')}"
        aweme = data.get("aweme_detail")
        if not aweme:
            return False, "无详情"

        # 提取新URL
        video_info = aweme.get("video") or {}
        play_addr = video_info.get("play_addr") or {}
        new_url = ""
        # 优先取 265/h264
        for key in ["play_addr_265", "play_addr_h264"]:
            alt = video_info.get(key) or {}
            if alt.get("url_list"):
                new_url = alt["url_list"][0]
                break
        if not new_url and play_addr.get("url_list"):
            new_url = play_addr["url_list"][0]

        # 更新 prevent_download
        prevent = 1 if aweme.get("prevent_download", False) else 0
        conn.execute("UPDATE videos SET video_url=?, prevent_download=? WHERE aweme_id=?",
                      (new_url, prevent, aweme_id))
        conn.commit()

        if new_url:
            return True, new_url
        else:
            return False, "无下载链接"
    except Exception as e:
        return False, str(e)[:60]


def run_download(tag=None, type_=None, author=None, source=None, limit=0, dry_run=False, refresh=False):
    init_db()
    conn = get_conn()

    videos = query_videos(conn, tag=tag, type_=type_, author=author, source=source, status="pending", limit=limit)

    if not videos:
        print("没有待下载的视频")
        conn.close()
        return

    print("=" * 60)
    print(f"待下载: {len(videos)} 条视频")
    if source:
        src_name = {"likes": "点赞", "favorites": "收藏"}.get(source, source)
        print(f"来源: {src_name}")
    if tag:
        print(f"筛选标签: {tag}")
    if type_:
        print(f"筛选类型: {type_}")
    if author:
        print(f"筛选作者: {author}")
    if refresh:
        print("🔗 下载前刷新URL: 开启")
    if limit:
        print(f"本次上限: {limit}")
    print("=" * 60)

    if dry_run:
        print("\n[预览模式] 不实际下载，只显示列表:\n")
        for i, v in enumerate(videos[:20], 1):
            tag_name = get_primary_tag(v["video_tags"])
            prevent = "🔒禁止下载" if v["prevent_download"] else ""
            src = source_tags_str(v["in_likes"], v["in_favorites"])
            print(f"  {i}. [{src}][{tag_name}] {v['nickname']} - {v['title'][:40] or v['desc'][:40]} {prevent}")
        if len(videos) > 20:
            print(f"  ... 还有 {len(videos) - 20} 条")
        conn.close()
        return

    success = 0
    failed = 0
    skipped = 0

    for i, v in enumerate(videos, 1):
        aweme_id = v["aweme_id"]
        title = v["title"] or v["desc"] or aweme_id
        nickname = v["nickname"]
        primary_tag = get_primary_tag(v["video_tags"])
        video_url = v["video_url"]
        prevent = v["prevent_download"]
        src = source_tags_str(v["in_likes"], v["in_favorites"])

        print(f"\n[{i}/{len(videos)}] [{src}] {nickname}: {title[:40]}")

        if prevent:
            print(f"  ⏭️  跳过 (禁止下载)")
            update_download_status(conn, v["id"], 2, error="禁止下载")
            skipped += 1
            continue

        safe_tag = sanitize_filename(primary_tag)
        safe_title = sanitize_filename(title, max_len=40)
        filename = f"{aweme_id}_{safe_title}.mp4"
        save_dir = os.path.join(DOWNLOAD_ROOT, safe_tag)
        save_path = os.path.join(save_dir, filename)

        if os.path.exists(save_path):
            file_size = os.path.getsize(save_path)
            if file_size > 1024:
                print(f"  ✅ 已存在 ({file_size / 1024 / 1024:.1f}MB)")
                update_download_status(conn, v["id"], 1, path=save_path)
                success += 1
                continue

        # 刷新URL（如果开启）
        actual_url = video_url
        if refresh and HAS_REFRESH:
            src_for_refresh = "likes" if v["in_likes"] else "favorites"
            rf_ok, rf_msg = refresh_single_url(conn, aweme_id, source=src_for_refresh)
            if rf_ok:
                actual_url = rf_msg  # rf_msg 是新 URL
                print(f"  🔗 URL已刷新")
            else:
                print(f"  🔗 URL刷新失败: {rf_msg}")
                if not actual_url:
                    # 没有旧URL，也刷新不到，跳过
                    update_download_status(conn, v["id"], 2, error=f"URL刷新失败: {rf_msg}")
                    failed += 1
                    delay = random.uniform(0.5, 1.0)
                    time.sleep(delay)
                    continue
            delay_rf = random.uniform(0.5, 1.0)
            time.sleep(delay_rf)

        print(f"  📥 下载中... → {safe_tag}/{filename[:50]}")
        ok, msg = download_video(actual_url, save_path)

        if ok:
            print(f"  ✅ {msg}")
            update_download_status(conn, v["id"], 1, path=save_path)
            success += 1
        else:
            # 下载失败时，如果是非刷新模式，尝试刷新一次再重试
            if not refresh and HAS_REFRESH and actual_url:
                print(f"  🔄 下载失败，尝试刷新URL重试...")
                src_for_refresh = "likes" if v["in_likes"] else "favorites"
                rf_ok, rf_msg = refresh_single_url(conn, aweme_id, source=src_for_refresh)
                if rf_ok:
                    time.sleep(0.5)
                    ok2, msg2 = download_video(rf_msg, save_path)
                    if ok2:
                        print(f"  ✅ 重试成功 {msg2}")
                        update_download_status(conn, v["id"], 1, path=save_path)
                        success += 1
                        delay = random.uniform(DOWNLOAD_DELAY_MIN, DOWNLOAD_DELAY_MAX)
                        time.sleep(delay)
                        continue
            print(f"  ❌ {msg}")
            update_download_status(conn, v["id"], 2, error=msg)
            failed += 1

        delay = random.uniform(DOWNLOAD_DELAY_MIN, DOWNLOAD_DELAY_MAX)
        time.sleep(delay)

    print("\n" + "=" * 60)
    print(f"下载完成! 成功={success}, 失败={failed}, 跳过={skipped}")
    print(f"下载目录: {DOWNLOAD_ROOT}")
    print("=" * 60)

    conn.close()


def show_status(conn, source=None):
    """显示数据库状态概览"""
    total = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
    likes_count = conn.execute("SELECT COUNT(*) FROM videos WHERE in_likes=1").fetchone()[0]
    favs_count = conn.execute("SELECT COUNT(*) FROM videos WHERE in_favorites=1").fetchone()[0]
    both_count = conn.execute("SELECT COUNT(*) FROM videos WHERE in_likes=1 AND in_favorites=1").fetchone()[0]

    print("=" * 60)
    print("📊 数据库状态")
    print("=" * 60)
    print(f"\n  视频总数: {total}")
    print(f"  [👍点赞] {likes_count} 条")
    print(f"  [⭐收藏] {favs_count} 条")
    if both_count:
        print(f"  [👍⭐同时点赞+收藏] {both_count} 条")

    # 下载统计
    dl = conn.execute("""
        SELECT
            SUM(CASE WHEN is_downloaded=0 THEN 1 ELSE 0 END) AS pending,
            SUM(CASE WHEN is_downloaded=1 THEN 1 ELSE 0 END) AS done,
            SUM(CASE WHEN is_downloaded=2 THEN 1 ELSE 0 END) AS failed
        FROM videos
    """).fetchone()
    print(f"\n  下载总状态: ⬜未下载={dl[0] or 0}  ✅已下载={dl[1] or 0}  ❌失败={dl[2] or 0}")

    prevent = conn.execute('SELECT COUNT(*) FROM videos WHERE prevent_download=1').fetchone()[0]
    print(f"  🔒 禁止下载: {prevent}")

    # 按来源显示下载统计
    for src, label, flag_col in [("likes", "👍点赞", "in_likes"), ("favorites", "⭐收藏", "in_favorites")]:
        if source and source != src:
            continue
        src_count = conn.execute(f"SELECT COUNT(*) FROM videos WHERE {flag_col}=1").fetchone()[0]
        if src_count == 0:
            continue
        src_dl = conn.execute(f"""
            SELECT
                SUM(CASE WHEN is_downloaded=0 THEN 1 ELSE 0 END),
                SUM(CASE WHEN is_downloaded=1 THEN 1 ELSE 0 END),
                SUM(CASE WHEN is_downloaded=2 THEN 1 ELSE 0 END)
            FROM videos WHERE {flag_col}=1
        """).fetchone()
        print(f"\n  [{label}] 下载状态: ⬜未下载={src_dl[0] or 0}  ✅已下载={src_dl[1] or 0}  ❌失败={src_dl[2] or 0}")

        # 按标签统计
        print(f"\n  [{label}] 各标签下载情况:")
        rows = conn.execute(f"""
            SELECT video_tags,
                   SUM(CASE WHEN is_downloaded=0 THEN 1 ELSE 0 END) AS pending,
                   SUM(CASE WHEN is_downloaded=1 THEN 1 ELSE 0 END) AS done,
                   SUM(CASE WHEN is_downloaded=2 THEN 1 ELSE 0 END) AS failed
            FROM videos WHERE {flag_col}=1 AND video_url != ''
            GROUP BY video_tags
        """).fetchall()

        tag_stats = {}
        for r in rows:
            for tag in json.loads(r[0]):
                name = tag.get("tag_name", "")
                if name and tag.get("level") == 1:
                    if name not in tag_stats:
                        tag_stats[name] = {"pending": 0, "done": 0, "failed": 0}
                    tag_stats[name]["pending"] += r[1] or 0
                    tag_stats[name]["done"] += r[2] or 0
                    tag_stats[name]["failed"] += r[3] or 0

        for name, s in sorted(tag_stats.items(), key=lambda x: -(x[1]["pending"] + x[1]["done"] + x[1]["failed"]))[:15]:
            total_tag = s["pending"] + s["done"] + s["failed"]
            pct = s["done"] / total_tag * 100 if total_tag > 0 else 0
            bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
            print(f"  {name:10s} [{bar}] {s['done']}/{total_tag} ({pct:.0f}%)")


def main():
    parser = argparse.ArgumentParser(description="抖音视频下载器 (SQLite版)")
    parser.add_argument("--source", "-S", choices=list(VALID_SOURCES),
                        help="按来源筛选: likes=点赞, favorites=收藏")
    parser.add_argument("--tag", "-t", help="按标签筛选 (如: 二次元, 游戏)")
    parser.add_argument("--type", "-T", dest="type_", help="按类型筛选 (视频/图集)")
    parser.add_argument("--author", "-a", help="按作者昵称筛选")
    parser.add_argument("--limit", "-n", type=int, default=0, help="本次最多下载数量")
    parser.add_argument("--dry-run", "-d", action="store_true", help="只预览不下载")
    parser.add_argument("--refresh", "-R", action="store_true",
                        help="下载前刷新URL（推荐，解决链接过期问题）")
    parser.add_argument("--status", "-s", action="store_true", help="查看下载状态")
    args = parser.parse_args()

    if args.status:
        init_db()
        conn = get_conn()
        show_status(conn, source=args.source)
        conn.close()
        return

    run_download(
        source=args.source,
        tag=args.tag,
        type_=args.type_,
        author=args.author,
        limit=args.limit,
        dry_run=args.dry_run,
        refresh=args.refresh,
    )


if __name__ == "__main__":
    main()
