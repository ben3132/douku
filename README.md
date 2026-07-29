# DouKU 2.1

DouKU 用于归档和整理当前登录账号自己的抖音数据。它不提供公开搜索、
批量账号爬取或绕过登录功能。

## 能力范围

- 扫码登录并保存浏览器登录态
- 采集自己的点赞和收藏
- 补全视频详情、播放地址和近期视频评论
- 记录数据来源，重复运行时更新而不是重复插入
- 规则分类、按分类下载、生成本地 HTML 报告
- MySQL 8.0 分层存储，按更新频率拆表并针对 1 万条以上视频建立索引

抖音 Web 接口属于站点内部接口，可能随时调整。本项目把接口配置集中在
`lib/collector.py`，遇到变化时只需更新该模块。

## 环境

- Windows 10/11
- Python 3.10+
- Microsoft Edge 或 Playwright Chromium
- 可正常访问抖音的国内网络

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

把公开配置模板复制到当前 Windows 用户的应用配置目录，并按本机情况修改
数据目录和 MySQL 路径：

```powershell
$configDir = Join-Path $env:LOCALAPPDATA "DouKU"
New-Item -ItemType Directory -Force $configDir
Copy-Item douku_config.example.json (Join-Path $configDir "config.json")
```

也可以通过 `DOUKU_CONFIG` 指定其他配置文件位置。用户配置始终放在项目目录
之外，避免提交或打包源码时携带个人路径。

机器上有 Edge 时无需下载浏览器。若没有：

```powershell
python -m playwright install chromium
```

可通过环境变量指定 Chrome：

```powershell
$env:DOUKU_BROWSER_CHANNEL = "chrome"
```

## 使用

项目使用 MySQL 8.0。请先创建 `douku` 数据库和仅操作该库的应用账号，然后
把连接信息保存到数据目录的 `private/mysql.json`。可以复制模板：

```powershell
New-Item -ItemType Directory -Force D:\DouKUData\private
Copy-Item mysql.example.json D:\DouKUData\private\mysql.json
```

修改其中的数据库密码后再初始化：

```powershell
# 初始化/升级数据库
python douku.py init

# 首次扫码登录；自动创建 private/edge_profile 专用 Edge 用户目录
python douku.py login

# 检查数据和登录态
python douku.py status

# 一次采集主要数据；默认每类 3 页，评论和详情各 20 个视频
python douku.py fetch all

# 也可以分别采集
python douku.py fetch favorites --pages 10
python douku.py fetch likes --pages 10
python douku.py fetch details --limit 100
python douku.py fetch comments --limit 50

python douku.py classify
python douku.py download --limit 20
python douku.py report
python douku.py check
```

`login` 使用 DouKU 专用的 Edge 持久化用户目录。用户只需在该窗口登录一次，
后续 Cookie、Local Storage 和设备会话由 Edge 自动维护。原有
`private/douyin_state.json` 会在首次运行时自动导入，并继续作为状态备份。
项目不会读取或修改用户日常 Edge 的主配置目录。

`fetch` 默认显示浏览器窗口，这是当前抖音 Web 端更可靠的方式。确认账号
环境稳定后可增加 `--headless` 在后台运行，但无头模式更容易触发空白页或
验证。

## 数据与隐私

本机数据目录由 `%LOCALAPPDATA%\DouKU\config.json`、
`DOUKU_DATA_DIR` 环境变量或 `--data-dir` 参数配置。例如
`D:\DouKUData`：

- `mysql/data/`：DouKU 专用 MySQL 8.0 实例数据
- `private/mysql.json`：MySQL 应用账号配置
- `private/douyin_state.json`：浏览器登录态
- `private/edge_profile/`：DouKU 专用 Edge 持久化用户目录
- `downloads/`：视频
- `output/report.html`：报告

下载内容按数据类型集中存放，文件名使用
`五位内部编号_作者_文案摘要`。编号来自 `videos_base.file_code`，数据库仍以
完整 `aweme_id` 关联，避免截断抖音作品 ID 产生冲突：

```text
downloads/
├── videos/
│   └── 00017_作者名_周末家常菜.mp4
├── covers/
│   └── 00017_作者名_周末家常菜.jpg
└── image_posts/
    ├── covers/
    │   └── 00018_作者名_旅行记录.jpg
    ├── images/
    │   ├── 00018_作者名_旅行记录_01.jpg
    │   └── 00018_作者名_旅行记录_02.webp
    └── music/
        └── 00018_作者名_旅行记录.mp3
```

编号固定显示为五位，范围为 `00001`—`99999`；作者最多 16 个字符，
文案摘要最多 32 个字符，Windows 禁用字符会自动替换。视频作品与图文作品
使用完全独立的目录；同一图文作品的封面、原图和背景音乐共用文件名主体，
原图再追加顺序号。图文接口返回的背景音乐不会再误存为 `.mp4`。不再生成
每视频 JSON 或文件索引。
所有关联统一由 MySQL 的 `aweme_id` 维护：

| 更新频率 | 表 | 用途 |
|---|---|---|
| 低频 | `videos_base`、`authors_base` | 视频与作者基础信息 |
| 中频 | `videos_stats`、`authors_stats` | 点赞、评论、粉丝等统计 |
| 高频 | `video_urls` | 视频、封面、音乐 URL 及刷新状态 |
| 高频 | `download_tasks` | 下载队列、重试、错误和本地视频路径 |
| 按次写入 | `video_sources` | 点赞/收藏来源、采集时间和列表顺序 |
| 按资源 | `media_assets` | 封面、图文、背景音乐 URL、序号和本地路径 |
| 大文本 | `comments` | 评论内容 |
| 低频 | `videos_classification` | 内容分类 |

关键队列均有组合索引，例如来源顺序
`(source, captured_at, position_no)`、下载队列
`(status, priority, updated_at)` 和 URL 刷新队列
`(url_status, refreshed_at)`。1 万视频规模无需扫描全表。

建议 MySQL 仅监听 `127.0.0.1`。程序优先连接已经运行的 MySQL；使用独立
实例时，可通过 `DOUKU_MYSQLD` 或用户配置文件的 `mysqld_path`
指定启动程序，并在数据目录提供 `mysql/my.ini`。

认证文件和数据目录均已加入 `.gitignore`。不要把
`private/douyin_state.json` 或 `private/mysql.json` 发给他人。

可以使用外置数据目录：

```powershell
python douku.py --data-dir D:\DouKUData status
```

或设置 `DOUKU_DATA_DIR` 环境变量。

## 发布与隐私

以下内容只保存在用户本机，均被 `.gitignore` 排除：

- `%LOCALAPPDATA%\DouKU\config.json`
- `data/` 和外置数据目录
- `private/mysql.json`
- `private/douyin_state.json`
- `private/edge_profile/`
- MySQL 数据、日志、下载媒体和本地报告

不要提交现有项目的旧 Git 历史；旧历史可能包含早期登录文件。公开发布时应
从当前清理后的文件重新初始化一个全新的 Git 仓库。

## 使用边界

仅采集本人账号中本人有权访问的数据，并控制采集频率。站点出现验证码、
频率限制或权限提示时应停止采集，不应尝试绕过。
