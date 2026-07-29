"""
content_classifier.py - 视频内容分类（17 类赛道）

整合自 DouKu v4 content_classifier_v4.py
双层引擎：标签映射 + 关键词推断
"""

import json
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any
from lib.db.db_v4 import get_conn

# 17 个分类
CATEGORIES = [
    "颜值", "舞蹈", "二次元", "游戏", "美食", "知识", "剧情",
    "搞笑", "音乐", "萌宠", "时尚", "生活", "情感", "运动",
    "影视", "明星", "其他",
]

# Layer 1: 抖音官方 video_tags → 目标分类映射
TAG_MAP = {
    "颜值": {"颜值", "美女", "帅哥", "自拍", "素颜"},
    "舞蹈": {"舞蹈", "街舞", "芭蕾", "韩舞", "手势舞", "舞动"},
    "二次元": {"二次元", "动漫", "cos", "cosplay", "动画", "番剧", "漫画"},
    "游戏": {"游戏", "电竞", "王者荣耀", "原神", "吃鸡", "主机游戏", "网游", "手游"},
    "美食": {"美食", "吃播", "烹饪", "探店", "深夜放毒", "烘培", "菜谱", "小吃"},
    "知识": {"知识", "科普", "涨知识", "教育", "学习", "历史", "科技", "财经", "心理学"},
    "剧情": {"剧情", "短剧", "演绎", "情景剧", "反转", "脑洞"},
    "搞笑": {"搞笑", "幽默", "段子", "沙雕", "整活", "笑死"},
    "音乐": {"音乐", "唱歌", "翻唱", "乐器", "弹唱", "说唱", "声乐"},
    "萌宠": {"萌宠", "狗", "猫", "宠物", "金毛", "哈士奇", "喵星人"},
    "时尚": {"时尚", "穿搭", "美妆", "护肤", "变装", "发型", "化妆"},
    "生活": {"生活", "日常", "volg", "Vlog", "记录", "家居", "旅行", "亲子", "三农"},
    "情感": {"情感", "恋爱", "婚姻", "文案", "扎心", "治愈", "鸡汤"},
    "运动": {"运动", "健身", "体育", "篮球", "足球", "跑步", "瑜伽", "极限运动"},
    "影视": {"影视", "电影", "电视剧", "剪辑", "解说", "影评", "综艺"},
    "明星": {"明星", "演员", "歌手", "偶像", "娱乐圈", "综艺"},
}

# Layer 2: 关键词推断规则
KEYWORD_RULES: List[Tuple[str, List[str], int]] = [
    ("二次元", ["cos", "cosplay", "动漫", "原神", "二次元", "番剧推荐",
                "漫剪", "oc", "设子", "语c", "oc人"], 1),
    ("游戏", ["游戏", "GTA", "steam", "通关", "攻略", "电竞", "吃鸡",
              "王者", "原神", "我的世界", "mc", "lol", "瓦洛兰特",
              "游戏日常", "游戏实况", "打游戏", "上分"], 1),
    ("美食", ["美食", "吃播", "探店", "烹饪", "菜谱", "做饭", "厨房",
              "烘焙", "甜品", "吃货", "小吃", "外卖"], 1),
    ("知识", ["科普", "知识", "干货", "教程", "学习", "冷知识", "涨知识",
              "财经", "科技", "历史", "地理", "物理", "数学"], 1),
    ("搞笑", ["搞笑", "幽默", "段子", "整活", "神操作", "笑死",
              "哈哈哈哈", "哈哈哈"], 1),
    ("萌宠", ["狗", "猫", "宠物", "金毛", "哈士奇", "猫咪", "狗狗",
              "小狗", "小猫", "萌宠"], 1),
    ("时尚", ["穿搭", "美妆", "护肤", "化妆", "变装", "发型",
              "ootd", "试色", "口红"], 1),
    ("颜值", ["颜值", "美女", "帅哥", "自拍", "神仙颜值"], 1),
    ("舞蹈", ["舞蹈", "舞", "街舞", "爵士", "kpop", "编舞"], 1),
    ("音乐", ["唱歌", "翻唱", "音乐", "弹唱", "乐器", "钢琴",
              "吉他", "演唱", "原创歌曲"], 1),
    ("运动", ["健身", "运动", "减脂", "增肌", "瑜伽", "跑步",
              "篮球", "足球", "体育"], 1),
    ("情感", ["情感", "恋爱", "分手", "文案", "扎心", "治愈",
              "前任", "异地恋"], 1),
    ("生活", ["日常", "vlog", "volg", "记录", "旅行", "亲子",
              "农村", "三农", "种植", "养殖"], 1),
    ("影视", ["电影", "电视剧", "影评", "解说", "影视", "剪辑",
              "番剧", "国漫"], 1),
    ("剧情", ["短剧", "剧情", "反转", "悬疑", "虐心", "甜剧"], 1),
    ("明星", ["明星", "演员", "歌手", "偶像", "娱乐圈"], 1),
]


