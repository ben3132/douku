"""
UP主画像聚合与分类呈现
从数据库中按UP主聚合视频标签、统计数据，生成画像并按赛道分组

用法:
  python author_portrait.py                    # 生成所有UP主画像 + 按赛道分组展示
  python author_portrait.py --update           # 更新画像数据到数据库
  python author_portrait.py --track 二次元      # 只看某个赛道
  python author_portrait.py --top 30           # 展示Top 30
  python author_portrait.py --export           # 导出画像到 output/ 目录
  python author_portrait.py --min-videos 2     # 只看2条视频以上的UP主
"""

import os
import sys
import json
import argparse
import io
from collections import Counter
from datetime import datetime

if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db_utils import (
    init_db, get_conn, update_author_portrait, get_author_count, get_video_count,
)

OUTPUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "output"
)


def format_count(n):
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    elif n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def compute_portrait(conn, author_id, sec_uid):
    """计算单个UP主的画像数据"""
    # 获取该作者的所有被点赞/收藏视频
    rows = conn.execute("""
        SELECT video_tags, stats, duration_sec, type, desc, comment_tags
        FROM videos
        WHERE author_id = ?
    """, (author_id,)).fetchall()

    if not rows:
        return None

    # 1. 标签聚合（视频标签 + 评论标签）
    tag_counter = Counter()
    tag_level1 = Counter()  # level=1 的一级标签
    comment_tag_counter = Counter()  # 评论提取的领域标签
    sentiment_counter = Counter()  # 情感统计

    for r in rows:
        # 视频标签
        tags = json.loads(r["video_tags"]) if r["video_tags"] else []
        for t in tags:
            name = t.get("tag_name", "")
            level = t.get("level", 0)
            if name:
                tag_counter[name] += 1
                if level == 1:
                    tag_level1[name] += 1

        # 评论标签
        ctags = json.loads(r["comment_tags"]) if r["comment_tags"] else []
        for t in ctags:
            source = t.get("source", "")
            tag_name = t.get("tag", "")
            weight = t.get("weight", 1)
            if source == "domain":  # 领域标签加权
                comment_tag_counter[tag_name] += weight
            elif source == "sentiment":
                sentiment_counter[tag_name] += weight

    # 2. 统计数据
    total_digg = 0
    total_duration = 0
    count = 0
    for r in rows:
        stats = json.loads(r["stats"]) if r["stats"] else {}
        total_digg += stats.get("digg", 0)
        total_duration += r["duration_sec"] or 0
        count += 1

    # 3. 确定赛道（视频标签 + 评论领域标签加权）
    # 视频标签权重1, 评论领域标签权重0.5
    track_counter = Counter()
    if tag_level1:
        for name, cnt in tag_level1.items():
            track_counter[name] += cnt
    elif tag_counter:
        for name, cnt in tag_counter.most_common(5):
            track_counter[name] += cnt
    # 加入评论领域标签
    for name, weight in comment_tag_counter.most_common(5):
        track_counter[name] += weight * 0.5  # 评论标签降权

    if track_counter:
        sorted_tracks = track_counter.most_common()
        track = sorted_tracks[0][0]
        track_2 = sorted_tracks[1][0] if len(sorted_tracks) > 1 else ""
    else:
        track = "未分类"
        track_2 = ""

    # 4. 构建标签分布
    total_videos = len(rows)
    tag_dist = []
    for name, cnt in tag_counter.most_common(10):
        tag_dist.append({
            "tag": name,
            "count": cnt,
            "pct": round(cnt / total_videos * 100, 1),
        })

    portrait = {
        "tags": tag_dist,
        "track": track,
        "track_2": track_2,
        "video_count": count,
        "avg_digg": round(total_digg / count) if count > 0 else 0,
        "avg_duration": round(total_duration / count) if count > 0 else 0,
        "sentiment": dict(sentiment_counter.most_common(3)) if sentiment_counter else {},
        "comment_domains": dict(comment_tag_counter.most_common(5)) if comment_tag_counter else {},
    }

    return portrait


