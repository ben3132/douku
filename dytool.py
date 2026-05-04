#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抖音数据工具 - CLI 统一入口
用法: python dytool.py <command> [options]

Commands:
  fetch likes       获取点赞列表
  fetch favorites   获取收藏列表
  fetch comments    抓取评论（需指定视频）
  fetch profiles    抓取UP主画像
  download          下载视频
  refresh           刷新数据（T1/T2/T3）
  classify          视频内容分类
  report            生成HTML报告
  stats             查看数据库统计
"""
import sys
import argparse
from pathlib import Path

# 确保项目根目录在 Python 路径中
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))


def _safe_print(msg):
    """跨平台安全打印，Windows GBK 终端不崩"""
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode('utf-8', errors='replace').decode('utf-8', errors='replace'))


def cmd_fetch(args):
    sub = args.subcommand
    if sub == 'likes':
        from modules.fetch_likes_db import main
    elif sub == 'favorites':
        from modules.fetch_favorites_db import main
    elif sub == 'comments':
        from modules.fetch_comments import main
    elif sub == 'profiles':
        from modules.fetch_up_profiles import main
    else:
        _safe_print(f"未知 fetch 子命令: {sub}")
        sys.exit(1)
    sys.argv = ['fetch'] + args.extra
    main()


def cmd_download(args):
    from modules.download_videos import main
    sys.argv = ['download'] + args.extra
    main()


def cmd_refresh(args):
    from modules.refresh_data import main as refresh_main
    sys.argv = ['refresh'] + args.extra
    refresh_main()


def cmd_classify(args):
    from modules.content_classifier import main as classify_main
    sys.argv = ['classify'] + args.extra
    classify_main()


def cmd_report(args):
    from modules.generate_report import main
    sys.argv = ['report'] + args.extra
    main()


def cmd_stats(args):
    from modules.db_utils import get_conn

    conn = get_conn()
    cur = conn.cursor()

    _safe_print("=" * 50)
    _safe_print("Database Stats")
    _safe_print("=" * 50)

    v = cur.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
    a = cur.execute("SELECT COUNT(*) FROM authors").fetchone()[0]
    c = cur.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
    dl = cur.execute("SELECT COUNT(*) FROM videos WHERE is_downloaded=1").fetchone()[0]
    cat = cur.execute(
        "SELECT content_category, COUNT(*) FROM videos WHERE content_category!='' GROUP BY content_category ORDER BY COUNT(*) DESC"
    ).fetchall()

    _safe_print(f"  Total videos  : {v:,}")
    _safe_print(f"  Total authors : {a:,}")
    _safe_print(f"  Total comments: {c:,}")
    _safe_print(f"  Downloaded    : {dl:,} ({dl/v*100:.1f}%)")
    _safe_print("")
    _safe_print("  Classification:")
    _safe_print(f"  {'Category':<10} {'Count':>7} {'%':>6}")
    _safe_print(f"  {'-'*25}")
    for name, cnt in cat:
        pct = cnt / v * 100
        bar = "*" * int(pct / 2)
        _safe_print(f"  {name:<10} {cnt:>7,} {pct:>5.1f}% {bar}")

    conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="抖音数据工具 - 统一CLI入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    # dytool.py fetch likes
    fetch_p = subparsers.add_parser('fetch', help='Data fetching')
    fetch_sub = fetch_p.add_subparsers(dest='subcommand', help='Fetch type')
    for name in ['likes', 'favorites', 'comments', 'profiles']:
        sp = fetch_sub.add_parser(name, help=f'Fetch {name}')
        sp.add_argument('extra', nargs=argparse.REMAINDER, default=[])
        sp.set_defaults(func=cmd_fetch)

    # dytool.py download
    dl_p = subparsers.add_parser('download', help='Download videos')
    dl_p.add_argument('extra', nargs=argparse.REMAINDER, default=[])
    dl_p.set_defaults(func=cmd_download)

    # dytool.py refresh
    rf_p = subparsers.add_parser('refresh', help='Refresh data')
    rf_p.add_argument('extra', nargs=argparse.REMAINDER, default=[])
    rf_p.set_defaults(func=cmd_refresh)

    # dytool.py classify
    cl_p = subparsers.add_parser('classify', help='Video classification')
    cl_p.add_argument('extra', nargs=argparse.REMAINDER, default=[])
    cl_p.set_defaults(func=cmd_classify)

    # dytool.py report
    rp_p = subparsers.add_parser('report', help='Generate report')
    rp_p.add_argument('extra', nargs=argparse.REMAINDER, default=[])
    rp_p.set_defaults(func=cmd_report)

    # dytool.py stats
    st_p = subparsers.add_parser('stats', help='Database stats')
    st_p.set_defaults(func=cmd_stats)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    # 处理 fetch 的子命令分发
    if hasattr(args, 'subcommand') and args.subcommand:
        args.func(args)
    elif hasattr(args, 'func'):
        args.func(args)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()