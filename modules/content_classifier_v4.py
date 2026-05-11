"""
content_classifier_v4.py - 视频内容分类器 (v4)
与 v3 的区别：
  - 使用 v4 多表 (videos_base + videos_stats + comments 等) 替代单表
  - 分类结果写入 videos_classification 表
  - init_db/get_conn 替换为 v4 版本
  - 分类规则引擎完全不变（TAG_MAP + KEYWORD_RULES）
"""

import json
import os
from collections import Counter

from .db_v4 import get_conn_v4, init_db_v4, upsert_classification, get_category_distribution


def _get_v4_db_path() -> str:
    from .meta import get_data_root
    return os.path.join(get_data_root(), "douku_v4.db")


# ============================================================
# 目标分类维度
# ============================================================

CATEGORIES = [
    "颜值", "舞蹈", "二次元", "游戏", "美食", "知识", "剧情", "搞笑",
    "音乐", "萌宠", "时尚", "生活", "情感", "运动", "影视", "明星", "其他",
]

# ============================================================
# Layer 1: 抖音官方 video_tags → 目标分类映射
# ============================================================

TAG_MAP = {
    "颜值": "颜值", "美女": "颜值", "帅哥": "颜值", "单人随拍": "颜值",
    "多人随拍": "颜值", "人物图片轮播": "颜值", "身体特写": "颜值",
    "健身房美女": "颜值", "街拍": "颜值", "女装": "颜值", "制服": "颜值",
    "舞蹈": "舞蹈", "专业舞蹈": "舞蹈", "手势舞类": "舞蹈", "街舞": "舞蹈",
    "手势舞": "舞蹈", "汉舞": "舞蹈", "钢管舞": "舞蹈",
    "二次元": "二次元", "二次元衍生": "二次元", "二次元内容": "二次元",
    "角色扮演": "二次元", "cosplay": "二次元", "动漫IP": "二次元",
    "汉服": "二次元", "Lolita": "二次元", "模型手办": "二次元",
    "二次元画作": "二次元", "二次元声音创作": "二次元", "古风": "二次元",
    "人偶": "二次元", "其他宅物": "二次元",
    "游戏": "游戏", "竞技游戏": "游戏", "射击游戏": "游戏",
    "模拟养成": "游戏", "吃鸡类": "游戏", "卡牌游戏": "游戏",
    "动作游戏": "游戏", "棋牌": "游戏", "小游戏": "游戏", "棋牌类": "游戏",
    "节奏类": "游戏", "手机软件演示录屏": "游戏", "文字录屏": "游戏",
    "聊天记录录屏": "游戏", "休闲类": "游戏",
    "美食": "美食", "美食展示": "美食", "美食探店": "美食",
    "美食教程": "美食", "美食知识": "美食", "美食测评": "美食",
    "吃播": "美食", "普通美食展示": "美食", "餐厅探店": "美食",
    "路边小吃": "美食", "面食教程": "美食", "创意美食展示": "美食",
    "普通吃播": "美食", "饮品测评": "美食", "减肥餐教程": "美食",
    "烘焙教程": "美食", "食材选买": "美食", "美食文化": "美食",
    "科普": "知识", "人文社科": "知识", "科技": "知识", "人文艺术": "知识",
    "冷知识": "知识", "数理科学": "知识", "职场技能": "知识",
    "健康": "知识", "健身知识": "知识", "财经知识": "知识",
    "科技周边": "知识", "读书文学": "知识", "人文综合": "知识",
    "办公效率": "知识", "历史": "知识", "心理学": "知识",
    "学科教育": "知识", "创业商业知识": "知识", "语言学习": "知识",
    "艺术科普": "知识", "书法": "知识", "非遗": "知识",
    "科学实验": "知识", "地理科普": "知识", "传统手工艺": "知识",
    "创业指导": "知识", "沟通技巧": "知识", "金融知识": "知识",
    "宣教科普": "知识", "校园教育": "知识", "医疗健康": "知识",
    "财经": "知识",
    "剧情演绎": "剧情", "剧情": "剧情", "演绎": "剧情",
    "片场演绎": "剧情", "其他剧情": "剧情",
    "搞笑日常": "搞笑", "搞笑段子": "搞笑", "恶搞": "搞笑",
    "整蛊": "搞笑", "脱口秀": "搞笑",
    "音乐": "音乐", "音乐演唱": "音乐", "西洋乐器": "音乐",
    "民族乐器": "音乐", "音乐知识": "音乐", "琵琶": "音乐",
    "唢呐": "音乐", "钢琴": "音乐", "吉他": "音乐",
    "架子鼓": "音乐", "乐理知识": "音乐",
    "萌宠": "萌宠", "宠物猫": "萌宠", "宠物狗": "萌宠",
    "其他动物": "萌宠", "宠物资讯": "萌宠", "猫vlog日常": "萌宠",
    "狗vlog日常": "萌宠", "非家养动物": "萌宠", "家养宠物": "萌宠",
    "狗剧情": "萌宠", "熊猫": "萌宠",
    "时尚": "时尚", "穿搭": "时尚", "彩妆": "时尚", "摄影": "时尚",
    "时尚资讯": "时尚", "时尚其他": "时尚", "摄影写真": "时尚",
    "全脸/底妆/眼妆": "时尚", "美甲": "时尚", "配饰": "时尚",
    "发型": "时尚", "唇部彩妆": "时尚", "男士穿搭": "时尚",
    "女士鞋靴": "时尚", "香水": "时尚",
    "随拍": "生活", "居家": "生活", "休闲": "生活",
    "生活记录": "生活", "生活健身": "生活", "手工DIY": "生活",
    "生活窍门": "生活", "日常vlog": "生活", "亲子随拍": "生活",
    "亲子": "生活", "三农": "生活", "旅游": "生活",
    "情感心理": "情感", "人际情感": "情感",
    "体育": "运动", "健身": "运动", "专业健身": "运动",
    "极限运动": "运动", "球类项目": "运动", "竞技体育": "运动",
    "水/雪上运动": "运动", "健身肌肉男": "运动", "健身随拍": "运动",
    "篮球": "运动", "足球": "运动", "滑雪": "运动", "瑜伽": "运动",
    "女性单人室内健身": "运动", "滑板": "运动", "游泳": "运动",
    "跑酷": "运动", "武术": "运动",
    "影视": "影视", "影视剪辑": "影视", "影视解说": "影视",
    "电视剧剪辑": "影视", "电影剪辑": "影视", "电影解说": "影视",
    "电视剧解说": "影视", "综艺": "影视",
    "明星八卦": "明星", "明星随拍资讯": "明星", "明星访谈": "明星",
    "时政社会": "其他", "汽车": "其他",
}

