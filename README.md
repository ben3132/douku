# 抖库 (DouKu)

> 抖音点赞数据分类工具 — 从自己的点赞列表提取结构化信息，支持内容分类、UP主画像、视频下载。

---

## 🚀 v4 推荐使用（16 表架构，WAL 并发模式）

**v4 是当前推荐版本**，基于 16 张表分层设计，支持高并发读写，提供更完善的数据分析和报告功能。

### v4 快速开始

```bash
# 首次初始化（建库建表）
python dytool_v4.py init

# 重新获取 Cookie（浏览器扫码）
python dytool_v4.py cookie

# 抓取点赞视频数据
python dytool_v4.py fetch likes

# 抓取收藏视频数据
python dytool_v4.py fetch favorites

# 抓取 UP主资料
python dytool_v4.py fetch profiles

# 抓取评论（支持预估 API 消耗）
python dytool_v4.py fetch comments

# 内容分类（17 类）
python dytool_v4.py classify

# 生成交互式 HTML 报告
python dytool_v4.py report

# 查看统计摘要
python dytool_v4.py info

# 完整性检查
python dytool_v4.py check

# 视频下载
python dytool_v4.py download --limit 50

# 刷新过期视频 URL
python dytool_v4.py refresh
```

### v4 架构说明

| 特性 | v3 | v4 |
|------|----|----|
| 表数量 | 3 张 | **16 张** |
| 并发模式 | 默认 | **WAL**（读不阻塞写） |
| 下载状态 | 0/1 | **5 态状态机** |
| 断点续传 | cursor | **cursor + liked_time 书签** |
| 分类体系 | 17 类 | **17 类 + 赛道分层** |
| 评论标签 | 基础 | **多标签** |
| UP主画像 | 基础 | **多维画像** |
| 分析决策 | 盲跑 | **预估 API 消耗** |

### v4 数据库结构

```
data/douku_v4.db
├── videos_base          # 视频基本信息
├── videos_stats         # 视频动态数据（点赞/评论/分享）
├── videos_classification  # 17 类内容分类
├── videos_download      # 下载状态机（5 态）
├── videos_url           # 多 Tier URL 管理
├── authors_base         # UP主基础信息
├── authors_portrait     # UP主画像（多维度）
├── comments             # 视频评论
├── comment_tags         # 评论标签
├── bookmarks_base       # 收藏夹基础
├── bookmarks_items      # 收藏视频关联
├── auth_state           # Cookie 状态管理
└── ...                  # 迁移日志/操作记录等
```

---

## v3（经典版）

# 抖库 (DouKu)

> 抖音点赞数据分类工具 — 从自己的点赞列表提取结构化信息，支持内容分类、UP主画像、视频下载。

[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](./LICENSE)

**这是一个开源工具模板，不包含任何用户数据。**

---

## ⚠️ 免责声明 / Disclaimer

1. 本项目（抖库）为**个人学习研究**用途开发，仅供学习交流使用
2. **严禁**用于任何商业用途
3. **严禁**用于以下行为：
   - 爬取他人隐私数据
   - 批量抓取对平台造成压力
   - 违反《抖音用户服务协议》的任何行为
   - 违反《中华人民共和国网络安全法》《数据安全法》《个人信息保护法》等法律法规的行为
   - 任何其他违法行为
4. 使用本工具即表示你已阅读并同意：**使用者需自行承担全部法律风险和责任，作者不承担任何连带责任**
5. 本项目不提供任何明示或暗示的担保，包括但不限于适销性和特定用途的适用性
6. 如果你是抖音平台方，认为本项目侵犯了你的权益，请联系作者删除

---

## 核心功能

| # | 功能 | 状态 | 说明 |
|---|------|:----:|------|
| 1 | 点赞列表同步 | ✅ | SQLite 持久化，支持增量续爬 |
| 2 | 收藏列表同步 | ✅ | 同上 |
| 3 | 评论抓取（热评） | ✅ | 默认按热度排序，每视频 1 页 20 条 |
| 4 | UP主信息补全 | ✅ | 通过视频详情 API 批量更新 |
| 5 | 视频内容分类 | ✅ | 双层引擎（官方标签映射 + 关键词推断），17 个分类 |
| 6 | 分层数据刷新 | ✅ | Tier1(URL) / Tier2(动态) / Tier3(UP主) |
| 7 | 视频下载 | ✅ | 按分类/UP主/标签筛选，下载前自动刷新 URL |
| 8 | HTML 报告生成 | ✅ | 交互式筛选 + 分组统计 |

---

## 参考项目 / Acknowledgements

本项目开发过程中参考了以下开源项目，感谢它们的贡献：

