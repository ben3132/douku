#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dytool_v4.py - 抖库 v4 CLI 入口

v4 特性（相比 v3 dytool.py）：
  - 14 张表按更新频率分层（低频/中频/高频/独立）
  - WAL 模式，读不阻塞写
  - 下载 5 态状态机
  - 书签断点续传（cursor + liked_time）
  - 分析消耗评估（classify/portrait/tagger 决策辅助）

用法（与 v3 完全兼容）:
  python dytool_v4.py init                    # 初始化 v4 数据库
  python dytool_v4.py info                    # 数据摘要
  python dytool_v4.py check                   # 数据完整性检查
  python dytool_v4.py fetch likes             # 抓取点赞列表
  python dytool_v4.py fetch favorites         # 抓取收藏列表
  python dytool_v4.py fetch profiles          # 抓取UP主资料
  python dytool_v4.py fetch comments          # 抓取评论
  python dytool_v4.py download --category 颜值  # 按分类下载
  python dytool_v4.py classify                # 内容分类
  python dytool_v4.py stats                   # 显示统计
  python dytool_v4.py report                  # 生成报告
  python dytool_v4.py portrait                # UP主画像
  python dytool_v4.py tagger                  # 评论标签提取
  python dytool_v4.py refresh urls            # 刷新过期URL
  python dytool_v4.py cookie                  # 自动获取Cookie

全局选项:
  --data-dir PATH    指定数据目录路径