def classify_one(content: Dict[str, Any]) -> Dict[str, Any]:
    """
    对单条视频内容进行分类

    参数:
        content: 包含 desc, video_tags, desc_hashtags 等字段的 dict

    返回:
        {"category": str, "confidence": float (0-10), "source": str}
    """
    desc = (content.get("desc") or content.get("title") or "").lower()
    video_tags = content.get("video_tags") or content.get("tags") or []
    hashtags = content.get("desc_hashtags") or content.get("hashtags") or []

    # 提取标签名称
    tag_names = set()
    for t in video_tags:
        if isinstance(t, dict):
            name = t.get("tag_name", "") or t.get("name", "")
            if name:
                tag_names.add(name.lower())
        elif isinstance(t, str):
            tag_names.add(t.lower())

    for h in hashtags:
        if isinstance(h, dict):
            n = h.get("name", "") or h.get("hashtag_name", "")
            if n:
                tag_names.add(n.lower())
        elif isinstance(h, str):
            tag_names.add(h.lower())

    # Layer 1: 标签映射
    for cat, tags in TAG_MAP.items():
        if tag_names & tags:
            return {"category": cat, "confidence": 8.0, "source": "tag_map"}

    # Layer 2: 关键词推断
    best_cat = "其他"
    best_score = 0
    desc_lower = desc.lower()

    for cat, keywords, weight in KEYWORD_RULES:
        score = 0
        for kw in keywords:
            if kw.lower() in desc_lower:
                score += 1
            if kw.lower() in tag_names:
                score += 2
        if score > best_score:
            best_score = score
            best_cat = cat

    if best_score >= 2:
        return {"category": best_cat, "confidence": min(best_score * 2, 8.0), "source": "keyword"}
    elif best_score >= 1:
        return {"category": best_cat, "confidence": 3.0, "source": "keyword_weak"}
    else:
        return {"category": "其他", "confidence": 1.0, "source": "fallback"}


def _load_tags(value: str) -> list:
    """兼容早期版本写入的逗号分隔标签。"""
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return [item.strip() for item in value.split(",") if item.strip()]


def run_classify(limit: int = 0, reclassify: bool = False) -> Dict[str, Any]:
    """对未分类视频分类，也可显式重算全部视频。"""
    conn = get_conn()

    where = "" if reclassify else "WHERE vc.aweme_id IS NULL"
    unclassified = conn.execute(f"""
        SELECT vb.aweme_id, vb.description, vb.tags, vb.hashtags
        FROM videos_base vb
        LEFT JOIN videos_classification vc ON vb.aweme_id = vc.aweme_id
        {where}
        LIMIT ?
    """, (limit or 100000,)).fetchall()

    if not unclassified:
        print("所有视频已分类")
        conn.close()
        return {"classified": 0}

    classified = 0
    cat_stats = {}
    for row in unclassified:
        content = {
            "desc": row["description"],
            "video_tags": _load_tags(row["tags"]),
            "desc_hashtags": _load_tags(row["hashtags"]),
        }
        result = classify_one(content)
        conn.execute("""
            INSERT INTO videos_classification
            (aweme_id, content_category, category_detail, classified_at)
            VALUES (?, ?, ?, ?)
            ON DUPLICATE KEY UPDATE
              content_category=VALUES(content_category),
              category_detail=VALUES(category_detail),
              classified_at=VALUES(classified_at)
        """, (row["aweme_id"], result["category"],
              json.dumps(result, ensure_ascii=False),
              datetime.now().isoformat()))
        classified += 1
        cat_stats[result["category"]] = cat_stats.get(result["category"], 0) + 1

    conn.commit()
    conn.close()

    print(f"\n分类完成: {classified} 条")
    for cat, cnt in sorted(cat_stats.items(), key=lambda x: -x[1]):
        print(f"  {cat}: {cnt}")
    return {"classified": classified, "stats": cat_stats}
