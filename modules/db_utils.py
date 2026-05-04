"""
db_utils.py - SQLite 数据库工具
抖音点赞/收藏数据存储：videos(含in_likes/in_favorites标记) + authors(共享) + bookmark(按来源分行)
同一视频只存一条记录，通过标记字段区分属于哪个集合
"""

import sqlite3
import os
import json
from datetime import datetime

# 数据库文件路径（data/likes.db）
DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "likes.db"
)

# 合法来源值
VALID_SOURCES = ("likes", "favorites")


def get_conn():
    """获取数据库连接"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """初始化数据库，创建表结构（含迁移）"""
    conn = get_conn()
    c = conn.cursor()

    # ========== 先迁移旧结构（必须在 CREATE TABLE 之前） ==========
    _migrate(conn)

    # ========== authors 表（共享，含UP主画像字段） ==========
    c.execute("""
        CREATE TABLE IF NOT EXISTS authors (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            sec_uid         TEXT    UNIQUE NOT NULL,
            nickname        TEXT    DEFAULT '',
            avatar          TEXT    DEFAULT '',
            signature       TEXT    DEFAULT '',           -- 个人简介
            ip_location     TEXT    DEFAULT '',           -- IP属地
            follower_count  INTEGER DEFAULT 0,            -- 粉丝数
            following_count INTEGER DEFAULT 0,            -- 关注数
            aweme_count     INTEGER DEFAULT 0,            -- 作品数
            favoriting_count INTEGER DEFAULT 0,           -- 获赞数
            verification_type INTEGER DEFAULT 0,          -- 认证类型 0=无 1=个人 2=机构
            verification_label TEXT DEFAULT '',            -- 认证标签
            is_gov_media_vip INTEGER DEFAULT 0,           -- 是否政务/媒体
            -- 画像聚合字段（由 author_portrait.py 生成）
            portrait_tags   TEXT    DEFAULT '[]',         -- 主标签分布 [{tag, count, pct}]
            portrait_track  TEXT    DEFAULT '',           -- 主赛道（如: 二次元/cosplay/游戏）
            portrait_track_2 TEXT   DEFAULT '',           -- 副赛道
            video_count     INTEGER DEFAULT 0,            -- 被点赞/收藏的视频数
            avg_digg        REAL    DEFAULT 0,            -- 平均点赞
            avg_duration    REAL    DEFAULT 0,            -- 平均时长(秒)
            portrait_updated_at TEXT DEFAULT '',           -- 画像更新时间
            -- 元数据
            updated_at      TEXT    DEFAULT ''
        )
    """)

    # ========== videos 表（in_likes / in_favorites 标记） ==========
    c.execute("""
        CREATE TABLE IF NOT EXISTS videos (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            aweme_id        TEXT    UNIQUE NOT NULL,
            in_likes        INTEGER DEFAULT 0,     -- 1=在点赞列表中
            in_favorites    INTEGER DEFAULT 0,     -- 1=在收藏列表中
            title           TEXT    DEFAULT '',
            desc            TEXT    DEFAULT '',
            create_time     TEXT    DEFAULT '',
            liked_time      TEXT    DEFAULT '',
            type            TEXT    DEFAULT '',
            aweme_type_raw  INTEGER DEFAULT 0,
            duration_sec    INTEGER DEFAULT 0,
            author_id       INTEGER NOT NULL,
            video_tags      TEXT    DEFAULT '[]',
            hashtags        TEXT    DEFAULT '[]',
            desc_hashtags   TEXT    DEFAULT '[]',
            stats           TEXT    DEFAULT '{}',
            video_url       TEXT    DEFAULT '',
            cover_url       TEXT    DEFAULT '',
            music_url       TEXT    DEFAULT '',
            music_title     TEXT    DEFAULT '',
            share_url       TEXT    DEFAULT '',
            is_top          INTEGER DEFAULT 0,
            prevent_download INTEGER DEFAULT 0,
            -- 下载状态（只一份，不区分来源）
            is_downloaded   INTEGER DEFAULT 0,
            downloaded_at   TEXT    DEFAULT '',
            download_path   TEXT    DEFAULT '',
            download_error  TEXT    DEFAULT '',
            -- 评论标签（由 comment_tagger.py 生成）
            comment_tags    TEXT    DEFAULT '[]',         -- [{tag, source, weight}]
            comment_fetched INTEGER DEFAULT 0,           -- 1=已抓评论
            -- 数据刷新追踪
            video_data_updated_at TEXT DEFAULT '',       -- 视频动态数据(stats/comments/tags)最后刷新时间
            -- 内容分类（由 content_classifier.py 生成）
            content_category       TEXT DEFAULT '',      -- 最终分类标签(颜值/舞蹈/二次元/...)
            content_category_detail TEXT DEFAULT '{}',   -- 分类详情JSON{confidence,source,layer1,layer2}
            -- 元数据
            added_at        TEXT    DEFAULT '',
            FOREIGN KEY (author_id) REFERENCES authors(id)
        )
    """)

    # ========== comments 表（视频热评） ==========
    c.execute("""
        CREATE TABLE IF NOT EXISTS comments (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            aweme_id        TEXT    NOT NULL,
            cid             TEXT    UNIQUE NOT NULL,       -- 评论ID
            content         TEXT    DEFAULT '',            -- 评论内容
            user_name       TEXT    DEFAULT '',            -- 评论者昵称
            digg_count      INTEGER DEFAULT 0,             -- 点赞数
            reply_count     INTEGER DEFAULT 0,             -- 回复数
            is_hot          INTEGER DEFAULT 0,             -- 是否热评
            create_time     TEXT    DEFAULT '',            -- 评论时间
            ip_location     TEXT    DEFAULT '',            -- IP属地
            added_at        TEXT    DEFAULT '',
            FOREIGN KEY (aweme_id) REFERENCES videos(aweme_id)
        )
    """)

    # ========== bookmark 表（按来源分行） ==========
    c.execute("""
        CREATE TABLE IF NOT EXISTS bookmark (
            source          TEXT    PRIMARY KEY,
            last_cursor     TEXT    DEFAULT '0',
            last_liked_time TEXT    DEFAULT '',
            total_fetched   INTEGER DEFAULT 0,
            updated_at      TEXT    DEFAULT ''
        )
    """)
    for src in VALID_SOURCES:
        c.execute("INSERT OR IGNORE INTO bookmark (source) VALUES (?)", (src,))

    # ========== 索引 ==========
    c.execute("CREATE INDEX IF NOT EXISTS idx_videos_aweme_id ON videos(aweme_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_videos_in_likes ON videos(in_likes)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_videos_in_favorites ON videos(in_favorites)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_videos_author_id ON videos(author_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_videos_is_downloaded ON videos(is_downloaded)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_videos_type ON videos(type)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_authors_sec_uid ON authors(sec_uid)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_authors_portrait_track ON authors(portrait_track)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_authors_follower_count ON authors(follower_count)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_comments_aweme_id ON comments(aweme_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_comments_digg_count ON comments(digg_count)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_videos_content_category ON videos(content_category)")

    conn.commit()
    conn.close()
    print(f"[db] 数据库已初始化: {DB_PATH}")


def _migrate(conn):
    """迁移旧表结构到新结构"""
    # 检查 videos 表是否存在
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    if "videos" not in tables:
        return  # 全新数据库，无需迁移

    cols = [r[1] for r in conn.execute("PRAGMA table_info(videos)").fetchall()]

    # 1. 旧 source 列 → 先合并重复，再转为标记字段
    if "source" in cols and "in_likes" not in cols:
        # 1a. 先合并旧版因 source 不同导致的重复 aweme_id
        _merge_duplicate_aweme_ids(conn)
        # 1b. 添加标记列
        conn.execute("ALTER TABLE videos ADD COLUMN in_likes INTEGER DEFAULT 0")
        conn.execute("ALTER TABLE videos ADD COLUMN in_favorites INTEGER DEFAULT 0")
        conn.execute("UPDATE videos SET in_likes=1 WHERE source='likes'")
        conn.execute("UPDATE videos SET in_favorites=1 WHERE source='favorites'")
        # 1c. 删掉 source 列
        _rebuild_videos_drop_source(conn)
        print("[db] 迁移: source 列 → in_likes/in_favorites 标记")

    elif "source" not in cols and "in_likes" not in cols:
        # 更老版本：连 source 都没有
        conn.execute("ALTER TABLE videos ADD COLUMN in_likes INTEGER DEFAULT 1")
        conn.execute("ALTER TABLE videos ADD COLUMN in_favorites INTEGER DEFAULT 0")
        print("[db] 迁移: 添加 in_likes/in_favorites 列 (旧数据标记为点赞)")

    # 2. bookmark 表迁移
    bm_cols = [r[1] for r in conn.execute("PRAGMA table_info(bookmark)").fetchall()] if "bookmark" in tables else []
    if "id" in bm_cols and "source" not in bm_cols:
        old = conn.execute("SELECT last_cursor, last_liked_time, total_fetched FROM bookmark WHERE id=1").fetchone()
        if old:
            old_cursor = old[0] or "0"
            old_time = old[1] or ""
            old_total = old[2] or 0
            conn.execute("DROP TABLE bookmark")
            conn.execute("""
                CREATE TABLE bookmark (
                    source          TEXT    PRIMARY KEY,
                    last_cursor     TEXT    DEFAULT '0',
                    last_liked_time TEXT    DEFAULT '',
                    total_fetched   INTEGER DEFAULT 0,
                    updated_at      TEXT    DEFAULT ''
                )
            """)
            for src in VALID_SOURCES:
                if src == "likes":
                    conn.execute(
                        "INSERT INTO bookmark (source, last_cursor, last_liked_time, total_fetched) VALUES (?,?,?,?)",
                        (src, old_cursor, old_time, old_total)
                    )
                else:
                    conn.execute("INSERT INTO bookmark (source) VALUES (?)", (src,))
            print(f"[db] 迁移: bookmark 从单行改为按 source 分行 (保留了 likes cursor={old_cursor})")

    # 3. authors 表扩展（UP主画像字段）
    if "authors" in tables:
        auth_cols = [r[1] for r in conn.execute("PRAGMA table_info(authors)").fetchall()]
        new_cols = {
            "signature": "TEXT DEFAULT ''",
            "ip_location": "TEXT DEFAULT ''",
            "follower_count": "INTEGER DEFAULT 0",
            "following_count": "INTEGER DEFAULT 0",
            "aweme_count": "INTEGER DEFAULT 0",
            "favoriting_count": "INTEGER DEFAULT 0",
            "verification_type": "INTEGER DEFAULT 0",
            "verification_label": "TEXT DEFAULT ''",
            "is_gov_media_vip": "INTEGER DEFAULT 0",
            "portrait_tags": "TEXT DEFAULT '[]'",
            "portrait_track": "TEXT DEFAULT ''",
            "portrait_track_2": "TEXT DEFAULT ''",
            "video_count": "INTEGER DEFAULT 0",
            "avg_digg": "REAL DEFAULT 0",
            "avg_duration": "REAL DEFAULT 0",
            "portrait_updated_at": "TEXT DEFAULT ''",
        }
        added = []
        for col_name, col_def in new_cols.items():
            if col_name not in auth_cols:
                conn.execute(f"ALTER TABLE authors ADD COLUMN {col_name} {col_def}")
                added.append(col_name)
        if added:
            print(f"[db] 迁移: authors 表添加 {len(added)} 个画像字段: {', '.join(added[:5])}...")

    # 4. videos 表扩展（评论标签字段）
    if "videos" in tables:
        vid_cols = [r[1] for r in conn.execute("PRAGMA table_info(videos)").fetchall()]
        vid_new = {
            "comment_tags": "TEXT DEFAULT '[]'",
            "comment_fetched": "INTEGER DEFAULT 0",
        }
        added_v = []
        for col_name, col_def in vid_new.items():
            if col_name not in vid_cols:
                conn.execute(f"ALTER TABLE videos ADD COLUMN {col_name} {col_def}")
                added_v.append(col_name)
        if added_v:
            print(f"[db] 迁移: videos 表添加评论字段: {', '.join(added_v)}")

    # 5. videos 表扩展（数据刷新追踪字段）
    if "videos" in tables:
        vid_cols = [r[1] for r in conn.execute("PRAGMA table_info(videos)").fetchall()]
        if "video_data_updated_at" not in vid_cols:
            conn.execute("ALTER TABLE videos ADD COLUMN video_data_updated_at TEXT DEFAULT ''")
            print("[db] 迁移: videos 表添加 video_data_updated_at 字段")

    # 6. videos 表扩展（内容分类字段）
    if "videos" in tables:
        vid_cols = [r[1] for r in conn.execute("PRAGMA table_info(videos)").fetchall()]
        cat_new = {
            "content_category": "TEXT DEFAULT ''",
            "content_category_detail": "TEXT DEFAULT '{}'",
        }
        added_c = []
        for col_name, col_def in cat_new.items():
            if col_name not in vid_cols:
                conn.execute(f"ALTER TABLE videos ADD COLUMN {col_name} {col_def}")
                added_c.append(col_name)
        if added_c:
            print(f"[db] 迁移: videos 表添加分类字段: {', '.join(added_c)}")


def _rebuild_videos_drop_source(conn):
    """重建 videos 表，去掉 source 列"""
    # 获取现有列（除了 source）
    cols = [r[1] for r in conn.execute("PRAGMA table_info(videos)").fetchall() if r[1] != "source"]
    col_list = ", ".join(cols)
    conn.execute(f"""
        CREATE TABLE videos_new AS SELECT {col_list} FROM videos
    """)
    conn.execute("DROP TABLE videos")
    conn.execute("ALTER TABLE videos_new RENAME TO videos")


def _merge_duplicate_aweme_ids(conn):
    """合并旧版因 source 不同导致的重复 aweme_id 记录"""
    dupes = conn.execute("""
        SELECT aweme_id, COUNT(*) as cnt, 
               GROUP_CONCAT(source) as sources
        FROM videos 
        GROUP BY aweme_id 
        HAVING cnt > 1
    """).fetchall()
    
    if not dupes:
        return
    
    merged = 0
    for d in dupes:
        aweme_id = d["aweme_id"]
        sources = d["sources"] or ""
        rows = conn.execute(
            "SELECT id, source FROM videos WHERE aweme_id=? ORDER BY id", (aweme_id,)
        ).fetchall()
        
        # 保留第一条（id 最小），删除其余
        keep_id = rows[0]["id"]
        del_ids = [r["id"] for r in rows[1:]]
        
        # 设置标记
        in_likes = 1 if "likes" in sources else 0
        in_favorites = 1 if "favorites" in sources else 0
        conn.execute("UPDATE videos SET in_likes=?, in_favorites=? WHERE id=?",
                      (in_likes, in_favorites, keep_id))
        conn.execute(f"DELETE FROM videos WHERE id IN ({','.join('?' * len(del_ids))})", del_ids)
        merged += 1
    
    if merged:
        print(f"[db] 迁移: 合并了 {merged} 组重复的 aweme_id 记录")


# ============================================================
# 写入操作
# ============================================================

def upsert_author(conn, sec_uid, nickname="", avatar=""):
    """插入或更新作者，返回 author_id"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute("SELECT id FROM authors WHERE sec_uid=?", (sec_uid,)).fetchone()
    if row:
        conn.execute(
            "UPDATE authors SET nickname=?, avatar=?, updated_at=? WHERE sec_uid=?",
            (nickname, avatar, now, sec_uid)
        )
        return row["id"]
    else:
        conn.execute(
            "INSERT INTO authors (sec_uid, nickname, avatar, updated_at) VALUES (?,?,?,?)",
            (sec_uid, nickname, avatar, now)
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def update_author_profile(conn, sec_uid, profile: dict):
    """更新UP主详细资料（由 fetch_up_profiles.py 调用）"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        UPDATE authors SET
            nickname=?, avatar=?, signature=?, ip_location=?,
            follower_count=?, following_count=?, aweme_count=?, favoriting_count=?,
            verification_type=?, verification_label=?, is_gov_media_vip=?,
            updated_at=?
        WHERE sec_uid=?
    """, (
        profile.get("nickname", ""),
        profile.get("avatar", ""),
        profile.get("signature", ""),
        profile.get("ip_location", ""),
        profile.get("follower_count", 0),
        profile.get("following_count", 0),
        profile.get("aweme_count", 0),
        profile.get("favoriting_count", 0),
        profile.get("verification_type", 0),
        profile.get("verification_label", ""),
        1 if profile.get("is_gov_media_vip") else 0,
        now, sec_uid,
    ))


