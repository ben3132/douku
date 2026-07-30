"""用站点 Extractor 解析陌生链接，并把解析与下载分成两个阶段。"""

from __future__ import annotations

import ipaddress
import os
import shutil
import socket
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from lib.db.db_v4 import get_conn, init_db, now
from lib.download.download_videos import (
    _download_url,
    _has_expected_media_header,
    _safe_component,
)
from lib.utils.auth import load_auth
from lib.utils.meta import get_direct_downloads_dir, load_config


def detect_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host == "b23.tv" or host.endswith(".bilibili.com"):
        return "bilibili"
    if (
        host in {"douyin.com", "iesdouyin.com", "v.douyin.com"}
        or host.endswith(".douyin.com")
        or host.endswith(".iesdouyin.com")
    ):
        return "douyin"
    if host.endswith(".youtube.com") or host == "youtu.be":
        return "youtube"
    if host.endswith(".xiaohongshu.com") or host == "xhslink.com":
        return "xiaohongshu"
    return (host.removeprefix("www.").split(".")[0] or "generic")[:64]


def validate_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"只支持 http/https 公网链接: {url}")
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith(".local"):
        raise ValueError("拒绝解析本机或局域网地址")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, None)}
    except socket.gaierror as exc:
        raise ValueError(f"域名无法解析: {host}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("拒绝解析本机、局域网或保留地址")
    return url.strip()


def _cookie_header(platform: str) -> str:
    if platform != "douyin":
        return ""
    return "; ".join(
        f"{cookie['name']}={cookie['value']}"
        for cookie in load_auth().get("cookies") or []
        if cookie.get("name") and cookie.get("value")
    )


def _yt_options(platform: str, download: bool, output_template: str = "") -> dict:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 3,
        "extractor_retries": 2,
    }
    if download:
        configured_ffmpeg = (
            os.environ.get("DOUKU_FFMPEG")
            or load_config().get("ffmpeg_path")
        )
        ffmpeg_available = bool(
            configured_ffmpeg or shutil.which("ffmpeg")
        )
        options.update(
            {
                "format": (
                    "bv*+ba/b"
                    if ffmpeg_available
                    else "best[ext=mp4]/best"
                ),
                "outtmpl": output_template,
                "merge_output_format": "mp4",
                "windowsfilenames": True,
                "overwrites": False,
                "continuedl": True,
            }
        )
        if configured_ffmpeg:
            options["ffmpeg_location"] = configured_ffmpeg
    else:
        options["skip_download"] = True
    return options


def _cookie_file(platform: str) -> Path | None:
    if platform != "douyin":
        return None
    cookies = load_auth().get("cookies") or []
    if not cookies:
        return None
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".cookies.txt",
        delete=False,
    )
    try:
        handle.write("# Netscape HTTP Cookie File\n")
        for cookie in cookies:
            name = str(cookie.get("name") or "")
            value = str(cookie.get("value") or "")
            domain = str(cookie.get("domain") or ".douyin.com")
            if not name or not value:
                continue
            include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
            path = str(cookie.get("path") or "/")
            secure = "TRUE" if cookie.get("secure") else "FALSE"
            expires = int(float(cookie.get("expires") or 0))
            handle.write(
                "\t".join(
                    (
                        domain,
                        include_subdomains,
                        path,
                        secure,
                        str(max(0, expires)),
                        name,
                        value,
                    )
                )
                + "\n"
            )
    finally:
        handle.close()
    return Path(handle.name)


@contextmanager
def _yt_option_scope(
    platform: str,
    download: bool,
    output_template: str = "",
):
    cookie_path = _cookie_file(platform)
    options = _yt_options(platform, download, output_template)
    if cookie_path:
        options["cookiefile"] = str(cookie_path)
    try:
        yield options
    finally:
        if cookie_path and cookie_path.exists():
            cookie_path.unlink()


