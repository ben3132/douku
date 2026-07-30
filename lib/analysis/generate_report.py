"""生成无需联网即可打开的个人数据 HTML 报告。"""

from __future__ import annotations

import html
from pathlib import Path

from lib.db.db_v4 import get_conn, get_summary, init_db
from lib.utils.meta import get_account_key, get_output_dir


def _table(headers: list[str], rows: list[list[object]]) -> str:
    head = "".join(f"<th>{html.escape(value)}</th>" for value in headers)
    body = "".join(
        "<tr>"
        + "".join(f"<td>{html.escape(str(value or ''))}</td>" for value in row)
        + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def generate_report(output_path: Path | None = None) -> Path:
    account_key = get_account_key()
    output_path = (
        output_path
        or get_output_dir() / "accounts" / account_key / "report.html"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        init_db(conn)
        summary = get_summary(conn, account_key)
        categories = [
            list(row.values())
            for row in conn.execute(
                """
                SELECT COALESCE(vc.content_category,'未分类'),
                       COUNT(DISTINCT avs.aweme_id)
                FROM account_video_sources avs
                LEFT JOIN videos_classification vc ON vc.aweme_id=avs.aweme_id
                WHERE avs.account_key=?
                GROUP BY vc.content_category ORDER BY COUNT(*) DESC
                """,
                (account_key,),
            )
        ]
        authors = [
            list(row.values())
            for row in conn.execute(
                """
                SELECT ab.nickname, COUNT(vb.aweme_id) AS videos,
                       COALESCE(SUM(vs.digg_count),0) AS likes
                FROM authors_base ab
                JOIN videos_base vb ON vb.author_sec_uid=ab.sec_uid
                JOIN (
                  SELECT DISTINCT aweme_id FROM account_video_sources
                  WHERE account_key=?
                ) avs ON avs.aweme_id=vb.aweme_id
                LEFT JOIN videos_stats vs ON vs.aweme_id=vb.aweme_id
                GROUP BY ab.sec_uid ORDER BY videos DESC LIMIT 50
                """,
                (account_key,),
            )
        ]
        videos = [
            list(row.values())
            for row in conn.execute(
                """
                SELECT vb.title, ab.nickname,
                       COALESCE(vc.content_category,'未分类'),
                       COALESCE(vs.digg_count,0), vb.share_url
                FROM videos_base vb
                JOIN (
                  SELECT DISTINCT aweme_id FROM account_video_sources
                  WHERE account_key=?
                ) avs ON avs.aweme_id=vb.aweme_id
                JOIN authors_base ab ON ab.sec_uid=vb.author_sec_uid
                LEFT JOIN videos_stats vs ON vs.aweme_id=vb.aweme_id
                LEFT JOIN videos_classification vc ON vc.aweme_id=vb.aweme_id
                ORDER BY vb.updated_at DESC LIMIT 200
                """,
                (account_key,),
            )
        ]

    cards = "".join(
        f"<div class='card'><strong>{value}</strong><span>{html.escape(key)}</span></div>"
        for key, value in summary.items()
    )
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DouKU 个人数据报告</title>
<style>
body{{font:14px/1.6 system-ui,sans-serif;margin:0;background:#f5f6f8;color:#1f2329}}
main{{max-width:1200px;margin:auto;padding:32px}} h1,h2{{margin:0 0 16px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:12px;margin:20px 0 32px}}
.card{{background:white;border-radius:12px;padding:18px;box-shadow:0 1px 3px #0001}}
.card strong{{display:block;font-size:28px}} .card span{{color:#667085}}
section{{background:white;border-radius:12px;padding:20px;margin:16px 0;overflow:auto}}
table{{border-collapse:collapse;width:100%}} th,td{{padding:9px;border-bottom:1px solid #eee;text-align:left}}
th{{background:#fafafa;position:sticky;top:0}}
</style></head><body><main>
<h1>DouKU 个人抖音数据报告</h1>
<div class="cards">{cards}</div>
<section><h2>内容分类</h2>{_table(["分类","视频数"], categories)}</section>
<section><h2>常看作者</h2>{_table(["作者","视频数","获赞合计"], authors)}</section>
<section><h2>最近视频</h2>{_table(["标题","作者","分类","获赞","链接"], videos)}</section>
</main></body></html>"""
    output_path.write_text(document, encoding="utf-8")
    return output_path.resolve()
