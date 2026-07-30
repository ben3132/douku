#!/usr/bin/env python3
"""DouKU 2.1 - 个人抖音数据归档与通用媒体链接下载工具。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from lib.db.db_v4 import (
    check_database,
    get_conn,
    get_database_label,
    get_summary,
    init_db,
)
from lib.utils.meta import init_project, migrate_legacy_default_downloads


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="采集并整理当前登录账号的个人抖音数据")
    parser.add_argument("--data-dir", help="数据目录，默认使用项目下的 data")
    parser.add_argument(
        "--account",
        default="default",
        help="抖音账号本地别名，用于隔离浏览器、任务和下载目录",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化或升级数据库")
    sub.add_parser("login", help="打开浏览器扫码登录并保存登录态")
    sub.add_parser("status", help="显示本地数据和登录态状态")

    fetch = sub.add_parser("fetch", help="采集个人账号数据")
    fetch.add_argument(
        "source",
        choices=["likes", "favorites", "comments", "details", "all"],
    )
    fetch.add_argument("--pages", type=int, default=3, help="列表最多采集页数")
    fetch.add_argument("--limit", type=int, default=20, help="评论/详情最多处理视频数")
    fetch.add_argument("--headless", action="store_true", help="后台运行（更容易触发风控）")

    classify = sub.add_parser("classify", help="按标题、话题和抖音标签分类")
    classify.add_argument("--all", action="store_true", help="重新分类全部视频")

    download = sub.add_parser("download", help="下载已采集的视频")
    download.add_argument("--category", default="")
    download.add_argument("--author", default="")
    download.add_argument("--limit", type=int, default=10)
    download.add_argument("--retry-failed", action="store_true")

    link = sub.add_parser("link", help="解析并下载抖音、B站及其他支持的网站链接")
    link.add_argument("urls", nargs="*", help="一个或多个分享链接")
    link.add_argument("--file", help="每行一个链接的 UTF-8 文本文件")
    link.add_argument("--resolve-only", action="store_true", help="只解析并保存真实 URL")

    report = sub.add_parser("report", help="生成本地 HTML 报告")
    report.add_argument("--output", help="输出路径")

    sub.add_parser("check", help="执行数据库一致性检查")
    return parser


def configure_data_dir(value: str | None) -> None:
    if value:
        from lib.utils.meta import set_data_dir

        set_data_dir(value)


def print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_data_dir(args.data_dir)
    from lib.utils.meta import get_account_key, set_account

    set_account(args.account)

    if args.command == "init":
        result = init_project()
        with get_conn() as conn:
            init_db(conn)
            migration = migrate_legacy_default_downloads()
            if migration["path_changes"]:
                replacements = migration["path_changes"]
                for old_path, new_path in replacements:
                    conn.execute(
                        """
                        UPDATE account_download_tasks
                        SET video_path = %s
                        WHERE account_key = 'default' AND video_path = %s
                        """,
                        (new_path, old_path),
                    )
                    conn.execute(
                        """
                        UPDATE account_media_files
                        SET local_path = %s
                        WHERE account_key = 'default' AND local_path = %s
                        """,
                        (new_path, old_path),
                    )
                conn.commit()
        result["storage_migration"] = {
            "moved": migration["moved"],
            "conflicts": migration["conflicts"],
        }
        print_json(result)
        return 0

    if args.command == "login":
        from lib.collector import login

        print_json(login())
        return 0

    if args.command == "status":
        from lib.utils.auth import auth_status

        with get_conn() as conn:
            init_db(conn)
            summary = get_summary(conn, get_account_key())
        summary["account"] = get_account_key()
        summary["auth"] = auth_status()
        summary["database"] = get_database_label()
        print_json(summary)
        return 0

    if args.command == "fetch":
        from lib.collector import collect

        print_json(
            collect(
                source=args.source,
                max_pages=max(1, args.pages),
                limit=max(1, args.limit),
                headless=args.headless,
            )
        )
        return 0

    if args.command == "classify":
        from lib.analysis.content_classifier import run_classify

        print_json(run_classify(reclassify=args.all))
        return 0

    if args.command == "download":
        from lib.download.download_videos import run

        print_json(
            run(
                category=args.category,
                author=args.author,
                limit=max(1, args.limit),
                retry_failed=args.retry_failed,
            )
        )
        return 0

    if args.command == "link":
        from lib.link.resolver import run_links

        urls = list(args.urls)
        if args.file:
            urls.extend(
                line.strip()
                for line in Path(args.file).read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
        if not urls:
            raise RuntimeError("请提供至少一个链接，或使用 --file 指定链接文件")
        print_json(run_links(urls, download=not args.resolve_only))
        return 0

    if args.command == "report":
        from lib.analysis.generate_report import generate_report

        path = generate_report(Path(args.output) if args.output else None)
        print(f"报告已生成: {path}")
        return 0

    if args.command == "check":
        with get_conn() as conn:
            init_db(conn)
            result = check_database(conn)
        print_json(result)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