| 项目 | 说明 |
|------|------|
| [Johnserf-Seed/f2](https://github.com/Johnserf-Seed/f2) | 抖音 API 调用模式、a_bogus 签名算法、请求构造思路 |
| [Johnserf-Seed/TikTokDownload](https://github.com/Johnserf-Seed/TikTokDownload) | 早期项目架构参考 |
| [douyin-downloader](https://gitcode.com/GitHub_Trending/do/douyin-downloader) | 加权关键词分类方案（分类器设计参考） |
| [zcx2077/abogus](https://github.com/zcx2077/-abogus-a_bogus-ttwid-req_sign-bd_ticket_guard_client_data-sig3-) | a_bogus 签名算法实现 |
| [microsoft/playwright](https://github.com/microsoft/playwright) | 自动化 Cookie 获取方案 |

---

## 安装与配置

### 环境要求

- Python 3.8+
- Windows / macOS / Linux
- Edge / Chrome 浏览器（用于自动获取 Cookie）

### 安装

```bash
git clone <repo>
cd douyin-likes-parser
pip install -r requirements.txt
```

### 配置 Cookie（自动化）

运行脚本，自动打开浏览器扫码登录，Cookie 自动写入配置文件：

```bash
python -m modules.playwright_cookie
```

完成后验证：

```bash
python dytool.py stats
```

---

## 快速开始

### 首次配置

```bash
# 自动获取 Cookie（浏览器自动弹出，扫码登录）
python -m modules.playwright_cookie
```

### 同步数据

```bash
# 同步点赞列表
python dytool.py fetch likes

# 同步收藏列表
python dytool.py fetch favorites

# 抓取评论（热评）
python dytool.py fetch comments

# 补全 UP主信息
python dytool.py fetch profiles
```

### 分类与下载

```bash
# 运行内容分类器
python dytool.py classify

# 下载视频（按分类）
python dytool.py download --tag 颜值 --limit 10

# 下载视频（按 UP主）
python dytool.py download --author 琪琳 --limit 5
```

### 查看报告

```bash
# 生成 HTML 报告
python dytool.py report

# 查看统计
python dytool.py stats
```

### 分层刷新

```bash
# Tier1: 下载 URL（每次下载前）
python dytool.py refresh --tier 1

# Tier2: 视频动态数据（点赞/评论/标签）
python dytool.py refresh --tier 2

# Tier3: UP主画像（粉丝数等）
python dytool.py refresh --tier 3
```

---

## 项目结构

```
douyin-likes-parser/
├── dytool.py              # CLI 统一入口
├── requirements.txt       # 依赖
├── README.md              # 本文档
├── config.py              # 配置文件（自动生成）
├── modules/
│   ├── __init__.py
│   ├── config.py              # 配置加载
│   ├── db_utils.py            # SQLite 工具
│   ├── playwright_cookie.py   # Cookie 自动获取
│   ├── fetch_likes_db.py      # 点赞抓取
│   ├── fetch_favorites_db.py  # 收藏抓取
│   ├── fetch_comments.py      # 评论抓取
│   ├── fetch_up_profiles.py   # UP主信息
│   ├── download_videos.py     # 视频下载
│   ├── refresh_data.py        # 分层刷新
│   ├── refresh_urls.py        # URL 刷新
│   ├── content_classifier.py  # 内容分类
│   ├── comment_tagger.py      # 评论标签
│   ├── author_portrait.py     # UP主画像
│   └── generate_report.py     # 报告生成
└── data/
    ├── likes.db               # SQLite 数据库
    └── downloads/             # 下载视频目录
```

### 模块说明

| 模块 | 功能 |
|------|------|
| dytool.py | CLI 统一入口，子命令：fetch/download/refresh/classify/report/stats |
| db_utils.py | 数据库初始化、迁移、辅助函数 |
| fetch_* | 抓取点赞/收藏/评论/UP主信息 |
| download_videos.py | 视频下载，支持 --tag/--author/--limit |
| refresh_data.py | 分层刷新机制（Tier1/2/3） |
| content_classifier.py | 视频内容分类（17类） |
| generate_report.py | HTML 交互式报告生成 |

---

## 常见问题

### Q: Cookie 过期怎么办？
A: 重新运行 `python -m modules.playwright_cookie` 扫码登录

### Q: 下载失败怎么办？
A: 使用 `--refresh` 自动刷新 URL，或手动运行 `python dytool.py refresh`

### Q: 如何查看数据统计？
A: `python dytool.py stats`

### Q: 视频无法下载？
A: 检查是否被UP主设置"禁止下载"，这类视频 API 不返回下载链接

---

## 数据说明

本项目获取的数据为用户授权公开信息，包括：
- 您点赞/收藏的视频基本信息
- 视频公开评论
- UP主公开主页信息

请勿将数据用于商业用途或二次传播。尊重创作者劳动成果。

---

## 开源协议

MIT License - 自由使用，商用需保留版权声明。

---

*如果对你有帮助，欢迎 Star ⭐*