LIFESTYLE_L3_TAGS = {
    "随拍", "日常vlog", "生活记录", "生活", "记录",
    "日常", "vlog", "打卡", "自律", "收纳", "整理",
    "居家", "休闲", "生活健身", "手工DIY", "生活窍门",
    "亲子随拍", "亲子", "三农", "旅游",
}

# ============================================================
# Layer 2: 文本推断 — 关键词规则
# ============================================================

NEGATION_PREFIXES = ["不", "没", "非", "别", "无", "少", "反"]

NEGATION_AWARE_KEYWORDS = {
    "好看": ["不好看"], "漂亮": ["不漂亮"], "心动": ["不心动", "没心动"],
    "爱了": ["不爱了"], "搞笑": ["不搞笑", "没搞笑"], "可爱": ["不可爱"],
}

KEYWORD_RULES = {
    "颜值": {"weight": 2.0, "keywords": [
        "美女", "帅哥", "颜值", "仙女", "女神", "男神",
        "好美", "太美了", "美哭", "盛世美颜", "绝美", "美爆",
        "太帅了", "颜值天花板", "五官", "氛围感",
        "甜妹", "御姐", "辣妹", "清纯", "性感",
        "老公", "老婆", "嫁了", "娶了", "想嫁", "想娶",
        "好帅", "漂亮", "身材", "腿", "腰", "眼睛", "笑容", "气质",
        "好白", "皮肤", "大长腿", "A4腰",
    ]},
    "舞蹈": {"weight": 1.8, "keywords": [
        "跳舞", "舞蹈", "编舞", "街舞", "宅舞", "翻跳", "cover",
        "律动", "基本功", "jazz", "kpop", "韩舞", "踩点",
    ]},
    "二次元": {"weight": 1.8, "keywords": [
        "cos", "cosplay", "coser", "还原", "漫展", "二次元", "动漫",
        "原神", "崩坏", "明日方舟", "王者", "LOL", "乙女", "番",
        "手办", "模型", "汉服", "lo", "lolita", "jk", "洛丽塔",
        "鸣潮", "碧蓝航线", "赛马娘", "洛克王国", "终末地",
        "崩坏星穹铁道", "星穹铁道", "绝区零", "塞尔达",
        "明日方舟", "方舟", "泰拉", "罗德岛",
        "碧蓝", "大凤", "企业", "赤城", "约克城",
        "ウマ娘", "马娘", "菲比", "长离", "今汐", "红孩儿", "哪吒",
        "魔兽", "wow", "WorldOfWarcraft",
    ]},
    "游戏": {"weight": 1.6, "keywords": [
        "游戏", "打法", "攻略", "上分", "段位", "排位", "fps",
        "MVP", "带飞", "吃鸡", "王者", "抽卡", "出金",
        "操作", "意识", "走位", "闪现", "大招",
        "鸣潮", "崩坏星穹铁道", "星穹铁道", "绝区零",
        "洛克王国", "洛克王国世界", "原神", "崩坏3", "明日方舟",
        "英雄联盟", "LOL", "dota", "csgo", "valorant",
        "魔兽世界", "wow", "炉石", "守望先锋", "ow",
        "赛马娘", "人狼", "AmongUs", "开放世界", "RPG", "MMORPG",
    ]},
    "美食": {"weight": 1.5, "keywords": [
        "好吃", "美食", "做法", "食谱", "烹饪", "食材", "味道",
        "馋", "想吃", "探店", "吃播", "下饭", "美味",
    ]},
    "知识": {"weight": 1.3, "keywords": [
        "学到", "知识", "科普", "原理", "干货", "涨知识", "原来",
        "终于懂", "长见识", "教程", "方法", "技巧", "怎么做到",
        "ai", "openai", "agent", "智能助手", "元宝", "即梦", "剪映",
        "大模型", "LLM", "GPT", "Claude", "Gemini", "教程", "学习", "教学",
    ]},
    "剧情": {"weight": 1.5, "keywords": [
        "剧情", "反转", "结局", "演的", "编剧", "脚本", "故事",
        "下一集", "催更", "续集", "番外",
    ]},
    "搞笑": {"weight": 2.0, "keywords": [
        "搞笑", "段子", "整活", "沙雕", "迷惑行为", "名场面",
        "整蛊", "恶搞", "脱口秀", "吐槽",
        "笑死", "乐死", "笑喷", "笑不活了", "笑尿",
        "哈哈哈", "哈哈哈哈", "笑出猪叫", "笑cry",
        "绷不住了", "笑崩了", "笑拉了", "笑麻了",
        "笑发财了", "笑的肚子疼", "笑出声",
        "好笑", "太逗了", "逗死", "乐死我了",
        "离谱", "抽象", "逆天", "绷不住",
        "神操作", "人才", "天才", "离大谱",
        "蚌埠住", "寄了", "麻了", "屑",
    ]},
    "音乐": {"weight": 1.5, "keywords": [
        "唱歌", "嗓音", "翻唱", "吉他", "钢琴", "BGM",
        "节奏", "旋律", "音色", "歌名", "原唱", "cover",
        "开口跪", "天籁", "好听", "单曲循环",
    ]},
    "萌宠": {"weight": 1.8, "keywords": [
        "猫", "狗", "宠物", "可爱", "萌", "喵", "汪", "主子",
        "铲屎", "撸猫", "吸猫", "修勾", "哈基米",
        "小猫", "小狗", "猫咪", "狗子", "毛孩子", "猫猫", "云吸猫",
    ]},
    "时尚": {"weight": 1.3, "keywords": [
        "穿搭", "搭配", "化妆", "美妆", "口红", "眼妆", "发型",
        "护肤", "种草", "同款", "品牌", "潮流", "时尚",
    ]},
    "生活": {"weight": 0.8, "keywords": [
        "日常", "生活", "记录", "vlog", "打卡", "自律", "减肥",
        "早起", "收纳", "整理", "旅行vlog", "旅行", "摄影", "情侣",
    ]},
    "情感": {"weight": 1.3, "keywords": [
        "爱情", "恋爱", "分手", "暗恋", "对象", "男朋友", "女朋友",
        "单身", "脱单", "喜欢", "表白", "失恋", "感动", "心疼",
    ]},
    "运动": {"weight": 1.3, "keywords": [
        "运动", "健身", "锻炼", "肌肉", "跑步", "篮球", "足球",
        "滑雪", "游泳", "瑜伽", "体能", "训练", "增肌", "减脂",
    ]},
    "影视": {"weight": 1.3, "keywords": [
        "电影", "电视剧", "影视", "剪辑", "解说", "推荐", "追剧",
        "番剧", "评分", "导演", "演员",
    ]},
    "明星": {"weight": 1.3, "keywords": [
        "明星", "偶像", "爱豆", "粉丝", "应援", "打榜", "出道",
        "演唱会", "综艺", "访谈",
    ]},
}


