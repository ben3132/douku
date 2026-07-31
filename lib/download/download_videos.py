"""按媒体类型归档视频、封面和图文原图，关联关系保存在 MySQL。"""

from __future__ import annotations

import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from lib.db.db_v4 import (
    DL_DONE,
    DL_FAILED,
    DL_PENDING,
    get_conn,
    init_db,
    now,
    update_download_status,
)
from lib.utils.auth import load_auth
from lib.utils.meta import get_account_downloads_dir, get_account_key


def _cookie_dict() -> dict[str, str]:
    return {
        str(cookie.get("name")): str(cookie.get("value"))
        for cookie in (load_auth().get("cookies") or [])
        if cookie.get("name") and cookie.get("value")
    }


def _new_session() -> requests.Session:
    session = requests.Session()
    session.cookies.update(_cookie_dict())
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/131 Safari/537.36"
            ),
            "Referer": "https://www.douyin.com/",
        }
    )
    return session


def _media_size_for_code(root: Path, file_code: int) -> int:
    prefix = f"{int(file_code):05d}_"
    return sum(
        path.stat().st_size
        for path in root.rglob(f"{prefix}*")
        if path.is_file() and not path.name.endswith(".part")
    )


def _print_progress(message: str) -> None:
    encoding = sys.stdout.encoding or "utf-8"
    safe = message.encode(encoding, errors="replace").decode(encoding)
    print(safe, flush=True)


def _queue(
    conn: Any,
    account_key: str,
    category: str,
    author: str,
    limit: int,
    retry_failed: bool,
    aweme_ids: list[str] | None = None,
    search_job: int = 0,
    per_keyword: int = 0,
) -> list[dict[str, Any]]:
    statuses = [DL_PENDING]
    if retry_failed:
        statuses.append(DL_FAILED)
    placeholders = ",".join("?" for _ in statuses)
    query = f"""
        SELECT vb.aweme_id, vb.file_code, vb.title, vb.description AS `desc`,
               vb.content_type AS `type`, vb.create_time,
               vb.share_url, vb.duration_ms, ab.nickname,
               COALESCE(vd.video_path,'') AS saved_video_path,
               COALESCE(vu.video_url,'') AS video_url,
               COALESCE(vu.cover_url,'') AS cover_url,
               COALESCE(vc.content_category,'未分类') AS category
        FROM videos_base vb
        JOIN authors_base ab ON ab.sec_uid=vb.author_sec_uid
        JOIN account_download_tasks vd ON vd.aweme_id=vb.aweme_id
          AND vd.account_key=?
        LEFT JOIN video_urls vu ON vu.aweme_id=vb.aweme_id
        LEFT JOIN videos_classification vc ON vc.aweme_id=vb.aweme_id
        WHERE vd.status IN ({placeholders})
          AND (
            COALESCE(vu.video_url,'')!=''
            OR EXISTS (
              SELECT 1 FROM media_assets ma
              WHERE ma.aweme_id=vb.aweme_id AND ma.remote_url!=''
            )
          )
    """
    params: list[Any] = [account_key, *statuses]
    if category:
        query += " AND vc.content_category=?"
        params.append(category)
    if author:
        query += " AND ab.nickname LIKE ?"
        params.append(f"%{author}%")
    if aweme_ids:
        id_placeholders = ",".join("?" for _ in aweme_ids)
        query += f" AND vb.aweme_id IN ({id_placeholders})"
        params.extend(aweme_ids)
    if search_job:
        if per_keyword:
            query += """
              AND EXISTS (
                SELECT 1 FROM (
                  SELECT lsr.aweme_id,
                         ROW_NUMBER() OVER (
                           PARTITION BY lsr.keyword
                           ORDER BY lsr.position_no,lsr.aweme_id
                         ) AS keyword_rank
                  FROM like_search_results lsr
                  JOIN like_search_jobs lsj ON lsj.id=lsr.job_id
                  WHERE lsr.job_id=? AND lsj.account_key=?
                ) ranked
                WHERE ranked.aweme_id=vb.aweme_id
                  AND ranked.keyword_rank<=?
              )
            """
            params.extend((search_job, account_key, per_keyword))
        else:
            query += """
          AND EXISTS (
            SELECT 1 FROM like_search_results lsr
            JOIN like_search_jobs lsj ON lsj.id=lsr.job_id
            WHERE lsr.job_id=? AND lsr.aweme_id=vb.aweme_id
              AND lsj.account_key=?
          )
        """
            params.extend((search_job, account_key))
    query += " ORDER BY vb.updated_at DESC LIMIT ?"
    params.append(limit)
    return [dict(row) for row in conn.execute(query, params)]


