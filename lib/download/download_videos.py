"""按媒体类型归档视频、封面和图文原图，关联关系保存在 MySQL。"""

from __future__ import annotations

import re
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
from lib.utils.meta import get_downloads_dir


def _cookie_dict() -> dict[str, str]:
    return {
        str(cookie.get("name")): str(cookie.get("value"))
        for cookie in (load_auth().get("cookies") or [])
        if cookie.get("name") and cookie.get("value")
    }


def _queue(
    conn: Any,
    category: str,
    author: str,
    limit: int,
    retry_failed: bool,
    aweme_ids: list[str] | None = None,
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
        JOIN download_tasks vd ON vd.aweme_id=vb.aweme_id
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
    params: list[Any] = list(statuses)
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
        )
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
) -> tuple[bool, str]:
    partial = target.with_suffix(target.suffix + ".part")
    try:
        with session.get(url, stream=True, timeout=(15, 90)) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            if "text/html" in content_type or "application/json" in content_type:
                raise RuntimeError("资源地址已过期或返回了验证页面")
            with partial.open("wb") as handle:
                for chunk in response.iter_content(1024 * 256):
                    if chunk:
                        handle.write(chunk)
        if partial.stat().st_size < minimum_size:
            raise RuntimeError("下载内容过小，资源地址可能已过期")
        if expected_type and not _has_expected_media_header(partial, expected_type):
            raise RuntimeError(f"下载内容不是有效的{expected_type}文件")
        partial.replace(target)
        return True, ""
    except Exception as exc:
        if partial.exists():
            partial.unlink()
        return False, str(exc)


def _download_one(
    conn: Any, row: dict[str, Any], session: requests.Session
) -> bool:
    aweme_id = row["aweme_id"]
    root = get_downloads_dir()
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
            SELECT asset_type,position_no,local_path
            FROM media_assets
            WHERE aweme_id=? AND local_path!=''
            ORDER BY CASE asset_type
              WHEN 'cover' THEN 0 WHEN 'image' THEN 1 ELSE 2 END,position_no
            LIMIT 1
            """,
            (aweme_id,),
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
            )
            if not ok:
                failures.append(f"video: {error}")

    assets = [
        dict(asset)
        for asset in conn.execute(
            """
            SELECT id,asset_type,position_no,remote_url,local_path,status,download_error
            FROM media_assets WHERE aweme_id=?
            ORDER BY CASE asset_type
              WHEN 'cover' THEN 0 WHEN 'image' THEN 1 ELSE 2 END, position_no
            """,
            (aweme_id,),
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
            )
        conn.execute(
            """
            UPDATE media_assets
            SET local_path=?,status=?,download_error=?,updated_at=?
            WHERE id=?
            """,
            (str(target) if ok else "", 2 if ok else 3, error, now(), asset["id"]),
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
                "UPDATE download_tasks SET video_path='' WHERE aweme_id=?",
                (aweme_id,),
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
        )
        return False
    if is_image_post:
        conn.execute(
            "UPDATE download_tasks SET video_path='' WHERE aweme_id=?",
            (aweme_id,),
        )
    update_download_status(
        conn,
        aweme_id,
        DL_DONE,
        str(video_target)
        if not is_image_post and video_target.exists()
        else "",
    )
    return True


def run(
    category: str = "",
    author: str = "",
    limit: int = 10,
    retry_failed: bool = False,
) -> dict[str, int]:
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
        queue = _queue(conn, category, author, limit, retry_failed)
        for index, row in enumerate(queue, 1):
            print(f"[{index}/{len(queue)}] {row['nickname']} - {row['desc'][:40]}")
            if _download_one(conn, row, session):
                success += 1
            else:
                failed += 1
            conn.commit()
    return {"total": success + failed, "success": success, "failed": failed}


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
        queue = _queue(
            conn,
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
            if _download_one(conn, row, session):
                success += 1
            else:
                failed += 1
            conn.commit()
    return {"total": len(aweme_ids), "success": success, "failed": failed}
