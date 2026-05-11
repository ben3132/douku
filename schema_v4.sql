-- ============================================================
-- 抖库 v4 Schema — 按更新频率分层
-- 低频: authors_base, videos_base
-- 中频: authors_stats, authors_portrait, videos_stats, videos_meta, videos_classification, videos_comment_tags
-- 高频: videos_urls, videos_download
-- 独立: comments, bookmark, auth_state, run_state, rules_meta
-- ============================================================

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ============================================================
-- 低频：基本不变，fetch 时首次写入即可
-- ============================================================

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

-- ============================================================
-- 中频：每周或按需刷新
-- ============================================================

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

-- ============================================================
-- 高频：每天或按使用刷新
-- ============================================================

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

-- ============================================================
-- 独立表
-- ============================================================

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

-- ============================================================
-- 索引
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_videos_author ON videos_base(author_sec_uid);
CREATE INDEX IF NOT EXISTS idx_videos_added ON videos_base(added_at);
CREATE INDEX IF NOT EXISTS idx_comments_aweme ON comments(aweme_id);
CREATE INDEX IF NOT EXISTS idx_comments_cid ON comments(cid);
CREATE INDEX IF NOT EXISTS idx_videos_dl_status ON videos_download(status);
CREATE INDEX IF NOT EXISTS idx_videos_urls_refreshed ON videos_urls(refreshed_at);
CREATE INDEX IF NOT EXISTS idx_videos_class_category ON videos_classification(content_category);
CREATE INDEX IF NOT EXISTS idx_videos_meta_likes ON videos_meta(in_likes);
CREATE INDEX IF NOT EXISTS idx_videos_meta_fav ON videos_meta(in_favorites);