# DouKU 2.2

DouKU（斗库）是一套面向国内网络环境的本地视频数据归档工具。它帮助用户在自己的电脑上采集、整理和下载本人有权访问的抖音账号数据，也可以同步指定抖音作者、B站 UP 主，并处理单个或批量分享链接。

项目的目标不是搭建公开视频站点，而是将分散在平台页面中的作品信息、媒体地址和本地文件整理成可查询、可增量更新、可继续下载的个人资料库。

## 项目解决什么问题

日常保存短视频数据通常会遇到这些问题：

- 点赞和收藏数量较多，网页只能逐页浏览。
- Cookie 容易失效，脚本与日常浏览器登录状态不一致。
- 视频、封面、图文图片、配乐和作品信息分散，无法建立对应关系。
- 不同账号、不同作者和临时分享链接下载的文件容易混在一起。
- 单文件 JSON 不适合管理数千到数万条作品。
- 第三方数据平台能够辅助发现热门作品，但最终仍需要回到原平台核验和下载。

DouKU 将这些步骤组合为一条本地工作流：

```text
浏览器登录或导入 Cookie
        ↓
采集账号、作者或链接的轻量元数据
        ↓
MySQL 去重、索引、筛选和生成下载任务
        ↓
解析真实媒体 URL
        ↓
下载视频、封面、图文图片和配乐
        ↓
校验文件并记录本地路径
```

## 核心能力

### 个人抖音账号

- 采集当前登录账号的点赞、收藏、作品详情和评论。
- 自动分页并记录采集来源和原始顺序。
- 使用抖音个人主页自带搜索框筛选已点赞作品。
- 支持多个关键词、每个关键词取前 N 条，以及跨关键词去重。
- 不同登录账号使用独立的数据和下载目录。

### 指定作者与 UP 主

- 遍历抖音作者或 B站 UP 主的当前公开投稿。
- 获取最新 N 条、指定时间范围或后续新增作品。
- 按视频/图文、发布时间、点赞、评论、分享、收藏和时长筛选。
- 排除置顶作品，或按作品 ID 精确下载。
- 使用抖音作者主页的“搜索 Ta 的作品”核验第三方平台定位出的作品。
- 定期重复执行 `sync` 时跳过已经入库和下载的内容。

### 分享链接解析与下载

- 支持单个链接、多个链接和文本文件批量导入。
- 重点支持抖音与 B站，其他站点由 `yt-dlp` 的可用解析器处理。
- 可只解析真实媒体 URL，也可直接下载。
- 直接链接下载与个人账号、指定作者的数据分开保存。

### 数据和文件管理

- MySQL 分表保存作品、统计数据、媒体 URL、下载任务和本地文件索引。
- 视频、封面、图文图片和配乐按类型存放。
- 文件名包含五位以内本地编号、作者名和作品文案。
- 支持并发下载、失败重试、Range 续传和 `.part` 临时文件。
- 下载完成后检查视频或音频文件头，避免将错误响应保存为媒体文件。

### 登录状态与隐私

- 使用独立的 Edge 持久化用户目录保存登录状态。
- 支持导入浏览器扩展导出的抖音 Cookie。
- Cookie 使用 Windows DPAPI 加密后保存在用户本机，不写入项目目录。
- 代码目录与数据库、浏览器状态、下载内容相互分离。

## 技术结构

| 模块 | 作用 |
|---|---|
| Playwright + Edge | 登录、页面操作、站内搜索和接口响应捕获 |
| Requests | 媒体文件下载、连接复用和断点续传 |
| yt-dlp | B站及其他受支持站点的链接解析与下载 |
| MySQL 8.0 | 作品、统计、媒体 URL、任务和文件索引 |
| Windows DPAPI | 本机 Cookie 加密存储 |
| FFmpeg（可选） | 部分站点的音视频合并 |

主要代码目录：

```text
lib/
├─ analysis/    # 分类和本地报告
├─ creator/     # 抖音作者、B站 UP 主采集与筛选
├─ db/          # MySQL 表结构和迁移
├─ download/    # 账号作品下载
├─ link/        # 分享链接解析与直接下载
├─ search/      # 点赞作品站内关键词搜索
└─ utils/       # 配置、账号、Cookie 和路径管理
```

## 环境要求

- Windows 10 或 Windows 11
- Python 3.10+
- Microsoft Edge
- MySQL 8.0
- FFmpeg（部分 B站或其他站点需要）

