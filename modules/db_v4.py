
"""
db_v4.py — 抖库 v4 数据库层

按更新频率分表管理：
  低频: authors_base, videos_base        ← fetch 时首次写入
  中频: authors_stats, videos_stats,      ← 每周/按需刷新
        authors_portrait, videos_meta,
        videos_classification, videos_comment_tags
  高频: videos_urls, videos_download      ← 每天/使用前刷新
  独立: comments, bookmark, auth_state, run_state, rules_meta

核心原则：
  - 不同更新频率 = 不同表，避免并发写冲突
  - 所有写操作通过 upsert（INSERT OR REPLACE）
  - WAL 模式，读不阻塞写、写不阻塞读

用法:
    from modules.db_v4 import init_db_v4, get_conn_v4
    conn = get_conn_v4(db_path)
"""

import sqlite3
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple


# ============================================================
# Schema SQL（内联）
# ============================================================

SCHEMA_SQL = """\
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS authors_base (
    sec_uid             TEXT PRIMARY KEY,
    nickname            TEXT DEFAULT '',
    avatar              TEXT DEFAULT '',
    signature           TEXT DEFAULT '',
    ip_location         TEXT DEFAULT '',
    verification_type   INTEGER DEFAULT 0,
    verification_label  TEXT DEFAULT '',
    is_gov_media_vip    INTEGER DEFAULT 0,
    created_at          TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS videos_base (
    aweme_id            TEXT PRIMARY KEY,
    title               TEXT DEFAULT '',
    desc                TEXT DEFAULT '',
    create_time         TEXT DEFAULT '',
    type                TEXT DEFAULT '',
    aweme_type_raw      INTEGER DEFAULT 0,
    duration_sec        INTEGER DEFAULT 0,
    author_sec_uid      TEXT NOT NULL,
    video_tags          TEXT DEFAULT '[]',
    hashtags            TEXT DEFAULT '[]',
    desc_hashtags       TEXT DEFAULT '[]',
    share_url           TEXT DEFAULT '',
    is_top              INTEGER DEFAULT 0,
    prevent_download    INTEGER DEFAULT 0,
    added_at            TEXT DEFAULT '',
    FOREIGN KEY (author_sec_uid) REFERENCES authors_base(sec_uid)
);

CREATE TABLE IF NOT EXISTS authors_stats (
    sec_uid             TEXT PRIMARY KEY,
    follower_count      INTEGER DEFAULT 0,
    following_count     INTEGER DEFAULT 0,
    aweme_count         INTEGER DEFAULT 0,
    favoriting_count    INTEGER DEFAULT 0,
    updated_at          TEXT DEFAULT '',
    FOREIGN KEY (sec_uid) REFERENCES authors_base(sec_uid)
);

CREATE TABLE IF NOT EXISTS authors_portrait (
    sec_uid             TEXT PRIMARY KEY,
    portrait_tags       TEXT DEFAULT '[]',
    portrait_track      TEXT DEFAULT '',
    portrait_track_2    TEXT DEFAULT '',
    video_count         INTEGER DEFAULT 0,
    avg_digg            REAL DEFAULT 0,
    avg_duration        REAL DEFAULT 0,
    updated_at          TEXT DEFAULT '',
    FOREIGN KEY (sec_uid) REFERENCES authors_base(sec_uid)
);

CREATE TABLE IF NOT EXISTS videos_stats (
    aweme_id            TEXT PRIMARY KEY,
    digg_count          INTEGER DEFAULT 0,
    comment_count       INTEGER DEFAULT 0,
    share_count         INTEGER DEFAULT 0,
    collect_count       INTEGER DEFAULT 0,
    play_count          INTEGER DEFAULT 0,
    updated_at          TEXT DEFAULT '',
    FOREIGN KEY (aweme_id) REFERENCES videos_base(aweme_id)
);

CREATE TABLE IF NOT EXISTS videos_meta (
    aweme_id            TEXT PRIMARY KEY,
    in_likes            INTEGER DEFAULT 0,
    in_favorites        INTEGER DEFAULT 0,
    liked_time          TEXT DEFAULT '',
    favorited_time      TEXT DEFAULT '',
    updated_at          TEXT DEFAULT '',
    FOREIGN KEY (aweme_id) REFERENCES videos_base(aweme_id)
);

CREATE TABLE IF NOT EXISTS videos_classification (
    aweme_id            TEXT PRIMARY KEY,
    content_category    TEXT DEFAULT '',
    category_detail     TEXT DEFAULT '{}',
    classified_at       TEXT DEFAULT '',
    FOREIGN KEY (aweme_id) REFERENCES videos_base(aweme_id)
);

CREATE TABLE IF NOT EXISTS videos_comment_tags (
    aweme_id            TEXT PRIMARY KEY,
    comment_tags        TEXT DEFAULT '[]',
    tagged_at           TEXT DEFAULT '',
    FOREIGN KEY (aweme_id) REFERENCES videos_base(aweme_id)
);

CREATE TABLE IF NOT EXISTS videos_urls (
    aweme_id            TEXT PRIMARY KEY,
    video_url           TEXT DEFAULT '',
    cover_url           TEXT DEFAULT '',
    music_url           TEXT DEFAULT '',
    music_title         TEXT DEFAULT '',
    refreshed_at        TEXT DEFAULT '',
    FOREIGN KEY (aweme_id) REFERENCES videos_base(aweme_id)
);

CREATE TABLE IF NOT EXISTS videos_download (
    aweme_id            TEXT PRIMARY KEY,
    status              INTEGER DEFAULT 0,
    downloaded_at       TEXT DEFAULT '',
    download_path       TEXT DEFAULT '',
    download_error      TEXT DEFAULT '',
    retry_count         INTEGER DEFAULT 0,
    updated_at          TEXT DEFAULT '',
    FOREIGN KEY (aweme_id) REFERENCES videos_base(aweme_id)
);

CREATE TABLE IF NOT EXISTS comments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    aweme_id            TEXT NOT NULL,
    cid                 TEXT UNIQUE NOT NULL,
    content             TEXT DEFAULT '',
    user_name           TEXT DEFAULT '',
    digg_count          INTEGER DEFAULT 0,
    reply_count         INTEGER DEFAULT 0,
    is_hot              INTEGER DEFAULT 0,
    create_time         TEXT DEFAULT '',
    ip_location         TEXT DEFAULT '',
    added_at            TEXT DEFAULT '',
    FOREIGN KEY (aweme_id) REFERENCES videos_base(aweme_id)
);

CREATE TABLE IF NOT EXISTS bookmark (
    source              TEXT PRIMARY KEY,
    last_cursor         TEXT DEFAULT '0',
    last_liked_time     TEXT DEFAULT '',
    total_fetched       INTEGER DEFAULT 0,
    updated_at          TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS auth_state (
    key                 TEXT PRIMARY KEY,
    value               TEXT DEFAULT '',
    updated_at          TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS run_state (
    key                 TEXT PRIMARY KEY,
    value               TEXT DEFAULT '',
    updated_at          TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS rules_meta (
    rule_name           TEXT PRIMARY KEY,
    version             INTEGER DEFAULT 1,
    description         TEXT DEFAULT '',
    updated_at          TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_videos_author ON videos_base(author_sec_uid);
CREATE INDEX IF NOT EXISTS idx_videos_added ON videos_base(added_at);
CREATE INDEX IF NOT EXISTS idx_comments_aweme ON comments(aweme_id);
CREATE INDEX IF NOT EXISTS idx_comments_cid ON comments(cid);
CREATE INDEX IF NOT EXISTS idx_videos_dl_status ON videos_download(status);
CREATE INDEX IF NOT EXISTS idx_videos_urls_refreshed ON videos_urls(refreshed_at);
CREATE INDEX IF NOT EXISTS idx_videos_class_category ON videos_classification(content_category);
CREATE INDEX IF NOT EXISTS idx_videos_meta_likes ON videos_meta(in_likes);
CREATE INDEX IF NOT EXISTS idx_videos_meta_fav ON videos_meta(in_favorites);
"""


