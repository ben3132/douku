"""
content_classifier.py - 视频内容分类器 (v2 优化版)
基于抖音官方标签映射 + 评论/文本关键词推断，为视频生成内容分类标签

v2 优化:
  1. 搞笑分类增强: 评论信号提升权重, 加否定规则防止泛匹配
  2. 颜值关键词加否定上下文: "脸""可爱"等泛词需排除干扰
  3. Layer1/Layer2冲突策略改进: 允许高分Layer2纠正低分Layer1
  4. comment_tags所有source参与分类(不仅限domain/keyword)
  5. 评论原文参与文本推断(不仅依赖comment_tags标签)

分类体系:
  Layer 1: 抖音官方 video_tags (L2/L3) → 目标维度映射
  Layer 2: 文本推断 — 评论关键词 > 作者赛道 > hashtags > 文案

目标分类维度:
  颜值, 舞蹈, 二次元, 游戏, 美食, 知识, 剧情, 搞笑, 音乐,
  萌宠, 时尚, 生活, 情感, 运动, 影视, 明星, 其他

用法:
  python content_classifier.py                    # 对所有视频分类
  python content_classifier.py --aweme_id XXX     # 指定视频
  python content_classifier.py --force            # 重新分类(包括已分类的)
  python content_classifier.py --limit 100        # 最多处理100个
  python content_classifier.py --show             # 展示分类详情
  python content_classifier.py --stats            # 只看统计不分类
"""

import os
import sys
import json
import re
import argparse
import io
from collections import Counter
from datetime import datetime

