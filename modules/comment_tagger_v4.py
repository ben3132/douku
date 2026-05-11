"""
comment_tagger_v4.py - 评论标签提取规则引擎 (v4)
与 v3 的区别：
  - 使用 v4 多表 (videos_base + comments) 替代单表
  - 导入 db_v4 函数替代 db_utils
  - 标签写入 videos_comment_tags 表
  - 规则引擎（情感/领域/模式/关键词）完全不变
"""

import os
import sys
import json
import re
from collections import Counter
from datetime import datetime

from .db_v4 import get_conn_v4, init_db_v4, upsert_comment, get_summary


def _get_v4_db_path() -> str:
    from .meta import get_data_root
    return os.path.join(get_data_root(), "douku_v4.db")


# ============================================================
# 规则引擎 - 情感词典（完全不变）
# ============================================================

POSITIVE_WORDS = {
    "好看": 2, "喜欢": 2, "爱": 2, "牛": 2, "厉害": 2, "绝了": 3, "yyds": 3,
    "神仙": 3, "太棒了": 2, "优秀": 2, "赞": 1, "棒": 1, "强": 1, "美": 2,
    "帅": 2, "酷": 2, "超": 1, "惊艳": 3, "完美": 3, "绝美": 3, "绝杀": 2,
    "封神": 3, "天花板": 3, "天花板级别": 3, "顶级": 3, "绝绝子": 3,
    "太绝了": 3, "爱了": 2, "心动": 2, "上头": 2, "真香": 2,
    "永远可以相信": 3, "救命": 2, "啊啊啊": 1, "太会了": 2, "拿捏": 2,
    "氛围感": 2, "沉浸": 2, "治愈": 2, "感动": 2, "泪目": 2, "破防": 2,
    "笑死": 1, "哈哈哈": 1, "乐死": 1, "有趣": 1, "好玩": 1, "搞笑": 1,
    "宝藏": 2, "宝藏up": 2, "宝藏博主": 2, "宝藏女孩": 2,
    "好美": 2, "太美了": 3, "美哭": 3, "好看死了": 3, "盛世美颜": 3,
    "仙女": 2, "女神": 2, "漂亮": 2, "可爱": 2, "甜": 2, "甜妹": 2,
    "有才": 2, "才华": 2, "专业": 2, "技术": 2, "功底": 2, "实力": 2,
}

NEGATIVE_WORDS = {
    "难看": -2, "讨厌": -2, "恶心": -3, "丑": -2, "差": -1, "烂": -2,
    "失望": -2, "无聊": -1, "假": -2, "作": -2, "作秀": -2,
    "尴尬": -1, "离谱": -1, "迷惑": -1, "不理解": -1,
    "浪费": -2, "低俗": -3, "过度": -1, "过头": -1,
    "不好看": -2, "不想看": -2, "划走": -1,
}

# ============================================================
# 规则引擎 - 内容领域词典（完全不变）
# ============================================================

DOMAIN_PATTERNS = {
    "cosplay": {"keywords": ["cos", "cosplay", "coser", "还原", "漫展", "角色扮演",
        "原神", "崩坏", "明日方舟", "王者", "LOL", "乙女"], "weight": 2},
    "舞蹈": {"keywords": ["舞", "跳舞", "编舞", "街舞", "jazz", "kpop", "韩舞",
        "宅舞", "翻跳", "cover", "基本功", "律动"], "weight": 2},
    "音乐": {"keywords": ["歌", "唱", "声音", "嗓音", "翻唱", "弹", "吉他",
        "钢琴", "BGM", "节奏", "旋律", "音色"], "weight": 2},
    "颜值": {"keywords": ["颜值", "脸", "五官", "妆", "化妆", "美妆", "口红",
        "眼妆", "穿搭", "身材", "皮肤", "发型"], "weight": 2},
    "二次元": {"keywords": ["二次元", "动漫", "番", "漫画", "手办", "模型",
        "lo", "lolita", "洛丽塔", "jk", "汉服"], "weight": 2},
    "游戏": {"keywords": ["游戏", "打法", "攻略", "上分", "段位", "排位",
        "帧", "fps", "MVP", "带飞"], "weight": 2},
    "美食": {"keywords": ["好吃", "美食", "做法", "食谱", "烹饪", "食材",
        "锅", "味道", "馋", "想吃"], "weight": 2},
    "知识": {"keywords": ["学到", "知识", "科普", "原理", "干货", "涨知识",
        "原来", "终于懂", "长见识"], "weight": 2},
    "情感": {"keywords": ["爱情", "恋爱", "分手", "暗恋", "对象", "老公",
        "老婆", "男朋友", "女朋友", "单身", "脱单"], "weight": 2},
    "搞笑": {"keywords": ["搞笑", "段子", "整活", "沙雕", "迷惑行为",
        "名场面", "笑", "乐", "整蛊"], "weight": 2},
    "生活": {"keywords": ["生活", "日常", "记录", "打卡", "自律",
        "减肥", "健身", "早起", "收纳"], "weight": 1},
    "旅行": {"keywords": ["旅行", "旅游", "风景", "景点", "攻略",
        "打卡", "拍照", "出片"], "weight": 2},
}

