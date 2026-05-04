"""
权限管理模块
检测权限、请求提权、执行特权操作
"""

import sys
import os
import ctypes
import subprocess


def is_admin():
    """检测当前是否以管理员权限运行"""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False


def run_as_admin(script_path, params=None):
    """
    以管理员权限运行脚本
    返回：是否成功启动
    """
    if params is None:
        params = []
    
    try:
        # 使用ShellExecuteW API提权
        # 参数：HWND, 操作, 程序, 参数, 目录, 显示方式(1=SW_SHOWNORMAL)
        params_str = " ".join(params) if params else ""
        ctypes.windll.shell32.ShellExecuteW(
            None, 
            "runas",  # UAC提权
            sys.executable,  # python.exe
            f'"{script_path}" {params_str}',  # 脚本路径和参数
            None, 
            1  # SW_SHOWNORMAL
        )
        return True
    except Exception as e:
        print(f"[错误] 提权失败: {e}")
        return False


def request_admin_for_cookie():
    """
    请求管理员权限以读取浏览器Cookie
    返回：用户是否同意
    """
    print("\n" + "=" * 60)
    print("[权限请求] 需要管理员权限")
    print("=" * 60)
    print("原因：读取浏览器Cookie需要解密Windows凭据")
    print("安全说明：")
    print("  - 仅用于读取抖音Cookie（sessionid, ttwid）")
    print("  - 不会修改任何系统文件")
    print("  - Cookie仅保存在本地配置文件中")
    print("=" * 60)
    
    try:
        response = input("\n是否允许以管理员权限运行？(Y/n): ").strip().lower()
        return response in ["", "y", "yes"]
    except:
        return False


if __name__ == "__main__":
    # 测试
    if is_admin():
        print("[OK] 当前以管理员权限运行")
    else:
        print("[信息] 当前以普通用户权限运行")
        
        # 测试请求权限
        if request_admin_for_cookie():
            print("\n用户同意提权，正在启动...")
            # 在实际使用中，这里会重新以管理员身份运行自己
        else:
            print("\n用户拒绝提权")