if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db_utils import init_db, get_conn

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
    # ---------- 颜值 ----------
    "颜值": "颜值",
    "美女": "颜值",
    "帅哥": "颜值",
    "单人随拍": "颜值",
    "多人随拍": "颜值",
    "人物图片轮播": "颜值",
    "身体特写": "颜值",
    "健身房美女": "颜值",
    "街拍": "颜值",
    "女装": "颜值",
    "制服": "颜值",

    # ---------- 舞蹈 ----------
    "舞蹈": "舞蹈",
    "专业舞蹈": "舞蹈",
    "手势舞类": "舞蹈",
    "街舞": "舞蹈",
    "手势舞": "舞蹈",
    "汉舞": "舞蹈",
    "钢管舞": "舞蹈",

    # ---------- 二次元 ----------
    "二次元": "二次元",
    "二次元衍生": "二次元",
    "二次元内容": "二次元",
    "角色扮演": "二次元",
    "cosplay": "二次元",
    "动漫IP": "二次元",
    "汉服": "二次元",
    "Lolita": "二次元",
    "模型手办": "二次元",
    "二次元画作": "二次元",
    "二次元声音创作": "二次元",
    "古风": "二次元",
    "人偶": "二次元",
    "其他宅物": "二次元",

    # ---------- 游戏 ----------
    "游戏": "游戏",
    "竞技游戏": "游戏",
    "射击游戏": "游戏",
    "模拟养成": "游戏",
    "吃鸡类": "游戏",
    "卡牌游戏": "游戏",
    "动作游戏": "游戏",
    "棋牌": "游戏",
    "小游戏": "游戏",
    "棋牌类": "游戏",
    "节奏类": "游戏",
    "手机软件演示录屏": "游戏",
    "文字录屏": "游戏",
    "聊天记录录屏": "游戏",

    # ---------- 美食 ----------
    "美食": "美食",
    "美食展示": "美食",
    "美食探店": "美食",
    "美食教程": "美食",
    "美食知识": "美食",
    "美食测评": "美食",
    "吃播": "美食",
    "普通美食展示": "美食",
    "餐厅探店": "美食",
    "路边小吃": "美食",
    "面食教程": "美食",
    "创意美食展示": "美食",
    "普通吃播": "美食",
    "饮品测评": "美食",
    "减肥餐教程": "美食",
    "烘焙教程": "美食",
    "食材选买": "美食",
    "美食文化": "美食",

    # ---------- 知识 ----------
    "科普": "知识",
    "人文社科": "知识",
    "科技": "知识",
    "人文艺术": "知识",
    "冷知识": "知识",
    "数理科学": "知识",
    "职场技能": "知识",
    "健康": "知识",
    "健身知识": "知识",
    "财经知识": "知识",
    "科技周边": "知识",
    "读书文学": "知识",
    "人文综合": "知识",
    "办公效率": "知识",
    "历史": "知识",
    "心理学": "知识",
    "学科教育": "知识",
    "创业商业知识": "知识",
    "语言学习": "知识",
    "艺术科普": "知识",
    "书法": "知识",
    "非遗": "知识",
    "科学实验": "知识",
    "地理科普": "知识",
    "传统手工艺": "知识",
    "创业指导": "知识",
    "沟通技巧": "知识",
    "金融知识": "知识",
    "宣教科普": "知识",

    # ---------- 剧情 ----------
    "剧情演绎": "剧情",
    "剧情": "剧情",
    "演绎": "剧情",
    "片场演绎": "剧情",
    "其他剧情": "剧情",

    # ---------- 搞笑 ----------
    # v2: 休闲类不再统一归搞笑，改为游戏(更合理)
    # 搞笑主要靠Layer2评论/文本信号推断
    # L2/L3中真正的搞笑相关tag
    "搞笑日常": "搞笑",
    "搞笑段子": "搞笑",
    "恶搞": "搞笑",
    "整蛊": "搞笑",
    "脱口秀": "搞笑",

    # ---------- 音乐 ----------
    "音乐": "音乐",
    "音乐演唱": "音乐",
    "西洋乐器": "音乐",
    "民族乐器": "音乐",
    "音乐知识": "音乐",
    "琵琶": "音乐",
    "唢呐": "音乐",
    "钢琴": "音乐",
    "吉他": "音乐",
    "架子鼓": "音乐",
    "乐理知识": "音乐",

    # ---------- 萌宠 ----------
    "萌宠": "萌宠",
    "宠物猫": "萌宠",
    "宠物狗": "萌宠",
    "其他动物": "萌宠",
    "宠物资讯": "萌宠",
    "猫vlog日常": "萌宠",
    "狗vlog日常": "萌宠",
    "非家养动物": "萌宠",
    "家养宠物": "萌宠",
    "狗剧情": "萌宠",
    "熊猫": "萌宠",

    # ---------- 时尚 ----------
    "时尚": "时尚",
    "穿搭": "时尚",
    "彩妆": "时尚",
    "摄影": "时尚",
    "时尚资讯": "时尚",
    "时尚其他": "时尚",
    "摄影写真": "时尚",
    "全脸/底妆/眼妆": "时尚",
    "美甲": "时尚",
    "配饰": "时尚",
    "发型": "时尚",
    "唇部彩妆": "时尚",
    "男士穿搭": "时尚",
    "女士鞋靴": "时尚",
    "香水": "时尚",

    # ---------- 生活 ----------
    "随拍": "生活",
    "居家": "生活",
    "休闲": "生活",
    "生活记录": "生活",
    "生活健身": "生活",
    "手工DIY": "生活",
    "生活窍门": "生活",
    "日常vlog": "生活",
    "亲子随拍": "生活",

    # ---------- 情感 ----------
    "情感心理": "情感",
    "人际情感": "情感",

    # ---------- 运动 ----------
    "体育": "运动",
    "健身": "运动",
    "专业健身": "运动",
    "极限运动": "运动",
    "球类项目": "运动",
    "竞技体育": "运动",
    "水/雪上运动": "运动",
    "健身肌肉男": "运动",
    "健身随拍": "运动",
    "篮球": "运动",
    "足球": "运动",
    "滑雪": "运动",
    "瑜伽": "运动",
    "女性单人室内健身": "运动",
    "滑板": "运动",
    "游泳": "运动",
    "跑酷": "运动",
    "武术": "运动",

    # ---------- 影视 ----------
    "影视": "影视",
    "影视剪辑": "影视",
    "影视解说": "影视",
    "电视剧剪辑": "影视",
    "电影剪辑": "影视",
    "电影解说": "影视",
    "电视剧解说": "影视",

    # ---------- 明星 ----------
    "明星八卦": "明星",
    "明星随拍资讯": "明星",
    "明星访谈": "明星",

    # ---------- 其他兜底 ----------
    "校园教育": "知识",
    "亲子": "生活",
    "时政社会": "其他",
    "医疗健康": "知识",
    "汽车": "其他",
    "三农": "生活",
    "财经": "知识",
    "旅游": "生活",
    "综艺": "影视",
    "休闲类": "游戏",  # v2: 休闲类是游戏下的子类，归游戏更合理
}