# ============================================================
# 分类引擎
# ============================================================

def _check_negation(text, keyword, pos):
    if keyword in NEGATION_AWARE_KEYWORDS:
        for neg_pattern in NEGATION_AWARE_KEYWORDS[keyword]:
            if neg_pattern in text:
                return True
    if pos > 0:
        prev_char = text[pos - 1]
        if prev_char in NEGATION_PREFIXES:
            return True
    return False


def classify_by_video_tags(video_tags_json):
    if not video_tags_json or video_tags_json == "[]":
        return None, 0, "", ""
    tags = json.loads(video_tags_json)
    if not tags:
        return None, 0, "", ""
    level_order = {3: 0, 2: 1, 1: 2}
    tags_sorted = sorted(tags, key=lambda t: level_order.get(t.get("level", 0), 9))
    for tag in tags_sorted:
        name = tag.get("tag_name", "")
        level = tag.get("level", 0)
        if name in TAG_MAP:
            confidence = {3: 2.0, 2: 1.5, 1: 1.0}.get(level, 1.0)
            if TAG_MAP[name] == "生活" and level == 3 and name in LIFESTYLE_L3_TAGS:
                confidence = 1.2
            return TAG_MAP[name], confidence, f"video_tags_L{level}", name
    return None, 0, "", ""