# ============================================================
# 规则引擎 - 模式匹配（完全不变）
# ============================================================

PATTERN_RULES = [
    (r"太(.{1,4})了", "感叹句", 1),
    (r"好想.{1,6}", "向往", 1),
    (r"能不能.{1,6}", "请求/催更", 1.5),
    (r"什么时候.{1,4}", "催更", 1.5),
    (r"催更", "催更", 2),
    (r"求.{1,4}(链接|教程|同款|BGM|歌名)", "求资源", 2),
    (r"同款|同求", "求资源", 1.5),
    (r"我也想.{1,4}", "向往", 1),
    (r"嫁了|娶了", "爱慕", 2),
    (r"想嫁|想娶", "爱慕", 2),
    (r"老婆|老公", "爱慕", 1.5),
    (r"妈妈我上桌了|我上桌了", "粉丝认同", 2),
    (r"关注了|已关注", "转化", 2),
    (r"第一", "前排", 0.5),
    (r"前排", "前排", 0.5),
]

# ============================================================
# 停用词（完全不变）
# ============================================================

STOP_WORDS = set("""
的 了 在 是 我 有 和 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有 看 好 自己
这 那 么 什么 他 她 它 们 吧 呢 啊 嗯 哦 哈哈哈 哈哈 这个 那个 可以 还 这里 就是 因为
但是 而且 所以 如果 虽然 然后 现在 已经 可能 应该 需要 或者 比 更 最 不过 只是 当时
当然 其实 还是 不是 没 做 让 被 把 从 得 地 过 给 对 与 为 向 以 跟 等 吗 吧 呀 啦 嘛
嗯 嗯嗯 哦哦 抖音 视频 评论 回复 呀吧 哈啊 阿 呀呢 了啦 的吧 的啊 了呢 的呢
这个 那个 这些 那些 这么 那么 这样 那样 什么 怎么 为什么 哪个 多少 几个
""".split())


# ============================================================
# 规则引擎函数（完全不变）
# ============================================================

def extract_sentiment(text):
    score = 0
    for word, weight in POSITIVE_WORDS.items():
        if word in text:
            score += weight
    for word, weight in NEGATIVE_WORDS.items():
        if word in text:
            score += weight
    if score >= 3:
        return score, "强正面"
    elif score >= 1:
        return score, "正面"
    elif score <= -3:
        return score, "强负面"
    elif score <= -1:
        return score, "负面"
    else:
        return score, "中性"


def extract_domain_tags(text):
    tags = []
    for domain, config in DOMAIN_PATTERNS.items():
        keywords = config["keywords"]
        weight = config["weight"]
        hits = sum(1 for kw in keywords if kw in text)
        if hits > 0:
            tags.append({"tag": domain, "weight": hits * weight, "source": "domain"})
    return tags


def extract_pattern_tags(text):
    tags = []
    for pattern, tag_name, weight in PATTERN_RULES:
        if re.search(pattern, text):
            tags.append({"tag": tag_name, "weight": weight, "source": "pattern"})
    return tags


def extract_keywords(texts, top_n=10):
    word_counter = Counter()
    for text in texts:
        text = re.sub(r'[^\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ffa-zA-Z0-9]', '', text)
        if not text:
            continue
        for length in [4, 3, 2]:
            for i in range(len(text) - length + 1):
                word = text[i:i + length]
                if word in STOP_WORDS or word.isdigit():
                    continue
                if length == 2 and all(c in STOP_WORDS for c in word):
                    continue
                word_counter[word] += 1

    filtered = {}
    for word, count in word_counter.most_common(top_n * 5):
        if count < 2:
            continue
        dominated = False
        for longer, lcount in filtered.items():
            if word in longer and lcount >= count * 0.5:
                dominated = True
                break
        if not dominated:
            filtered[word] = count

    keywords = []
    for word, count in sorted(filtered.items(), key=lambda x: -x[1]):
        keywords.append({"tag": word, "weight": count, "source": "keyword"})
        if len(keywords) >= top_n:
            break
    return keywords