# ============================================================
# Layer 2: 文本推断 — 关键词规则 (v2优化版)
# ============================================================
# v2改动:
#   1. 颜值: 删除泛词"脸""可爱""好看"，加否定上下文规则
#   2. 搞笑: 提升权重1.5→2.0，大幅扩充关键词，加入评论常见信号
#   3. 萌宠: "可爱"从颜值移到萌宠优先
#   4. 各分类关键词去重叠，减少跨类误匹配

# 否定上下文规则: 当关键词前后出现否定词时，不匹配
NEGATION_PREFIXES = [
    "不", "没", "非", "别", "无", "少", "反",
]

# 需要检查否定上下文的关键词
NEGATION_AWARE_KEYWORDS = {
    "好看": ["不好看"],
    "漂亮": ["不漂亮"],
    "心动": ["不心动", "没心动"],
    "爱了": ["不爱了"],
    "搞笑": ["不搞笑", "没搞笑"],
    "可爱": ["不可爱"],
}

KEYWORD_RULES = {
    "颜值": {
        "weight": 2.0,
        "keywords": [
            # 强信号 (高特异性)
            "美女", "帅哥", "颜值", "仙女", "女神", "男神",
            "好美", "太美了", "美哭", "盛世美颜", "绝美", "美爆",
            "太帅了", "颜值天花板", "五官", "氛围感",
            "甜妹", "御姐", "辣妹", "清纯", "性感",
            # 社交信号
            "老公", "老婆", "嫁了", "娶了", "想嫁", "想娶",
            # 中等信号
            "好帅", "漂亮", "身材", "腿", "腰", "眼睛", "笑容", "气质",
            "好白", "皮肤", "大长腿", "A4腰",
        ],
    },
    "舞蹈": {
        "weight": 1.8,
        "keywords": [
            "跳舞", "舞蹈", "编舞", "街舞", "宅舞", "翻跳", "cover",
            "律动", "基本功", "jazz", "kpop", "韩舞", "踩点",
        ],
    },
    "二次元": {
        "weight": 1.8,
        "keywords": [
            "cos", "cosplay", "coser", "还原", "漫展", "二次元", "动漫",
            "原神", "崩坏", "明日方舟", "王者", "LOL", "乙女", "番",
            "手办", "模型", "汉服", "lo", "lolita", "jk", "洛丽塔",
        ],
    },
    "游戏": {
        "weight": 1.6,
        "keywords": [
            "游戏", "打法", "攻略", "上分", "段位", "排位", "fps",
            "MVP", "带飞", "吃鸡", "王者", "原神", "抽卡", "出金",
            "操作", "意识", "走位", "闪现", "大招",
        ],
    },
    "美食": {
        "weight": 1.5,
        "keywords": [
            "好吃", "美食", "做法", "食谱", "烹饪", "食材", "味道",
            "馋", "想吃", "探店", "吃播", "下饭", "美味",
        ],
    },
    "知识": {
        "weight": 1.3,
        "keywords": [
            "学到", "知识", "科普", "原理", "干货", "涨知识", "原来",
            "终于懂", "长见识", "教程", "方法", "技巧", "怎么做到",
        ],
    },
    "剧情": {
        "weight": 1.5,
        "keywords": [
            "剧情", "反转", "结局", "演的", "编剧", "脚本", "故事",
            "下一集", "催更", "续集", "番外",
        ],
    },
    "搞笑": {
        "weight": 2.0,  # v2: 从1.5提升到2.0，评论信号很重要
        "keywords": [
            # 直接信号
            "搞笑", "段子", "整活", "沙雕", "迷惑行为", "名场面",
            "整蛊", "恶搞", "脱口秀",
            # 观众反应（最可靠的评论信号）
            "笑死", "乐死", "笑喷", "笑不活了", "笑尿",
            "哈哈哈", "哈哈哈哈", "笑出猪叫", "笑cry",
            "绷不住了", "笑崩了", "笑拉了", "笑麻了",
            "笑发财了", "笑的肚子疼", "笑出声",
            "好笑", "太逗了", "逗死", "乐死我了",
            # 场景信号
            "离谱", "抽象", "逆天", "绷不住",
            "神操作", "人才", "天才", "离大谱",
            "蚌埠住", "寄了", "麻了", "屑",
        ],
    },
    "音乐": {
        "weight": 1.5,
        "keywords": [
            "唱歌", "嗓音", "翻唱", "吉他", "钢琴", "BGM",
            "节奏", "旋律", "音色", "歌名", "原唱", "cover",
            "开口跪", "天籁", "好听", "单曲循环",
        ],
    },
    "萌宠": {
        "weight": 1.8,  # v2: 从1.5提升，"可爱"优先归萌宠
        "keywords": [
            "猫", "狗", "宠物", "可爱", "萌", "喵", "汪", "主子",
            "铲屎", "撸猫", "吸猫", "修勾", "哈基米",
            "小猫", "小狗", "猫咪", "狗子", "毛孩子",
        ],
    },
    "时尚": {
        "weight": 1.3,
        "keywords": [
            "穿搭", "搭配", "化妆", "美妆", "口红", "眼妆", "发型",
            "护肤", "种草", "同款", "品牌", "潮流", "时尚",
        ],
    },
    "生活": {
        "weight": 0.8,  # v2: 降低权重，生活是兜底分类，不应轻易抢分
        "keywords": [
            "日常", "生活", "记录", "vlog", "打卡", "自律", "减肥",
            "早起", "收纳", "整理",
        ],
    },
    "情感": {
        "weight": 1.3,
        "keywords": [
            "爱情", "恋爱", "分手", "暗恋", "对象", "男朋友", "女朋友",
            "单身", "脱单", "喜欢", "表白", "失恋", "感动", "心疼",
        ],
    },
    "运动": {
        "weight": 1.3,
        "keywords": [
            "运动", "健身", "锻炼", "肌肉", "跑步", "篮球", "足球",
            "滑雪", "游泳", "瑜伽", "体能", "训练", "增肌", "减脂",
        ],
    },
    "影视": {
        "weight": 1.3,
        "keywords": [
            "电影", "电视剧", "影视", "剪辑", "解说", "推荐", "追剧",
            "番剧", "评分", "导演", "演员",
        ],
    },
    "明星": {
        "weight": 1.3,
        "keywords": [
            "明星", "偶像", "爱豆", "粉丝", "应援", "打榜", "出道",
            "演唱会", "综艺", "访谈",
        ],
    },
}

