"""
评论标签提取规则引擎
从评论内容中提取情感标签、内容标签、属性标签

规则体系:
  1. 情感分析: 正面/负面/中性 → sentiment标签
  2. 内容关键词: 基于领域词典匹配 → content标签
  3. 高频词提取: TF统计 + 停用词过滤 → keyword标签
  4. 模式匹配: 正则捕捉常见表达 → pattern标签
  5. 点赞加权: 高赞评论的标签权重更高

用法:
  python comment_tagger.py                      # 对所有已抓评论的视频提取标签
  python comment_tagger.py --aweme_id XXX       # 只处理指定视频
  python comment_tagger.py --force              # 重新提取(包括已提取过的)
  python comment_tagger.py --limit 100          # 最多处理100个视频
  python comment_tagger.py --show               # 处理后展示标签详情
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

from db_utils import init_db, get_conn, update_video_comment_tags

# ============================================================
# 规则引擎 - 情感词典
# ============================================================

POSITIVE_WORDS = {
    # 通用正面
    "好看": 2, "喜欢": 2, "爱": 2, "牛": 2, "厉害": 2, "绝了": 3, "yyds": 3,
    "神仙": 3, "太棒了": 2, "优秀": 2, "赞": 1, "棒": 1, "强": 1, "美": 2,
    "帅": 2, "酷": 2, "超": 1, "惊艳": 3, "完美": 3, "绝美": 3, "绝杀": 2,
    # 抖音风格正面
    "封神": 3, "天花板": 3, "天花板级别": 3, "顶级": 3, "绝绝子": 3,
    "太绝了": 3, "爱了": 2, "心动": 2, "上头": 2, "真香": 2, "离谱": 1,
    "永远可以相信": 3, "救命": 2, "啊啊啊": 1, "太会了": 2, "拿捏": 2,
    "氛围感": 2, "沉浸": 2, "治愈": 2, "感动": 2, "泪目": 2, "破防": 2,
    "笑死": 1, "哈哈哈": 1, "乐死": 1, "有趣": 1, "好玩": 1, "搞笑": 1,
    "宝藏": 2, "宝藏up": 2, "宝藏博主": 2, "宝藏女孩": 2,
    # 颜值相关
    "好美": 2, "太美了": 3, "美哭": 3, "好看死了": 3, "盛世美颜": 3,
    "仙女": 2, "女神": 2, "漂亮": 2, "可爱": 2, "甜": 2, "甜妹": 2,
    # 技能相关
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
# 规则引擎 - 内容领域词典
# ============================================================

DOMAIN_PATTERNS = {
    "cosplay": {
        "keywords": ["cos", "cosplay", "coser", "还原", "漫展", "角色扮演",
                      "原神", "崩坏", "明日方舟", "王者", "LOL", "乙女"],
        "weight": 2,
    },
    "舞蹈": {
        "keywords": ["舞", "跳舞", "编舞", "街舞", "jazz", "kpop", "韩舞",
                      "宅舞", "翻跳", "cover", "基本功", "律动"],
        "weight": 2,
    },
    "音乐": {
        "keywords": ["歌", "唱", "声音", "嗓音", "翻唱", "弹", "吉他",
                      "钢琴", "BGM", "节奏", "旋律", "音色"],
        "weight": 2,
    },
    "颜值": {
        "keywords": ["颜值", "脸", "五官", "妆", "化妆", "美妆", "口红",
                      "眼妆", "穿搭", "身材", "皮肤", "发型"],
        "weight": 2,
    },
    "二次元": {
        "keywords": ["二次元", "动漫", "番", "漫画", "手办", "模型",
                      "lo", "lolita", "洛丽塔", "jk", "汉服"],
        "weight": 2,
    },
    "游戏": {
        "keywords": ["游戏", "打法", "攻略", "上分", "段位", "排位",
                      "帧", "fps", "MVP", "带飞"],
        "weight": 2,
    },
    "美食": {
        "keywords": ["好吃", "美食", "做法", "食谱", "烹饪", "食材",
                      "锅", "味道", "馋", "想吃"],
        "weight": 2,
    },
    "知识": {
        "keywords": ["学到", "知识", "科普", "原理", "干货", "涨知识",
                      "原来", "终于懂", "长见识"],
        "weight": 2,
    },
    "情感": {
        "keywords": ["爱情", "恋爱", "分手", "暗恋", "对象", "老公",
                      "老婆", "男朋友", "女朋友", "单身", "脱单"],
        "weight": 2,
    },
    "搞笑": {
        "keywords": ["搞笑", "段子", "整活", "沙雕", "迷惑行为",
                      "名场面", "笑", "乐", "整蛊"],
        "weight": 2,
    },
    "生活": {
        "keywords": ["生活", "日常", "记录", "打卡", "自律",
                      "减肥", "健身", "早起", "收纳"],
        "weight": 1,
    },
    "旅行": {
        "keywords": ["旅行", "旅游", "风景", "景点", "攻略",
                      "打卡", "拍照", "出片"],
        "weight": 2,
    },
}

# ============================================================
# 规则引擎 - 模式匹配
# ============================================================

PATTERN_RULES = [
    # (pattern, tag, weight)
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
# 停用词
# ============================================================

STOP_WORDS = set("""
的 了 在 是 我 有 和 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有 看 好 自己
这 那 么 什么 他 她 它 们 吧 呢 啊 嗯 哦 哈哈哈 哈哈 这个 那个 可以 还 这里 就是 因为
但是 而且 所以 如果 虽然 然后 现在 已经 可能 应该 需要 或者 比 更 最 不过 只是 当时
当然 其实 还是 不是 没 做 让 被 把 从 得 地 过 给 对 与 为 向 以 跟 等 吗 吧 呀 啦 嘛
嗯 嗯嗯 哦哦 抖音 视频 评论 回复 呀吧 哈啊 阿 呀呢 了啦 的吧 的啊 了呢 的呢
这个 那个 这些 那些 这么 那么 这样 那样 什么 怎么 为什么 哪个 多少 几个
""".split())


def extract_sentiment(text):
    """情感分析: 返回 (score, label)"""
    score = 0
    for word, weight in POSITIVE_WORDS.items():
        if word in text:
            score += weight
    for word, weight in NEGATIVE_WORDS.items():
        if word in text:
            score += weight  # weight is already negative

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
    """领域标签匹配"""
    tags = []
    for domain, config in DOMAIN_PATTERNS.items():
        keywords = config["keywords"]
        weight = config["weight"]
        hits = sum(1 for kw in keywords if kw in text)
        if hits > 0:
            tags.append({
                "tag": domain,
                "weight": hits * weight,
                "source": "domain",
            })
    return tags


def extract_pattern_tags(text):
    """模式匹配标签"""
    tags = []
    for pattern, tag_name, weight in PATTERN_RULES:
        if re.search(pattern, text):
            tags.append({
                "tag": tag_name,
                "weight": weight,
                "source": "pattern",
            })
    return tags


def extract_keywords(texts, top_n=10):
    """高频关键词提取(简易分词: 基于规则+滑动窗口)"""
    # 简易中文分词: 提取2-4字词组
    word_counter = Counter()

    for text in texts:
        # 清理
        text = re.sub(r'[^\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ffa-zA-Z0-9]', '', text)
        if not text:
            continue

        # 2-4字滑动窗口
        for length in [4, 3, 2]:
            for i in range(len(text) - length + 1):
                word = text[i:i + length]
                # 跳过停用词和纯数字
                if word in STOP_WORDS or word.isdigit():
                    continue
                # 2字词至少含一个有意义字符
                if length == 2 and all(c in STOP_WORDS for c in word):
                    continue
                word_counter[word] += 1

    # 后处理: 合并子串 (如果"高层次"和"高层"同时出现, 保留较长的)
    filtered = {}
    for word, count in word_counter.most_common(top_n * 5):
        if count < 2:
            continue
        # 检查是否有更长的父串出现次数相近(>=count*0.5)
        dominated = False
        for longer, lcount in filtered.items():
            if word in longer and lcount >= count * 0.5:
                dominated = True
                break
        if not dominated:
            filtered[word] = count

    keywords = []
    for word, count in sorted(filtered.items(), key=lambda x: -x[1]):
        keywords.append({
            "tag": word,
            "weight": count,
            "source": "keyword",
        })
        if len(keywords) >= top_n:
            break

    return keywords


def tag_video_comments(conn, aweme_id):
    """对单个视频的评论提取标签，返回标签列表"""
    # 获取该视频所有评论
    rows = conn.execute("""
        SELECT content, digg_count, is_hot
        FROM comments
        WHERE aweme_id = ?
        ORDER BY digg_count DESC
    """, (aweme_id,)).fetchall()

    if not rows:
        return []

    # 收集所有标签和文本
    all_tags = []
    all_texts = []
    sentiment_scores = []

    for r in rows:
        text = r["content"] or ""
        if not text:
            continue
        digg = r["digg_count"] or 0
        is_hot = r["is_hot"]

        # 点赞权重: 基础1, 高赞+1, 热评+1
        digg_weight = 1
        if digg >= 10000:
            digg_weight += 2
        elif digg >= 1000:
            digg_weight += 1
        if is_hot:
            digg_weight += 1

        # 情感
        score, label = extract_sentiment(text)
        sentiment_scores.append(score * digg_weight)

        # 领域标签
        domain_tags = extract_domain_tags(text)
        for t in domain_tags:
            t["weight"] *= digg_weight
        all_tags.extend(domain_tags)

        # 模式标签
        pattern_tags = extract_pattern_tags(text)
        for t in pattern_tags:
            t["weight"] *= digg_weight
        all_tags.extend(pattern_tags)

        all_texts.append(text)

    # 高频关键词
    keyword_tags = extract_keywords(all_texts, top_n=8)
    all_tags.extend(keyword_tags)

    # 情感汇总
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

        all_tags.append({
            "tag": sentiment_label,
            "weight": round(abs(avg_sentiment), 2),
            "source": "sentiment",
        })

    # 合并同类标签
    merged = {}
    for t in all_tags:
        key = t["tag"]
        if key in merged:
            merged[key]["weight"] += t["weight"]
        else:
            merged[key] = {
                "tag": key,
                "weight": t["weight"],
                "source": t["source"],
            }

    # 按权重排序，只保留有意义的
    result = sorted(merged.values(), key=lambda x: -x["weight"])

    # 过滤: 至少 weight >= 1
    result = [t for t in result if t["weight"] >= 1]

    # 限制数量
    result = result[:20]

    # 格式化 weight
    for t in result:
        t["weight"] = round(t["weight"], 1)

    return result


def main():
    parser = argparse.ArgumentParser(description="评论标签提取规则引擎")
    parser.add_argument("--aweme_id", "-a", help="只处理指定视频")
    parser.add_argument("--force", "-f", action="store_true",
                        help="重新提取(包括已提取过的)")
    parser.add_argument("--limit", "-n", type=int, default=0,
                        help="最多处理N个视频")
    parser.add_argument("--show", "-s", action="store_true",
                        help="展示标签详情")
    args = parser.parse_args()

    init_db()
    conn = get_conn()

    # 查询需要处理的视频
    if args.aweme_id:
        videos = [{"aweme_id": args.aweme_id}]
    else:
        where = "1=1" if args.force else "comment_fetched = 1 AND comment_tags = '[]'"
        sql = f"""
            SELECT aweme_id FROM videos
            WHERE {where}
            ORDER BY json_extract(stats, '$.digg') DESC
        """
        if args.limit > 0:
            sql += f" LIMIT {args.limit}"
        videos = [dict(r) for r in conn.execute(sql).fetchall()]

    if not videos:
        print("没有需要处理的视频")
        conn.close()
        return

    print("=" * 60)
    print("🏷️ 评论标签提取规则引擎")
    print("=" * 60)
    print(f"待处理: {len(videos)} 个视频\n")

    tagged = 0

    for i, v in enumerate(videos, 1):
        aweme_id = v["aweme_id"]
        tags = tag_video_comments(conn, aweme_id)

        if tags:
            update_video_comment_tags(conn, aweme_id, tags)
            tagged += 1

            if args.show:
                # 获取视频标题
                row = conn.execute(
                    "SELECT title, desc FROM videos WHERE aweme_id=?", (aweme_id,)
                ).fetchone()
                title = (row["title"] or row["desc"] or "")[:30]
                print(f"\n[{i}] {aweme_id} {title}")

                # 按来源分组展示
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

    # 统计标签分布
    if tagged > 0:
        all_tag_rows = conn.execute("""
            SELECT comment_tags FROM videos
            WHERE comment_tags != '[]'
        """).fetchall()

        source_counter = Counter()
        tag_counter = Counter()
        for r in all_tag_rows:
            tags = json.loads(r["comment_tags"])
            for t in tags:
                source_counter[t["source"]] += 1
                tag_counter[t["tag"]] += 1

        print(f"\n📊 标签统计:")
        print(f"  来源分布: {dict(source_counter.most_common(6))}")
        print(f"  热门标签: {dict(tag_counter.most_common(15))}")

    conn.close()


if __name__ == "__main__":
    main()