def update_author_portrait(conn, sec_uid, portrait: dict):
    """更新UP主画像聚合数据（由 author_portrait.py 调用）"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        UPDATE authors SET
            portrait_tags=?, portrait_track=?, portrait_track_2=?,
            video_count=?, avg_digg=?, avg_duration=?,
            portrait_updated_at=?
        WHERE sec_uid=?
    """, (
        json.dumps(portrait.get("tags", []), ensure_ascii=False),
        portrait.get("track", ""),
        portrait.get("track_2", ""),
        portrait.get("video_count", 0),
        portrait.get("avg_digg", 0),
        portrait.get("avg_duration", 0),
        now, sec_uid,
    ))


def insert_comments(conn, aweme_id, comments: list):
    """批量插入评论，返回新增数量"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new = 0
    for c in comments:
        try:
            conn.execute("""
                INSERT OR IGNORE INTO comments
                    (aweme_id, cid, content, user_name, digg_count, reply_count,
                     is_hot, create_time, ip_location, added_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                aweme_id,
                c.get("cid", ""),
                c.get("text", ""),
                c.get("user", {}).get("nickname", ""),
                c.get("digg_count", 0) or 0,
                c.get("reply_comment_total", 0) or 0,
                1 if c.get("is_hot") else 0,
                c.get("create_time", ""),
                c.get("ip_label", ""),
                now,
            ))
            if conn.total_changes:
                new += 1
        except Exception:
            pass
    return new