def update_all_portraits(conn, min_videos=0):
    """更新所有UP主的画像数据"""
    authors = conn.execute("""
        SELECT a.id, a.sec_uid, a.nickname, COUNT(v.id) as video_count
        FROM authors a
        JOIN videos v ON v.author_id = a.id
        GROUP BY a.id
    """).fetchall()

    updated = 0
    skipped = 0

    for a in authors:
        if min_videos > 0 and a["video_count"] < min_videos:
            skipped += 1
            continue

        portrait = compute_portrait(conn, a["id"], a["sec_uid"])
        if portrait:
            update_author_portrait(conn, a["sec_uid"], portrait)
            updated += 1

    conn.commit()
    print(f"[画像] 更新了 {updated} 位UP主画像, 跳过 {skipped} 位")
    return updated


def get_portrait_authors(conn, track=None, min_videos=0, top=0):
    """获取有画像的UP主列表"""
    sql = """
        SELECT a.id, a.sec_uid, a.nickname, a.signature, a.ip_location,
               a.follower_count, a.following_count, a.aweme_count,
               a.favoriting_count, a.verification_type, a.verification_label,
               a.portrait_tags, a.portrait_track, a.portrait_track_2,
               a.video_count, a.avg_digg, a.avg_duration
        FROM authors a
        WHERE a.portrait_track != ''
    """
    params = []

    if track:
        sql += " AND (a.portrait_track = ? OR a.portrait_track_2 = ?)"
        params.extend([track, track])

    if min_videos > 0:
        sql += f" AND a.video_count >= {min_videos}"

    sql += " ORDER BY a.video_count DESC, a.follower_count DESC"

    if top > 0:
        sql += f" LIMIT {top}"

    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def display_track_overview(conn):
    """按赛道分组概览"""
    tracks = conn.execute("""
        SELECT portrait_track, COUNT(*) as cnt,
               SUM(video_count) as total_videos,
               SUM(follower_count) as total_followers,
               AVG(avg_digg) as avg_digg
        FROM authors
        WHERE portrait_track != ''
        GROUP BY portrait_track
        ORDER BY cnt DESC
    """).fetchall()

    if not tracks:
        print("暂无画像数据，请先运行: python author_portrait.py --update")
        return

    print("\n" + "=" * 70)
    print("📊 赛道分布概览")
    print("=" * 70)
    print(f"{'赛道':<12} {'UP主数':>6} {'被赞视频':>8} {'总粉丝':>10} {'均赞':>8}")
    print("-" * 70)
    for r in tracks:
        track = r["portrait_track"] or "未分类"
        cnt = r["cnt"]
        vids = r["total_videos"] or 0
        followers = r["total_followers"] or 0
        avg_digg = r["avg_digg"] or 0
        print(f"{track:<12} {cnt:>6} {vids:>8} {format_count(followers):>10} {format_count(int(avg_digg)):>8}")

    return tracks


def display_portrait_detail(authors, track_name=""):
    """展示UP主画像详情"""
    if not authors:
        print("没有符合条件的UP主")
        return

    header = f"赛道: {track_name}" if track_name else "全部赛道"
    print(f"\n{'=' * 70}")
    print(f"👤 UP主画像 - {header} (共 {len(authors)} 位)")
    print("=" * 70)

    for i, a in enumerate(authors, 1):
        tags = json.loads(a["portrait_tags"]) if a["portrait_tags"] else []
        tag_str = " > ".join([f"{t['tag']}({t['pct']}%)" for t in tags[:4]])
        track = a["portrait_track"] or "未分类"
        track2 = f" / {a['portrait_track_2']}" if a["portrait_track_2"] else ""
        verify = f" ✅{a['verification_label']}" if a["verification_type"] > 0 else ""
        ip = f" 📍{a['ip_location']}" if a["ip_location"] else ""
        sig = a["signature"][:40] if a["signature"] else ""

        print(f"\n{i}. {a['nickname']}{verify}{ip}")
        print(f"   赛道: {track}{track2}  |  粉丝: {format_count(a['follower_count'])}  作品: {a['aweme_count']}  获赞: {format_count(a['favoriting_count'])}")
        print(f"   被赞视频: {a['video_count']}条  平均赞: {format_count(int(a['avg_digg']))}  平均时长: {int(a['avg_duration'])}s")
        if tag_str:
            print(f"   标签: {tag_str}")
        if sig:
            print(f"   简介: {sig}")