def classify_by_keywords(text_sources: dict):
    source_weights = {
        "comments": 2.5, "comment_raw": 3.0, "author_track": 2.0,
        "hashtags": 1.5, "title": 2.5, "desc": 1.5,
    }
    scores = {}
    matched = {}
    for cat, rule in KEYWORD_RULES.items():
        cat_score = 0
        cat_matched = []
        for src_name, weight in source_weights.items():
            text = text_sources.get(src_name, "")
            if not text:
                continue
            kw_weight = rule["weight"]
            for kw in rule["keywords"]:
                count = 0
                start = 0
                while True:
                    pos = text.find(kw, start)
                    if pos == -1:
                        break
                    if not _check_negation(text, kw, pos):
                        count += 1
                    start = pos + len(kw)
                    if count >= 3:
                        break
                if count > 0:
                    cat_score += count * kw_weight * weight
                    cat_matched.append(kw)
        if cat_score > 0:
            scores[cat] = cat_score
            matched[cat] = cat_matched
    if not scores:
        return None, 0, [], scores
    best_cat = max(scores, key=scores.get)
    return best_cat, scores[best_cat], matched.get(best_cat, []), scores


def classify_video(conn, aweme_id):
    """v4 版：使用多表 JOIN 获取分类所需数据"""
    row = conn.execute("""
        SELECT vb.aweme_id, vb.title, vb.desc,
               vb.video_tags,
               vb.hashtags,
               ab.sec_uid, ap.portrait_track, ap.portrait_track_2
        FROM videos_base vb
        LEFT JOIN authors_base ab ON vb.author_sec_uid = ab.sec_uid
        LEFT JOIN authors_portrait ap ON ab.sec_uid = ap.sec_uid
        WHERE vb.aweme_id = ?
    """, (aweme_id,)).fetchone()

    if not row:
        return None

    result = {
        "category": "其他", "confidence": 0, "source": "default",
        "layer1": None, "layer2": None,
    }

    # Layer 1: video_tags 映射
    video_tags_raw = row["video_tags"]
    if video_tags_raw:
        l1_cat, l1_conf, l1_src, l1_tag = classify_by_video_tags(video_tags_raw)
    else:
        l1_cat, l1_conf, l1_src, l1_tag = None, 0, "", ""

    if l1_cat:
        result["layer1"] = {
            "category": l1_cat, "confidence": l1_conf,
            "source": l1_src, "tag_name": l1_tag,
        }
        result["category"] = l1_cat
        result["confidence"] = l1_conf
        result["source"] = l1_src

    # Layer 2: 文本推断
    comment_text = ""
    ct_rows = conn.execute("""
        SELECT comment_tags FROM videos_comment_tags WHERE aweme_id = ?
    """, (aweme_id,)).fetchone()
    if ct_rows and ct_rows["comment_tags"]:
        try:
            ct_data = json.loads(ct_rows["comment_tags"]) if isinstance(ct_rows["comment_tags"], str) else ct_rows["comment_tags"]
            comment_keywords = [ct.get("tag", "") for ct in ct_data if ct.get("tag")]
            comment_text = " ".join(comment_keywords)
        except (json.JSONDecodeError, TypeError):
            pass

    # 评论原文
    comment_raw = ""
    try:
        raw_rows = conn.execute("""
            SELECT content FROM comments
            WHERE aweme_id = ? ORDER BY digg_count DESC LIMIT 20
        """, (aweme_id,)).fetchall()
        comment_raw = " ".join([r["content"] or "" for r in raw_rows])
    except Exception:
        pass

    # 作者赛道
    author_track = row["portrait_track"] or ""
    if row["portrait_track_2"]:
        author_track += " " + row["portrait_track_2"]

    # hashtags
    hashtag_text = ""
    try:
        hl = json.loads(row["hashtags"]) if row["hashtags"] else []
        hashtag_text = " ".join([h.get("tag_name", h.get("name", "")) if isinstance(h, dict) else str(h) for h in hl])
    except Exception:
        pass

    text_sources = {
        "comments": comment_text,
        "comment_raw": comment_raw,
        "author_track": author_track,
        "hashtags": hashtag_text,
        "title": row["title"] or "",
        "desc": row["desc"] or "",
    }

    l2_cat, l2_score, l2_matched, l2_all_scores = classify_by_keywords(text_sources)
    if l2_cat:
        result["layer2"] = {
            "category": l2_cat, "score": round(l2_score, 2),
            "matched": l2_matched[:5],
            "all_scores": {k: round(v, 2) for k, v in sorted(l2_all_scores.items(), key=lambda x: -x[1])[:3]},
        }
        if l1_cat and l1_cat == l2_cat:
            result["confidence"] = l1_conf + min(l2_score, 3.0)
            result["source"] = f"{l1_src}+keyword_confirm"
        elif l1_cat and l1_cat != l2_cat:
            if l1_tag == "休闲类" and l2_cat == "搞笑" and l2_score >= 6.0:
                result["category"] = "搞笑"
                result["confidence"] = min(l2_score, 5.0)
                result["source"] = "keyword_override(休闲类->搞笑)"
            elif l2_score > l1_conf * 2.0:
                result["category"] = l2_cat
                result["confidence"] = min(l2_score, 5.0)
                result["source"] = f"keyword_override({l1_src})"
        else:
            result["category"] = l2_cat
            result["confidence"] = min(l2_score, 5.0)
            result["source"] = "keyword_inference"

    if result["category"] == "其他" and not result["layer1"] and not result["layer2"]:
        result["source"] = "no_signal"

    return result