def _extension(url: str, default: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".webp"} else default


def _music_extension(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in {".mp3", ".m4a", ".aac"} else ".mp3"


def _has_expected_media_header(path: Path, media_type: str) -> bool:
    header = path.read_bytes()[:16]
    if media_type == "video":
        return len(header) >= 12 and header[4:8] == b"ftyp"
    if media_type == "music":
        return header.startswith(b"ID3") or (
            len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0
        ) or (len(header) >= 12 and header[4:8] == b"ftyp")
    return True


def _safe_component(value: str, max_length: int) -> str:
    """生成适用于 Windows 文件名的短文本，保留中文以提高辨识度。"""
    value = re.sub(
        r"[\x00-\x1f<>:\"/\\|?*：，。、“”‘’；！]+", "_", str(value or "")
    )
    value = re.sub(r"[\s_]+", "_", value).strip(" ._")
    return value[:max_length].rstrip(" ._")


def media_stem(
    file_code: int,
    nickname: str,
    title: str,
    aweme_id: str = "",
) -> str:
    """返回同一作品所有媒体共用的可读文件名主体。"""
    code = int(file_code)
    if not 1 <= code <= 99999:
        raise ValueError(f"文件标识编号超出五位数范围: {code}")
    author = _safe_component(nickname, 16) or "未知作者"
    summary = _safe_component(title, 32)
    parts = [f"{code:05d}", author]
    if summary and summary != author:
        parts.append(summary)
    # aweme_id 只在数据库中保留；这里的回退参数供迁移工具识别旧文件。
    return "_".join(parts)


def _download_url(
    session: requests.Session,
    url: str,
    target: Path,
    minimum_size: int = 256,
    expected_type: str = "",
    retries: int = 3,
) -> tuple[bool, str]:
    partial = target.with_suffix(target.suffix + ".part")
    last_error = ""
    for attempt in range(1, retries + 1):
        try:
            existing = partial.stat().st_size if partial.exists() else 0
            headers = {"Range": f"bytes={existing}-"} if existing else {}
            with session.get(
                url, stream=True, timeout=(8, 15), headers=headers
            ) as response:
                if existing and response.status_code == 416:
                    if partial.stat().st_size >= minimum_size and (
                        not expected_type
                        or _has_expected_media_header(partial, expected_type)
                    ):
                        partial.replace(target)
                        return True, ""
                    partial.unlink()
                    raise RuntimeError("断点文件无效，已清除并准备重新下载")
                response.raise_for_status()
                append = existing > 0 and response.status_code == 206
                if existing and not append:
                    existing = 0
                content_type = response.headers.get("content-type", "").lower()
                if "text/html" in content_type or "application/json" in content_type:
                    raise RuntimeError("资源地址已过期或返回了验证页面")
                with partial.open("ab" if append else "wb") as handle:
                    for chunk in response.iter_content(1024 * 512):
                        if chunk:
                            handle.write(chunk)
            if partial.stat().st_size < minimum_size:
                raise RuntimeError("下载内容过小，资源地址可能已过期")
            if expected_type and not _has_expected_media_header(partial, expected_type):
                raise RuntimeError(f"下载内容不是有效的{expected_type}文件")
            partial.replace(target)
            return True, ""
        except Exception as exc:
            last_error = str(exc)
            if attempt < retries:
                time.sleep(min(8, 2 ** (attempt - 1)))
    return False, last_error


def _download_one(
    conn: Any,
    row: dict[str, Any],
    session: requests.Session,
    account_key: str,
    retries: int = 3,
) -> bool:
    aweme_id = row["aweme_id"]
    root = get_account_downloads_dir(account_key)
    video_dir = root / "videos"
    is_image_post = row.get("type") == "image"
    if is_image_post:
        post_root = root / "image_posts"
        cover_dir = post_root / "covers"
        image_dir = post_root / "images"
        music_dir = post_root / "music"
    else:
        cover_dir = root / "covers"
        image_dir = root / "images"
        music_dir = root / "music"
    for directory in (video_dir, cover_dir, image_dir, music_dir):
        directory.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    code_prefix = f"{int(row['file_code']):05d}_"
    saved_video_path = str(row.get("saved_video_path") or "")
    saved_stem = Path(saved_video_path).stem if saved_video_path else ""
    if saved_stem.startswith(code_prefix):
        stem = saved_stem
    else:
        saved_asset = conn.execute(
            """
            SELECT ma.asset_type,ma.position_no,amf.local_path
            FROM media_assets ma
            JOIN account_media_files amf ON amf.media_asset_id=ma.id
              AND amf.account_key=?
            WHERE ma.aweme_id=? AND amf.local_path!=''
            ORDER BY CASE ma.asset_type
              WHEN 'cover' THEN 0 WHEN 'image' THEN 1 ELSE 2 END,position_no
            LIMIT 1
            """,
            (account_key, aweme_id),
        ).fetchone()
        stem = ""
        if saved_asset:
            stem = Path(saved_asset["local_path"]).stem
            if saved_asset["asset_type"] == "image":
                image_suffix = f"_{int(saved_asset['position_no']):02d}"
                if stem.endswith(image_suffix):
                    stem = stem[: -len(image_suffix)]
        if not stem.startswith(code_prefix):
            stem = media_stem(
                row["file_code"],
                row.get("nickname", ""),
                row.get("title") or row.get("desc", ""),
            )

    video_target = video_dir / f"{stem}.mp4"
    if not is_image_post and row.get("video_url"):
        if not (
            video_target.exists()
            and video_target.stat().st_size > 1024
            and _has_expected_media_header(video_target, "video")
        ):
            ok, error = _download_url(
                session,
                row["video_url"],
                video_target,
                minimum_size=1024,
                expected_type="video",
                retries=retries,
            )
            if not ok:
                failures.append(f"video: {error}")

    assets = [
        dict(asset)
        for asset in conn.execute(
            """
            SELECT ma.id,ma.asset_type,ma.position_no,ma.remote_url,
                   COALESCE(amf.local_path,'') AS local_path,
                   COALESCE(amf.status,0) AS status,
                   COALESCE(amf.download_error,'') AS download_error
            FROM media_assets ma
            LEFT JOIN account_media_files amf ON amf.media_asset_id=ma.id
              AND amf.account_key=?
            WHERE ma.aweme_id=?
            ORDER BY CASE ma.asset_type
              WHEN 'cover' THEN 0 WHEN 'image' THEN 1 ELSE 2 END, position_no
            """,
            (account_key, aweme_id),
        )
    ]
    for asset in assets:
        if asset["asset_type"] == "cover":
            filename = stem + _extension(asset["remote_url"], ".jpg")
            target = cover_dir / filename
        elif asset["asset_type"] == "image":
            filename = f"{stem}_{int(asset['position_no']):02d}" + _extension(
                asset["remote_url"], ".jpg",
            )
            target = image_dir / filename
        else:
            filename = stem + _music_extension(asset["remote_url"])
            target = music_dir / filename
        existing_valid = target.exists() and target.stat().st_size > (
            1024 if asset["asset_type"] == "music" else 256
        )
        if asset["asset_type"] == "music" and existing_valid:
            existing_valid = _has_expected_media_header(target, "music")
        if existing_valid:
            ok, error = True, ""
        else:
            ok, error = _download_url(
                session,
                asset["remote_url"],
                target,
                minimum_size=1024 if asset["asset_type"] == "music" else 256,
                expected_type="music" if asset["asset_type"] == "music" else "",
                retries=retries,
            )
        if (
            ok
            and asset["asset_type"] == "music"
            and target.suffix.lower() != ".m4a"
            and target.read_bytes()[:8][4:8] == b"ftyp"
        ):
            m4a_target = target.with_suffix(".m4a")
            if not m4a_target.exists():
                target.replace(m4a_target)
            target = m4a_target
        conn.execute(
            """
            INSERT INTO account_media_files
              (account_key,media_asset_id,local_path,status,download_error,updated_at)
            VALUES (?,?,?,?,?,?)
            ON DUPLICATE KEY UPDATE local_path=VALUES(local_path),
              status=VALUES(status),download_error=VALUES(download_error),
              updated_at=VALUES(updated_at)
            """,
            (
                account_key,
                asset["id"],
                str(target) if ok else "",
                2 if ok else 3,
                error,
                now(),
            ),
        )
        asset.update(
            {
                "local_path": str(target) if ok else "",
                "status": 2 if ok else 3,
                "download_error": error,
            }
        )
        if not ok:
            failures.append(
                f"{asset['asset_type']}[{asset['position_no']}]: {error}"
            )
    if failures:
        if is_image_post:
            conn.execute(
                """
                UPDATE account_download_tasks SET video_path=''
                WHERE account_key=? AND aweme_id=?
                """,
                (account_key, aweme_id),
            )
        update_download_status(
            conn,
            aweme_id,
            DL_FAILED,
            download_path=(
                str(video_target)
                if not is_image_post and video_target.exists()
                else ""
            ),
            error="; ".join(failures),
            account_key=account_key,
        )
        return False
    if is_image_post:
        conn.execute(
            """
            UPDATE account_download_tasks SET video_path=''
            WHERE account_key=? AND aweme_id=?
            """,
            (account_key, aweme_id),
        )
    update_download_status(
        conn,
        aweme_id,
        DL_DONE,
        str(video_target)
        if not is_image_post and video_target.exists()
        else "",
        account_key=account_key,
    )
    return True


def run(
    category: str = "",
    author: str = "",
    limit: int = 10,
    retry_failed: bool = False,
    workers: int = 3,
    retries: int = 3,
    min_free_gb: float = 5.0,
    search_job: int = 0,
    per_keyword: int = 0,
) -> dict[str, Any]:
    success = failed = 0
    started = time.monotonic()
    with get_conn() as conn:
        init_db(conn)
        account_key = get_account_key()
        queue = _queue(
            conn,
            account_key,
            category,
            author,
            limit,
            retry_failed,
            search_job=search_job,
            per_keyword=per_keyword,
        )
    root = get_account_downloads_dir(account_key)
    free_gb = shutil.disk_usage(root).free / (1024 ** 3)
    if free_gb < min_free_gb:
        raise RuntimeError(
            f"磁盘剩余空间仅 {free_gb:.1f} GB，低于要求的 {min_free_gb:.1f} GB"
        )

    def worker(row: dict[str, Any]) -> tuple[bool, int]:
        session = _new_session()
        before = _media_size_for_code(root, row["file_code"])
        with get_conn() as worker_conn:
            init_db(worker_conn)
            ok = _download_one(
                worker_conn, row, session, account_key, retries=retries
            )
            worker_conn.commit()
        return ok, max(
            0, _media_size_for_code(root, row["file_code"]) - before
        )

    completed_bytes = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(worker, row): row for row in queue}
        for index, future in enumerate(as_completed(futures), 1):
            row = futures[future]
            try:
                ok, added = future.result()
            except Exception as exc:
                ok, added = False, 0
                with get_conn() as error_conn:
                    update_download_status(
                        error_conn,
                        row["aweme_id"],
                        DL_FAILED,
                        error=str(exc),
                        account_key=account_key,
                    )
                    error_conn.commit()
            completed_bytes += added
            success += int(ok)
            failed += int(not ok)
            elapsed = max(0.001, time.monotonic() - started)
            speed = completed_bytes / elapsed / (1024 ** 2)
            _print_progress(
                f"[{index}/{len(queue)}] {'完成' if ok else '失败'} "
                f"{row['nickname']} - {row['desc'][:32]} | {speed:.2f} MB/s"
            )
    elapsed = time.monotonic() - started
    return {
        "total": success + failed,
        "success": success,
        "failed": failed,
        "workers": workers,
        "elapsed_seconds": round(elapsed, 1),
        "average_mb_s": round(completed_bytes / max(elapsed, 0.001) / (1024 ** 2), 2),
        "downloaded_bytes": completed_bytes,
        "free_gb_after": round(shutil.disk_usage(root).free / (1024 ** 3), 1),
    }


def download_ids(aweme_ids: list[str]) -> dict[str, int]:
    """只下载明确指定的作品 ID，供组合来源选择使用。"""
    if not aweme_ids:
        return {"total": 0, "success": 0, "failed": 0}
    session = requests.Session()
    session.cookies.update(_cookie_dict())
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/131 Safari/537.36"
            ),
            "Referer": "https://www.douyin.com/",
        }
    )
    success = failed = 0
    with get_conn() as conn:
        init_db(conn)
        account_key = get_account_key()
        queue = _queue(
            conn,
            account_key,
            category="",
            author="",
            limit=len(aweme_ids),
            retry_failed=True,
            aweme_ids=aweme_ids,
        )
        by_id = {row["aweme_id"]: row for row in queue}
        for index, aweme_id in enumerate(aweme_ids, 1):
            row = by_id.get(aweme_id)
            if not row:
                failed += 1
                continue
            print(f"[{index}/{len(aweme_ids)}] {row['nickname']} - {row['desc'][:40]}")
            if _download_one(conn, row, session, account_key):
                success += 1
            else:
                failed += 1
            conn.commit()
    return {"total": len(aweme_ids), "success": success, "failed": failed}
