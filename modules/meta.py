"""
meta.py - 数据路径解析 + 元数据读写核心模块

职责：
  1. 解析数据目录路径（支持 --data-dir 参数和默认 ./data/）
  2. 读写 project_meta.json
  3. 更新运行状态和统计信息
  4. 提供所有数据子目录的路径常量

设计原则：
  - 项目代码与用户数据完全解耦
  - 数据目录可跨项目/跨 Agent 共享
  - 元数据自描述，新 Agent 可快速理解已有数据
"""

import os
import sys
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any

# ============================================================
# 全局数据目录路径（延迟初始化）
# ============================================================

_DATA_ROOT: Optional[Path] = None
_META: Optional[Dict[str, Any]] = None

META_FILENAME = "project_meta.json"

# ============================================================
# 数据目录解析
# ============================================================

def init_data_root(data_dir: Optional[str] = None) -> Path:
    """
    初始化数据目录路径。
    
    优先级：
      1. 显式传入的 data_dir 参数
      2. 环境变量 DOUKU_DATA_DIR
      3. 默认 ./data/（相对于项目根目录）
    
    如果目录不存在，自动创建。
    返回 Path 对象。
    """
    global _DATA_ROOT
    
    if data_dir:
        path = Path(data_dir).resolve()
    elif os.environ.get("DOUKU_DATA_DIR"):
        path = Path(os.environ["DOUKU_DATA_DIR"]).resolve()
    else:
        # 默认：项目根目录下的 data/
        project_root = Path(__file__).parent.parent
        path = project_root / "data"
    
    # 创建目录（如果不存在）
    path.mkdir(parents=True, exist_ok=True)
    
    # 创建子目录
    (path / "logs").mkdir(exist_ok=True)
    (path / "downloads").mkdir(exist_ok=True)
    (path / "scripts").mkdir(exist_ok=True)
    
    _DATA_ROOT = path
    return path


def get_data_root() -> Path:
    """
    获取数据目录路径。
    如果尚未初始化，自动初始化（使用默认路径）。
    """
    global _DATA_ROOT
    if _DATA_ROOT is None:
        init_data_root()
    return _DATA_ROOT


def set_data_dir(data_dir: str) -> Path:
    """设置数据目录路径（别名，方便外部调用）"""
    return init_data_root(data_dir)


def get_project_root() -> Path:
    """
    项目根目录（代码目录，不受 --data-dir 影响）。
    基于本文件位置向上两级（modules/meta.py → three/）。
    """
    return Path(__file__).parent.parent


def get_db_path() -> Path:
    """数据库文件路径"""
    return get_data_root() / "douku.db"


def get_cookie_path() -> Path:
    """
    Cookie 文件路径（JSON 格式）。
    始终位于项目根目录，不受 --data-dir 影响。
    """
    return get_project_root() / ".cookie"


def get_config_path() -> Path:
    """兼容旧路径，仅返回不存在的占位路径（已废弃）"""
    return get_data_root() / "config.py"