def _browser_resolve_douyin(url: str) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    from lib.collector import (
        _context_page,
        _launch_persistent_context,
        _restore_auth_backup,
    )

    captured: dict[str, Any] = {}
    with sync_playwright() as playwright:
        context = _launch_persistent_context(playwright, headless=False)
        _restore_auth_backup(context)
        page = _context_page(context)

        def handle_response(response: Any) -> None:
            if not response.request.url.split("?", 1)[0].endswith(
                "/aweme/v1/web/aweme/detail/"
            ):
                return
            try:
                data = response.json()
                item = data.get("aweme_detail") or data.get("aweme") or {}
                if item:
                    captured.update(item)
            except Exception:
                pass

        page.on("response", handle_response)
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(10_000)
        page.remove_listener("response", handle_response)
        final_url = page.url
        context.close()
    if not captured:
        raise RuntimeError("抖音页面没有产生作品详情响应，可能需要重新登录或验证")
    video = captured.get("video") or {}
    urls = (video.get("play_addr") or {}).get("url_list") or []
    if not urls:
        raise RuntimeError("该抖音链接不是可下载的视频作品")
    author = captured.get("author") or {}
    return {
        "id": str(captured.get("aweme_id") or ""),
        "title": captured.get("desc") or "",
        "uploader": author.get("nickname") or "",
        "webpage_url": final_url,
        "extractor": "DouKU-Douyin",
        "ext": "mp4",
        "formats": [
            {
                "format_id": "douyin-play",
                "url": urls[0],
                "ext": "mp4",
                "vcodec": "unknown",
                "acodec": "unknown",
                "protocol": "https",
            }
        ],
    }


def resolve_link(url: str) -> dict[str, Any]:
    url = validate_url(url)
    platform = detect_platform(url)
    try:
        from yt_dlp import YoutubeDL

        with _yt_option_scope(platform, download=False) as options:
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False)
                return YoutubeDL.sanitize_info(info)
    except Exception:
        if platform == "douyin":
            return _browser_resolve_douyin(url)
        raise


def _role(item: dict[str, Any]) -> str:
    if item.get("protocol") in {"m3u8", "m3u8_native", "http_dash_segments"}:
        return "manifest"
    has_video = item.get("vcodec") not in {None, "none"}
    has_audio = item.get("acodec") not in {None, "none"}
    if has_video and has_audio:
        return "combined"
    return "video" if has_video else "audio"


def _store_resolution(conn: Any, job_id: int, info: dict[str, Any]) -> int:
    formats = info.get("formats") or []
    saved = 0
    for index, item in enumerate(formats):
        remote_url = item.get("url")
        if not remote_url:
            continue
        format_id = str(item.get("format_id") or index)
        role = _role(item)
        conn.execute(
            """
            INSERT INTO direct_media_urls
              (job_id,format_id,media_role,remote_url,protocol,ext,width,height,
               filesize,selected,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON DUPLICATE KEY UPDATE remote_url=VALUES(remote_url),
              protocol=VALUES(protocol),ext=VALUES(ext),
              width=VALUES(width),height=VALUES(height),
              filesize=VALUES(filesize),selected=VALUES(selected)
            """,
            (
                job_id,
                format_id,
                role,
                remote_url,
                item.get("protocol") or "",
                item.get("ext") or "",
                item.get("width"),
                item.get("height"),
                item.get("filesize") or item.get("filesize_approx"),
                role == "combined",
                now(),
            ),
        )
        saved += 1
    thumbnail = info.get("thumbnail")
    if thumbnail:
        conn.execute(
            """
            INSERT INTO direct_media_urls
              (job_id,format_id,media_role,remote_url,protocol,ext,selected,created_at)
            VALUES (?,'thumbnail','thumbnail',?,'https','jpg',0,?)
            ON DUPLICATE KEY UPDATE remote_url=VALUES(remote_url)
            """,
            (job_id, thumbnail, now()),
        )
        saved += 1
    return saved