# ============================================================
# 连接管理
# ============================================================

def get_conn_v4(db_path: str) -> sqlite3.Connection:
    """获取 v4 数据库连接（WAL 模式）"""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db_v4(db_path: str) -> sqlite3.Connection:
    """初始化 v4 数据库（建表+索引），返回连接"""
    conn = get_conn_v4(db_path)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


# ============================================================
# 工具函数
# ============================================================

def _now() -> str:
    return datetime.now().isoformat()

def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)

def _json_loads(s: str, default: Any = None) -> Any:
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return default if default is not None else {}

def _safe_str(v: Any) -> str:
    return str(v) if v is not None else ''


# ============================================================
# UP主操作
# ============================================================

def upsert_author_base(conn: sqlite3.Connection, author: Dict[str, Any]) -> None:
    """写入/更新 UP主基础信息（低频）"""
    conn.execute("""
        INSERT INTO authors_base (sec_uid, nickname, avatar, signature, ip_location,
            verification_type, verification_label, is_gov_media_vip, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sec_uid) DO UPDATE SET
            nickname = COALESCE(EXCLUDED.nickname, nickname),
            avatar = COALESCE(NULLIF(EXCLUDED.avatar, ''), avatar),
            signature = COALESCE(NULLIF(EXCLUDED.signature, ''), signature),
            ip_location = COALESCE(NULLIF(EXCLUDED.ip_location, ''), ip_location),
            verification_type = EXCLUDED.verification_type,
            verification_label = COALESCE(NULLIF(EXCLUDED.verification_label, ''), verification_label),
            is_gov_media_vip = COALESCE(EXCLUDED.is_gov_media_vip, is_gov_media_vip)
    """, (
        author.get("sec_uid", ""),
        author.get("nickname", ""),
        author.get("avatar", ""),
        author.get("signature", ""),
        author.get("ip_location", ""),
        author.get("verification_type", 0),
        author.get("verification_label", ""),
        author.get("is_gov_media_vip", 0),
        author.get("created_at", _now()),
    ))


