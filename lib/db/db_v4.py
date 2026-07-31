"""DouKU MySQL 数据访问层。

表按更新频率拆分：基础信息低频、统计/URL 中高频、下载任务独立更新。
所有业务数据只写 MySQL；文件系统仅保存媒体文件。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from lib.utils.meta import get_data_root, load_config

DL_PENDING, DL_DOWNLOADING, DL_DONE, DL_FAILED, DL_EXPIRED = range(5)

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS authors_base (
      sec_uid VARCHAR(128) PRIMARY KEY,
      nickname VARCHAR(255) NOT NULL DEFAULT '',
      avatar_url TEXT,
      signature TEXT,
      ip_location VARCHAR(64) NOT NULL DEFAULT '',
      created_at DATETIME NOT NULL,
      updated_at DATETIME NOT NULL,
      INDEX idx_authors_nickname (nickname)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS authors_stats (
      sec_uid VARCHAR(128) PRIMARY KEY,
      follower_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
      following_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
      aweme_count INT UNSIGNED NOT NULL DEFAULT 0,
      favoriting_count INT UNSIGNED NOT NULL DEFAULT 0,
      updated_at DATETIME NOT NULL,
      CONSTRAINT fk_author_stats FOREIGN KEY (sec_uid)
        REFERENCES authors_base(sec_uid) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS videos_base (
      aweme_id VARCHAR(32) PRIMARY KEY,
      file_code INT UNSIGNED NOT NULL AUTO_INCREMENT,
      title VARCHAR(512) NOT NULL DEFAULT '',
      description TEXT,
      create_time BIGINT UNSIGNED NOT NULL DEFAULT 0,
      content_type ENUM('video','image','unknown') NOT NULL DEFAULT 'unknown',
      aweme_type_raw SMALLINT NOT NULL DEFAULT 0,
      duration_ms INT UNSIGNED NOT NULL DEFAULT 0,
      author_sec_uid VARCHAR(128) NOT NULL,
      share_url TEXT,
      is_top BOOLEAN NOT NULL DEFAULT FALSE,
      prevent_download BOOLEAN NOT NULL DEFAULT FALSE,
      tags JSON,
      hashtags JSON,
      first_seen_at DATETIME NOT NULL,
      updated_at DATETIME NOT NULL,
      UNIQUE KEY uq_videos_file_code (file_code),
      INDEX idx_videos_author_time (author_sec_uid, create_time DESC),
      INDEX idx_videos_create_time (create_time DESC),
      CONSTRAINT fk_video_author FOREIGN KEY (author_sec_uid)
        REFERENCES authors_base(sec_uid)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS video_sources (
      aweme_id VARCHAR(32) NOT NULL,
      source ENUM('likes','favorites','details') NOT NULL,
      position_no INT UNSIGNED NOT NULL DEFAULT 0,
      captured_at DATETIME NOT NULL,
      PRIMARY KEY (aweme_id, source),
      INDEX idx_source_order (source, captured_at DESC, position_no ASC),
      CONSTRAINT fk_source_video FOREIGN KEY (aweme_id)
        REFERENCES videos_base(aweme_id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS videos_stats (
      aweme_id VARCHAR(32) PRIMARY KEY,
      digg_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
      comment_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
      share_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
      collect_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
      play_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
      updated_at DATETIME NOT NULL,
      INDEX idx_stats_digg (digg_count DESC),
      CONSTRAINT fk_stats_video FOREIGN KEY (aweme_id)
        REFERENCES videos_base(aweme_id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS video_urls (
      aweme_id VARCHAR(32) PRIMARY KEY,
      video_url LONGTEXT,
      cover_url LONGTEXT,
      music_url LONGTEXT,
      music_title VARCHAR(512) NOT NULL DEFAULT '',
      refreshed_at DATETIME NOT NULL,
      expires_at DATETIME NULL,
      url_status TINYINT NOT NULL DEFAULT 1,
      INDEX idx_urls_status_refresh (url_status, refreshed_at),
      CONSTRAINT fk_url_video FOREIGN KEY (aweme_id)
        REFERENCES videos_base(aweme_id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS media_assets (
      id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
      aweme_id VARCHAR(32) NOT NULL,
      asset_type ENUM('cover','image','music') NOT NULL,
      position_no INT UNSIGNED NOT NULL DEFAULT 0,
      remote_url LONGTEXT,
      local_path VARCHAR(1024) NOT NULL DEFAULT '',
      status TINYINT NOT NULL DEFAULT 0,
      download_error VARCHAR(1000) NOT NULL DEFAULT '',
      updated_at DATETIME NOT NULL,
      UNIQUE KEY uq_asset (aweme_id, asset_type, position_no),
      INDEX idx_assets_download (status, asset_type, aweme_id),
      CONSTRAINT fk_asset_video FOREIGN KEY (aweme_id)
        REFERENCES videos_base(aweme_id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS download_tasks (
      aweme_id VARCHAR(32) PRIMARY KEY,
      status TINYINT NOT NULL DEFAULT 0,
      priority SMALLINT NOT NULL DEFAULT 0,
      downloaded_at DATETIME NULL,
      video_path VARCHAR(1024) NOT NULL DEFAULT '',
      download_error VARCHAR(1000) NOT NULL DEFAULT '',
      retry_count SMALLINT UNSIGNED NOT NULL DEFAULT 0,
      next_retry_at DATETIME NULL,
      updated_at DATETIME NOT NULL,
      INDEX idx_download_queue (status, priority DESC, updated_at),
      CONSTRAINT fk_download_video FOREIGN KEY (aweme_id)
        REFERENCES videos_base(aweme_id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS videos_classification (
      aweme_id VARCHAR(32) PRIMARY KEY,
      content_category VARCHAR(64) NOT NULL DEFAULT '',
      category_detail JSON,
      classified_at DATETIME NOT NULL,
      INDEX idx_category (content_category, classified_at),
      CONSTRAINT fk_class_video FOREIGN KEY (aweme_id)
        REFERENCES videos_base(aweme_id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS comments (
      cid VARCHAR(64) PRIMARY KEY,
      aweme_id VARCHAR(32) NOT NULL,
      content TEXT,
      user_name VARCHAR(255) NOT NULL DEFAULT '',
      digg_count INT UNSIGNED NOT NULL DEFAULT 0,
      reply_count INT UNSIGNED NOT NULL DEFAULT 0,
      is_hot BOOLEAN NOT NULL DEFAULT FALSE,
      create_time BIGINT UNSIGNED NOT NULL DEFAULT 0,
      ip_location VARCHAR(64) NOT NULL DEFAULT '',
      captured_at DATETIME NOT NULL,
      INDEX idx_comments_video_time (aweme_id, create_time DESC),
      CONSTRAINT fk_comment_video FOREIGN KEY (aweme_id)
        REFERENCES videos_base(aweme_id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS sync_cursors (
      source VARCHAR(32) PRIMARY KEY,
      last_cursor VARCHAR(255) NOT NULL DEFAULT '0',
      total_fetched BIGINT UNSIGNED NOT NULL DEFAULT 0,
      updated_at DATETIME NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS run_state (
      state_key VARCHAR(128) PRIMARY KEY,
      state_value JSON,
      updated_at DATETIME NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS account_profiles (
      account_key VARCHAR(80) PRIMARY KEY,
      platform VARCHAR(32) NOT NULL DEFAULT 'douyin',
      platform_user_id VARCHAR(255) NOT NULL DEFAULT '',
      nickname VARCHAR(255) NOT NULL DEFAULT '',
      created_at DATETIME NOT NULL,
      updated_at DATETIME NOT NULL,
      INDEX idx_account_platform_user (platform, platform_user_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS account_video_sources (
      account_key VARCHAR(80) NOT NULL,
      aweme_id VARCHAR(32) NOT NULL,
      source ENUM('likes','favorites','details') NOT NULL,
      position_no INT UNSIGNED NOT NULL DEFAULT 0,
      captured_at DATETIME NOT NULL,
      PRIMARY KEY (account_key, aweme_id, source),
      INDEX idx_account_source_order
        (account_key, source, captured_at DESC, position_no ASC),
      CONSTRAINT fk_account_source_profile FOREIGN KEY (account_key)
        REFERENCES account_profiles(account_key) ON DELETE CASCADE,
      CONSTRAINT fk_account_source_video FOREIGN KEY (aweme_id)
        REFERENCES videos_base(aweme_id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS account_download_tasks (
      account_key VARCHAR(80) NOT NULL,
      aweme_id VARCHAR(32) NOT NULL,
      status TINYINT NOT NULL DEFAULT 0,
      priority SMALLINT NOT NULL DEFAULT 0,
      downloaded_at DATETIME NULL,
      video_path VARCHAR(1024) NOT NULL DEFAULT '',
      download_error VARCHAR(1000) NOT NULL DEFAULT '',
      retry_count SMALLINT UNSIGNED NOT NULL DEFAULT 0,
      next_retry_at DATETIME NULL,
      updated_at DATETIME NOT NULL,
      PRIMARY KEY (account_key, aweme_id),
      INDEX idx_account_download_queue
        (account_key, status, priority DESC, updated_at),
      CONSTRAINT fk_account_download_profile FOREIGN KEY (account_key)
        REFERENCES account_profiles(account_key) ON DELETE CASCADE,
      CONSTRAINT fk_account_download_video FOREIGN KEY (aweme_id)
        REFERENCES videos_base(aweme_id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS account_media_files (
      account_key VARCHAR(80) NOT NULL,
      media_asset_id BIGINT UNSIGNED NOT NULL,
      local_path VARCHAR(1024) NOT NULL DEFAULT '',
      status TINYINT NOT NULL DEFAULT 0,
      download_error VARCHAR(1000) NOT NULL DEFAULT '',
      updated_at DATETIME NOT NULL,
      PRIMARY KEY (account_key, media_asset_id),
      INDEX idx_account_media_status (account_key, status),
      CONSTRAINT fk_account_media_profile FOREIGN KEY (account_key)
        REFERENCES account_profiles(account_key) ON DELETE CASCADE,
      CONSTRAINT fk_account_media_asset FOREIGN KEY (media_asset_id)
        REFERENCES media_assets(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS account_sync_cursors (
      account_key VARCHAR(80) NOT NULL,
      source VARCHAR(32) NOT NULL,
      last_cursor VARCHAR(255) NOT NULL DEFAULT '0',
      total_fetched BIGINT UNSIGNED NOT NULL DEFAULT 0,
      updated_at DATETIME NOT NULL,
      PRIMARY KEY (account_key, source),
      CONSTRAINT fk_account_cursor_profile FOREIGN KEY (account_key)
        REFERENCES account_profiles(account_key) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS direct_download_jobs (
      id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
      input_url TEXT NOT NULL,
      canonical_url TEXT,
      platform VARCHAR(64) NOT NULL DEFAULT 'generic',
      extractor VARCHAR(128) NOT NULL DEFAULT '',
      content_id VARCHAR(255) NOT NULL DEFAULT '',
      title VARCHAR(512) NOT NULL DEFAULT '',
      uploader VARCHAR(255) NOT NULL DEFAULT '',
      status TINYINT NOT NULL DEFAULT 0,
      download_error VARCHAR(1000) NOT NULL DEFAULT '',
      created_at DATETIME NOT NULL,
      updated_at DATETIME NOT NULL,
      INDEX idx_direct_jobs_status (status, updated_at),
      INDEX idx_direct_jobs_platform (platform, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS direct_media_urls (
      id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
      job_id BIGINT UNSIGNED NOT NULL,
      format_id VARCHAR(128) NOT NULL DEFAULT '',
      media_role ENUM('combined','video','audio','manifest','thumbnail')
        NOT NULL DEFAULT 'combined',
      remote_url LONGTEXT NOT NULL,
      protocol VARCHAR(32) NOT NULL DEFAULT '',
      ext VARCHAR(16) NOT NULL DEFAULT '',
      width INT UNSIGNED NULL,
      height INT UNSIGNED NULL,
      filesize BIGINT UNSIGNED NULL,
      selected BOOLEAN NOT NULL DEFAULT FALSE,
      created_at DATETIME NOT NULL,
      UNIQUE KEY uq_direct_format (job_id, format_id, media_role),
      INDEX idx_direct_selected (job_id, selected, media_role),
      CONSTRAINT fk_direct_url_job FOREIGN KEY (job_id)
        REFERENCES direct_download_jobs(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS direct_download_files (
      id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
      job_id BIGINT UNSIGNED NOT NULL,
      file_type VARCHAR(32) NOT NULL DEFAULT 'media',
      local_path VARCHAR(1024) NOT NULL,
      file_size BIGINT UNSIGNED NOT NULL DEFAULT 0,
      created_at DATETIME NOT NULL,
      UNIQUE KEY uq_direct_file (job_id, local_path(512)),
      CONSTRAINT fk_direct_file_job FOREIGN KEY (job_id)
        REFERENCES direct_download_jobs(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS like_search_jobs (
      id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
      account_key VARCHAR(80) NOT NULL,
      status TINYINT NOT NULL DEFAULT 0,
      result_count INT UNSIGNED NOT NULL DEFAULT 0,
      error_message VARCHAR(1000) NOT NULL DEFAULT '',
      created_at DATETIME NOT NULL,
      updated_at DATETIME NOT NULL,
      INDEX idx_like_search_account (account_key,created_at DESC),
      CONSTRAINT fk_like_search_account FOREIGN KEY (account_key)
        REFERENCES account_profiles(account_key) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS like_search_terms (
      job_id BIGINT UNSIGNED NOT NULL,
      keyword VARCHAR(100) NOT NULL,
      term_order SMALLINT UNSIGNED NOT NULL DEFAULT 0,
      result_count INT UNSIGNED NOT NULL DEFAULT 0,
      PRIMARY KEY (job_id,keyword),
      CONSTRAINT fk_like_search_term_job FOREIGN KEY (job_id)
        REFERENCES like_search_jobs(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS like_search_results (
      job_id BIGINT UNSIGNED NOT NULL,
      keyword VARCHAR(100) NOT NULL,
      aweme_id VARCHAR(32) NOT NULL,
      position_no INT UNSIGNED NOT NULL DEFAULT 0,
      captured_at DATETIME NOT NULL,
      PRIMARY KEY (job_id,keyword,aweme_id),
      INDEX idx_like_search_download (job_id,position_no,aweme_id),
      CONSTRAINT fk_like_search_result_job FOREIGN KEY (job_id)
        REFERENCES like_search_jobs(id) ON DELETE CASCADE,
      CONSTRAINT fk_like_search_result_video FOREIGN KEY (aweme_id)
        REFERENCES videos_base(aweme_id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS creator_profiles (
      id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
      platform VARCHAR(32) NOT NULL,
      platform_creator_id VARCHAR(255) NOT NULL,
      nickname VARCHAR(255) NOT NULL DEFAULT '',
      profile_url TEXT NOT NULL,
      last_sync_at DATETIME NULL,
      created_at DATETIME NOT NULL,
      updated_at DATETIME NOT NULL,
      UNIQUE KEY uq_creator_platform (platform,platform_creator_id),
      INDEX idx_creator_nickname (platform,nickname)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS creator_sync_jobs (
      id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
      creator_id BIGINT UNSIGNED NOT NULL,
      mode ENUM('inspect','fetch','sync') NOT NULL DEFAULT 'sync',
      status TINYINT NOT NULL DEFAULT 0,
      pages_fetched INT UNSIGNED NOT NULL DEFAULT 0,
      works_seen INT UNSIGNED NOT NULL DEFAULT 0,
      new_works INT UNSIGNED NOT NULL DEFAULT 0,
      error_message VARCHAR(1000) NOT NULL DEFAULT '',
      started_at DATETIME NOT NULL,
      finished_at DATETIME NULL,
      INDEX idx_creator_sync_history (creator_id,started_at DESC),
      CONSTRAINT fk_creator_sync_profile FOREIGN KEY (creator_id)
        REFERENCES creator_profiles(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS creator_works (
      creator_id BIGINT UNSIGNED NOT NULL,
      platform_work_id VARCHAR(255) NOT NULL,
      local_code INT UNSIGNED NOT NULL,
      title VARCHAR(512) NOT NULL DEFAULT '',
      published_at DATETIME NULL,
      content_type VARCHAR(32) NOT NULL DEFAULT 'video',
      like_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
      play_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
      comment_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
      share_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
      favorite_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
      duration_seconds INT UNSIGNED NOT NULL DEFAULT 0,
      is_pinned BOOLEAN NOT NULL DEFAULT FALSE,
      webpage_url TEXT NOT NULL,
      metadata_json LONGTEXT,
      first_seen_at DATETIME NOT NULL,
      last_seen_at DATETIME NOT NULL,
      is_removed BOOLEAN NOT NULL DEFAULT FALSE,
      PRIMARY KEY (creator_id,platform_work_id),
      UNIQUE KEY uq_creator_local_code (creator_id,local_code),
      INDEX idx_creator_works_latest
        (creator_id,published_at DESC,platform_work_id),
      INDEX idx_creator_works_likes
        (creator_id,like_count DESC,platform_work_id),
      CONSTRAINT fk_creator_work_profile FOREIGN KEY (creator_id)
        REFERENCES creator_profiles(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS creator_media_urls (
      id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
      creator_id BIGINT UNSIGNED NOT NULL,
      platform_work_id VARCHAR(255) NOT NULL,
      media_type ENUM('video','cover','image','music') NOT NULL,
      position_no INT UNSIGNED NOT NULL DEFAULT 0,
      remote_url LONGTEXT NOT NULL,
      refreshed_at DATETIME NOT NULL,
      UNIQUE KEY uq_creator_media
        (creator_id,platform_work_id,media_type,position_no),
      INDEX idx_creator_media_work (creator_id,platform_work_id),
      CONSTRAINT fk_creator_media_work
        FOREIGN KEY (creator_id,platform_work_id)
        REFERENCES creator_works(creator_id,platform_work_id)
        ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS creator_download_tasks (
      creator_id BIGINT UNSIGNED NOT NULL,
      platform_work_id VARCHAR(255) NOT NULL,
      status TINYINT NOT NULL DEFAULT 0,
      priority SMALLINT NOT NULL DEFAULT 0,
      local_path VARCHAR(1024) NOT NULL DEFAULT '',
      download_error VARCHAR(1000) NOT NULL DEFAULT '',
      retry_count SMALLINT UNSIGNED NOT NULL DEFAULT 0,
      downloaded_at DATETIME NULL,
      updated_at DATETIME NOT NULL,
      PRIMARY KEY (creator_id,platform_work_id),
      INDEX idx_creator_download_queue
        (creator_id,status,priority DESC,updated_at),
      CONSTRAINT fk_creator_download_work
        FOREIGN KEY (creator_id,platform_work_id)
        REFERENCES creator_works(creator_id,platform_work_id)
        ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS creator_media_files (
      media_url_id BIGINT UNSIGNED PRIMARY KEY,
      local_path VARCHAR(1024) NOT NULL DEFAULT '',
      status TINYINT NOT NULL DEFAULT 0,
      download_error VARCHAR(1000) NOT NULL DEFAULT '',
      updated_at DATETIME NOT NULL,
      CONSTRAINT fk_creator_media_file_url FOREIGN KEY (media_url_id)
        REFERENCES creator_media_urls(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
]


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _credentials() -> dict[str, Any]:
    path = get_data_root() / "private" / "mysql.json"
    if not path.exists():
        raise RuntimeError(f"MySQL 配置不存在: {path}")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def ensure_mysql_running() -> None:
    config = _credentials()
    if _port_open(config["host"], int(config["port"])):
        return
    configured = (
        os.environ.get("DOUKU_MYSQLD")
        or load_config().get("mysqld_path")
        or shutil.which("mysqld")
    )
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path(os.environ.get("ProgramFiles", ""))
        / "MySQL"
        / "MySQL Server 8.0"
        / "bin"
        / "mysqld.exe",
    ]
    mysqld = next(
        (candidate for candidate in candidates if candidate and candidate.is_file()),
        None,
    )
    ini = get_data_root() / "mysql" / "my.ini"
    if mysqld is None or not ini.exists():
        raise RuntimeError(
            "DouKU MySQL 未运行。请启动 MySQL 服务；若使用独立实例，请在 "
            "douku_config.json 中配置 mysqld_path，并在数据目录准备 mysql/my.ini。"
        )
    subprocess.Popen(
        [str(mysqld), f"--defaults-file={ini}"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(30):
        if _port_open(config["host"], int(config["port"])):
            return
        time.sleep(0.5)
    raise RuntimeError("DouKU MySQL 启动超时")


class Connection:
    def __init__(self, raw) -> None:
        self.raw = raw

    def execute(self, sql: str, params: Iterable[Any] = ()):
        cursor = self.raw.cursor(dictionary=True, buffered=True)
        cursor.execute(sql.replace("?", "%s"), tuple(params))
        return cursor

    def executemany(self, sql: str, params: Iterable[Iterable[Any]]):
        cursor = self.raw.cursor(dictionary=True)
        cursor.executemany(sql.replace("?", "%s"), list(params))
        return cursor

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type:
            self.rollback()
        else:
            self.commit()
        self.close()


_POOL = None


def get_conn() -> Connection:
    global _POOL
    ensure_mysql_running()
    if _POOL is None:
        from mysql.connector.pooling import MySQLConnectionPool

        config = _credentials()
        _POOL = MySQLConnectionPool(
            pool_name="douku_pool",
            pool_size=5,
            pool_reset_session=True,
            host=config["host"],
            port=int(config["port"]),
            database=config["database"],
            user=config["user"],
            password=config["password"],
            charset="utf8mb4",
            autocommit=False,
            connection_timeout=10,
        )
    return Connection(_POOL.get_connection())


def get_database_label() -> str:
    config = _credentials()
    return (
        f"mysql://{config['user']}@{config['host']}:{config['port']}/"
        f"{config['database']}"
    )


def init_db(conn: Connection) -> None:
    for statement in SCHEMA:
        conn.execute(statement)
    # 兼容已经创建的 MySQL 库。file_code 是仅用于本地文件名的稳定编号；
    # aweme_id 仍是各业务表之间的真实关联键。
    column = conn.execute(
        """
        SELECT COUNT(*) AS total
        FROM information_schema.columns
        WHERE table_schema=DATABASE()
          AND table_name='videos_base'
          AND column_name='file_code'
        """
    ).fetchone()
    if not column or int(column["total"]) == 0:
        conn.execute(
            """
            ALTER TABLE videos_base
              ADD COLUMN file_code INT UNSIGNED NOT NULL AUTO_INCREMENT,
              ADD UNIQUE KEY uq_videos_file_code (file_code)
            """
        )
    asset_column = conn.execute(
        """
        SELECT column_type AS asset_column_type
        FROM information_schema.columns
        WHERE table_schema=DATABASE()
          AND table_name='media_assets'
          AND column_name='asset_type'
        """
    ).fetchone()
    if asset_column and "'music'" not in asset_column["asset_column_type"]:
        conn.execute(
            """
            ALTER TABLE media_assets
              MODIFY asset_type ENUM('cover','image','music') NOT NULL
            """
        )
    creator_columns = {
        row["creator_column_name"]
        for row in conn.execute(
            """
            SELECT column_name AS creator_column_name
            FROM information_schema.columns
            WHERE table_schema=DATABASE()
              AND table_name='creator_works'
            """
        )
    }
    creator_column_defs = {
        "like_count": "BIGINT UNSIGNED NOT NULL DEFAULT 0",
        "play_count": "BIGINT UNSIGNED NOT NULL DEFAULT 0",
        "comment_count": "BIGINT UNSIGNED NOT NULL DEFAULT 0",
        "share_count": "BIGINT UNSIGNED NOT NULL DEFAULT 0",
        "favorite_count": "BIGINT UNSIGNED NOT NULL DEFAULT 0",
        "duration_seconds": "INT UNSIGNED NOT NULL DEFAULT 0",
        "is_pinned": "BOOLEAN NOT NULL DEFAULT FALSE",
    }
    for name, definition in creator_column_defs.items():
        if name not in creator_columns:
            conn.execute(
                f"ALTER TABLE creator_works ADD COLUMN {name} {definition}"
            )
    creator_like_index = conn.execute(
        """
        SELECT COUNT(*) AS total
        FROM information_schema.statistics
        WHERE table_schema=DATABASE()
          AND table_name='creator_works'
          AND index_name='idx_creator_works_likes'
        """
    ).fetchone()
    if not creator_like_index or int(creator_like_index["total"]) == 0:
        conn.execute(
            """
            ALTER TABLE creator_works
              ADD INDEX idx_creator_works_likes
                (creator_id,like_count DESC,platform_work_id)
            """
        )
    timestamp = now()
    conn.execute(
        """
        INSERT INTO account_profiles
          (account_key,platform,platform_user_id,nickname,created_at,updated_at)
        VALUES ('default','douyin','','',?,?)
        ON DUPLICATE KEY UPDATE updated_at=VALUES(updated_at)
        """,
        (timestamp, timestamp),
    )
    legacy_sources = conn.execute(
        """
        SELECT COUNT(*) AS total FROM information_schema.tables
        WHERE table_schema=DATABASE() AND table_name='video_sources'
        """
    ).fetchone()
    if legacy_sources and int(legacy_sources["total"]):
        conn.execute(
            """
            INSERT IGNORE INTO account_video_sources
              (account_key,aweme_id,source,position_no,captured_at)
            SELECT 'default',aweme_id,source,position_no,captured_at
            FROM video_sources
            """
        )
    legacy_downloads = conn.execute(
        """
        SELECT COUNT(*) AS total FROM information_schema.tables
        WHERE table_schema=DATABASE() AND table_name='download_tasks'
        """
    ).fetchone()
    if legacy_downloads and int(legacy_downloads["total"]):
        conn.execute(
            """
            INSERT IGNORE INTO account_download_tasks
              (account_key,aweme_id,status,priority,downloaded_at,video_path,
               download_error,retry_count,next_retry_at,updated_at)
            SELECT 'default',aweme_id,status,priority,downloaded_at,video_path,
                   download_error,retry_count,next_retry_at,updated_at
            FROM download_tasks
            """
        )
        conn.execute(
            """
            INSERT IGNORE INTO account_media_files
              (account_key,media_asset_id,local_path,status,download_error,updated_at)
            SELECT 'default',id,local_path,status,download_error,updated_at
            FROM media_assets
            WHERE local_path!='' OR status!=0
            """
        )
    conn.commit()


def resolve_account_profile(
    conn: Connection,
    nickname: str,
    platform_user_id: str,
) -> str:
    """Return a readable, stable account key without merging equal nicknames."""
    from lib.utils.meta import sanitize_dirname

    nickname = (nickname or "抖音账号").strip()
    base = sanitize_dirname(nickname)
    existing = conn.execute(
        """
        SELECT account_key FROM account_profiles
        WHERE platform='douyin' AND platform_user_id=? LIMIT 1
        """,
        (platform_user_id,),
    ).fetchone()
    if existing:
        key = existing["account_key"]
    else:
        collision = conn.execute(
            "SELECT platform_user_id FROM account_profiles WHERE account_key=?",
            (base,),
        ).fetchone()
        key = (
            f"{base}_{platform_user_id[-6:]}"
            if collision and collision["platform_user_id"] != platform_user_id
            else base
        )
    timestamp = now()
    conn.execute(
        """
        INSERT INTO account_profiles
          (account_key,platform,platform_user_id,nickname,created_at,updated_at)
        VALUES (?,'douyin',?,?,?,?)
        ON DUPLICATE KEY UPDATE nickname=VALUES(nickname),
          platform_user_id=VALUES(platform_user_id),updated_at=VALUES(updated_at)
        """,
        (key, platform_user_id, nickname, timestamp, timestamp),
    )
    conn.commit()
    return key


def upsert_aweme(
    conn: Connection,
    item: dict[str, Any],
    source: str,
    position: int = 0,
    account_key: str = "default",
) -> bool:
    aweme_id = str(item.get("aweme_id") or "")
    if not aweme_id:
        return False
    author = item.get("author") or {}
    sec_uid = str(author.get("sec_uid") or author.get("uid") or "unknown")
    video = item.get("video") or {}
    stats = item.get("statistics") or {}
    timestamp = now()
    conn.execute(
        """
        INSERT INTO account_profiles
          (account_key,platform,platform_user_id,nickname,created_at,updated_at)
        VALUES (?,'douyin','','',?,?)
        ON DUPLICATE KEY UPDATE updated_at=VALUES(updated_at)
        """,
        (account_key, timestamp, timestamp),
    )
    avatar_urls = (author.get("avatar_thumb") or {}).get("url_list") or []
    conn.execute(
        """
        INSERT INTO authors_base
          (sec_uid,nickname,avatar_url,signature,ip_location,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?)
        ON DUPLICATE KEY UPDATE nickname=VALUES(nickname),avatar_url=VALUES(avatar_url),
          signature=VALUES(signature),ip_location=VALUES(ip_location),
          updated_at=VALUES(updated_at)
        """,
        (
            sec_uid,
            author.get("nickname") or "",
            avatar_urls[0] if avatar_urls else "",
            author.get("signature") or "",
            author.get("ip_location") or "",
            timestamp,
            timestamp,
        ),
    )
    tags = [
        tag.get("tag_name") or tag.get("name")
        for tag in (item.get("video_tag") or [])
        if isinstance(tag, dict) and (tag.get("tag_name") or tag.get("name"))
    ]
    hashtags = [
        tag.get("hashtag_name") or tag.get("name")
        for tag in (item.get("text_extra") or [])
        if isinstance(tag, dict) and (tag.get("hashtag_name") or tag.get("name"))
    ]
    description = item.get("desc") or ""
    duration_ms = int(video.get("duration") or item.get("duration") or 0)
    content_type = "image" if item.get("images") else "video"
    conn.execute(
        """
        INSERT INTO videos_base
          (aweme_id,title,description,create_time,content_type,aweme_type_raw,
           duration_ms,author_sec_uid,share_url,is_top,prevent_download,tags,
           hashtags,first_seen_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON DUPLICATE KEY UPDATE title=VALUES(title),description=VALUES(description),
          create_time=VALUES(create_time),content_type=VALUES(content_type),
          duration_ms=VALUES(duration_ms),author_sec_uid=VALUES(author_sec_uid),
          share_url=VALUES(share_url),prevent_download=VALUES(prevent_download),
          tags=VALUES(tags),hashtags=VALUES(hashtags),updated_at=VALUES(updated_at)
        """,
        (
            aweme_id,
            description[:512],
            description,
            int(item.get("create_time") or 0),
            content_type,
            int(item.get("aweme_type") or 0),
            duration_ms,
            sec_uid,
            item.get("share_url") or f"https://www.douyin.com/video/{aweme_id}",
            bool(item.get("is_top")),
            bool(item.get("prevent_download")),
            json.dumps(tags, ensure_ascii=False),
            json.dumps(hashtags, ensure_ascii=False),
            timestamp,
            timestamp,
        ),
    )
    if source in {"likes", "favorites", "details"}:
        conn.execute(
            """
            INSERT INTO account_video_sources
              (account_key,aweme_id,source,position_no,captured_at)
            VALUES (?,?,?,?,?)
            ON DUPLICATE KEY UPDATE position_no=VALUES(position_no),
              captured_at=VALUES(captured_at)
            """,
            (account_key, aweme_id, source, position, timestamp),
        )
    conn.execute(
        """
        INSERT INTO videos_stats
          (aweme_id,digg_count,comment_count,share_count,collect_count,play_count,updated_at)
        VALUES (?,?,?,?,?,?,?)
        ON DUPLICATE KEY UPDATE digg_count=VALUES(digg_count),
          comment_count=VALUES(comment_count),share_count=VALUES(share_count),
          collect_count=VALUES(collect_count),play_count=VALUES(play_count),
          updated_at=VALUES(updated_at)
        """,
        (
            aweme_id,
            int(stats.get("digg_count") or 0),
            int(stats.get("comment_count") or 0),
            int(stats.get("share_count") or 0),
            int(stats.get("collect_count") or 0),
            int(stats.get("play_count") or 0),
            timestamp,
        ),
    )
    play_urls = (video.get("play_addr") or {}).get("url_list") or []
    cover_urls = (
        video.get("cover") or video.get("origin_cover") or {}
    ).get("url_list") or []
    music = item.get("music") or {}
    music_urls = (music.get("play_url") or {}).get("url_list") or []
    conn.execute(
        """
        INSERT INTO video_urls
          (aweme_id,video_url,cover_url,music_url,music_title,refreshed_at,url_status)
        VALUES (?,?,?,?,?,?,1)
        ON DUPLICATE KEY UPDATE
          video_url=IF(VALUES(video_url)!='',VALUES(video_url),video_url),
          cover_url=IF(VALUES(cover_url)!='',VALUES(cover_url),cover_url),
          music_url=IF(VALUES(music_url)!='',VALUES(music_url),music_url),
          music_title=IF(VALUES(music_title)!='',VALUES(music_title),music_title),
          refreshed_at=VALUES(refreshed_at),url_status=1
        """,
        (
            aweme_id,
            play_urls[0] if play_urls else "",
            cover_urls[0] if cover_urls else "",
            music_urls[0] if music_urls else "",
            music.get("title") or "",
            timestamp,
        ),
    )
    if content_type == "image":
        # 图文作品的 video.play_addr 在部分接口响应中实际等于背景音乐地址，
        # 不能把它当作视频下载。
        conn.execute(
            "UPDATE video_urls SET video_url='' WHERE aweme_id=?",
            (aweme_id,),
        )
    assets: list[tuple[str, int, str]] = []
    if cover_urls:
        assets.append(("cover", 0, cover_urls[0]))
    for asset_position, image in enumerate(item.get("images") or [], 1):
        urls = image.get("url_list") if isinstance(image, dict) else []
        if not urls and isinstance(image, dict):
            urls = (image.get("display_image") or {}).get("url_list") or []
        if urls:
            assets.append(("image", asset_position, urls[0]))
    if content_type == "image" and music_urls:
        assets.append(("music", 0, music_urls[0]))
    for asset_type, asset_position, remote_url in assets:
        conn.execute(
            """
            INSERT INTO media_assets
              (aweme_id,asset_type,position_no,remote_url,updated_at)
            VALUES (?,?,?,?,?)
            ON DUPLICATE KEY UPDATE remote_url=VALUES(remote_url),
              status=IF(local_path!='',status,0),updated_at=VALUES(updated_at)
            """,
            (aweme_id, asset_type, asset_position, remote_url, timestamp),
        )
    conn.execute(
        """
        INSERT INTO account_download_tasks
          (account_key,aweme_id,status,updated_at)
        VALUES (?,?,?,?)
        ON DUPLICATE KEY UPDATE aweme_id=VALUES(aweme_id)
        """,
        (account_key, aweme_id, DL_PENDING, timestamp),
    )
    return True


def upsert_comment(
    conn: Connection, aweme_id: str, comment: dict[str, Any]
) -> bool:
    cid = str(comment.get("cid") or comment.get("comment_id") or "")
    if not cid:
        return False
    user = comment.get("user") or {}
    conn.execute(
        """
        INSERT INTO comments
          (cid,aweme_id,content,user_name,digg_count,reply_count,is_hot,
           create_time,ip_location,captured_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON DUPLICATE KEY UPDATE content=VALUES(content),user_name=VALUES(user_name),
          digg_count=VALUES(digg_count),reply_count=VALUES(reply_count),
          ip_location=VALUES(ip_location)
        """,
        (
            cid,
            aweme_id,
            comment.get("text") or comment.get("content") or "",
            user.get("nickname") or comment.get("user_name") or "",
            int(comment.get("digg_count") or 0),
            int(comment.get("reply_comment_total") or comment.get("reply_count") or 0),
            bool(comment.get("is_hot")),
            int(comment.get("create_time") or 0),
            comment.get("ip_label") or comment.get("ip_location") or "",
            now(),
        ),
    )
    return True


def set_bookmark(
    conn: Connection,
    source: str,
    cursor: str,
    total_fetched: int,
    account_key: str = "default",
) -> None:
    conn.execute(
        """
        INSERT INTO account_sync_cursors
          (account_key,source,last_cursor,total_fetched,updated_at)
        VALUES (?,?,?,?,?)
        ON DUPLICATE KEY UPDATE last_cursor=VALUES(last_cursor),
          total_fetched=total_fetched+VALUES(total_fetched),
          updated_at=VALUES(updated_at)
        """,
        (account_key, source, str(cursor), total_fetched, now()),
    )


def update_download_status(
    conn: Connection,
    aweme_id: str,
    status: int,
    download_path: str = "",
    error: str = "",
    account_key: str = "default",
) -> None:
    conn.execute(
        """
        INSERT INTO account_download_tasks
          (account_key,aweme_id,status,downloaded_at,video_path,download_error,
           retry_count,updated_at)
        VALUES (?,?,?,?,?,?,?,?)
        ON DUPLICATE KEY UPDATE status=VALUES(status),
          downloaded_at=IF(VALUES(status)=2,VALUES(downloaded_at),downloaded_at),
          video_path=IF(VALUES(video_path)!='',VALUES(video_path),video_path),
          download_error=VALUES(download_error),
          retry_count=IF(VALUES(status)=3,retry_count+1,retry_count),
          updated_at=VALUES(updated_at)
        """,
        (
            account_key,
            aweme_id,
            status,
            now() if status == DL_DONE else None,
            download_path,
            error[:1000],
            1 if status == DL_FAILED else 0,
            now(),
        ),
    )


def _scalar(conn: Connection, sql: str, params: Iterable[Any] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(next(iter(row.values()))) if row else 0


def get_summary(conn: Connection, account_key: str = "default") -> dict[str, Any]:
    return {
        "videos": _scalar(
            conn,
            """
            SELECT COUNT(DISTINCT aweme_id) FROM account_video_sources
            WHERE account_key=?
            """,
            (account_key,),
        ),
        "authors": _scalar(
            conn,
            """
            SELECT COUNT(DISTINCT vb.author_sec_uid)
            FROM account_video_sources avs
            JOIN videos_base vb ON vb.aweme_id=avs.aweme_id
            WHERE avs.account_key=?
            """,
            (account_key,),
        ),
        "comments": _scalar(
            conn,
            """
            SELECT COUNT(DISTINCT c.cid)
            FROM comments c
            JOIN account_video_sources avs ON avs.aweme_id=c.aweme_id
            WHERE avs.account_key=?
            """,
            (account_key,),
        ),
        "likes": _scalar(
            conn,
            """
            SELECT COUNT(*) FROM account_video_sources
            WHERE account_key=? AND source='likes'
            """,
            (account_key,),
        ),
        "favorites": _scalar(
            conn,
            """
            SELECT COUNT(*) FROM account_video_sources
            WHERE account_key=? AND source='favorites'
            """,
            (account_key,),
        ),
        "with_video_url": _scalar(
            conn,
            """
            SELECT COUNT(DISTINCT vu.aweme_id)
            FROM video_urls vu
            JOIN account_video_sources avs ON avs.aweme_id=vu.aweme_id
            WHERE avs.account_key=? AND vu.video_url!=''
            """,
            (account_key,),
        ),
        "downloaded": _scalar(
            conn,
            """
            SELECT COUNT(*) FROM account_download_tasks
            WHERE account_key=? AND status=?
            """,
            (account_key, DL_DONE),
        ),
        "classified": _scalar(
            conn,
            """
            SELECT COUNT(DISTINCT vc.aweme_id)
            FROM videos_classification vc
            JOIN account_video_sources avs ON avs.aweme_id=vc.aweme_id
            WHERE avs.account_key=?
            """,
            (account_key,),
        ),
    }


def check_database(conn: Connection) -> dict[str, str]:
    version = conn.execute("SELECT VERSION() AS version").fetchone()["version"]
    tables = _scalar(
        conn,
        """
        SELECT COUNT(*) FROM information_schema.tables
        WHERE table_schema=DATABASE()
        """,
    )
    return {"connection": "ok", "mysql_version": version, "tables": str(tables)}
