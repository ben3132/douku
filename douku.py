#!/usr/bin/env python3
"""DouKU 2.2 - 个人抖音数据归档与通用媒体链接下载工具。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
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
        default="auto",
        help="默认 auto：使用当前登录账号昵称；也可指定固定账号名称",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化或升级数据库")
    sub.add_parser("login", help="打开浏览器扫码登录并保存登录态")
    sub.add_parser("status", help="显示本地数据和登录态状态")

    cookie = sub.add_parser("cookie", help="导入或检查本地加密 Cookie")
    cookie_sub = cookie.add_subparsers(dest="cookie_command", required=True)
    cookie_import = cookie_sub.add_parser("import", help="导入浏览器扩展导出的 JSON")
    cookie_import.add_argument("--file", required=True)
    cookie_sub.add_parser("capture", help="读取手动登录后的专用 Edge Cookie")
    cookie_sub.add_parser("status", help="检查加密 Cookie 状态")

    account = sub.add_parser("account", help="管理本地抖音账号档案")
    account_sub = account.add_subparsers(dest="account_command", required=True)
    account_use = account_sub.add_parser("use", help="绑定当前登录态的账号昵称")
    account_use.add_argument("nickname")
    account_sub.add_parser("list", help="列出本地账号")

    fetch = sub.add_parser("fetch", help="采集个人账号数据")
    fetch.add_argument(
        "source",
        choices=["likes", "favorites", "comments", "details", "all"],
    )
    fetch.add_argument("--pages", type=int, default=3, help="列表最多采集页数")
    fetch.add_argument("--limit", type=int, default=20, help="评论/详情最多处理视频数")
    fetch.add_argument("--headless", action="store_true", help="后台运行（更容易触发风控）")

    search = sub.add_parser("search", help="使用抖音个人主页搜索已点赞作品")
    search.add_argument("source", choices=["likes"])
    search.add_argument("--keywords", required=True, help="逗号分隔的关键词")
    search.add_argument(
        "--pages",
        type=int,
        default=3,
        help="每个关键词最大页数；0 表示持续搜索到没有下一页",
    )
    search.add_argument("--headless", action="store_true")

    classify = sub.add_parser("classify", help="按标题、话题和抖音标签分类")
    classify.add_argument("--all", action="store_true", help="重新分类全部视频")

    download = sub.add_parser("download", help="下载已采集的视频")
    download.add_argument("--category", default="")
    download.add_argument("--author", default="")
    download.add_argument("--limit", type=int, default=10)
    download.add_argument("--retry-failed", action="store_true")
    download.add_argument("--workers", type=int, default=3, help="并发下载数，建议 2-4")
    download.add_argument("--retries", type=int, default=3, help="单个资源最大尝试次数")
    download.add_argument("--min-free-gb", type=float, default=5.0)
    download.add_argument("--search-job", type=int, default=0)
    download.add_argument(
        "--per-keyword",
        type=int,
        default=0,
        help="搜索任务中每个关键词最多取前 N 条，跨关键词自动去重",
    )

    link = sub.add_parser("link", help="解析并下载抖音、B站及其他支持的网站链接")
    link.add_argument("urls", nargs="*", help="一个或多个分享链接")
    link.add_argument("--file", help="每行一个链接的 UTF-8 文本文件")
    link.add_argument("--resolve-only", action="store_true", help="只解析并保存真实 URL")

    creator = sub.add_parser("creator", help="采集、下载和增量同步创作者投稿")
    creator_sub = creator.add_subparsers(dest="creator_command", required=True)
    for name in ("inspect", "fetch", "sync"):
        command = creator_sub.add_parser(name)
        command.add_argument("profile_url")
        command.add_argument("--pages", type=int, default=0)
        command.add_argument("--latest", type=int, default=0)
        command.add_argument("--after")
        command.add_argument("--headless", action="store_true")
    creator_search = creator_sub.add_parser(
        "search", help="使用抖音作者主页自带搜索定位作品"
    )
    creator_search.add_argument("profile_url")
    creator_search.add_argument("keyword")
    creator_search.add_argument("--pages", type=int, default=3)
    creator_search.add_argument("--headless", action="store_true")
    creator_download = creator_sub.add_parser("download")
    creator_download.add_argument("creator", help="创作者ID、昵称或主页链接")
    creator_download.add_argument("--latest", type=int, default=0)
    creator_download.add_argument(
        "--type", choices=("all", "video", "image"), default="all"
    )
    creator_download.add_argument(
        "--sort",
        choices=(
            "published",
            "likes",
            "views",
            "comments",
            "shares",
            "favorites",
            "duration",
        ),
        default="published",
    )
    creator_download.add_argument(
        "--order", choices=("asc", "desc"), default="desc"
    )
    creator_download.add_argument("--limit", type=int, default=0)
    creator_download.add_argument("--exclude-pinned", action="store_true")
    creator_download.add_argument(
        "--work-id",
        action="append",
        default=[],
        help="精确下载指定作品ID，可重复使用",
    )
    creator_download.add_argument(
        "--dry-run",
        action="store_true",
        help="只列出入选作品，不下载",
    )
    creator_download.add_argument("--after")
    creator_download.add_argument("--before")
    creator_download.add_argument("--retry-failed", action="store_true")
    creator_download.add_argument("--retries", type=int, default=2)

    report = sub.add_parser("report", help="生成本地 HTML 报告")
    report.add_argument("--output", help="输出路径")

    sub.add_parser("check", help="执行数据库一致性检查")
    return parser


def configure_data_dir(value: str | None) -> None:
    if value:
        from lib.utils.meta import set_data_dir

        set_data_dir(value)


def print_json(value: object) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2)
    encoding = sys.stdout.encoding or "utf-8"
    print(text.encode(encoding, errors="replace").decode(encoding))


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

    if args.command == "cookie":
        from lib.utils.auth import auth_status, import_cookie_file

        if args.cookie_command == "import":
            print_json(import_cookie_file(Path(args.file)))
        elif args.cookie_command == "capture":
            from lib.collector import capture_profile_auth

            print_json(capture_profile_auth())
        else:
            print_json(auth_status())
        return 0

    if args.command == "account":
        from lib.db.db_v4 import now
        from lib.utils.meta import remember_active_account, sanitize_dirname

        with get_conn() as conn:
            init_db(conn)
            if args.account_command == "use":
                key = sanitize_dirname(args.nickname)
                timestamp = now()
                conn.execute(
                    """
                    INSERT INTO account_profiles
                      (account_key,platform,platform_user_id,nickname,
                       created_at,updated_at)
                    VALUES (?,'douyin','',?,?,?)
                    ON DUPLICATE KEY UPDATE nickname=VALUES(nickname),
                      updated_at=VALUES(updated_at)
                    """,
                    (key, args.nickname, timestamp, timestamp),
                )
                conn.commit()
                remember_active_account(key, "", args.nickname)
                print_json({"success": True, "account": key})
            else:
                rows = conn.execute(
                    """
                    SELECT account_key,nickname,
                           IF(platform_user_id!='',1,0) AS identity_confirmed
                    FROM account_profiles ORDER BY updated_at DESC
                    """
                ).fetchall()
                print_json(rows)
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
                max_pages=max(0, args.pages),
                limit=max(1, args.limit),
                headless=args.headless,
            )
        )
        return 0

    if args.command == "search":
        from lib.search.like_search import search_likes

        keywords = [
            value.strip()
            for value in args.keywords.replace("，", ",").split(",")
            if value.strip()
        ]
        print_json(
            search_likes(
                keywords,
                max_pages=max(1, args.pages),
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
                workers=max(1, min(8, args.workers)),
                retries=max(1, min(8, args.retries)),
                min_free_gb=max(0.0, args.min_free_gb),
                search_job=max(0, args.search_job),
                per_keyword=max(0, args.per_keyword),
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

    if args.command == "creator":
        from lib.creator.service import (
            download_creator,
            fetch_creator,
            search_creator_works,
        )

        parse_date = (
            lambda value: datetime.strptime(value, "%Y-%m-%d")
            if value
            else None
        )
        if args.creator_command == "search":
            print_json(
                search_creator_works(
                    args.profile_url,
                    args.keyword,
                    max_pages=max(1, args.pages),
                    headless=args.headless,
                )
            )
        elif args.creator_command == "download":
            print_json(
                download_creator(
                    args.creator,
                    latest=max(0, args.latest),
                    after=parse_date(args.after),
                    before=parse_date(args.before),
                    content_type=args.type,
                    sort_by=args.sort,
                    order=args.order,
                    exclude_pinned=args.exclude_pinned,
                    limit=max(0, args.limit),
                    dry_run=args.dry_run,
                    work_ids=args.work_id,
                    retry_failed=args.retry_failed,
                    retries=max(1, min(8, args.retries)),
                )
            )
        else:
            print_json(
                fetch_creator(
                    args.profile_url,
                    max_pages=(
                        1
                        if args.creator_command == "inspect"
                        else max(0, args.pages)
                    ),
                    latest=(
                        1
                        if args.creator_command == "inspect"
                        else max(0, args.latest)
                    ),
                    after=parse_date(args.after),
                    mode=args.creator_command,
                    headless=args.headless,
                )
            )
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