def upsert_author_stats(conn: sqlite3.Connection, stats: Dict[str, Any]) -> None:
    """写入/更新 UP主统计数据（中频）"""
    conn.execute("""
        INSERT INTO authors_stats (sec_uid, follower_count, following_count,
            aweme_count, favoriting_count, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(sec_uid) DO UPDATE SET
            follower_count = EXCLUDED.follower_count,
            following_count = EXCLUDED.following_count,
            aweme_count = EXCLUDED.aweme_count,
            favoriting_count = EXCLUDED.favoriting_count,
            updated_at = EXCLUDED.updated_at
    """, (
        stats.get("sec_uid", ""),
        stats.get("follower_count", 0),
        stats.get("following_count", 0),
        stats.get("aweme_count", 0),
        stats.get("favoriting_count", 0),
        stats.get("updated_at", _now()),
    ))


def upsert_author_portrait(conn: sqlite3.Connection, portrait: Dict[str, Any]) -> None:
    """写入/更新 UP主画像（中频，分析阶段调用）"""
    conn.execute("""
        INSERT INTO authors_portrait (sec_uid, portrait_tags, portrait_track,
            portrait_track_2, video_count, avg_digg, avg_duration, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sec_uid) DO UPDATE SET
            portrait_tags = EXCLUDED.portrait_tags,
            portrait_track = EXCLUDED.portrait_track,
            portrait_track_2 = EXCLUDED.portrait_track_2,
            video_count = EXCLUDED.video_count,
            avg_digg = EXCLUDED.avg_digg,
            avg_duration = EXCLUDED.avg_duration,
            updated_at = EXCLUDED.updated_at
    """, (
        portrait.get("sec_uid", ""),
        _json_dumps(portrait.get("portrait_tags", [])),
        portrait.get("portrait_track", ""),
        portrait.get("portrait_track_2", ""),
        portrait.get("video_count", 0),
        portrait.get("avg_digg", 0.0),
        portrait.get("avg_duration", 0.0),
        portrait.get("updated_at", _now()),
    ))


def get_author(conn: sqlite3.Connection, sec_uid: str) -> Optional[Dict[str, Any]]:
    """获取 UP主完整信息（JOIN 三表）"""
    row = conn.execute("""
        SELECT b.*, s.follower_count, s.following_count, s.aweme_count, s.favoriting_count,
               p.portrait_tags, p.portrait_track, p.portrait_track_2, p.video_count AS portrait_vc
        FROM authors_base b
        LEFT JOIN authors_stats s ON b.sec_uid = s.sec_uid
        LEFT JOIN authors_portrait p ON b.sec_uid = p.sec_uid
        WHERE b.sec_uid = ?
    """, (sec_uid,)).fetchone()
    return dict(row) if row else None


def get_author_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM authors_base").fetchone()
    return row[0] if row else 0


def get_authors_with_portrait(conn: sqlite3.Connection, limit: int = 50) -> List[Dict[str, Any]]:
    """获取有画像数据的 UP主列表"""
    rows = conn.execute("""
        SELECT b.sec_uid, b.nickname, b.avatar, s.follower_count,
               p.portrait_track, p.video_count, p.avg_digg
        FROM authors_base b
        JOIN authors_portrait p ON b.sec_uid = p.sec_uid
        LEFT JOIN authors_stats s ON b.sec_uid = s.sec_uid
        ORDER BY s.follower_count DESC
        LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]


# ============================================================
# 视频操作
# ============================================================

def upsert_video_base(conn: sqlite3.Connection, video: Dict[str, Any]) -> None:
    """写入视频基础信息（低频）。已存在则跳过（基础信息不变）。"""
    conn.execute("""
        INSERT OR IGNORE INTO videos_base (aweme_id, title, desc, create_time,
            type, aweme_type_raw, duration_sec, author_sec_uid,
            video_tags, hashtags, desc_hashtags, share_url,
            is_top, prevent_download, added_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        _safe_str(video.get("aweme_id")),
        video.get("title", "") or "",
        video.get("desc", "") or "",
        _safe_str(video.get("create_time")),
        video.get("type", "") or "",
        video.get("aweme_type_raw", 0) or 0,
        video.get("duration_sec", 0) or 0,
        _safe_str(video.get("author_sec_uid")),
        _json_dumps(video.get("video_tags", [])),
        _json_dumps(video.get("hashtags", [])),
        _json_dumps(video.get("desc_hashtags", [])),
        video.get("share_url", "") or "",
        video.get("is_top", 0) or 0,
        video.get("prevent_download", 0) or 0,
        video.get("added_at", _now()),
    ))


def upsert_video_meta(conn: sqlite3.Connection, aweme_id: str, meta: Dict[str, Any]) -> None:
    """更新视频来源标记（点赞/收藏状态）"""
    conn.execute("""
        INSERT INTO videos_meta (aweme_id, in_likes, in_favorites, liked_time, favorited_time, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(aweme_id) DO UPDATE SET
            in_likes = MAX(in_likes, EXCLUDED.in_likes),
            in_favorites = MAX(in_favorites, EXCLUDED.in_favorites),
            liked_time = COALESCE(NULLIF(EXCLUDED.liked_time, ''), liked_time),
            favorited_time = COALESCE(NULLIF(EXCLUDED.favorited_time, ''), favorited_time),
            updated_at = EXCLUDED.updated_at
    """, (
        aweme_id,
        meta.get("in_likes", 0) or 0,
        meta.get("in_favorites", 0) or 0,
        meta.get("liked_time", "") or "",
        meta.get("favorited_time", "") or "",
        meta.get("updated_at", _now()),
    ))


