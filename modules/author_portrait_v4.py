"""
author_portrait_v4.py - UP主画像聚合与分类呈现 (v4)
与 v3 的区别：
  - 使用 v4 多表 (authors_base + authors_stats + authors_portrait) 替代单表
  - compute_portrait 查询改为 JOIN videos_base + videos_stats
  - 画像写入 authors_portrait 表
  - 展示/导出逻辑不变
"""

import os
import sys
import json
from collections import Counter
from datetime import datetime

from .db_v4 import (
    get_conn_v4, init_db_v4,
    upsert_author_portrait, get_category_distribution,
)
from .meta import get_data_root


def _get_v4_db_path() -> str:
    return os.path.join(get_data_root(), "douku_v4.db")

OUTPUT_DIR = os.path.join(get_data_root(), "output")


def format_count(n):
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    elif n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def compute_portrait(conn, sec_uid):
    """v4 版：计算单个UP主画像"""
    # 获取该作者所有被点赞/收藏的视频
    rows = conn.execute("""
        SELECT vb.video_tags, vb.duration_sec, vb.desc,
               vc.comment_tags
        FROM videos_base vb
        LEFT JOIN videos_comment_tags vc ON vb.aweme_id = vc.aweme_id
        WHERE vb.author_sec_uid = ?
    """, (sec_uid,)).fetchall()

    if not rows:
        return None

    # 1. 标签聚合
    tag_counter = Counter()
    tag_level1 = Counter()
    comment_tag_counter = Counter()
    sentiment_counter = Counter()

    for r in rows:
        tags = json.loads(r["video_tags"]) if r["video_tags"] else []
        for t in tags:
            name = t.get("tag_name", "")
            level = t.get("level", 0)
            if name:
                tag_counter[name] += 1
                if level == 1:
                    tag_level1[name] += 1

        ctags = json.loads(r["comment_tags"]) if r["comment_tags"] else []
        for t in ctags:
            source = t.get("source", "")
            tag_name = t.get("tag", "")
            weight = t.get("weight", 1)
            if source == "domain":
                comment_tag_counter[tag_name] += weight
            elif source == "sentiment":
                sentiment_counter[tag_name] += weight

    # 2. 统计数据
    total_digg = 0
    total_duration = 0
    count = 0
    for r in rows:
        # 从 videos_stats 获取点赞数
        stats = conn.execute(
            "SELECT digg_count FROM videos_stats WHERE aweme_id = ?",
            (rows[0]["aweme_id"] if hasattr(rows[0], "__getitem__") else "",)
        )
        total_duration += r["duration_sec"] or 0
        count += 1

    # 从 videos_stats 批量获取所有视频的 total_digg
    stats_rows = conn.execute("""
        SELECT vs.digg_count
        FROM videos_base vb
        JOIN videos_stats vs ON vb.aweme_id = vs.aweme_id
        WHERE vb.author_sec_uid = ?
    """, (sec_uid,)).fetchall()
    for sr in stats_rows:
        total_digg += sr["digg_count"] or 0

    # 3. 确定赛道
    track_counter = Counter()
    if tag_level1:
        for name, cnt in tag_level1.items():
            track_counter[name] += cnt
    elif tag_counter:
        for name, cnt in tag_counter.most_common(5):
            track_counter[name] += cnt
    for name, weight in comment_tag_counter.most_common(5):
        track_counter[name] += weight * 0.5

    if track_counter:
        sorted_tracks = track_counter.most_common()
        track = sorted_tracks[0][0]
        track_2 = sorted_tracks[1][0] if len(sorted_tracks) > 1 else ""
    else:
        track = "未分类"
        track_2 = ""

    # 4. 标签分布
    total_videos = len(rows)
    tag_dist = []
    for name, cnt in tag_counter.most_common(10):
        tag_dist.append({
            "tag": name, "count": cnt,
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
    """v4 版：更新所有UP主画像"""
    # 从 v4 表获取作者列表 + 视频数
    authors = conn.execute("""
        SELECT ab.sec_uid, ab.nickname,
               COUNT(vb.aweme_id) as video_count
        FROM authors_base ab
        JOIN videos_base vb ON vb.author_sec_uid = ab.sec_uid
        GROUP BY ab.sec_uid
    """).fetchall()

    updated = 0
    skipped = 0

    for a in authors:
        if min_videos > 0 and a["video_count"] < min_videos:
            skipped += 1
            continue

        portrait = compute_portrait(conn, a["sec_uid"])
        if portrait:
            upsert_author_portrait(conn, {
                "sec_uid": a["sec_uid"],
                "portrait_track": portrait["track"],
                "portrait_track_2": portrait["track_2"],
                "video_count": portrait["video_count"],
                "avg_digg": portrait["avg_digg"],
                "avg_duration": portrait["avg_duration"],
            })
            updated += 1

    conn.commit()
    print(f"[画像] 更新了 {updated} 位UP主画像, 跳过 {skipped} 位")
    return updated


def get_portrait_authors(conn, track=None, min_videos=0, top=0):
    """v4 版：获取有画像的UP主列表"""
    sql = """
        SELECT ab.sec_uid, ab.nickname, ab.signature,
               ap.portrait_track, ap.portrait_track_2,
               ap.video_count, ap.avg_digg, ap.avg_duration,
               ast.follower_count, ast.aweme_count,
               ast.verification_type, ast.verification_label,
               ast.ip_location, ast.favoriting_count
        FROM authors_base ab
        JOIN authors_portrait ap ON ab.sec_uid = ap.sec_uid
        LEFT JOIN authors_stats ast ON ab.sec_uid = ast.sec_uid
        WHERE ap.portrait_track IS NOT NULL AND ap.portrait_track != ''
    """
    params = []

    if track:
        sql += " AND (ap.portrait_track = ? OR ap.portrait_track_2 = ?)"
        params.extend([track, track])

    if min_videos > 0:
        sql += " AND ap.video_count >= ?"
        params.append(min_videos)

    sql += " ORDER BY ap.video_count DESC, ast.follower_count DESC"

    if top > 0:
        sql += f" LIMIT {top}"

    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def display_track_overview(conn):
    """按赛道分组概览 (v4)"""
    tracks = conn.execute("""
        SELECT ap.portrait_track, COUNT(*) as cnt,
               SUM(ap.video_count) as total_videos,
               SUM(ast.follower_count) as total_followers,
               AVG(ap.avg_digg) as avg_digg
        FROM authors_portrait ap
        LEFT JOIN authors_stats ast ON ap.sec_uid = ast.sec_uid
        WHERE ap.portrait_track != ''
        GROUP BY ap.portrait_track
        ORDER BY cnt DESC
    """).fetchall()

    if not tracks:
        print("暂无画像数据，请先运行 portrait --update")
        return

    print(f"\n{'=' * 70}")
    print("赛道分布概览 (v4)")
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
    """展示UP主画像详情（逻辑不变）"""
    if not authors:
        print("没有符合条件的UP主")
        return

    header = f"赛道: {track_name}" if track_name else "全部赛道"
    print(f"\n{'=' * 70}")
    print(f"UP主画像 - {header} (共 {len(authors)} 位)")
    print("=" * 70)

    for i, a in enumerate(authors, 1):
        track = a["portrait_track"] or "未分类"
        track2 = f" / {a['portrait_track_2']}" if a.get("portrait_track_2") else ""
        verify = f" [认证:{a.get('verification_label', '')}]" if a.get("verification_type", 0) > 0 else ""
        ip = f" {a.get('ip_location', '')}" if a.get("ip_location") else ""
        sig = (a.get("signature") or "")[:40]

        print(f"\n{i}. {a['nickname']}{verify}{ip}")
        track_str = f"  赛道: {track}{track2} | "
        follower = format_count(a.get("follower_count", 0))
        aweme_cnt = a.get("aweme_count", 0)
        favoriting = format_count(a.get("favoriting_count", 0))
        print(f"{track_str}粉丝: {follower}  作品: {aweme_cnt}  获赞: {favoriting}")
        print(f"  被赞视频: {a['video_count']}条  平均赞: {format_count(int(a['avg_digg']))}  平均时长: {int(a['avg_duration'])}s")
        if sig:
            print(f"  简介: {sig}")


def export_portraits(conn, min_videos=0):
    """导出画像数据到 output/ 目录 (v4)"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    authors = get_portrait_authors(conn, min_videos=min_videos)
    if not authors:
        print("没有可导出的画像数据")
        return

    track_groups = {}
    for a in authors:
        track = a["portrait_track"] or "未分类"
        if track not in track_groups:
            track_groups[track] = []
        track_groups[track].append(a)

    export_data = {
        "export_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_authors": len(authors),
        "total_tracks": len(track_groups),
        "tracks": {},
    }

    for track, group in sorted(track_groups.items(), key=lambda x: -len(x[1])):
        export_data["tracks"][track] = {"count": len(group), "authors": []}
        for a in group:
            export_data["tracks"][track]["authors"].append({
                "nickname": a["nickname"], "sec_uid": a["sec_uid"],
                "signature": a.get("signature", ""),
                "ip_location": a.get("ip_location", ""),
                "follower_count": a.get("follower_count", 0),
                "aweme_count": a.get("aweme_count", 0),
                "favoriting_count": a.get("favoriting_count", 0),
                "verification_type": a.get("verification_type", 0),
                "verification_label": a.get("verification_label", ""),
                "track": a["portrait_track"], "track_2": a.get("portrait_track_2", ""),
                "video_count": a["video_count"], "avg_digg": a["avg_digg"],
                "avg_duration": a["avg_duration"],
            })

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = os.path.join(OUTPUT_DIR, f"author_portrait_v4_{ts}.json")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(export_data, ensure_ascii=False, indent=2)

    print(f"\n画像数据已导出(v4):")
    print(f"  JSON: {json_path}")
    print(f"  UP主数: {len(authors)}")
    print(f"  赛道数: {len(track_groups)}")

    # 赛道摘要
    summary_path = os.path.join(OUTPUT_DIR, f"track_summary_v4_{ts}.txt")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(f"UP主画像赛道摘要 (v4) - {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
        f.write("=" * 50 + "\n\n")
        for track, group in sorted(track_groups.items(), key=lambda x: -len(x[1])):
            f.write(f"\n## {track} ({len(group)} 位UP主)\n\n")
            for a in sorted(group, key=lambda x: -x["video_count"]):
                verify = f" [{a.get('verification_label', '')}]" if a.get("verification_type", 0) > 0 else ""
                f.write(f"  - {a['nickname']}{verify} | 粉丝:{format_count(a.get('follower_count', 0))} | 被赞视频:{a['video_count']}条\n")


def run_from_cli(args):
    """供 dytool_v4.py CLI 调用"""
    db_path = _get_v4_db_path()
    init_db_v4(db_path)
    conn = get_conn_v4(db_path)

    if getattr(args, 'update', False):
        print("正在计算UP主画像 (v4)...")
        update_all_portraits(conn, min_videos=getattr(args, 'min_videos', 0))

    tracks = display_track_overview(conn)

    authors = get_portrait_authors(
        conn,
        track=getattr(args, 'track', None),
        min_videos=getattr(args, 'min_videos', 0),
        top=getattr(args, 'top', 30),
    )
    display_portrait_detail(authors, track_name=getattr(args, 'track', "") or "")

    if getattr(args, 'export', False):
        export_portraits(conn, min_videos=getattr(args, 'min_videos', 0))

    conn.close()