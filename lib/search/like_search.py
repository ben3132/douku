"""Search only inside the current account's liked works via Douyin's own UI."""

from __future__ import annotations

from typing import Any

from lib.collector import (
    _context_page,
    _launch_persistent_context,
    _restore_auth_backup,
)
from lib.db.db_v4 import get_conn, init_db, now, upsert_aweme
from lib.utils.meta import get_account_key

SEARCH_ENDPOINT = "/aweme/v1/web/home/search/item/"
SEARCH_PLACEHOLDER = "搜索你赞过的作品"


def _clean_keywords(keywords: list[str]) -> list[str]:
    result: list[str] = []
    for keyword in keywords:
        value = keyword.strip()
        if value and value not in result:
            result.append(value[:100])
    if not result:
        raise ValueError("至少提供一个搜索关键词")
    return result


def search_likes(
    keywords: list[str],
    max_pages: int = 3,
    headless: bool = False,
) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    keywords = _clean_keywords(keywords)
    page_limit = 500 if max_pages == 0 else max(1, max_pages)
    account_key = get_account_key()
    with get_conn() as conn:
        init_db(conn)
        profile = conn.execute(
            """
            SELECT platform_user_id FROM account_profiles
            WHERE account_key=?
            """,
            (account_key,),
        ).fetchone()
        sec_uid = str((profile or {}).get("platform_user_id") or "")
        if not sec_uid:
            raise RuntimeError("当前账号尚未补齐身份标识，请先采集一次点赞")
        timestamp = now()
        cursor = conn.execute(
            """
            INSERT INTO like_search_jobs
              (account_key,status,created_at,updated_at)
            VALUES (?,0,?,?)
            """,
            (account_key, timestamp, timestamp),
        )
        job_id = int(cursor.lastrowid)
        for order, keyword in enumerate(keywords, 1):
            conn.execute(
                """
                INSERT INTO like_search_terms
                  (job_id,keyword,term_order,result_count)
                VALUES (?,?,?,0)
                """,
                (job_id, keyword, order),
            )
        conn.commit()

        active_keyword = ""
        page_counts = {keyword: 0 for keyword in keywords}
        result_counts = {keyword: 0 for keyword in keywords}
        has_more: dict[str, bool | None] = {
            keyword: None for keyword in keywords
        }
        callback_errors: list[str] = []
        matched_responses = 0

        def handle_response(response: Any) -> None:
            nonlocal active_keyword, matched_responses
            path = response.url.split("?", 1)[0]
            if (
                not active_keyword
                or not path.endswith(SEARCH_ENDPOINT)
                or page_counts[active_keyword] >= page_limit
            ):
                return
            matched_responses += 1
            try:
                data = response.json()
                if data.get("status_code") not in (None, 0):
                    return
                has_more[active_keyword] = bool(data.get("has_more"))
                items = data.get("aweme_list") or []
                if not items:
                    return
                page_counts[active_keyword] += 1
                offset = (page_counts[active_keyword] - 1) * len(items)
                for position, wrapper in enumerate(items, 1):
                    item = (
                        wrapper.get("item")
                        if isinstance(wrapper, dict)
                        and isinstance(wrapper.get("item"), dict)
                        else wrapper
                    )
                    if not upsert_aweme(
                        conn,
                        item,
                        "likes",
                        position=offset + position,
                        account_key=account_key,
                    ):
                        continue
                    aweme_id = str(item.get("aweme_id") or "")
                    conn.execute(
                        """
                        INSERT INTO like_search_results
                          (job_id,keyword,aweme_id,position_no,captured_at)
                        VALUES (?,?,?,?,?)
                        ON DUPLICATE KEY UPDATE
                          position_no=VALUES(position_no),
                          captured_at=VALUES(captured_at)
                        """,
                        (
                            job_id,
                            active_keyword,
                            aweme_id,
                            offset + position,
                            now(),
                        ),
                    )
                    result_counts[active_keyword] += 1
                conn.commit()
            except Exception as exc:
                callback_errors.append(str(exc))

        try:
            with sync_playwright() as playwright:
                context = _launch_persistent_context(
                    playwright, headless=headless
                )
                _restore_auth_backup(context)
                page = _context_page(context)
                page.on("response", handle_response)
                page.goto(
                    f"https://www.douyin.com/user/{sec_uid}?showTab=like",
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                page.wait_for_timeout(8_000)
                search = page.locator(
                    f'input[placeholder="{SEARCH_PLACEHOLDER}"]'
                ).first
                if search.count() != 1:
                    raise RuntimeError("没有找到“搜索你赞过的作品”输入框")
                for keyword in keywords:
                    active_keyword = keyword
                    search.fill(keyword)
                    search.press("Enter")
                    search.evaluate("element => element.blur()")
                    page.wait_for_timeout(5_000)
                    idle_rounds = 0
                    while (
                        page_counts[keyword] < page_limit
                        and has_more[keyword] is not False
                        and idle_rounds < 6
                    ):
                        previous_pages = page_counts[keyword]
                        viewport = page.viewport_size or {
                            "width": 1280,
                            "height": 720,
                        }
                        page.mouse.move(
                            viewport["width"] // 2,
                            int(viewport["height"] * 0.82),
                        )
                        page.mouse.wheel(0, 5000)
                        page.wait_for_timeout(2_500)
                        if page_counts[keyword] == previous_pages:
                            idle_rounds += 1
                        else:
                            idle_rounds = 0
                        if page_counts[keyword] >= page_limit:
                            break
                    conn.execute(
                        """
                        UPDATE like_search_terms SET result_count=?
                        WHERE job_id=? AND keyword=?
                        """,
                        (
                            int(
                                conn.execute(
                                    """
                                    SELECT COUNT(*) AS total
                                    FROM like_search_results
                                    WHERE job_id=? AND keyword=?
                                    """,
                                    (job_id, keyword),
                                ).fetchone()["total"]
                            ),
                            job_id,
                            keyword,
                        ),
                    )
                    conn.commit()
                active_keyword = ""
                page.remove_listener("response", handle_response)
                context.close()
            unique_count = int(
                conn.execute(
                    """
                    SELECT COUNT(DISTINCT aweme_id) AS total
                    FROM like_search_results WHERE job_id=?
                    """,
                    (job_id,),
                ).fetchone()["total"]
            )
            term_counts = {
                row["keyword"]: int(row["result_count"])
                for row in conn.execute(
                    """
                    SELECT keyword,result_count FROM like_search_terms
                    WHERE job_id=? ORDER BY term_order
                    """,
                    (job_id,),
                )
            }
            conn.execute(
                """
                UPDATE like_search_jobs
                SET status=2,result_count=?,updated_at=? WHERE id=?
                """,
                (unique_count, now(), job_id),
            )
            conn.commit()
            return {
                "success": True,
                "job_id": job_id,
                "account": account_key,
                "keywords": term_counts,
                "pages": page_counts,
                "has_more": has_more,
                "complete": all(value is False for value in has_more.values()),
                "unique_results": unique_count,
                "matched_responses": matched_responses,
                "callback_errors": callback_errors[-3:],
            }
        except Exception as exc:
            conn.execute(
                """
                UPDATE like_search_jobs
                SET status=3,error_message=?,updated_at=? WHERE id=?
                """,
                (str(exc)[:1000], now(), job_id),
            )
            conn.commit()
            raise