def load_cookie() -> Dict[str, str]:
    """
    加载 Cookie 配置（从 .cookie JSON 文件）。

    返回格式：
        {"sessionid": "...", "sec_user_id": "...", "ttwid": "..."}

    如果文件不存在或解析失败，返回空字典。
    """
    cookie_path = get_cookie_path()
    if not cookie_path.exists():
        return {}

    try:
        with open(cookie_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            # 确保所有字段存在
            return {
                "sessionid": data.get("sessionid", ""),
                "sec_user_id": data.get("sec_user_id", ""),
                "ttwid": data.get("ttwid", ""),
            }
    except (json.JSONDecodeError, IOError, OSError):
        return {}


def get_logs_dir() -> Path:
    """日志目录路径"""
    return get_data_root() / "logs"


def get_downloads_dir(category: Optional[str] = None) -> Path:
    """
    下载目录路径。
    如果指定 category，返回分类子目录。
    """
    base = get_data_root() / "downloads"
    if category:
        path = base / category
        path.mkdir(parents=True, exist_ok=True)
        return path
    return base


def get_scripts_dir() -> Path:
    """运行时生成的脚本目录"""
    return get_data_root() / "scripts"


def get_meta_path() -> Path:
    """元数据文件路径"""
    return get_data_root() / META_FILENAME

# ============================================================
# 元数据读写
# ============================================================

def _default_meta() -> Dict[str, Any]:
    """返回默认的元数据结构"""
    now = datetime.now().isoformat()
    return {
        "version": "1.0",
        "created_at": now,
        "data_root": str(get_data_root()),
        "last_run": None,
        "stats": {
            "videos": 0,
            "authors": 0,
            "comments": 0,
            "likes_count": 0,
            "favorites_count": 0,
            "downloaded": 0
        },
        "last_sync": {
            "likes": None,
            "favorites": None,
            "profiles": None,
            "comments": None
        },
        "classification": {},
        "data_snapshot": {
            "category_distribution": {},
            "top_authors": []
        }
    }


def load_meta() -> Dict[str, Any]:
    """
    加载元数据。
    如果文件不存在，返回默认结构。
    """
    global _META
    meta_path = get_meta_path()
    
    if meta_path.exists():
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                _META = json.load(f)
        except (json.JSONDecodeError, IOError):
            _META = _default_meta()
    else:
        _META = _default_meta()
    
    return _META


def save_meta(meta: Optional[Dict[str, Any]] = None) -> None:
    """
    保存元数据到文件。
    如果不传入 meta 参数，保存当前缓存的元数据。
    """
    global _META
    if meta:
        _META = meta
    
    if _META is None:
        return
    
    meta_path = get_meta_path()
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(_META, f, ensure_ascii=False, indent=2)


def get_meta() -> Dict[str, Any]:
    """
    获取当前元数据（带缓存）。
    """
    global _META
    if _META is None:
        load_meta()
    return _META


def update_meta(**kwargs) -> None:
    """
    更新元数据的指定字段并保存。
    
    用法：
        update_meta(last_run=datetime.now().isoformat())
        update_meta(stats={"videos": 100, "authors": 50})
    """
    meta = get_meta()
    for key, value in kwargs.items():
        if isinstance(value, dict) and key in meta and isinstance(meta[key], dict):
            # 深度合并字典
            meta[key].update(value)
        else:
            meta[key] = value
    save_meta(meta)


def update_stats(**kwargs) -> None:
    """
    更新统计信息（便捷方法）。
    
    用法：
        update_stats(videos=100, authors=50)
    """
    meta = get_meta()
    meta["stats"].update(kwargs)
    save_meta(meta)


def update_last_sync(source: str, timestamp: Optional[str] = None) -> None:
    """
    更新最后同步时间。
    
    参数：
        source: "likes" | "favorites" | "profiles" | "comments"
        timestamp: ISO 格式时间字符串，默认当前时间
    """
    if timestamp is None:
        timestamp = datetime.now().isoformat()
    meta = get_meta()
    meta["last_sync"][source] = timestamp
    save_meta(meta)


def touch_run() -> None:
    """记录本次运行时间"""
    update_meta(last_run=datetime.now().isoformat())

# ============================================================
# 数据自检
# ============================================================

def check_data_integrity() -> Dict[str, Any]:
    """
    检查数据目录完整性。
    返回检查结果字典。
    """
    results = {
        "data_root_exists": get_data_root().exists(),
        "db_exists": get_db_path().exists(),
        "cookie_exists": get_cookie_path().exists(),
        "downloads_dir_exists": get_downloads_dir().exists(),
        "logs_dir_exists": get_logs_dir().exists(),
        "scripts_dir_exists": get_scripts_dir().exists(),
        "meta_valid": True,
        "issues": []
    }
    
    # 检查必要文件
    if not results["cookie_exists"]:
        results["issues"].append("缺少 .cookie 文件（运行 dytool.py cookie 获取，或手动创建）")
    
    if not results["db_exists"]:
        results["issues"].append("数据库文件不存在（需运行 fetch 同步数据）")
    
    # 检查元数据
    try:
        load_meta()
    except Exception as e:
        results["meta_valid"] = False
        results["issues"].append(f"元数据损坏: {e}")
    
    return results


def get_data_summary() -> Dict[str, Any]:
    """
    获取数据摘要，用于快速展示给用户或新 Agent。
    """
    meta = get_meta()
    integrity = check_data_integrity()
    
    return {
        "data_root": str(get_data_root()),
        "created_at": meta.get("created_at", "未知"),
        "last_run": meta.get("last_run", "从未运行"),
        "stats": meta.get("stats", {}),
        "last_sync": meta.get("last_sync", {}),
        "integrity": integrity,
        "ready": len(integrity["issues"]) == 0
    }

# ============================================================
# 初始化命令
# ============================================================

def init_project(data_dir: Optional[str] = None) -> Dict[str, Any]:
    """
    初始化项目数据目录。
    创建目录结构，生成默认元数据。
    返回初始化结果。
    """
    # 初始化数据目录
    root = init_data_root(data_dir)
    
    # 初始化元数据
    meta_path = get_meta_path()
    if not meta_path.exists():
        meta = _default_meta()
        meta["data_root"] = str(root)
        save_meta(meta)
    else:
        meta = load_meta()
    
    return {
        "success": True,
        "data_root": str(root),
        "meta": meta,
        "cookie_path": str(get_cookie_path()),
        "message": "项目初始化完成"
    }


# ============================================================
# 命令行测试
# ============================================================

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="数据目录管理")
    parser.add_argument("--data-dir", help="指定数据目录路径")
    parser.add_argument("--init", action="store_true", help="初始化数据目录")
    parser.add_argument("--info", action="store_true", help="显示数据摘要")
    parser.add_argument("--check", action="store_true", help="检查数据完整性")
    
    args = parser.parse_args()
    
    if args.init:
        result = init_project(args.data_dir)
        print(f"✅ {result['message']}")
        print(f"   数据目录: {result['data_root']}")
        print(f"   配置文件: {result['config_path']}")
    
    if args.info:
        summary = get_data_summary()
        print(f"📂 数据目录: {summary['data_root']}")
        print(f"📅 创建时间: {summary['created_at']}")
        print(f"🕐 最后运行: {summary['last_run']}")
        print(f"📊 统计信息:")
        for k, v in summary["stats"].items():
            print(f"   {k}: {v}")
        print(f"🔄 最后同步:")
        for k, v in summary["last_sync"].items():
            print(f"   {k}: {v or '从未'}")
    
    if args.check:
        integrity = check_data_integrity()
        print("🔍 数据完整性检查:")
        for k, v in integrity.items():
            if k != "issues":
                print(f"   {k}: {'✅' if v else '❌'}")
        if integrity["issues"]:
            print("⚠️ 问题:")
            for issue in integrity["issues"]:
                print(f"   - {issue}")
        else:
            print("✅ 数据完整，可以正常使用")