安装依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
```

## 配置

推荐将数据放在项目目录之外。复制示例配置：

```powershell
$configDir = Join-Path $env:LOCALAPPDATA "DouKU"
New-Item -ItemType Directory -Force $configDir
Copy-Item douku_config.example.json (Join-Path $configDir "config.json")
```

编辑 `%LOCALAPPDATA%\DouKU\config.json`：

```json
{
  "data_dir": "D:\\DouKUData",
  "database": "mysql",
  "mysql_port": 3307,
  "mysqld_path": "C:\\Program Files\\MySQL\\MySQL Server 8.0\\bin\\mysqld.exe",
  "ffmpeg_path": "C:\\ffmpeg\\bin"
}
```

也可以临时指定数据目录：

```powershell
python douku.py --data-dir D:\DouKUData status
```

MySQL连接配置保存在数据目录，而不是代码仓库：

```powershell
New-Item -ItemType Directory -Force D:\DouKUData\private
Copy-Item mysql.example.json D:\DouKUData\private\mysql.json
```

## 快速开始

初始化数据库：

```powershell
python douku.py init
```

打开专用 Edge 并完成抖音登录：

```powershell
python douku.py login
```

检查登录状态和本地数据：

```powershell
python douku.py status
```

采集当前账号：

```powershell
python douku.py fetch all --pages 10
```

下载已采集作品：

```powershell
python douku.py download --limit 100 --workers 3
```

## 常用示例

### 搜索当前账号赞过的作品

```powershell
python douku.py search likes `
  --keywords "美女,舞蹈,cos,二次元" `
  --pages 0
```

根据搜索任务下载，每个关键词取前20条：

```powershell
python douku.py download --search-job 1 --per-keyword 20
```

### 同步指定抖音作者

```powershell
python douku.py creator fetch "抖音作者主页链接" --pages 0
python douku.py creator sync "抖音作者主页链接" --pages 0
```

点赞最高的5条视频，先预览：

```powershell
python douku.py creator download "作者名称或ID" `
  --type video --sort likes --order desc --limit 5 --dry-run
```

2025年最早发布的2条视频：

```powershell
python douku.py creator download "作者名称或ID" `
  --type video `
  --after 2025-01-01 --before 2026-01-01 `
  --sort published --order asc --limit 2
```

排除置顶后的最新3条视频：

```powershell
python douku.py creator download "作者名称或ID" `
  --type video --exclude-pinned `
  --sort published --order desc --limit 3
```

### 第三方平台定位后回到抖音核验

第三方数据平台只用于发现候选作品。取得作品文案后，在作者主页调用抖音自带搜索：

```powershell
python douku.py creator search `
  "抖音作者主页链接" `
  "候选作品标题或文案"
```

确认结果中的作品ID后精准下载：

```powershell
python douku.py creator download "作者名称或ID" `
  --work-id 作品ID
```

### 同步 B站 UP 主

```powershell
python douku.py creator fetch "https://space.bilibili.com/数字ID/video" --latest 50
python douku.py creator download "UP主名称或ID" --latest 50
```

### 下载分享链接

```powershell
python douku.py link "https://v.douyin.com/..."
python douku.py link "https://www.bilibili.com/video/BV..."
python douku.py link --file links.txt
python douku.py link --resolve-only "分享链接"
```

## 数据目录

```text
DouKUData/
├─ private/
│  ├─ mysql.json
│  ├─ active_account.json
│  └─ edge_profile/
├─ mysql/
├─ downloads/
│  ├─ accounts/
│  │  └─ 账号名称/
│  ├─ creators/
│  │  ├─ douyin/作者名称/
│  │  └─ bilibili/UP主名称/
│  └─ direct/
├─ output/
└─ logs/
```

Cookie密文默认位于：

```text
%LOCALAPPDATA%\DouKU\secrets\
```

这些目录均被排除在Git版本之外。

## 使用边界

- 只能采集当前账号有权访问的内容。
- 项目不会绕过登录、私密账号、付费、会员、地区限制、DRM或平台验证。
- 作者删除、隐藏或平台接口不再返回的作品无法保证获取。
- 播放量等指标可能不会出现在作者列表接口中，此时数据库会记录为0。
- 第三方数据平台的排行和历史统计只作为候选依据，最终以抖音站内核验结果为准。
- 平台接口和页面结构变化后，采集规则可能需要更新。
- 使用者应遵守平台服务条款、著作权规则和所在地法律，仅处理本人有权保存的数据。

## 测试

```powershell
python -m unittest discover -s tests -v
```

本机MySQL集成测试：

```powershell
$env:DOUKU_INTEGRATION_TESTS = "1"
python -m unittest tests.test_core.CoreTest.test_mysql_connection -v
```

## 版本

- `v2.2.x`：作者/UP主同步、站内搜索、精确作品下载和高级筛选。
- `v2.1`：通用链接下载、账号隔离和代码数据分离。

详细变化见 [RELEASE_NOTES_2.2.md](RELEASE_NOTES_2.2.md)。

## License

本项目采用 [MIT License](LICENSE)。