"""

import os
import sys
import argparse
from pathlib import Path

# ── 编码安全 ────────────────────────────────────────────────
import io as _io
if sys.platform == 'win32' and sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        try:
            sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        except Exception:
            pass

# ── 路径 ────────────────────────────────────────────────────
_BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(_BASE))

DEFAULT_DATA_DIR = str(_BASE / "data")
DEFAULT_DB_PATH = str(_BASE / "data" / "douku_v4.db")

# 全局 data_dir 引用
_data_dir = DEFAULT_DATA_DIR


def get_data_dir() -> str:
    return _data_dir


def get_db_path() -> str:
    return str(Path(_data_dir) / "douku_v4.db")


# ============================================================
# v4 数据库初始化
# ============================================================

def init_v4_db() -> "sqlite3.Connection":
    """初始化 v4 数据库，返回连接"""
    from modules.db_v4 import init_db_v4
    db_path = get_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    return init_db_v4(db_path)


def get_v4_conn() -> "sqlite3.Connection":
    """获取 v4 数据库连接"""
    from modules.db_v4 import get_conn_v4
    return get_conn_v4(get_db_path())


# ============================================================
# 子命令实现
# ============================================================

def cmd_init(args):
    """初始化数据目录 + v4 数据库"""
    data_dir = args.data_dir or DEFAULT_DATA_DIR
    global _data_dir
    _data_dir = data_dir

    os.makedirs(data_dir, exist_ok=True)

    # 创建子目录
    for sub in ["downloads", "output", "logs"]:
        os.makedirs(os.path.join(data_dir, sub), exist_ok=True)

    conn = init_v4_db()
    from modules.db_v4 import mark_setup_done
    mark_setup_done(conn)
    conn.close()

    print("=" * 50)
    print("抖库 v4 项目初始化完成")
    print("=" * 50)
    print(f"数据目录: {data_dir}")
    print(f"数据库:   {get_db_path()}")
    print(f"下载目录: {os.path.join(data_dir, 'downloads')}")
    print(f"报告输出: {os.path.join(data_dir, 'output')}")
    print()
    print("下一步:")
    print("  1. dytool_v4.py cookie           → 获取抖音 Cookie")
    print("  2. dytool_v4.py fetch likes      → 抓取点赞数据")
    print("  3. dytool_v4.py classify         → 自动分类")
    print("  4. dytool_v4.py report           → 生成报告")


def cmd_info(args):
    """显示数据摘要（v4 多表 JOIN）"""
    conn = get_v4_conn()
    from modules.db_v4 import get_summary, get_download_stats, get_category_distribution

    summary = get_summary(conn)

    print("=" * 50)
    print("抖库 v4 数据摘要")
    print("=" * 50)
    print(f"数据库: {get_db_path()}")
    print(f"就绪状态: {'已就绪' if summary['setup_done'] else '未初始化'}")
    print()

    print("数据统计:")
    print(f"  视频:   {summary['videos']}")
    print(f"  作者:   {summary['authors']}")
    print(f"  评论:   {summary['comments']} (覆盖 {summary['videos_with_comments']} 个视频)")

    dl = summary['download']
    print(f"  已下载: {dl['done']} / {dl['total']} "
          f"(待: {dl['pending']}, 失败: {dl['failed']}, 过期: {dl['expired']})")

    cat = summary['categories']
    if cat:
        print(f"\n赛道分布 ({summary['category_count']} 个):")
        for name, cnt in list(cat.items())[:10]:
            print(f"  {name}: {cnt}")
        if len(cat) > 10:
            print(f"  ... 还有 {len(cat) - 10} 个赛道")

    print(f"\nCookie: {'有效' if summary['cookie_valid'] else '无效/过期'}")
    conn.close()


def cmd_check(args):
    """检查数据完整性（v4）"""
    conn = get_v4_conn()
    from modules.db_v4 import (
        get_video_count, get_author_count, get_comment_count,
        is_setup_done, is_cookie_valid,
    )

    print("=" * 50)
    print("数据完整性检查 (v4)")
    print("=" * 50)

    checks = [
        ("数据目录",   os.path.isdir(get_data_dir())),
        ("v4 数据库",  os.path.isfile(get_db_path())),
        ("下载目录",   os.path.isdir(os.path.join(get_data_dir(), "downloads"))),
        ("输出目录",   os.path.isdir(os.path.join(get_data_dir(), "output"))),
        ("日志目录",   os.path.isdir(os.path.join(get_data_dir(), "logs"))),
        ("已初始化",  is_setup_done(conn)),
    ]

    for label, ok in checks:
        print(f"  {'OK' if ok else '!!'} {label}")

    print()
    print(f"  视频: {get_video_count(conn)}")
    print(f"  作者: {get_author_count(conn)}")
    print(f"  评论: {get_comment_count(conn)}")
    print(f"  Cookie: {'有效' if is_cookie_valid(conn) else '无效'}")

    print()
    print("OK 所有检查通过" if all(c[1] for c in checks) else "!! 存在问题，请检查上述项目")
    conn.close()


def cmd_migrate(args):
    """v3 → v4 数据迁移"""
    v3_path = os.path.join(get_data_dir(), "douku.db")
    v4_path = get_db_path()

    if not os.path.exists(v3_path):
        print(f"v3 数据库不存在: {v3_path}")
        print("请确认数据目录正确，或先运行 dytool.py fetch likes 产生 v3 数据")
        return

    from modules.db_v4 import migrate_v3_to_v4
    print("=" * 50)
    print("v3 -> v4 data migration")
    print("=" * 50)
    print(f"v3 source: {v3_path}")
    print(f"v4 target: {v4_path}")
    print()
    print("Migrating...")

    result = migrate_v3_to_v4(v3_path, v4_path)

    print(f"  authors:   {result['authors']}  (portrait: {result.get('portrait', 0)})")
    print(f"  videos:    {result['videos']}  (classified: {result.get('classify', 0)}, downloaded: {result.get('download_done', 0)})")
    print(f"  comments:  {result['comments']}")
    print(f"  bookmarks: {result.get('bookmark', 0)}")

    if result["errors"]:
        print(f"  errors: {', '.join(result['errors'])}")
    else:
        print("\nOK migration complete!")


def cmd_fetch(args):
    """抓取数据（v4 原生模块）"""
    target = getattr(args, 'fetch_target', None)
    if target not in ('likes', 'favorites', 'profiles', 'comments'):
        print("请指定抓取目标: likes, favorites, profiles, comments")
        print("示例: dytool_v4.py fetch likes")
        return

    # 确保 v4 数据库已初始化
    init_v4_db().close()

    if target == 'likes':
        from modules.fetch_likes_v4 import run as fetch_likes_v4
        print("开始抓取点赞列表 (v4)...")
        count = getattr(args, 'count', 0)
        reset = getattr(args, 'reset', False)
        result = fetch_likes_v4(count=count, reset=reset)
        print(f"完成: 新增 {result['new']} 条")

    elif target == 'favorites':
        from modules.fetch_favorites_v4 import run as fetch_favorites_v4
        print("开始抓取收藏列表 (v4)...")
        count = getattr(args, 'count', 0)
        reset = getattr(args, 'reset', False)
        result = fetch_favorites_v4(count=count, reset=reset)
        print(f"完成: 新增 {result['new']} 条")

    elif target == 'profiles':
        from modules.fetch_up_profiles_v4 import run as fetch_profiles_v4
        print("开始抓取UP主资料 (v4)...")
        force = getattr(args, 'reset', False)
        limit = getattr(args, 'count', 0)
        result = fetch_profiles_v4(force=force, limit=limit)
        print(f"完成: 成功={result['success']}, 失败={result['failed']}")

    elif target == 'comments':
        from modules.fetch_comments_v4 import run as fetch_comments_v4
        print("开始抓取评论 (v4)...")
        result = fetch_comments_v4(
            aweme_id=getattr(args, 'aweme_id', None),
            min_digg=getattr(args, 'min_digg', 0),
            force=getattr(args, 'force', False),
            limit=getattr(args, 'limit', 0),
            max_pages=getattr(args, 'max_pages', 1),
        )
        if 'error' in result:
            print(f"错误: {result['error']}")
        else:
            print(f"完成: 成功={result['success']}个视频, 新增={result['new_comments']}条评论")


def cmd_download(args):
    """下载视频 (v4 原生 5 态状态机)"""
    from modules.download_videos_v4 import run_from_cli
    run_from_cli(args)


def cmd_classify(args):
    """内容分类 (v4 原生)"""
    from modules.content_classifier_v4 import run_from_cli
    run_from_cli(args)


def cmd_stats(args):
    """显示内容分类统计"""
    conn = get_v4_conn()
    from modules.db_v4 import get_summary

    summary = get_summary(conn)
    cat = summary['categories']

    print("=" * 50)
    print(f"赛道分布 ({summary['category_count']} 个)")
    print("=" * 50)

    if not cat:
        print("暂无分类数据，请先运行 dytool_v4.py classify")
    else:
        max_len = max(len(n) for n in cat.keys())
        bar_max = 30
        max_cnt = max(cat.values())
        for name, cnt in sorted(cat.items(), key=lambda x: -x[1]):
            bar_len = int(cnt / max_cnt * bar_max) if max_cnt else 0
            bar = "█" * bar_len
            print(f"  {name:<{max_len}}  {cnt:>5d}  {bar}")

    conn.close()


def cmd_report(args):
    """生成HTML报告 (v4 原生)"""
    from modules.generate_report_v4 import run_from_cli
    run_from_cli(args)


def cmd_refresh(args):
    """刷新数据 (v4 原生)"""
    target = getattr(args, 'refresh_target', None)
    if target == 'urls':
        from modules.refresh_urls_v4 import refresh_urls
        refresh_urls(
            source=getattr(args, 'source', None),
            tag=getattr(args, 'tag', None),
            failed_only=getattr(args, 'failed', False),
            limit=getattr(args, 'limit', 0),
            dry_run=getattr(args, 'dry_run', False),
        )
    elif target == 'meta':
        print("元数据刷新尚未实现")
    else:
        print("请指定刷新目标: urls, meta")
        print("示例: dytool_v4.py refresh urls")


def cmd_portrait(args):
    """UP主画像分析 (v4 原生)"""
    from modules.author_portrait_v4 import run_from_cli
    run_from_cli(args)


def cmd_tagger(args):
    """评论标签提取 (v4 原生)"""
    from modules.comment_tagger_v4 import run_from_cli
    run_from_cli(args)


def cmd_cookie(args):
    """自动从浏览器获取 Cookie"""
    from modules.fetch_cookie import main as fetch_cookie
    print("Cookie 获取向导...")
    fetch_cookie()


# ============================================================
# CLI 框架
# ============================================================

def build_parser():
    """构建命令行参数解析器"""
    parser = argparse.ArgumentParser(
        prog='dytool_v4.py',
        description='抖库 v4 - 抖音数据获取与管理工具（多表分层架构）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  dytool_v4.py init                        # 初始化
  dytool_v4.py info                        # 数据摘要
  dytool_v4.py fetch likes                 # 抓取点赞
  dytool_v4.py fetch comments -n 50        # 抓取50个视频评论
  dytool_v4.py download --category 颜值    # 按赛道下载
  dytool_v4.py classify                    # 内容分类
  dytool_v4.py report                      # 生成报告
"""
    )

    parser.add_argument('--data-dir', help='指定数据目录路径（默认 ./data/）')

    subparsers = parser.add_subparsers(dest='command', help='可用命令')

    # ── init ──
    p = subparsers.add_parser('init', help='初始化 v4 数据目录')
    p.set_defaults(func=cmd_init)

    # ── info ──
    p = subparsers.add_parser('info', help='显示数据摘要')
    p.set_defaults(func=cmd_info)

    # ── check ──
    p = subparsers.add_parser('check', help='检查数据完整性')
    p.set_defaults(func=cmd_check)

    # ── migrate ──
    p = subparsers.add_parser('migrate', help='v3 → v4 数据迁移')
    p.set_defaults(func=cmd_migrate)

    # ── fetch ──
    p_fetch = subparsers.add_parser('fetch', help='抓取数据')
    p_fetch.set_defaults(func=cmd_fetch)
    fetch_sub = p_fetch.add_subparsers(dest='fetch_target')

    pf = fetch_sub.add_parser('likes', help='抓取点赞列表')
    pf.add_argument('--count', '-n', type=int, default=0, help='最多抓取N条')
    pf.add_argument('--reset', '-r', action='store_true', help='重置书签')

    pf = fetch_sub.add_parser('favorites', help='抓取收藏列表')
    pf.add_argument('--count', '-n', type=int, default=0, help='最多抓取N条')
    pf.add_argument('--reset', '-r', action='store_true', help='重置书签')

    pf = fetch_sub.add_parser('profiles', help='抓取作者信息')
    pf.add_argument('--count', '-n', type=int, default=0)

    pf = fetch_sub.add_parser('comments', help='抓取评论')
    pf.add_argument('--aweme_id', '-a', help='指定视频ID')
    pf.add_argument('--min-digg', '-m', type=int, default=0)
    pf.add_argument('--force', '-f', action='store_true')
    pf.add_argument('--limit', '-n', type=int, default=0)
    pf.add_argument('--max-pages', '-p', type=int, default=1)

    # ── download ──
    p = subparsers.add_parser('download', help='下载视频')
    p.set_defaults(func=cmd_download)
    p.add_argument('--source', '-S', choices=['likes', 'favorites'])
    p.add_argument('--category', '-c', help='按分类下载')
    p.add_argument('--author', '-a', help='按作者昵称')
    p.add_argument('--tag', '-t', help='按标签')
    p.add_argument('--limit', '-n', type=int, help='最多下载')
    p.add_argument('--dry-run', '-d', action='store_true')
    p.add_argument('--refresh', '-R', action='store_true')
    p.add_argument('--yes', '-y', action='store_true')
    p.add_argument('--status', '-s', action='store_true', help='查看下载状态')

    # ── classify ──
    p = subparsers.add_parser('classify', help='分类视频')
    p.set_defaults(func=cmd_classify)
    p.add_argument('--force', action='store_true')
    p.add_argument('--limit', type=int, default=0)
    p.add_argument('--aweme_id')
    p.add_argument('--show', '-s', action='store_true')
    p.add_argument('--stats', action='store_true', help='只显示统计')

    # ── stats ──
    p = subparsers.add_parser('stats', help='显示分类统计')
    p.set_defaults(func=cmd_stats)

    # ── report ──
    p = subparsers.add_parser('report', help='生成HTML报告')
    p.set_defaults(func=cmd_report)
    p.add_argument('--output', '-o', help='输出路径')
    p.add_argument('--no-comments', action='store_true')

    # ── cookie ──
    p = subparsers.add_parser('cookie', help='从浏览器获取Cookie')
    p.set_defaults(func=cmd_cookie)

    # ── refresh ──
    p_refresh = subparsers.add_parser('refresh', help='刷新数据')
    p_refresh.set_defaults(func=cmd_refresh)
    refresh_sub = p_refresh.add_subparsers(dest='refresh_target')

    pr = refresh_sub.add_parser('urls', help='刷新过期URL')
    pr.add_argument('--source', '-S', choices=['likes', 'favorites'])
    pr.add_argument('--tag', '-t')
    pr.add_argument('--limit', '-n', type=int, default=0)
    pr.add_argument('--failed', '-f', action='store_true')
    pr.add_argument('--dry-run', '-d', action='store_true')
    refresh_sub.add_parser('meta', help='更新元数据')

    # ── portrait ──
    p = subparsers.add_parser('portrait', help='UP主画像分析')
    p.set_defaults(func=cmd_portrait)
    p.add_argument('--update', '-u', action='store_true')
    p.add_argument('--track', '-t', help='按赛道筛选')
    p.add_argument('--top', '-n', type=int, default=30)
    p.add_argument('--min-videos', '-m', type=int, default=0)
    p.add_argument('--export', '-e', action='store_true')

    # ── tagger ──
    p = subparsers.add_parser('tagger', help='评论标签提取')
    p.set_defaults(func=cmd_tagger)
    p.add_argument('--aweme_id', '-a')
    p.add_argument('--force', '-f', action='store_true')
    p.add_argument('--limit', '-n', type=int, default=0)
    p.add_argument('--show', '-s', action='store_true')

    return parser


def main():
    """主入口"""
    if sys.platform == 'win32':
        try:
            sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
            sys.stderr = _io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
        except ValueError:
            pass

    parser = build_parser()
    args = parser.parse_args()

    # 设置数据目录
    global _data_dir
    if args.data_dir:
        _data_dir = args.data_dir

    # 确保 v4 数据库初始化
    try:
        init_v4_db().close()
    except Exception:
        pass

    # 执行子命令
    if hasattr(args, 'func'):
        args.func(args)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