# ============================================================
# 分类引擎
# ============================================================

def _check_negation(text, keyword, pos):
    """检查关键词在文本中是否被否定上下文修饰
    
    pos: 关键词在text中的起始位置
    返回: True=被否定, False=正常匹配
    """
    # 检查预定义的否定模式
    if keyword in NEGATION_AWARE_KEYWORDS:
        for neg_pattern in NEGATION_AWARE_KEYWORDS[keyword]:
            if neg_pattern in text:
                return True
    
    # 检查前一个字是否是否定词
    if pos > 0:
        prev_char = text[pos - 1]
        if prev_char in NEGATION_PREFIXES:
            return True
    
    return False


def classify_by_video_tags(video_tags_json):
    """Layer 1: 基于抖音官方 video_tags 映射分类

    优先使用 L3 → L2 → L1（越细越可靠）
    返回: (category, confidence, source, tag_name)
      confidence: 1.0=L1映射, 1.5=L2映射, 2.0=L3映射
    """
    if not video_tags_json or video_tags_json == "[]":
        return None, 0, "", ""

    tags = json.loads(video_tags_json)
    if not tags:
        return None, 0, "", ""

    # 按 level 排序: L3 > L2 > L1
    level_order = {3: 0, 2: 1, 1: 2}
    tags_sorted = sorted(tags, key=lambda t: level_order.get(t.get("level", 0), 9))

    for tag in tags_sorted:
        name = tag.get("tag_name", "")
        level = tag.get("level", 0)
        if name in TAG_MAP:
            confidence = {3: 2.0, 2: 1.5, 1: 1.0}.get(level, 1.0)
            return TAG_MAP[name], confidence, f"video_tags_L{level}", name

    return None, 0, "", ""


