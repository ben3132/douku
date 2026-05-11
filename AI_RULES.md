# AI 运行规则

> ⚠️ AI 助手运行本项目时必须遵守以下规则。

## 核心规则

### 1. 下载前必须确认
**fetch** 和 **download** 命令默认只获取元数据（标题、标签、评论、画像等），**不会自动下载视频文件**。

下载视频文件前，AI 必须：
1. 明确告知用户：待下载数量、预计磁盘占用、耗时
2. 等待用户确认后再执行

快速跳过确认（用户已明确要下载时）：
```bash
python dytool.py fetch likes --mode video --yes   # 获取信息 + 下载
python dytool.py download --yes                    # 直接下载
```

### 2. 命令速查

| 命令 | 说明 | 是否下载视频 |
|------|------|:---:|
| `fetch likes --count N` | 抓取点赞列表 | ❌ |
| `fetch favorites --count N` | 抓取收藏列表 | ❌ |
| `fetch comments` | 抓取评论 | ❌ |
| `portrait` | UP主画像分析 | ❌ |
| `classify` | 视频内容分类 | ❌ |
| `report` | 生成HTML报告 | ❌ |
| `download --category X` | 下载指定分类视频 | ✅ |
| `fetch likes --mode video --yes` | 获取信息+下载 | ✅ |

### 3. 长时间任务
- 抓取点赞/收藏（大批量）可能运行 10-30 分钟
- 下载视频（批量）每个 ~1 秒，按数量估算
- 务必使用长超时或后台模式运行
- fetch 类支持断点续传（cursor 书签）

### 4. 并发冲突
- **下载和评论抓取不能同时跑**，SQLite 不支持并发写
- 优先级：先下载 → 再抓评论 → 再画像 → 最后生成报告