def save_classification(conn, aweme_id, result: dict):
    """写入 v4 videos_classification 表"""
    if result is None:
        return
    upsert_classification(conn, aweme_id, result["category"])


def show_stats(conn):
    """显示 v4 分类统计"""
    cat = get_category_distribution(conn)
    total = sum(cat.values())

    if not cat:
        print("尚未进行分类，请先运行 classify")
        return

    print("=" * 60)
    print(f"v4 内容分类统计 (已分类 {total} 条)")
    print("=" * 60)

    for name, cnt in sorted(cat.items(), key=lambda x: -x[1]):
        pct = cnt / total * 100
        bar = "█" * int(pct / 2)
        print(f"  {name:6s} {cnt:5d} ({pct:5.1f}%) {bar}")

    uncategorized = conn.execute(
        "SELECT COUNT(*) FROM videos_base WHERE aweme_id NOT IN (SELECT aweme_id FROM videos_classification)"
    ).fetchone()[0]
    if uncategorized:
        print(f"\n  未分类: {uncategorized} 条")


def run_classify(conn, args):
    """执行批量分类 (v4)"""
    aweme_id = getattr(args, 'aweme_id', None)
    force = getattr(args, 'force', False)
    limit = getattr(args, 'limit', 0)
    show = getattr(args, 'show', False)

    if aweme_id:
        videos = [{"aweme_id": aweme_id}]
    else:
        if force:
            sql = "SELECT vb.aweme_id FROM videos_base vb ORDER BY vb.create_time DESC"
        else:
            sql = """
                SELECT vb.aweme_id FROM videos_base vb
                WHERE vb.aweme_id NOT IN (SELECT aweme_id FROM videos_classification)
                ORDER BY vb.create_time DESC
            """
        if limit > 0:
            sql += f" LIMIT {limit}"
        try:
            videos = [dict(r) for r in conn.execute(sql).fetchall()]
        except Exception:
            # videos_classification 表可能不存在
            sql = "SELECT vb.aweme_id FROM videos_base vb ORDER BY vb.create_time DESC"
            if limit > 0:
                sql += f" LIMIT {limit}"
            videos = [dict(r) for r in conn.execute(sql).fetchall()]

    if not videos:
        print("没有需要分类的视频")
        return

    print("=" * 60)
    print("v4 内容分类器")
    print("=" * 60)
    print(f"待分类: {len(videos)} 个视频\n")

    classified = 0
    category_counter = Counter()
    source_counter = Counter()

    for i, v in enumerate(videos, 1):
        aweme_id = v["aweme_id"]
        result = classify_video(conn, aweme_id)

        if result:
            save_classification(conn, aweme_id, result)
            conn.commit()
            classified += 1
            category_counter[result["category"]] += 1
            source_counter[result["source"]] += 1

            if show:
                title = conn.execute(
                    "SELECT title FROM videos_base WHERE aweme_id=?", (aweme_id,)
                ).fetchone()
                title_str = (title["title"] if title else "")[:30]
                print(f"  [{i}] {aweme_id} -> {result['category']} "
                      f"(conf={result['confidence']:.1f}, src={result['source']}) {title_str}")

        if i % 100 == 0:
            print(f"  ... 已处理 {i}/{len(videos)}")

    conn.commit()

    print(f"\nOK 已分类 {classified}/{len(videos)} 个视频\n")
    print("分类分布:")
    for cat, cnt in category_counter.most_common():
        pct = cnt / max(classified, 1) * 100
        bar = "█" * int(pct / 2)
        print(f"  {cat:6s} {cnt:5d} ({pct:5.1f}%) {bar}")

    print("\n分类来源:")
    for src, cnt in source_counter.most_common():
        print(f"  {src:35s} {cnt:5d}")


def run_from_cli(args):
    """供 dytool_v4.py CLI 调用"""
    db_path = _get_v4_db_path()
    init_db_v4(db_path)
    conn = get_conn_v4(db_path)

    if getattr(args, 'stats', False):
        show_stats(conn)
        conn.close()
        return

    run_classify(conn, args)
    conn.close()