def _download_resolved(
    conn: Any,
    job_id: int,
    platform: str,
    input_url: str,
    info: dict[str, Any],
) -> list[str]:
    title = _safe_component(info.get("title") or "未命名", 60)
    prefix = f"D{job_id:06d}_{title}"
    output_dir = get_direct_downloads_dir(platform)
    extractor = str(info.get("extractor") or "")
    if extractor == "DouKU-Douyin":
        target = output_dir / f"{prefix}.mp4"
        url = (info.get("formats") or [{}])[0].get("url") or ""
        session = requests.Session()
        cookie = _cookie_header("douyin")
        if cookie:
            session.headers["Cookie"] = cookie
        session.headers["Referer"] = "https://www.douyin.com/"
        ok, error = _download_url(
            session,
            url,
            target,
            minimum_size=1024,
            expected_type="video",
        )
        if not ok:
            raise RuntimeError(error)
    else:
        from yt_dlp import YoutubeDL

        template = str(output_dir / f"{prefix}.%(ext)s")
        with _yt_option_scope(platform, True, template) as options:
            with YoutubeDL(options) as ydl:
                ydl.extract_info(input_url, download=True)
    paths = [
        path
        for path in output_dir.glob(f"{prefix}.*")
        if path.is_file() and path.suffix not in {".part", ".ytdl"}
    ]
    if not paths:
        raise RuntimeError("下载器完成后没有找到输出文件")
    for path in paths:
        if path.suffix.lower() in {".mp4", ".mov", ".m4v"}:
            if not _has_expected_media_header(path, "video"):
                raise RuntimeError(f"下载结果不是有效视频文件: {path.name}")
        conn.execute(
            """
            INSERT INTO direct_download_files
              (job_id,file_type,local_path,file_size,created_at)
            VALUES (?,'media',?,?,?)
            ON DUPLICATE KEY UPDATE file_size=VALUES(file_size)
            """,
            (job_id, str(path), path.stat().st_size, now()),
        )
    return [str(path) for path in paths]


def process_link(url: str, download: bool = True) -> dict[str, Any]:
    platform = detect_platform(url)
    with get_conn() as conn:
        init_db(conn)
        cursor = conn.execute(
            """
            INSERT INTO direct_download_jobs
              (input_url,platform,status,created_at,updated_at)
            VALUES (?,?,0,?,?)
            """,
            (url, platform, now(), now()),
        )
        job_id = int(cursor.lastrowid)
        try:
            info = resolve_link(url)
            extractor = str(info.get("extractor_key") or info.get("extractor") or "")
            platform = detect_platform(str(info.get("webpage_url") or url))
            conn.execute(
                """
                UPDATE direct_download_jobs
                SET canonical_url=?,platform=?,extractor=?,content_id=?,title=?,
                    uploader=?,status=1,updated_at=?
                WHERE id=?
                """,
                (
                    info.get("webpage_url") or url,
                    platform,
                    extractor,
                    str(info.get("id") or ""),
                    str(info.get("title") or "")[:512],
                    str(info.get("uploader") or "")[:255],
                    now(),
                    job_id,
                ),
            )
            format_count = _store_resolution(conn, job_id, info)
            files = (
                _download_resolved(conn, job_id, platform, url, info)
                if download
                else []
            )
            conn.execute(
                """
                UPDATE direct_download_jobs SET status=?,updated_at=? WHERE id=?
                """,
                (2 if download else 1, now(), job_id),
            )
            conn.commit()
            return {
                "success": True,
                "job_id": job_id,
                "platform": platform,
                "extractor": extractor,
                "title": info.get("title") or "",
                "formats": format_count,
                "files": files,
            }
        except Exception as exc:
            conn.execute(
                """
                UPDATE direct_download_jobs
                SET status=3,download_error=?,updated_at=? WHERE id=?
                """,
                (str(exc)[:1000], now(), job_id),
            )
            conn.commit()
            return {
                "success": False,
                "job_id": job_id,
                "platform": platform,
                "error": str(exc),
            }


def run_links(urls: list[str], download: bool = True) -> dict[str, Any]:
    results = [process_link(url, download=download) for url in urls]
    return {
        "total": len(results),
        "success": sum(bool(item["success"]) for item in results),
        "failed": sum(not item["success"] for item in results),
        "results": results,
    }
