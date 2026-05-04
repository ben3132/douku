"""Playwright 获取抖音 Cookie - 自动关闭Edge后重新加载配置"""
import subprocess, os, time
from playwright.sync_api import sync_playwright

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
USER_DATA = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data")

print("关闭 Edge...")
subprocess.run(["taskkill", "/f", "/im", "msedge.exe"], capture_output=True)
time.sleep(2)

print("启动 Edge（加载已有配置）...")
with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        user_data_dir=USER_DATA,
        executable_path=EDGE,
        headless=False,
        args=["--profile-directory=Default", "--disable-blink-features=AutomationControlled"]
    )
    page = context.pages[0] if context.pages else context.new_page()
    page.goto("https://www.douyin.com", timeout=60000, wait_until="domcontentloaded")
    
    print(f"当前URL: {page.url}")
    print("\n请确认浏览器已显示抖音页面（已登录状态）")
    input("确认后按回车继续...")
    
    cookies = context.cookies()
    sessionid = ttwid = None
    for c in cookies:
        if c["name"] == "sessionid": sessionid = c["value"]
        if c["name"] == "ttwid": ttwid = c["value"]
    
    print(f"\nsessionid: {sessionid}")
    print(f"ttwid: {ttwid}")
    
    if sessionid and ttwid:
        # 读取现有配置，保留 SEC_USER_ID
        config_path = r"E:\xn\ai_xm\DY_huoqu_ag\two\modules\config.py"
        existing_sec_uid = ""
        try:
            with open(config_path, "r") as f:
                for line in f:
                    if line.startswith("SEC_USER_ID"):
                        existing_sec_uid = line.split('"')[1] if '"' in line else ""
                        break
        except FileNotFoundError:
            pass
        
        cfg = f'SESSION_ID = "{sessionid}"\nTTWID = "{ttwid}"\nSEC_USER_ID = "{existing_sec_uid}"\n'
        with open(config_path, "w") as f:
            f.write(cfg)
        print(f"已保存到 config.py（SEC_USER_ID 保留为: {existing_sec_uid}）")
    else:
        print("未获取到完整Cookie，请在浏览器中登录抖音后重试")
    
    context.close()