def update_video_comment_tags(conn, aweme_id, tags: list):
    """更新视频的评论标签"""
    conn.execute("UPDATE videos SET comment_tags=?, comment_fetched=1 WHERE aweme_id=?",
                (json.dumps(tags, ensure_ascii=False), aweme_id))


def upsert_video(conn, parsed: dict, author_id: int, source: str = "likes"):
    """插入或更新单条视频，返回是否新增 (True/False)
    
    source: "likes" 或 "favorites"
    同一 aweme_id 只有一条记录，通过 in_likes/in_favorites 标记所属集合
    如果视频已在另一集合中，只追加标记，不创建新记录
    """
    aweme_id = parsed["aweme_id"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    source = source if source in VALID_SOURCES else "likes"

    existing = conn.execute(
        "SELECT id, in_likes, in_favorites FROM videos WHERE aweme_id=?", (aweme_id,)
    ).fetchone()

    if existing:
        # 已存在 → 更新内容 + 追加来源标记
        flag_field = "in_likes" if source == "likes" else "in_favorites"
        conn.execute(f"""
            UPDATE videos SET
                title=?, desc=?, create_time=?, liked_time=?,
                type=?, aweme_type_raw=?, duration_sec=?,
                author_id=?,
                video_tags=?, hashtags=?, desc_hashtags=?,
                stats=?, video_url=?, cover_url=?,
                music_url=?, music_title=?, share_url=?,
                is_top=?, prevent_download=?,
                {flag_field}=1
            WHERE aweme_id=?
        """, (
            parsed["item_title"], parsed["desc"],
            parsed["create_time"], parsed["liked_time"],
            parsed["type"], parsed["aweme_type_raw"], parsed["duration_sec"],
            author_id,
            json.dumps(parsed["video_tags"], ensure_ascii=False),
            json.dumps(parsed["hashtags"], ensure_ascii=False),
            json.dumps(parsed["desc_hashtags"], ensure_ascii=False),
            json.dumps(parsed["stats"], ensure_ascii=False),
            parsed["urls"]["video"], parsed["urls"]["cover"],
            parsed["urls"]["music"], parsed["music"]["title"],
            parsed["urls"]["share"],
            int(parsed["is_top"]), int(parsed["prevent_download"]),
            aweme_id,
        ))
        return False  # 非新增
    else:
        # 新视频
        in_likes = 1 if source == "likes" else 0
        in_favorites = 1 if source == "favorites" else 0
        conn.execute("""
            INSERT INTO videos (
                aweme_id, in_likes, in_favorites,
                title, desc, create_time, liked_time,
                type, aweme_type_raw, duration_sec, author_id,
                video_tags, hashtags, desc_hashtags, stats,
                video_url, cover_url, music_url, music_title, share_url,
                is_top, prevent_download, added_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            aweme_id, in_likes, in_favorites,
            parsed["item_title"], parsed["desc"],
            parsed["create_time"], parsed["liked_time"],
            parsed["type"], parsed["aweme_type_raw"], parsed["duration_sec"],
            author_id,
            json.dumps(parsed["video_tags"], ensure_ascii=False),
            json.dumps(parsed["hashtags"], ensure_ascii=False),
            json.dumps(parsed["desc_hashtags"], ensure_ascii=False),
            json.dumps(parsed["stats"], ensure_ascii=False),
            parsed["urls"]["video"], parsed["urls"]["cover"],
            parsed["urls"]["music"], parsed["music"]["title"],
            parsed["urls"]["share"],
            int(parsed["is_top"]), int(parsed["prevent_download"]),
            now,
        ))
        return True  # 新增


def update_bookmark(conn, source, last_cursor, last_liked_time, new_count):
    """更新书签（按来源）"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    source = source if source in VALID_SOURCES else "likes"
    conn.execute("""
        UPDATE bookmark SET
            last_cursor=?,
            last_liked_time=?,
            total_fetched=total_fetched+?,
            updated_at=?
        WHERE source=?
    """, (str(last_cursor), last_liked_time, new_count, now, source))


def get_bookmark(conn, source="likes"):
    """读取书签（按来源）"""
    source = source if source in VALID_SOURCES else "likes"
    row = conn.execute("SELECT * FROM bookmark WHERE source=?", (source,)).fetchone()
    return dict(row) if row else None


def reset_bookmark(conn, source="likes"):
    """重置书签"""
    source = source if source in VALID_SOURCES else "likes"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        UPDATE bookmark SET
            last_cursor='0',
            last_liked_time='',
            total_fetched=0,
            updated_at=?
        WHERE source=?
    """, (now, source))


# ============================================================
# 查询操作
# ============================================================

def get_video_count(conn, source=None):
    """视频总数，可按来源筛选"""
    if source == "likes":
        return conn.execute("SELECT COUNT(*) FROM videos WHERE in_likes=1").fetchone()[0]
    elif source == "favorites":
        return conn.execute("SELECT COUNT(*) FROM videos WHERE in_favorites=1").fetchone()[0]
    return conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]

def get_author_count(conn):
    return conn.execute("SELECT COUNT(*) FROM authors").fetchone()[0]

def get_download_stats(conn, source=None):
    """下载统计，可按来源筛选"""
    if source == "likes":
        r = conn.execute("""
            SELECT
                SUM(CASE WHEN is_downloaded=0 THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN is_downloaded=1 THEN 1 ELSE 0 END) AS done,
                SUM(CASE WHEN is_downloaded=2 THEN 1 ELSE 0 END) AS failed
            FROM videos WHERE in_likes=1
        """).fetchone()
    elif source == "favorites":
        r = conn.execute("""
            SELECT
                SUM(CASE WHEN is_downloaded=0 THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN is_downloaded=1 THEN 1 ELSE 0 END) AS done,
                SUM(CASE WHEN is_downloaded=2 THEN 1 ELSE 0 END) AS failed
            FROM videos WHERE in_favorites=1
        """).fetchone()
    else:
        r = conn.execute("""
            SELECT
                SUM(CASE WHEN is_downloaded=0 THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN is_downloaded=1 THEN 1 ELSE 0 END) AS done,
                SUM(CASE WHEN is_downloaded=2 THEN 1 ELSE 0 END) AS failed
            FROM videos
        """).fetchone()
    return dict(r)

def get_tag_distribution(conn, source=None, limit=30):
    """标签分布统计"""
    if source == "likes":
        rows = conn.execute("SELECT video_tags FROM videos WHERE in_likes=1").fetchall()
    elif source == "favorites":
        rows = conn.execute("SELECT video_tags FROM videos WHERE in_favorites=1").fetchall()
    else:
        rows = conn.execute("SELECT video_tags FROM videos").fetchall()
    counter = {}
    for row in rows:
        for tag in json.loads(row["video_tags"]):
            name = tag.get("tag_name", "")
            if name:
                counter[name] = counter.get(name, 0) + 1
    sorted_tags = sorted(counter.items(), key=lambda x: -x[1])
    return sorted_tags[:limit]


def get_source_summary(conn):
    """按来源汇总统计"""
    rows = conn.execute("""
        SELECT
            SUM(in_likes) AS likes_total,
            SUM(in_favorites) AS favorites_total,
            SUM(CASE WHEN in_likes=1 AND is_downloaded=0 THEN 1 ELSE 0 END) AS likes_pending,
            SUM(CASE WHEN in_likes=1 AND is_downloaded=1 THEN 1 ELSE 0 END) AS likes_done,
            SUM(CASE WHEN in_likes=1 AND is_downloaded=2 THEN 1 ELSE 0 END) AS likes_failed,
            SUM(CASE WHEN in_favorites=1 AND is_downloaded=0 THEN 1 ELSE 0 END) AS fav_pending,
            SUM(CASE WHEN in_favorites=1 AND is_downloaded=1 THEN 1 ELSE 0 END) AS fav_done,
            SUM(CASE WHEN in_favorites=1 AND is_downloaded=2 THEN 1 ELSE 0 END) AS fav_failed
        FROM videos
    """).fetchone()
    return dict(rows)


# ============================================================
# 数据刷新操作
# ============================================================

def update_video_data_refreshed(conn, aweme_id):
    """标记视频动态数据已刷新"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE videos SET video_data_updated_at=? WHERE aweme_id=?",
        (now, aweme_id)
    )


def update_video_stats_and_tags(conn, aweme_id, parsed: dict, author_id: int):
    """更新视频动态数据（stats/tags/comments count等），由 refresh_data.py 调用
    不更新下载链接等下载相关字段，也不修改 in_likes/in_favorites 标记
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        UPDATE videos SET
            title=?, desc=?,
            video_tags=?, hashtags=?, desc_hashtags=?,
            stats=?, cover_url=?, music_title=?, share_url=?,
            duration_sec=?, type=?, aweme_type_raw=?,
            video_data_updated_at=?
        WHERE aweme_id=?
    """, (
        parsed["item_title"], parsed["desc"],
        json.dumps(parsed["video_tags"], ensure_ascii=False),
        json.dumps(parsed["hashtags"], ensure_ascii=False),
        json.dumps(parsed["desc_hashtags"], ensure_ascii=False),
        json.dumps(parsed["stats"], ensure_ascii=False),
        parsed["urls"]["cover"],
        parsed["music"]["title"],
        parsed["urls"]["share"],
        parsed["duration_sec"],
        parsed["type"],
        parsed["aweme_type_raw"],
        now, aweme_id,
    ))


