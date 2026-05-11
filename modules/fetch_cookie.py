# -*- coding: utf-8 -*-
"""
playwright_cookie.py - 自动从浏览器获取抖音 Cookie

支持 Edge（默认）和 Chrome，自动从已登录的浏览器会话中提取：
- sessionid（必需，用于认证）
- ttwid（防CSRF token）
- sec_user_id（用户唯一标识）

用法：
    python -m modules.fetch_cookie
    python dytool.py cookie
"""
import subprocess, os, time, sys, json
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    sync_playwright = None  # type: ignore

# 浏览器选择
EDGE_PATH = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
DEFAULT_PROFILE = r"%LOCALAPPDATA%\Microsoft\Edge\User Data"
CHROME_PROFILE = r"%LOCALAPPDATA%\Google\Chrome\User Data"

# Cookie 保存路径（项目根目录的 .cookie 文件）
SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
COOKIE_PATH = PROJECT_ROOT / ".cookie"


def check_dependencies():
    """B1+B2: 检测 playwright 包和 chromium 浏览器是否安装"""
    errors = []

    # B1: playwright 包
    if not PLAYWRIGHT_AVAILABLE:
        errors.append("playwright 未安装 → pip install playwright")
        return False, errors

    # B2: chromium 浏览器
    try:
        pw_browsers = Path(os.environ["LOCALAPPDATA"]) / "ms-playwright"
        chromium_dirs = list(pw_browsers.glob("chromium-*")) if pw_browsers.exists() else []
        if not chromium_dirs:
            errors.append("chromium 浏览器未安装 → playwright install chromium")
    except Exception:
        errors.append("无法检测 chromium 浏览器 → 请运行: playwright install chromium")

    return len(errors) == 0, errors


def get_browser_exe():
    """查找可用的浏览器"""
    if os.path.exists(EDGE_PATH):
        return EDGE_PATH, os.path.expandvars(DEFAULT_PROFILE)
    if os.path.exists(CHROME_PATH):
        return CHROME_PATH, os.path.expandvars(CHROME_PROFILE)
    return None, None


def check_browser_closed(exe_path):
    """
    B3: 检测浏览器是否运行，提示用户手动关闭（不强制杀进程）
    因为 Playwright 需要独占 profile 目录，必须确保浏览器未运行
    """
    name = os.path.basename(exe_path).replace(".exe", "")

    # 检查进程是否在运行
    result = subprocess.run(
        ["tasklist", "/fi", f"IMAGENAME eq {name}.exe"],
        capture_output=True, text=True
    )

    if f"{name}.exe" in result.stdout:
        print(f"检测到 {name} 正在运行")
        print(f"请手动关闭所有 {name} 窗口后，按回车继续...")
        input()

        # 二次确认（用户可能没关干净）
        result2 = subprocess.run(
            ["tasklist", "/fi", f"IMAGENAME eq {name}.exe"],
            capture_output=True, text=True
        )
        if f"{name}.exe" in result2.stdout:
            print(f"{name} 仍在运行，请关闭后重试")
            sys.exit(1)
    else:
        print(f"{name} 未运行，准备启动...\n")


def fetch_cookies(exe_path: str, profile_dir: str) -> dict:
    """从浏览器会话中获取抖音 Cookie"""
    print(f"启动浏览器（加载配置目录: {os.path.basename(os.path.dirname(profile_dir))}）...")
    print("请确认浏览器已显示抖音页面（已登录状态）\n")

    cookies_data = {}

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            executable_path=exe_path,
            headless=False,
            args=[
                "--profile-directory=Default",
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
            ],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.douyin.com", timeout=60000, wait_until="domcontentloaded")

        print(f"当前URL: {page.url}")
        input("\n确认已显示抖音登录页面后，按回车继续...")

        cookies = context.cookies()
        for c in cookies:
            cookies_data[c["name"]] = c["value"]

        context.close()

    return cookies_data


def save_cookie(sessionid: str, ttwid: str = "", sec_user_id: str = ""):
    """保存 Cookie 到 .cookie JSON 文件"""
    cookie_data = {
        "sessionid": sessionid,
        "sec_user_id": sec_user_id,
        "ttwid": ttwid,
    }
    with open(COOKIE_PATH, "w", encoding="utf-8") as f:
        json.dump(cookie_data, f, ensure_ascii=False, indent=2)
    print(f"已保存到 {COOKIE_PATH}")


def main():
    # B1+B2: 依赖检测
    ok, errors = check_dependencies()
    if not ok:
        print("\n".join(errors))
        sys.exit(1)

    exe_path, profile_dir = get_browser_exe()
    if not exe_path:
        print("未找到 Edge 或 Chrome，请安装后重试")
        sys.exit(1)

    # B3: 检测浏览器是否运行，提示手动关闭
    check_browser_closed(exe_path)

    # 获取 Cookie
    cookies = fetch_cookies(exe_path, profile_dir)

    sessionid = cookies.get("sessionid", "")
    ttwid = cookies.get("ttwid", "")
    sec_user_id = cookies.get("sid_tt", "")  # 有时 sec_user_id 就是 sid_tt

    print(f"\nsessionid: {sessionid[:20]}..." if sessionid else "\nsessionid: 未找到")
    print(f"ttwid: {ttwid[:20]}..." if ttwid else "ttwid: 未找到")

    if sessionid:
        save_cookie(sessionid, ttwid, sec_user_id)
        print("\nCookie 获取成功！")
    else:
        print("\n未获取到 sessionid，请在浏览器中登录抖音后重试")


if __name__ == "__main__":
    main()