def upsert_video_stats(conn: sqlite3.Connection, aweme_id: str, stats: Dict[str, Any]) -> None:
    """更新视频统计数据（中频）"""
    conn.execute("""
        INSERT INTO videos_stats (aweme_id, digg_count, comment_count, share_count,
            collect_count, play_count, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(aweme_id) DO UPDATE SET
            digg_count = EXCLUDED.digg_count,
            comment_count = EXCLUDED.comment_count,
            share_count = EXCLUDED.share_count,
            collect_count = EXCLUDED.collect_count,
            play_count = EXCLUDED.play_count,
            updated_at = EXCLUDED.updated_at
    """, (
        aweme_id,
        stats.get("digg_count", 0) or 0,
        stats.get("comment_count", 0) or 0,
        stats.get("share_count", 0) or 0,
        stats.get("collect_count", 0) or 0,
        stats.get("play_count", 0) or 0,
        stats.get("updated_at", _now()),
    ))


def upsert_video_urls(conn: sqlite3.Connection, aweme_id: str, urls: Dict[str, Any]) -> None:
    """更新视频 URL（高频，下载前刷新）"""
    conn.execute("""
        INSERT INTO videos_urls (aweme_id, video_url, cover_url,
            music_url, music_title, refreshed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(aweme_id) DO UPDATE SET
            video_url = COALESCE(NULLIF(EXCLUDED.video_url, ''), video_url),
            cover_url = COALESCE(NULLIF(EXCLUDED.cover_url, ''), cover_url),
            music_url = COALESCE(NULLIF(EXCLUDED.music_url, ''), music_url),
            music_title = COALESCE(NULLIF(EXCLUDED.music_title, ''), music_title),
            refreshed_at = EXCLUDED.refreshed_at
    """, (
        aweme_id,
        urls.get("video_url", "") or "",
        urls.get("cover_url", "") or "",
        urls.get("music_url", "") or "",
        urls.get("music_title", "") or "",
        urls.get("refreshed_at", _now()),
    ))


def get_video(conn: sqlite3.Connection, aweme_id: str) -> Optional[Dict[str, Any]]:
    """获取视频完整信息（JOIN 多表）"""
    row = conn.execute("""
        SELECT b.*,
               s.digg_count, s.comment_count, s.share_count, s.collect_count, s.play_count,
               m.in_likes, m.in_favorites, m.liked_time, m.favorited_time,
               u.video_url, u.cover_url, u.music_url, u.refreshed_at,
               c.content_category,
               d.status AS download_status, d.download_path, d.downloaded_at, d.download_error
        FROM videos_base b
        LEFT JOIN videos_stats s ON b.aweme_id = s.aweme_id
        LEFT JOIN videos_meta m ON b.aweme_id = m.aweme_id
        LEFT JOIN videos_urls u ON b.aweme_id = u.aweme_id
        LEFT JOIN videos_classification c ON b.aweme_id = c.aweme_id
        LEFT JOIN videos_download d ON b.aweme_id = d.aweme_id
        WHERE b.aweme_id = ?
    """, (aweme_id,)).fetchone()
    return dict(row) if row else None


def get_video_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM videos_base").fetchone()
    return row[0] if row else 0


def get_videos_by_author(conn: sqlite3.Connection, sec_uid: str, limit: int = 100) -> List[Dict[str, Any]]:
    rows = conn.execute("""
        SELECT aweme_id, title, create_time
        FROM videos_base WHERE author_sec_uid = ?
        ORDER BY create_time DESC LIMIT ?
    """, (sec_uid, limit)).fetchall()
    return [dict(r) for r in rows]


def get_videos_by_category(conn: sqlite3.Connection, category: str, limit: int = 200) -> List[Dict[str, Any]]:
    rows = conn.execute("""
        SELECT b.aweme_id, b.title, b.create_time, c.content_category
        FROM videos_base b
        JOIN videos_classification c ON b.aweme_id = c.aweme_id
        WHERE c.content_category = ?
        ORDER BY b.create_time DESC LIMIT ?
    """, (category, limit)).fetchall()
    return [dict(r) for r in rows]


# ============================================================
# 下载状态操作
# ============================================================

DL_PENDING      = 0   # 待下载
DL_DONE         = 1   # 已下载
DL_FAILED       = 2   # 下载失败
DL_URL_EXPIRED  = 3   # URL 过期待刷新
DL_IN_PROGRESS  = 4   # 下载中


def init_download_status(conn: sqlite3.Connection, aweme_id: str) -> None:
    """为新视频初始化下载状态记录"""
    conn.execute("""
        INSERT OR IGNORE INTO videos_download (aweme_id, status, updated_at)
        VALUES (?, ?, ?)
    """, (aweme_id, DL_PENDING, _now()))