def query_videos_needing_refresh(conn, days=7, limit=0, aweme_id=None, force=False):
    """查询需要刷新动态数据的视频
    
    days: 超过N天未刷新的视频需要刷新
    limit: 最多返回N条 (0=不限)
    aweme_id: 指定视频
    force: 忽略时间限制，全部刷新
    """
    if aweme_id:
        return [{"aweme_id": aweme_id, "title": "", "video_data_updated_at": "", "digg": 0}]

    if force:
        where = "1=1"
    else:
        cutoff = (datetime.now() - __import__('datetime').timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        where = f"(video_data_updated_at = '' OR video_data_updated_at < '{cutoff}')"

    sql = f"""
        SELECT v.aweme_id,
               COALESCE(v.title, v.desc, '') as title,
               v.video_data_updated_at,
               COALESCE(json_extract(v.stats, '$.digg'), 0) as digg
        FROM videos v
        WHERE {where}
        ORDER BY json_extract(v.stats, '$.digg') DESC
    """

    if limit > 0:
        sql += f" LIMIT {limit}"

    rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def query_authors_needing_refresh(conn, days=30, limit=0, sec_uid=None, force=False):
    """查询需要刷新画像的UP主
    
    days: 超过N天未刷新的UP主需要刷新
    limit: 最多返回N条 (0=不限)
    sec_uid: 指定UP主
    force: 忽略时间限制，全部刷新
    """
    if sec_uid:
        return [{"sec_uid": sec_uid, "nickname": "", "sample_aweme_id": "", "updated_at": "", "video_count": 0}]

    if force:
        where = "1=1"
    else:
        cutoff = (datetime.now() - __import__('datetime').timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        where = f"(a.updated_at = '' OR a.updated_at < '{cutoff}')"

    sql = f"""
        SELECT a.sec_uid, a.nickname,
               MIN(v.aweme_id) as sample_aweme_id,
               a.updated_at,
               COUNT(v.id) as video_count
        FROM authors a
        JOIN videos v ON v.author_id = a.id
        WHERE {where}
        GROUP BY a.id
        ORDER BY video_count DESC
    """

    if limit > 0:
        sql += f" LIMIT {limit}"

    rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def replace_comments_for_video(conn, aweme_id, comments: list):
    """替换某个视频的所有评论（先删后插），用于刷新评论数据"""
    conn.execute("DELETE FROM comments WHERE aweme_id=?", (aweme_id,))
    new = insert_comments(conn, aweme_id, comments)
    return new


# ============================================================
# 自检
# ============================================================
if __name__ == "__main__":
    import io as _io, sys as _sys
    _sys.stdout = _io.TextIOWrapper(_sys.stdout.buffer, encoding='utf-8', errors='replace')
    init_db()
    conn = get_conn()
    total = get_video_count(conn)
    likes = get_video_count(conn, source="likes")
    favs = get_video_count(conn, source="favorites")
    both = conn.execute("SELECT COUNT(*) FROM videos WHERE in_likes=1 AND in_favorites=1").fetchone()[0]
    print(f"  视频总数: {total}")
    print(f"  [点赞] {likes} 条")
    print(f"  [收藏] {favs} 条")
    print(f"  [同时点赞+收藏] {both} 条")
    print(f"  作者数: {get_author_count(conn)}")
    for src in VALID_SOURCES:
        bm = get_bookmark(conn, source=src)
        dl = get_download_stats(conn, source=src)
        print(f"\n  [{src}]")
        print(f"    书签: cursor={bm['last_cursor']}, total_fetched={bm['total_fetched']}")
        print(f"    下载: 未下载={dl.get('pending',0) or 0}, 已下载={dl.get('done',0) or 0}, 失败={dl.get('failed',0) or 0}")
    conn.close()