def export_portraits(conn, min_videos=0):
    """导出画像数据到 output/ 目录"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    authors = get_portrait_authors(conn, min_videos=min_videos)

    if not authors:
        print("没有可导出的画像数据")
        return

    # 按赛道分组
    track_groups = {}
    for a in authors:
        track = a["portrait_track"] or "未分类"
        if track not in track_groups:
            track_groups[track] = []
        track_groups[track].append(a)

    # 导出完整画像 JSON
    export_data = {
        "export_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_authors": len(authors),
        "total_tracks": len(track_groups),
        "tracks": {},
    }

    for track, group in sorted(track_groups.items(), key=lambda x: -len(x[1])):
        export_data["tracks"][track] = {
            "count": len(group),
            "authors": [],
        }
        for a in group:
            tags = json.loads(a["portrait_tags"]) if a["portrait_tags"] else []
            export_data["tracks"][track]["authors"].append({
                "nickname": a["nickname"],
                "sec_uid": a["sec_uid"],
                "signature": a["signature"],
                "ip_location": a["ip_location"],
                "follower_count": a["follower_count"],
                "aweme_count": a["aweme_count"],
                "favoriting_count": a["favoriting_count"],
                "verification_type": a["verification_type"],
                "verification_label": a["verification_label"],
                "track": a["portrait_track"],
                "track_2": a["portrait_track_2"],
                "video_count": a["video_count"],
                "avg_digg": a["avg_digg"],
                "avg_duration": a["avg_duration"],
                "tags": tags,
            })

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = os.path.join(OUTPUT_DIR, f"author_portrait_{ts}.json")

    # 写入文件
    import subprocess
    content = json.dumps(export_data, ensure_ascii=False, indent=2)
    tmp_path = os.path.join(os.environ.get("TEMP", "/tmp"), f"_portrait_{ts}.txt")
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content)

    # 用 write_file.py 写入（走规范流程）
    from pathlib import Path
    write_script = str(Path(__file__).parent.parent / "modules" / ".." / "modules" / "db_utils.py")
    # 直接写 JSON
    with open(json_path, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"\n📁 画像数据已导出:")
    print(f"  JSON: {json_path}")
    print(f"  UP主数: {len(authors)}")
    print(f"  赛道数: {len(track_groups)}")

    # 导出赛道摘要
    summary_path = os.path.join(OUTPUT_DIR, f"track_summary_{ts}.txt")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(f"UP主画像赛道摘要 - {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
        f.write("=" * 50 + "\n\n")
        for track, group in sorted(track_groups.items(), key=lambda x: -len(x[1])):
            f.write(f"\n## {track} ({len(group)} 位UP主)\n\n")
            for a in sorted(group, key=lambda x: -x["video_count"]):
                tags = json.loads(a["portrait_tags"]) if a["portrait_tags"] else []
                tag_str = ", ".join([t["tag"] for t in tags[:3]])
                verify = f" [{a['verification_label']}]" if a["verification_type"] > 0 else ""
                f.write(f"  - {a['nickname']}{verify} | 粉丝:{format_count(a['follower_count'])} | 被赞视频:{a['video_count']}条 | {tag_str}\n")

    print(f"  摘要: {summary_path}")

    # 清理临时文件
    if os.path.exists(tmp_path):
        os.remove(tmp_path)


def main():
    parser = argparse.ArgumentParser(description="UP主画像聚合与分类呈现")
    parser.add_argument("--update", "-u", action="store_true",
                        help="更新所有UP主画像数据（聚合视频标签/统计）")
    parser.add_argument("--track", "-t", help="按赛道筛选 (如: 二次元, 游戏)")
    parser.add_argument("--top", "-n", type=int, default=30,
                        help="展示前N位UP主 (默认30)")
    parser.add_argument("--min-videos", "-m", type=int, default=0,
                        help="最少N条被赞视频才展示 (默认0)")
    parser.add_argument("--export", "-e", action="store_true",
                        help="导出画像到 output/ 目录")
    args = parser.parse_args()

    init_db()
    conn = get_conn()

    if args.update:
        print("🔄 正在计算UP主画像...")
        update_all_portraits(conn, min_videos=args.min_videos)

    # 赛道概览
    tracks = display_track_overview(conn)

    # UP主详情
    authors = get_portrait_authors(
        conn,
        track=args.track,
        min_videos=args.min_videos,
        top=args.top,
    )
    display_portrait_detail(authors, track_name=args.track or "")

    # 导出
    if args.export:
        export_portraits(conn, min_videos=args.min_videos)

    conn.close()


if __name__ == "__main__":
    main()