def set_download_status(conn: sqlite3.Connection, aweme_id: str, status: int,
                         path: str = "", error: str = "") -> None:
    """更新下载状态"""
    conn.execute("""
        INSERT INTO videos_download (aweme_id, status, downloaded_at, download_path,
            download_error, retry_count, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(aweme_id) DO UPDATE SET
            status = EXCLUDED.status,
            downloaded_at = COALESCE(NULLIF(EXCLUDED.downloaded_at, ''), downloaded_at),
            download_path = COALESCE(NULLIF(EXCLUDED.download_path, ''), download_path),
            download_error = COALESCE(NULLIF(EXCLUDED.download_error, ''), download_error),
            retry_count = CASE
                WHEN EXCLUDED.status IN (?, ?, ?) THEN retry_count + 1
                ELSE retry_count
            END,
            updated_at = EXCLUDED.updated_at
    """, (aweme_id, status,
          _now() if status == DL_DONE else "",
          path if status == DL_DONE else "",
          error if status == DL_FAILED else "",
          DL_FAILED, DL_URL_EXPIRED, DL_IN_PROGRESS))


def get_downloads_by_status(conn: sqlite3.Connection, status: int, limit: int = 500) -> List[Dict[str, Any]]:
    """获取指定状态的下载队列"""
    rows = conn.execute("""
        SELECT d.aweme_id, d.status, d.retry_count, d.updated_at,
               b.title, b.author_sec_uid, b.prevent_download,
               u.video_url, u.cover_url, u.refreshed_at,
               cls.content_category
        FROM videos_download d
        JOIN videos_base b ON d.aweme_id = b.aweme_id
        LEFT JOIN videos_urls u ON d.aweme_id = u.aweme_id
        LEFT JOIN videos_classification cls ON d.aweme_id = cls.aweme_id
        WHERE d.status = ? AND b.prevent_download = 0
        ORDER BY d.retry_count ASC, d.updated_at ASC
        LIMIT ?
    """, (status, limit)).fetchall()
    return [dict(r) for r in rows]


def get_download_queue(conn: sqlite3.Connection, limit: int = 500) -> List[Dict[str, Any]]:
    """获取待下载队列（status=0 或 URL 过期）"""
    rows = conn.execute("""
        SELECT d.aweme_id, d.status, d.retry_count, u.video_url, u.refreshed_at,
               b.title, b.author_sec_uid, b.prevent_download,
               cls.content_category
        FROM videos_download d
        JOIN videos_base b ON d.aweme_id = b.aweme_id
        LEFT JOIN videos_urls u ON d.aweme_id = u.aweme_id
        LEFT JOIN videos_classification cls ON d.aweme_id = cls.aweme_id
        WHERE d.status IN (?, ?) AND b.prevent_download = 0
        ORDER BY d.retry_count ASC
        LIMIT ?
    """, (DL_PENDING, DL_URL_EXPIRED, limit)).fetchall()
    return [dict(r) for r in rows]


def get_download_stats(conn: sqlite3.Connection) -> Dict[str, int]:
    """下载统计"""
    rows = conn.execute("""
        SELECT status, COUNT(*) as cnt FROM videos_download GROUP BY status
    """).fetchall()
    stats = {"total": 0, "pending": 0, "done": 0, "failed": 0, "expired": 0}
    for r in rows:
        stats["total"] += r["cnt"]
        if r["status"] == DL_PENDING: stats["pending"] = r["cnt"]
        elif r["status"] == DL_DONE: stats["done"] = r["cnt"]
        elif r["status"] == DL_FAILED: stats["failed"] = r["cnt"]
        elif r["status"] == DL_URL_EXPIRED: stats["expired"] = r["cnt"]
    return stats


def is_video_downloaded(conn: sqlite3.Connection, aweme_id: str) -> bool:
    """检查视频是否已下载"""
    row = conn.execute(
        "SELECT status FROM videos_download WHERE aweme_id = ?", (aweme_id,)
    ).fetchone()
    return row is not None and row["status"] == DL_DONE


def mark_url_expired(conn: sqlite3.Connection, aweme_id: str) -> None:
    """标记 URL 过期"""
    conn.execute("""
        INSERT INTO videos_download (aweme_id, status, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(aweme_id) DO UPDATE SET
            status = CASE WHEN status = ? THEN ? ELSE status END,
            updated_at = EXCLUDED.updated_at
    """, (aweme_id, DL_URL_EXPIRED, _now(), DL_PENDING, DL_URL_EXPIRED))


# ============================================================
# 评论操作
# ============================================================

def upsert_comment(conn: sqlite3.Connection, comment: Dict[str, Any]) -> None:
    """写入/更新单条评论"""
    conn.execute("""
        INSERT INTO comments (aweme_id, cid, content, user_name,
            digg_count, reply_count, is_hot, create_time, ip_location, added_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(cid) DO UPDATE SET
            content = COALESCE(NULLIF(EXCLUDED.content, ''), content),
            digg_count = EXCLUDED.digg_count,
            reply_count = EXCLUDED.reply_count,
            ip_location = COALESCE(NULLIF(EXCLUDED.ip_location, ''), ip_location)
    """, (
        _safe_str(comment.get("aweme_id")),
        _safe_str(comment.get("cid")),
        comment.get("content", "") or "",
        comment.get("user_name", "") or "",
        comment.get("digg_count", 0) or 0,
        comment.get("reply_count", 0) or 0,
        comment.get("is_hot", 0) or 0,
        _safe_str(comment.get("create_time")),
        comment.get("ip_location", "") or "",
        comment.get("added_at", _now()),
    ))