def classify_by_keywords(text_sources: dict):
    """Layer 2: 基于文本关键词推断分类

    text_sources: {
        "comments": str,        # 高赞评论拼接文本 (权重最高)
        "comment_raw": str,     # 评论原文拼接 (v2新增，权重高)
        "author_track": str,    # UP主赛道 (中)
        "hashtags": str,        # hashtags文本 (中低)
        "title_desc": str,      # 标题+描述 (低)
    }

    返回: (category, score, matched_keywords, all_scores)
    """
    # 各信号源的权重 (v2: comment_raw新增)
    source_weights = {
        "comments": 2.5,       # comment_tags标签 (从3.0降到2.5)
        "comment_raw": 3.0,    # v2: 评论原文权重最高
        "author_track": 2.0,
        "hashtags": 1.5,
        "title_desc": 1.0,
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
                # 统计关键词出现次数(上限3次避免刷屏)
                count = 0
                start = 0
                while True:
                    pos = text.find(kw, start)
                    if pos == -1:
                        break
                    # 检查否定上下文
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

    # 取最高分分类
    best_cat = max(scores, key=scores.get)
    return best_cat, scores[best_cat], matched.get(best_cat, []), scores


def classify_video(conn, aweme_id):
    """对单个视频进行完整分类 (v2)

    返回: {
        "category": str,           # 最终分类
        "confidence": float,       # 置信度
        "source": str,             # 分类来源
        "layer1": dict | None,     # Layer1 详情
        "layer2": dict | None,     # Layer2 详情
    }
    """
    # 获取视频数据
    row = conn.execute("""
        SELECT v.video_tags, v.hashtags, v.title, v.desc, v.comment_tags,
               v.author_id,
               a.portrait_track, a.portrait_track_2
        FROM videos v
        LEFT JOIN authors a ON v.author_id = a.id
        WHERE v.aweme_id = ?
    """, (aweme_id,)).fetchone()

    if not row:
        return None

    result = {
        "category": "其他",
        "confidence": 0,
        "source": "default",
        "layer1": None,
        "layer2": None,
    }

    # ========== Layer 1: video_tags 映射 ==========
    l1_cat, l1_conf, l1_src, l1_tag = classify_by_video_tags(row["video_tags"])
    if l1_cat:
        result["layer1"] = {
            "category": l1_cat,
            "confidence": l1_conf,
            "source": l1_src,
            "tag_name": l1_tag,
        }
        result["category"] = l1_cat
        result["confidence"] = l1_conf
        result["source"] = l1_src

    # ========== Layer 2: 文本推断 ==========
    # 1. 评论关键词: 从 comment_tags 中提取 (v2: 所有source都参与)
    comment_text = ""
    comment_tags = json.loads(row["comment_tags"] or "[]")
    comment_keywords = []
    for ct in comment_tags:
        tag = ct.get("tag", "")
        # v2: 所有source都参与，不仅限domain/keyword
        if tag:
            comment_keywords.append(tag)
    comment_text = " ".join(comment_keywords)

    # 2. v2新增: 评论原文
    comment_raw = ""
    try:
        raw_rows = conn.execute("""
            SELECT content FROM comments 
            WHERE aweme_id = ? 
            ORDER BY digg_count DESC LIMIT 20
        """, (aweme_id,)).fetchall()
        comment_raw = " ".join([r["content"] or "" for r in raw_rows])
    except Exception:
        pass  # 无评论表或无数据，忽略

    # 3. 作者赛道
    author_track = row["portrait_track"] or ""
    if row["portrait_track_2"]:
        author_track += " " + row["portrait_track_2"]

    # 4. hashtags
    hashtags_list = json.loads(row["hashtags"] or "[]")
    hashtag_text = " ".join([h.get("tag_name", "") for h in hashtags_list if h.get("tag_name")])

    # 5. 标题+描述
    title_desc = f"{row['title'] or ''} {row['desc'] or ''}"

    text_sources = {
        "comments": comment_text,
        "comment_raw": comment_raw,
        "author_track": author_track,
        "hashtags": hashtag_text,
        "title_desc": title_desc,
    }

    l2_cat, l2_score, l2_matched, l2_all_scores = classify_by_keywords(text_sources)
    if l2_cat:
        result["layer2"] = {
            "category": l2_cat,
            "score": round(l2_score, 2),
            "matched": l2_matched[:5],
            "all_scores": {k: round(v, 2) for k, v in sorted(l2_all_scores.items(), key=lambda x: -x[1])[:3]},
        }

        # ========== v2: Layer1 vs Layer2 冲突决策 ==========
        if l1_cat and l1_cat == l2_cat:
            # 两者一致 → 增强置信度
            result["confidence"] = l1_conf + min(l2_score, 3.0)
            result["source"] = f"{l1_src}+keyword_confirm"
        elif l1_cat and l1_cat != l2_cat:
            # 两者冲突 → v2: 允许高分Layer2纠正低分Layer1
            l1_tag_name = l1_tag
            
            # 特殊规则: "休闲类"tag映射为游戏，但评论强烈说是搞笑
            if l1_tag_name == "休闲类" and l2_cat == "搞笑" and l2_score >= 6.0:
                result["category"] = "搞笑"
                result["confidence"] = min(l2_score, 5.0)
                result["source"] = "keyword_override(休闲类→搞笑)"
            # 一般规则: L1 confidence < 2.0 且 L2 score > L1 conf * 2
            elif l1_conf < 2.0 and l2_score > l1_conf * 3.0:
                result["category"] = l2_cat
                result["confidence"] = min(l2_score, 5.0)
                result["source"] = f"keyword_override({l1_src})"
            else:
                # Layer1 优先
                pass
        else:
            # 无Layer1 → 用Layer2
            result["category"] = l2_cat
            result["confidence"] = min(l2_score, 5.0)
            result["source"] = "keyword_inference"

    # ========== 无任何信号 → 其他 ==========
    if result["category"] == "其他" and not result["layer1"] and not result["layer2"]:
        result["source"] = "no_signal"

    return result


def update_content_category(conn, aweme_id, result: dict):
    """更新视频的 content_category 字段"""
    if result is None:
        return

    conn.execute("""
        UPDATE videos SET
            content_category = ?,
            content_category_detail = ?
        WHERE aweme_id = ?
    """, (
        result["category"],
        json.dumps({
            "confidence": round(result["confidence"], 2),
            "source": result["source"],
            "layer1": result.get("layer1"),
            "layer2": result.get("layer2"),
        }, ensure_ascii=False),
        aweme_id,
    ))


# ============================================================
# 主流程
# ============================================================

def run_classify(conn, args):
    """执行分类"""
    # 查询待分类视频
    if args.aweme_id:
        videos = [{"aweme_id": args.aweme_id}]
    else:
        where = "1=1" if args.force else "(content_category = '' OR content_category IS NULL)"
        sql = f"""
            SELECT aweme_id FROM videos
            WHERE {where}
            ORDER BY json_extract(stats, '$.digg') DESC
        """
        if args.limit > 0:
            sql += f" LIMIT {args.limit}"
        videos = [dict(r) for r in conn.execute(sql).fetchall()]

    if not videos:
        print("没有需要分类的视频")
        return

    print("=" * 60)
    print("🏷️ 视频内容分类器 v2")
    print("=" * 60)
    print(f"待分类: {len(videos)} 个视频\n")

    classified = 0
    category_counter = Counter()
    source_counter = Counter()

    for i, v in enumerate(videos, 1):
        aweme_id = v["aweme_id"]
        result = classify_video(conn, aweme_id)

        if result:
            update_content_category(conn, aweme_id, result)
            classified += 1
            category_counter[result["category"]] += 1
            source_counter[result["source"]] += 1

            if args.show:
                row = conn.execute(
                    "SELECT title, desc FROM videos WHERE aweme_id=?", (aweme_id,)
                ).fetchone()
                title = (row["title"] or row["desc"] or "")[:30]
                src = result["source"]
                conf = result["confidence"]
                cat = result["category"]
                print(f"  [{i}] {aweme_id} → {cat} (conf={conf:.1f}, src={src}) {title}")

                if result["layer2"] and result["layer2"]["matched"]:
                    kw = ", ".join(result["layer2"]["matched"][:3])
                    print(f"      ↳ 关键词: {kw}")

        if i % 100 == 0:
            conn.commit()
            print(f"  ... 已处理 {i}/{len(videos)}")

    conn.commit()

    print(f"\n✅ 完成! 已分类 {classified}/{len(videos)} 个视频")
    print(f"\n📊 分类分布:")
    for cat, cnt in category_counter.most_common():
        pct = cnt / max(classified, 1) * 100
        bar = "█" * int(pct / 2)
        print(f"  {cat:6s} {cnt:5d} ({pct:5.1f}%) {bar}")
    
    print(f"\n📋 分类来源:")
    for src, cnt in source_counter.most_common():
        print(f"  {src:35s} {cnt:5d}")


def show_stats(conn):
    """只看统计不分类"""
    rows = conn.execute("""
        SELECT content_category, COUNT(*) as cnt
        FROM videos
        WHERE content_category != '' AND content_category IS NOT NULL
        GROUP BY content_category
        ORDER BY cnt DESC
    """).fetchall()

    if not rows:
        print("尚未进行分类")
        return

    total = sum(r["cnt"] for r in rows)
    print("=" * 60)
    print(f"📊 内容分类统计 (已分类 {total} 条)")
    print("=" * 60)

    for r in rows:
        pct = r["cnt"] / total * 100
        bar = "█" * int(pct / 2)
        print(f"  {r['content_category']:6s} {r['cnt']:5d} ({pct:5.1f}%) {bar}")

    uncategorized = conn.execute("""
        SELECT COUNT(*) FROM videos
        WHERE content_category = '' OR content_category IS NULL
    """).fetchone()[0]
    if uncategorized:
        print(f"\n  未分类: {uncategorized} 条")

    # v2: 显示来源分布
    print(f"\n📋 分类来源分布:")
    src_rows = conn.execute("""
        SELECT 
            CASE 
                WHEN content_category_detail LIKE '%keyword_override%' THEN 'keyword_override'
                WHEN content_category_detail LIKE '%keyword_inference%' THEN 'keyword_inference'
                WHEN content_category_detail LIKE '%keyword_confirm%' THEN 'keyword_confirm'
                WHEN content_category_detail LIKE '%video_tags_L3%' THEN 'video_tags_L3'
                WHEN content_category_detail LIKE '%video_tags_L2%' THEN 'video_tags_L2'
                WHEN content_category_detail LIKE '%video_tags_L1%' THEN 'video_tags_L1'
                WHEN content_category_detail LIKE '%no_signal%' THEN 'no_signal'
                ELSE 'other'
            END as src_type,
            COUNT(*) as cnt
        FROM videos
        WHERE content_category != '' AND content_category IS NOT NULL
        GROUP BY src_type ORDER BY cnt DESC
    """).fetchall()
    for r in src_rows:
        print(f"  {r[0]:25s} {r[1]:5d}")


def main():
    parser = argparse.ArgumentParser(description="视频内容分类器 v2")
    parser.add_argument("--aweme_id", "-a", help="只分类指定视频")
    parser.add_argument("--force", "-f", action="store_true",
                        help="重新分类(包括已分类的)")
    parser.add_argument("--limit", "-n", type=int, default=0,
                        help="最多分类N个视频")
    parser.add_argument("--show", "-s", action="store_true",
                        help="展示分类详情")
    parser.add_argument("--stats", action="store_true",
                        help="只看统计不分类")
    args = parser.parse_args()

    init_db()
    conn = get_conn()

    if args.stats:
        show_stats(conn)
    else:
        run_classify(conn, args)

    conn.close()


if __name__ == "__main__":
    main()