def tag_video_comments(conn, aweme_id):
    """v4 版：从 comments 表提取标签"""
    rows = conn.execute("""
        SELECT content, digg_count
        FROM comments
        WHERE aweme_id = ?
        ORDER BY digg_count DESC
    """, (aweme_id,)).fetchall()

    if not rows:
        return []

    all_tags = []
    all_texts = []
    sentiment_scores = []

    for r in rows:
        text = r["content"] or ""
        if not text:
            continue
        digg = r["digg_count"] or 0

        digg_weight = 1
        if digg >= 10000:
            digg_weight += 2
        elif digg >= 1000:
            digg_weight += 1

        score, label = extract_sentiment(text)
        sentiment_scores.append(score * digg_weight)

        domain_tags = extract_domain_tags(text)
        for t in domain_tags:
            t["weight"] *= digg_weight
        all_tags.extend(domain_tags)

        pattern_tags = extract_pattern_tags(text)
        for t in pattern_tags:
            t["weight"] *= digg_weight
        all_tags.extend(pattern_tags)

        all_texts.append(text)

    keyword_tags = extract_keywords(all_texts, top_n=8)
    all_tags.extend(keyword_tags)

    if sentiment_scores:
        avg_sentiment = sum(sentiment_scores) / len(sentiment_scores)
        if avg_sentiment >= 2:
            sentiment_label = "强正面"
        elif avg_sentiment >= 0.5:
            sentiment_label = "正面"
        elif avg_sentiment <= -2:
            sentiment_label = "强负面"
        elif avg_sentiment <= -0.5:
            sentiment_label = "负面"
        else:
            sentiment_label = "中性"
        all_tags.append({"tag": sentiment_label, "weight": round(abs(avg_sentiment), 2), "source": "sentiment"})

    # 合并同类标签
    merged = {}
    for t in all_tags:
        key = t["tag"]
        if key in merged:
            merged[key]["weight"] += t["weight"]
        else:
            merged[key] = {"tag": key, "weight": t["weight"], "source": t["source"]}

    result = sorted(merged.values(), key=lambda x: -x["weight"])
    result = [t for t in result if t["weight"] >= 1]
    result = result[:20]
    for t in result:
        t["weight"] = round(t["weight"], 1)

    return result


def save_comment_tags(conn, aweme_id, tags):
    """写入 v4 videos_comment_tags 表"""
    if not tags:
        return
    conn.execute("""
        INSERT INTO videos_comment_tags (aweme_id, comment_tags)
        VALUES (?, ?)
        ON CONFLICT(aweme_id) DO UPDATE SET comment_tags = excluded.comment_tags
    """, (aweme_id, json.dumps(tags, ensure_ascii=False)))


def run_from_cli(args):
    """供 dytool_v4.py CLI 调用"""
    db_path = _get_v4_db_path()
    init_db_v4(db_path)
    conn = get_conn_v4(db_path)

    aweme_id = getattr(args, 'aweme_id', None)
    force = getattr(args, 'force', False)
    limit = getattr(args, 'limit', 0)
    show = getattr(args, 'show', False)

    if aweme_id:
        videos = [{"aweme_id": aweme_id}]
    else:
        # 查 v4: 有评论但未提取标签的视频
        if force:
            sql = """
                SELECT DISTINCT c.aweme_id FROM comments c
                ORDER BY c.aweme_id DESC
            """
        else:
            sql = """
                SELECT DISTINCT c.aweme_id FROM comments c
                WHERE c.aweme_id NOT IN (SELECT aweme_id FROM videos_comment_tags)
                ORDER BY c.aweme_id DESC
            """
        if limit > 0:
            sql += f" LIMIT {limit}"
        videos = [dict(r) for r in conn.execute(sql).fetchall()]

    if not videos:
        print("没有需要处理的视频")
        conn.close()
        return

    print("=" * 60)
    print("v4 评论标签提取")
    print("=" * 60)
    print(f"待处理: {len(videos)} 个视频\n")

    tagged = 0
    for i, v in enumerate(videos, 1):
        aweme_id = v["aweme_id"]
        tags = tag_video_comments(conn, aweme_id)

        if tags:
            save_comment_tags(conn, aweme_id, tags)
            tagged += 1

            if show:
                row = conn.execute(
                    "SELECT title FROM videos_base WHERE aweme_id=?", (aweme_id,)
                ).fetchone()
                title = (row["title"] if row else "")[:30]
                print(f"\n[{i}] {aweme_id} {title}")

                by_source = {}
                for t in tags:
                    src = t["source"]
                    if src not in by_source:
                        by_source[src] = []
                    by_source[src].append(t)
                for src, group in by_source.items():
                    tag_str = ", ".join([f"{t['tag']}({t['weight']})" for t in group[:5]])
                    print(f"  [{src}] {tag_str}")

        if i % 50 == 0:
            conn.commit()

    conn.commit()
    print(f"\n完成! 已标记 {tagged}/{len(videos)} 个视频")
    conn.close()