from __future__ import annotations

import unittest
import os

from lib.analysis.content_classifier import classify_one
from lib.creator.service import _bilibili_creator_id, _douyin_creator_id
from lib.db.db_v4 import SCHEMA, check_database, get_conn, get_summary, init_db
from pathlib import Path
from tempfile import TemporaryDirectory

from lib.download.download_videos import (
    _extension,
    _has_expected_media_header,
    _music_extension,
    media_stem,
)
from lib.link.resolver import detect_platform
from lib.search.like_search import _clean_keywords
from lib.utils.auth import normalize_imported_cookies
from lib.utils.meta import (
    get_account_downloads_dir,
    get_browser_profile_dir,
    get_creator_downloads_dir,
    get_data_root,
    migrate_legacy_default_downloads,
    set_account,
    set_data_dir,
)


class CoreTest(unittest.TestCase):
    def test_classifier(self) -> None:
        result = classify_one(
            {
                "desc": "周末做一份简单的家常菜 #美食",
                "video_tags": ["美食"],
                "desc_hashtags": [],
            }
        )
        self.assertEqual(result["category"], "美食")

    def test_mysql_schema_is_frequency_separated(self) -> None:
        schema = "\n".join(SCHEMA)
        for table in (
            "videos_base",
            "video_sources",
            "videos_stats",
            "video_urls",
            "media_assets",
            "download_tasks",
        ):
            self.assertIn(f"TABLE IF NOT EXISTS {table}", schema)
        self.assertIn("idx_source_order", schema)
        self.assertIn("idx_download_queue", schema)
        self.assertIn("idx_urls_status_refresh", schema)
        self.assertIn("file_code INT UNSIGNED NOT NULL AUTO_INCREMENT", schema)
        self.assertIn("ENUM('cover','image','music')", schema)
        self.assertIn("account_download_tasks", schema)
        self.assertIn("direct_download_jobs", schema)
        self.assertIn("direct_media_urls", schema)
        self.assertIn("like_search_jobs", schema)
        self.assertIn("like_search_terms", schema)
        self.assertIn("like_search_results", schema)
        self.assertIn("creator_profiles", schema)
        self.assertIn("creator_sync_jobs", schema)
        self.assertIn("creator_works", schema)
        self.assertIn("creator_media_urls", schema)
        self.assertIn("creator_download_tasks", schema)
        self.assertIn("creator_media_files", schema)
        self.assertIn("like_count BIGINT UNSIGNED", schema)
        self.assertIn("play_count BIGINT UNSIGNED", schema)
        self.assertIn("is_pinned BOOLEAN", schema)
        self.assertIn("idx_creator_works_likes", schema)

    def test_media_extension(self) -> None:
        self.assertEqual(_extension("https://x/a.webp?x=1", ".jpg"), ".webp")
        self.assertEqual(_extension("https://x/no-suffix", ".jpg"), ".jpg")
        self.assertEqual(_music_extension("https://x/no-suffix"), ".mp3")

    def test_media_header_detection(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "asset"
            path.write_bytes(b"ID3" + b"\0" * 20)
            self.assertTrue(_has_expected_media_header(path, "music"))
            self.assertFalse(_has_expected_media_header(path, "video"))
            path.write_bytes(b"\0\0\0\x18ftypisom" + b"\0" * 20)
            self.assertTrue(_has_expected_media_header(path, "video"))
            self.assertTrue(_has_expected_media_header(path, "music"))

    def test_readable_media_stem(self) -> None:
        stem = media_stem(17, '张三/测试', '周末："家常菜" | 简单做法')
        self.assertEqual(stem, "00017_张三_测试_周末_家常菜_简单做法")
        self.assertNotIn("/", stem)
        with self.assertRaises(ValueError):
            media_stem(100000, "作者", "文案")

    def test_persistent_profile_is_private(self) -> None:
        profile = get_browser_profile_dir()
        self.assertEqual(profile.name, "edge_profile")
        self.assertEqual(profile.parent.name, "private")

    def test_account_downloads_are_isolated(self) -> None:
        original_root = get_data_root()
        try:
            with TemporaryDirectory() as directory:
                set_data_dir(directory)
                set_account("账号 A")
                first = get_account_downloads_dir()
                set_account("账号 B")
                second = get_account_downloads_dir()
                self.assertNotEqual(first, second)
                self.assertEqual(first.parent, second.parent)
        finally:
            set_account("default")
            set_data_dir(original_root)

    def test_platform_detection(self) -> None:
        self.assertEqual(detect_platform("https://b23.tv/abc"), "bilibili")
        self.assertEqual(
            detect_platform("https://v.douyin.com/abc"), "douyin"
        )
        self.assertEqual(
            detect_platform("https://www.example.com/watch/1"), "example"
        )

    def test_creator_profile_ids(self) -> None:
        self.assertEqual(
            _douyin_creator_id("https://www.douyin.com/user/MS4wLjABAAAA"),
            "MS4wLjABAAAA",
        )
        self.assertEqual(
            _bilibili_creator_id("https://space.bilibili.com/123456/video"),
            "123456",
        )

    def test_creator_downloads_are_isolated(self) -> None:
        original_root = get_data_root()
        try:
            with TemporaryDirectory() as directory:
                set_data_dir(directory)
                douyin = get_creator_downloads_dir("douyin", "同名作者")
                bilibili = get_creator_downloads_dir("bilibili", "同名作者")
                self.assertNotEqual(douyin, bilibili)
                self.assertEqual(douyin.name, "同名作者")
                self.assertEqual(douyin.parent.name, "douyin")
        finally:
            set_data_dir(original_root)

    def test_cookie_import_normalization(self) -> None:
        cookies = normalize_imported_cookies(
            [
                {
                    "domain": ".douyin.com",
                    "name": "sessionid",
                    "value": "test-only",
                    "expirationDate": 2000000000,
                    "sameSite": "no_restriction",
                    "secure": True,
                },
                {
                    "domain": ".example.com",
                    "name": "ignored",
                    "value": "ignored",
                },
            ]
        )
        self.assertEqual(len(cookies), 1)
        self.assertEqual(cookies[0]["sameSite"], "None")
        self.assertEqual(cookies[0]["expires"], 2000000000.0)

    def test_like_search_keywords_are_deduplicated(self) -> None:
        self.assertEqual(
            _clean_keywords([" 舞蹈 ", "cos", "舞蹈", ""]),
            ["舞蹈", "cos"],
        )

    def test_legacy_download_migration(self) -> None:
        original_root = get_data_root()
        try:
            with TemporaryDirectory() as directory:
                set_data_dir(directory)
                legacy = get_data_root() / "downloads" / "videos" / "old.mp4"
                legacy.parent.mkdir(parents=True, exist_ok=True)
                legacy.write_bytes(b"video")
                result = migrate_legacy_default_downloads()
                migrated = (
                    get_data_root()
                    / "downloads"
                    / "accounts"
                    / "default"
                    / "videos"
                    / "old.mp4"
                )
                self.assertEqual(result["moved"], 1)
                self.assertTrue(migrated.exists())
                self.assertFalse(legacy.exists())
        finally:
            set_account("default")
            set_data_dir(original_root)

    @unittest.skipUnless(
        os.environ.get("DOUKU_INTEGRATION_TESTS") == "1",
        "设置 DOUKU_INTEGRATION_TESTS=1 后运行本机 MySQL 集成测试",
    )
    def test_mysql_connection(self) -> None:
        with get_conn() as conn:
            init_db(conn)
            health = check_database(conn)
            summary = get_summary(conn)
        self.assertEqual(health["connection"], "ok")
        self.assertGreaterEqual(summary["videos"], 0)


if __name__ == "__main__":
    unittest.main()
