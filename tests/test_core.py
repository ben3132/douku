from __future__ import annotations

import unittest
import os

from lib.analysis.content_classifier import classify_one
from lib.db.db_v4 import SCHEMA, check_database, get_conn, get_summary
from pathlib import Path
from tempfile import TemporaryDirectory

from lib.download.download_videos import (
    _extension,
    _has_expected_media_header,
    _music_extension,
    media_stem,
)
from lib.utils.meta import get_browser_profile_dir


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

    @unittest.skipUnless(
        os.environ.get("DOUKU_INTEGRATION_TESTS") == "1",
        "设置 DOUKU_INTEGRATION_TESTS=1 后运行本机 MySQL 集成测试",
    )
    def test_mysql_connection(self) -> None:
        with get_conn() as conn:
            health = check_database(conn)
            summary = get_summary(conn)
        self.assertEqual(health["connection"], "ok")
        self.assertGreaterEqual(summary["videos"], 0)


if __name__ == "__main__":
    unittest.main()