def get_comments_for_video(conn: sqlite3.Connection, aweme_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    rows = conn.execute("""
        SELECT * FROM comments WHERE aweme_id = ?
        ORDER BY digg_count DESC LIMIT ?
    """, (aweme_id, limit)).fetchall()
    return [dict(r) for r in rows]


def get_hot_comments(conn: sqlite3.Connection, aweme_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    rows = conn.execute("""
        SELECT * FROM comments WHERE aweme_id = ? AND is_hot = 1
        ORDER BY digg_count DESC LIMIT ?
    """, (aweme_id, limit)).fetchall()
    return [dict(r) for r in rows]


def get_comment_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM comments").fetchone()
    return row[0] if row else 0


def get_videos_with_comments(conn: sqlite3.Connection) -> List[str]:
    """获取有评论数据的视频 ID 列表"""
    rows = conn.execute(
        "SELECT DISTINCT aweme_id FROM comments ORDER BY aweme_id"
    ).fetchall()
    return [r["aweme_id"] for r in rows]


# ============================================================
# 分类操作（分析阶段，纯离线）
# ============================================================

def upsert_classification(conn: sqlite3.Connection, aweme_id: str,
                           category: str, detail: str = "{}") -> None:
    """写入视频分类结果"""
    conn.execute("""
        INSERT INTO videos_classification (aweme_id, content_category, category_detail, classified_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(aweme_id) DO UPDATE SET
            content_category = EXCLUDED.content_category,
            category_detail = EXCLUDED.category_detail,
            classified_at = EXCLUDED.classified_at
    """, (aweme_id, category, detail, _now()))


def get_category_distribution(conn: sqlite3.Connection) -> Dict[str, int]:
    """获取分类分布统计"""
    rows = conn.execute("""
        SELECT content_category, COUNT(*) as cnt
        FROM videos_classification WHERE content_category != ''
        GROUP BY content_category ORDER BY cnt DESC
    """).fetchall()
    return {r["content_category"]: r["cnt"] for r in rows}


def get_unclassified_videos(conn: sqlite3.Connection, limit: int = 1000) -> List[str]:
    """获取未分类的视频 ID"""
    rows = conn.execute("""
        SELECT b.aweme_id FROM videos_base b
        LEFT JOIN videos_classification c ON b.aweme_id = c.aweme_id
        WHERE c.aweme_id IS NULL OR c.content_category = ''
        LIMIT ?
    """, (limit,)).fetchall()
    return [r["aweme_id"] for r in rows]


def get_all_video_ids(conn: sqlite3.Connection) -> List[str]:
    rows = conn.execute("SELECT aweme_id FROM videos_base").fetchall()
    return [r["aweme_id"] for r in rows]


# ============================================================
# 评论标签操作（分析阶段，纯离线）
# ============================================================

def upsert_comment_tags(conn: sqlite3.Connection, aweme_id: str, tags: List[str]) -> None:
    conn.execute("""
        INSERT INTO videos_comment_tags (aweme_id, comment_tags, tagged_at)
        VALUES (?, ?, ?)
        ON CONFLICT(aweme_id) DO UPDATE SET
            comment_tags = EXCLUDED.comment_tags,
            tagged_at = EXCLUDED.tagged_at
    """, (aweme_id, _json_dumps(tags), _now()))


# ============================================================
# 书签操作（断点续传）
# ============================================================

def get_bookmark(conn: sqlite3.Connection, source: str) -> Dict[str, Any]:
    """获取书签（cursor + liked_time + 总数）"""
    row = conn.execute(
        "SELECT * FROM bookmark WHERE source = ?", (source,)
    ).fetchone()
    if row:
        return dict(row)
    return {"source": source, "last_cursor": "0", "last_liked_time": "", "total_fetched": 0}


def set_bookmark(conn: sqlite3.Connection, source: str,
                  cursor: str = "", liked_time: str = "", total_fetched: int = 0) -> None:
    conn.execute("""
        INSERT INTO bookmark (source, last_cursor, last_liked_time, total_fetched, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source) DO UPDATE SET
            last_cursor = COALESCE(NULLIF(EXCLUDED.last_cursor, ''), last_cursor),
            last_liked_time = COALESCE(NULLIF(EXCLUDED.last_liked_time, ''), last_liked_time),
            total_fetched = CASE
                WHEN EXCLUDED.total_fetched > 0 THEN EXCLUDED.total_fetched
                ELSE total_fetched
            END,
            updated_at = EXCLUDED.updated_at
    """, (source, cursor, liked_time, total_fetched, _now()))


# ============================================================
# 认证/运行状态操作
# ============================================================

def set_auth_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("""
        INSERT INTO auth_state (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
    """, (key, value, _now()))
    conn.commit()


def get_auth_state(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute(
        "SELECT value FROM auth_state WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else None


def is_cookie_valid(conn: sqlite3.Connection) -> bool:
    """检查 Cookie 是否已验证且未过期（24h 内）"""
    row = conn.execute(
        "SELECT value, updated_at FROM auth_state WHERE key = 'cookie_verified'"
    ).fetchone()
    if not row or row["value"] != "1":
        return False
    try:
        verified_at = datetime.fromisoformat(row["updated_at"])
        return datetime.now() - verified_at < timedelta(hours=24)
    except (ValueError, TypeError):
        return False


def set_cookie_verified(conn: sqlite3.Connection, valid: bool = True) -> None:
    set_auth_state(conn, "cookie_verified", "1" if valid else "0")


def set_run_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("""
        INSERT INTO run_state (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
    """, (key, value, _now()))
    conn.commit()


def get_run_state(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute(
        "SELECT value FROM run_state WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else default


def is_setup_done(conn: sqlite3.Connection) -> bool:
    return get_run_state(conn, "setup_done") == "true"


def mark_setup_done(conn: sqlite3.Connection) -> None:
    set_run_state(conn, "setup_done", "true")


def set_rules_meta(conn: sqlite3.Connection, rule_name: str, version: int, description: str = "") -> None:
    conn.execute("""
        INSERT INTO rules_meta (rule_name, version, description, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(rule_name) DO UPDATE SET
            version = EXCLUDED.version,
            description = EXCLUDED.description,
            updated_at = EXCLUDED.updated_at
    """, (rule_name, version, description, _now()))


def get_rules_meta(conn: sqlite3.Connection, rule_name: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM rules_meta WHERE rule_name = ?", (rule_name,)
    ).fetchone()
    return dict(row) if row else None


# ============================================================
# 汇总统计
# ============================================================

def get_summary(conn: sqlite3.Connection) -> Dict[str, Any]:
    """获取数据库概览统计"""
    vc = get_video_count(conn)
    ac = get_author_count(conn)
    cc = get_comment_count(conn)
    dl = get_download_stats(conn)
    cat = get_category_distribution(conn)

    videos_with_comments = conn.execute(
        "SELECT COUNT(DISTINCT aweme_id) FROM comments"
    ).fetchone()[0]

    return {
        "videos": vc,
        "authors": ac,
        "comments": cc,
        "videos_with_comments": videos_with_comments,
        "download": dl,
        "categories": cat,
        "category_count": len(cat),
        "setup_done": is_setup_done(conn),
        "cookie_valid": is_cookie_valid(conn),
    }


# ============================================================
# 分析消耗评估（用于分析阶段决策）
# ============================================================

def estimate_analysis_cost(conn: sqlite3.Connection) -> Dict[str, Any]:
    """评估(classify + portrait + tagger)的消耗"""
    video_count = get_video_count(conn)
    author_count = get_author_count(conn)
    unclassified = len(get_unclassified_videos(conn, limit=999999))
    videos_with_comments = conn.execute(
        "SELECT COUNT(DISTINCT aweme_id) FROM comments"
    ).fetchone()[0]

    cost_level = "low" if (unclassified < 5000 and author_count < 5000) else "high"
    rec = (
        "消耗较低，建议继续分析。" if unclassified < 3000
        else f"{unclassified} 个视频待分类 + {author_count} 个UP主待画像，消耗较大，建议确认后继续。"
    )

    return {
        "video_count": video_count,
        "unclassified": unclassified,
        "unclassified_ratio": round(unclassified / video_count * 100, 1) if video_count else 0,
        "author_count": author_count,
        "videos_with_comments": videos_with_comments,
        "estimated_cost": cost_level,
        "recommendation": rec,
    }


# ============================================================
# 数据迁移（v3 → v4）
# ============================================================

def migrate_v3_to_v4(v3_db_path: str, v4_db_path: str) -> Dict[str, Any]:
    """
    将 v3 数据库 (douku.db) 完整迁移到 v4 表结构 (douku_v4.db)
    
    迁移映射:
      v3 authors  → v4 authors_base + authors_stats + authors_portrait
      v3 videos   → v4 videos_base + videos_stats + videos_urls
                    + videos_meta + videos_classification + videos_download
      v3 comments → v4 comments
      v3 bookmark → v4 bookmark
    """
    v3 = sqlite3.connect(v3_db_path)
    v3.row_factory = sqlite3.Row

    v4 = init_db_v4(v4_db_path)

    result = {
        "authors": 0, "videos": 0, "comments": 0,
        "portrait": 0, "classify": 0, "download_done": 0,
        "bookmark": 0,
        "errors": [],
    }

    try:
        # ── 1. authors → authors_base + authors_stats + authors_portrait ──
        authors = v3.execute("SELECT * FROM authors").fetchall()
        author_id_to_sec = {}  # v3 author_id → sec_uid (for video FK mapping)
        for a in authors:
            ad = dict(a)
            sec_uid = ad["sec_uid"]
            author_id_to_sec[ad["id"]] = sec_uid
            upsert_author_base(v4, {
                "sec_uid": sec_uid,
                "nickname": ad.get("nickname", ""),
                "avatar": ad.get("avatar", ""),
                "signature": ad.get("signature", ""),
            })
            upsert_author_stats(v4, {
                "sec_uid": sec_uid,
                "follower_count": ad.get("follower_count", 0) or 0,
                "following_count": ad.get("following_count", 0) or 0,
                "aweme_count": ad.get("video_count", 0) or 0,
                "favoriting_count": ad.get("favoriting_count", 0) or 0,
            })
            # portrait data (v3 画像分析结果)
            pt = ad.get("portrait_track", "")
            pt2 = ad.get("portrait_track_2", "")
            if pt or pt2:
                upsert_author_portrait(v4, {
                    "sec_uid": sec_uid,
                    "portrait_track": pt,
                    "portrait_track_2": pt2,
                    "portrait_tags": _json_loads(ad.get("portrait_tags", "[]"), []),
                    "video_count": ad.get("video_count", 0) or 0,
                    "avg_digg": ad.get("avg_digg", 0) or 0,
                    "avg_duration": ad.get("avg_duration", 0) or 0,
                })
                result["portrait"] += 1
            result["authors"] += 1
        v4.commit()  # commit authors so video FKs can resolve

        # ── 2. videos → 6 v4 子表 ──
        videos = v3.execute("SELECT * FROM videos").fetchall()
        for v in videos:
            vd = dict(v)
            aweme_id = vd["aweme_id"]
            try:
                author_sec = author_id_to_sec.get(vd["author_id"], "")
                stats = _json_loads(vd.get("stats", "{}"), {})

                upsert_video_base(v4, {
                    "aweme_id": aweme_id,
                    "title": vd.get("title", ""),
                    "desc": vd.get("desc", ""),
                    "create_time": vd.get("create_time", ""),
                    "duration_sec": vd.get("duration_sec", 0),
                    "author_sec_uid": author_sec,
                    "video_tags": _json_loads(vd.get("video_tags", "[]"), []),
                    "hashtags": _json_loads(vd.get("hashtags", "[]"), []),
                    "share_url": vd.get("share_url", ""),
                })
                upsert_video_stats(v4, aweme_id, {
                    "digg_count": stats.get("digg", 0) or 0,
                    "comment_count": stats.get("comment", 0) or 0,
                    "share_count": stats.get("share", 0) or 0,
                })
                upsert_video_urls(v4, aweme_id, {
                    "video_url": vd.get("video_url", "") or "",
                    "cover_url": vd.get("cover_url", "") or "",
                })
                # videos_meta: 来源标记
                upsert_video_meta(v4, aweme_id, {
                    "in_likes": vd.get("in_likes", 0) or 0,
                    "in_favorites": vd.get("in_favorites", 0) or 0,
                })
                # videos_classification: 赛道分类
                cat = vd.get("content_category", "")
                if cat:
                    upsert_classification(v4, aweme_id, cat,
                        detail=vd.get("content_category_detail", "") or "{}")
                    result["classify"] += 1
                # videos_download: 下载状态
                init_download_status(v4, aweme_id)
                if vd.get("is_downloaded"):
                    set_download_status(v4, aweme_id, DL_DONE)
                    result["download_done"] += 1

                result["videos"] += 1
            except Exception as vid_err:
                result["errors"].append(f"{aweme_id}: {vid_err}")

        # ── 3. comments ──
        try:
            v3_comments = v3.execute("SELECT * FROM comments").fetchall()
            for c in v3_comments:
                upsert_comment(v4, dict(c))
                result["comments"] += 1
        except sqlite3.OperationalError:
            pass

        # ── 4. bookmark (断点续传书签) ──
        try:
            v3_bookmarks = v3.execute("SELECT * FROM bookmark").fetchall()
            for b in v3_bookmarks:
                bd = dict(b)
                set_bookmark(v4, bd.get("task_name", ""),
                    cursor=bd.get("cursor", ""),
                    liked_time=bd.get("liked_time", ""))
                result["bookmark"] += 1
        except sqlite3.OperationalError:
            pass

        # ── 5. auth_state ──
        try:
            state = v3.execute("SELECT * FROM auth_state").fetchall()
            for s in state:
                sd = dict(s)
                v4.execute(
                    "INSERT OR REPLACE INTO auth_state (key, value, updated_at) VALUES (?, ?, ?)",
                    (sd.get("key", ""), sd.get("value", ""), sd.get("updated_at", _now())))
        except sqlite3.OperationalError:
            pass

        # 标记迁移完成
        set_bookmark(v4, "migration", cursor="v4_complete")
        v4.commit()
    except Exception as e:
        result["errors"].append(str(e))
    finally:
        v3.close()
        v4.close()

    return result


# ============================================================
# 命令行测试
# ============================================================

if __name__ == "__main__":
    import sys
    db_path = sys.argv[1] if len(sys.argv) > 1 else "data/douku_v4.db"
    conn = init_db_v4(db_path)
    summary = get_summary(conn)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    conn.close()
