"""
generate_report_v4.py - 生成可交互的 HTML 报告 (v4)
与 v3 的区别：
  - 使用 v4 多表 JOIN 查询替代单表
  - tracks 优先 authors_portrait，降级 videos_classification
  - authors/videos 改为多表 JOIN
  - HTML 模板和前端 JS 完全不变
"""

import os
import sys
import json
from collections import Counter
from datetime import datetime

from .db_v4 import get_conn_v4, init_db_v4, get_summary, get_category_distribution
from .meta import get_data_root

DEFAULT_OUTPUT = os.path.join(str(get_data_root()), 'output', 'report.html')


def format_count(n):
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    elif n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def load_data(conn, include_comments=True):
    """v4 版：从多表加载数据"""

    # ── 赛道数据 ──
    raw_tracks = [dict(r) for r in conn.execute("""
        SELECT ap.portrait_track as track,
               COUNT(ap.sec_uid) as author_count,
               SUM(ap.video_count) as video_count,
               SUM(COALESCE(ast.follower_count, 0)) as total_followers,
               AVG(COALESCE(ap.avg_digg, 0)) as avg_digg
        FROM authors_portrait ap
        LEFT JOIN authors_stats ast ON ap.sec_uid = ast.sec_uid
        WHERE ap.portrait_track != ''
        GROUP BY ap.portrait_track
        ORDER BY author_count DESC
    """).fetchall()]

    if raw_tracks:
        tracks = raw_tracks
    else:
        # 降级：从 videos_classification 聚合赛道
        tracks = [dict(r) for r in conn.execute("""
            SELECT vc.content_category as track,
                   COUNT(DISTINCT vb.author_sec_uid) as author_count,
                   COUNT(*) as video_count,
                   0 as total_followers,
                   0.0 as avg_digg
            FROM videos_classification vc
            JOIN videos_base vb ON vc.aweme_id = vb.aweme_id
            WHERE vc.content_category IS NOT NULL AND vc.content_category != ''
            GROUP BY vc.content_category
            ORDER BY video_count DESC
        """).fetchall()]

    # ── 作者数据 ──
    authors = [dict(r) for r in conn.execute("""
        SELECT ab.sec_uid, ab.nickname, ab.avatar, ab.signature,
               COALESCE(ast.follower_count, 0) as follower_count,
               COALESCE(ast.following_count, 0) as following_count,
               COALESCE(ast.aweme_count, 0) as aweme_count,
               COALESCE(ast.favoriting_count, 0) as favoriting_count,
               COALESCE(ab.verification_type, 0) as verification_type,
               ab.verification_label,
               ab.ip_location,
               ap.portrait_track, ap.portrait_track_2,
               COALESCE(ap.video_count, 0) as video_count,
               COALESCE(ap.avg_digg, 0) as avg_digg,
               COALESCE(ap.avg_duration, 0) as avg_duration,
               '' as portrait_tags
        FROM authors_base ab
        LEFT JOIN authors_stats ast ON ab.sec_uid = ast.sec_uid
        LEFT JOIN authors_portrait ap ON ab.sec_uid = ap.sec_uid
        ORDER BY ap.video_count DESC, ast.follower_count DESC
    """).fetchall()]

    # ── 视频数据 ──
    videos = [dict(r) for r in conn.execute("""
        SELECT vb.aweme_id, vb.title, vb.desc,
               COALESCE(vb.duration_sec, 0) as duration_sec,
               vb.create_time,
               vb.video_tags, vb.hashtags,
               vs.digg_count, vs.comment_count, vs.share_count,
               vu.video_url, vu.cover_url,
               vm.in_likes, vm.in_favorites, COALESCE(vd.status, 0) as is_downloaded,
               COALESCE(vc.content_category, '') as content_category,
               '' as content_category_detail,
               json_object(
                   'digg', COALESCE(vs.digg_count, 0),
                   'comment', COALESCE(vs.comment_count, 0),
                   'share', COALESCE(vs.share_count, 0)
               ) as stats,
               '' as comment_tags,
               '' as comment_fetched,
               ab.nickname as author_name,
               ap.portrait_track as author_track,
               COALESCE(ast.follower_count, 0) as author_followers,
               ab.sec_uid
        FROM videos_base vb
        LEFT JOIN videos_stats vs ON vb.aweme_id = vs.aweme_id
        LEFT JOIN videos_urls vu ON vb.aweme_id = vu.aweme_id
        LEFT JOIN videos_meta vm ON vb.aweme_id = vm.aweme_id
        LEFT JOIN videos_download vd ON vb.aweme_id = vd.aweme_id
        LEFT JOIN videos_classification vc ON vb.aweme_id = vc.aweme_id
        LEFT JOIN authors_base ab ON vb.author_sec_uid = ab.sec_uid
        LEFT JOIN authors_portrait ap ON ab.sec_uid = ap.sec_uid
        LEFT JOIN authors_stats ast ON ab.sec_uid = ast.sec_uid
        ORDER BY vs.digg_count DESC
    """).fetchall()]

    # ── 评论数据 ──
    comments_by_video = {}
    if include_comments:
        try:
            rows = conn.execute("""
                SELECT aweme_id, cid, content, user_name, digg_count, is_hot, ip_location
                FROM comments ORDER BY digg_count DESC
            """).fetchall()
            for r in rows:
                aid = r["aweme_id"]
                if aid not in comments_by_video:
                    comments_by_video[aid] = []
                comments_by_video[aid].append(dict(r))
        except Exception:
            pass

    # ── 统计 ──
    video_count = conn.execute("SELECT COUNT(*) FROM videos_base").fetchone()[0]
    author_count = conn.execute("SELECT COUNT(*) FROM authors_base").fetchone()[0]
    likes_count = conn.execute(
        "SELECT COUNT(*) FROM videos_meta WHERE in_likes=1"
    ).fetchone()[0]
    favorites_count = conn.execute(
        "SELECT COUNT(*) FROM videos_meta WHERE in_favorites=1"
    ).fetchone()[0]
    downloaded_count = conn.execute(
        "SELECT COUNT(*) FROM videos_download WHERE status=1"
    ).fetchone()[0]
    try:
        comment_count = conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
    except Exception:
        comment_count = 0

    stats = {
        "video_count": video_count,
        "author_count": author_count,
        "likes_count": likes_count,
        "favorites_count": favorites_count,
        "downloaded_count": downloaded_count,
        "comment_count": comment_count,
        "tracks_count": len(tracks),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }

    return {
        "stats": stats,
        "tracks": tracks,
        "authors": authors,
        "videos": videos,
        "comments": comments_by_video,
    }


# ============================================================
# HTML 模板（与 v3 完全一致，前端 JS/样式不变）
# ============================================================

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>抖库 - 抖音点赞分析报告</title>
<style>
:root {
  --bg: #0f0f0f; --card: #1a1a2e; --card2: #16213e; --border: #2a2a4a;
  --text: #e0e0e0; --text2: #888; --accent: #e94560; --accent2: #0f3460;
  --green: #4ecca3; --blue: #3282b8; --yellow: #f0c929; --purple: #9b59b6;
  --tag-bg: #2a2a4a; --tag-text: #bbb;
}
* { margin:0; padding:0; box-sizing:border-box; }
body { background:var(--bg); color:var(--text); font-family:'Segoe UI',system-ui,-apple-system,sans-serif; line-height:1.6; }
a { color:var(--blue); text-decoration:none; } a:hover { color:var(--accent); }

.header { background:linear-gradient(135deg,#0f3460,#1a1a2e); padding:30px 40px; border-bottom:1px solid var(--border); }
.header h1 { font-size:28px; color:#fff; margin-bottom:8px; }
.header .meta { color:var(--text2); font-size:14px; }
.stats-bar { display:flex; gap:24px; margin-top:16px; flex-wrap:wrap; }
.stat-item { background:var(--card); border-radius:8px; padding:12px 20px; min-width:120px; }
.stat-item .num { font-size:24px; font-weight:700; color:#fff; }
.stat-item .label { font-size:12px; color:var(--text2); }

.nav { background:var(--card); padding:0 40px; border-bottom:1px solid var(--border); display:flex; gap:0; }
.nav button { background:none; border:none; color:var(--text2); padding:14px 24px; cursor:pointer; font-size:15px; border-bottom:2px solid transparent; transition:all 0.2s; }
.nav button:hover { color:var(--text); }
.nav button.active { color:var(--accent); border-bottom-color:var(--accent); }

.content { padding:24px 40px; }
.section { display:none; }
.section.active { display:block; }

.track-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(200px,1fr)); gap:12px; margin-top:16px; }
.track-card { background:var(--card); border-radius:10px; padding:16px; cursor:pointer; border:1px solid var(--border); transition:all 0.2s; }
.track-card:hover { border-color:var(--accent); transform:translateY(-2px); }
.track-card .name { font-size:18px; font-weight:600; color:#fff; }
.track-card .info { font-size:13px; color:var(--text2); margin-top:4px; }
.track-card .bar { height:4px; border-radius:2px; background:var(--accent2); margin-top:8px; }
.track-card .bar-fill { height:100%; border-radius:2px; background:var(--accent); }

.author-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(340px,1fr)); gap:16px; margin-top:16px; }
.author-card { background:var(--card); border-radius:12px; padding:20px; border:1px solid var(--border); transition:all 0.2s; }
.author-card:hover { border-color:var(--accent); }
.author-card .top { display:flex; gap:12px; align-items:center; }
.author-card .info { flex:1; }
.author-card .nickname { font-size:16px; font-weight:600; color:#fff; }
.author-card .track-tag { display:inline-block; background:var(--accent); color:#fff; font-size:11px; padding:2px 8px; border-radius:10px; margin-left:6px; }
.author-card .track-tag2 { background:var(--purple); }
.author-card .nums { font-size:13px; color:var(--text2); margin-top:4px; }
.author-card .nums span { margin-right:12px; }
.author-card .tags { margin-top:8px; display:flex; flex-wrap:wrap; gap:4px; }
.tag-pill { background:var(--tag-bg); color:var(--tag-text); font-size:11px; padding:2px 8px; border-radius:10px; }
.tag-pill.accent { background:var(--accent); color:#fff; }
.author-card .sig { font-size:12px; color:var(--text2); margin-top:6px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:300px; }

.video-table { width:100%; border-collapse:collapse; margin-top:16px; font-size:13px; }
.video-table th { text-align:left; padding:10px 12px; border-bottom:2px solid var(--border); color:var(--text2); font-weight:600; position:sticky; top:0; background:var(--bg); cursor:pointer; }
.video-table th:hover { color:var(--text); }
.video-table td { padding:8px 12px; border-bottom:1px solid var(--border); max-width:300px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.video-table tr:hover { background:var(--card); }
.video-table .title { color:#fff; max-width:200px; }

.filter-bar { display:flex; gap:12px; margin-bottom:16px; flex-wrap:wrap; align-items:center; }
.filter-bar input, .filter-bar select { background:var(--card); color:var(--text); border:1px solid var(--border); border-radius:6px; padding:8px 12px; font-size:13px; }
.filter-bar input { width:200px; }
.filter-bar select { min-width:140px; }
.filter-bar .result-count { color:var(--text2); font-size:13px; margin-left:auto; }

.comments-list { margin-top:8px; }
.comment-item { background:var(--card2); border-radius:6px; padding:8px 12px; margin-bottom:6px; font-size:12px; }
.comment-item .user { color:var(--accent); font-weight:600; }
.comment-item .digg { color:var(--yellow); float:right; }
.comment-item .ip { color:var(--text2); font-size:11px; }

.modal-overlay { display:none; position:fixed; inset:0; background:rgba(0,0,0,0.7); z-index:100; }
.modal-overlay.show { display:flex; align-items:center; justify-content:center; }
.modal { background:var(--card); border-radius:12px; padding:24px; max-width:600px; width:90%; max-height:80vh; overflow-y:auto; }
.modal h3 { color:#fff; margin-bottom:12px; }
.modal .close { float:right; cursor:pointer; color:var(--text2); font-size:20px; }

.scroll-table { max-height:70vh; overflow-y:auto; }

.charts-grid { display:flex; gap:20px; margin-bottom:20px; }
.chart-card { background:var(--card); border-radius:12px; padding:20px; border:1px solid var(--border); flex:1; min-width:0; }
.chart-card-wide { flex:2; }
.chart-title { color:#fff; font-size:15px; font-weight:600; margin-bottom:8px; }
.chart-container { width:100%; height:380px; }

@media (max-width:900px) {
  .charts-grid { flex-direction:column; }
  .chart-card-wide { flex:1; }
  .chart-container { height:300px; }
}

@media (max-width:768px) {
  .content { padding:16px; }
  .header { padding:20px 16px; }
  .stats-bar { gap:8px; }
  .stat-item { min-width:80px; padding:8px 12px; }
  .stat-item .num { font-size:18px; }
  .author-grid { grid-template-columns:1fr; }
  .track-grid { grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); }
}

.author-avatar { width:48px; height:48px; border-radius:50%; object-fit:cover; border:2px solid var(--border); flex-shrink:0; background:var(--card2); }
.video-cover { width:72px; height:40px; object-fit:cover; border-radius:4px; display:block; cursor:pointer; transition:transform 0.15s; }
.video-cover:hover { transform:scale(1.8); z-index:2; position:relative; }
.video-table th.sortable { user-select:none; }
.video-table th.sortable .th-sort { margin-left:4px; font-size:10px; opacity:0.35; display:inline-block; width:12px; text-align:center; }
.video-table th.sortable .th-sort.asc { opacity:1; color:var(--accent); }
.video-table th.sortable .th-sort.desc { opacity:1; color:var(--accent); }
.video-table th.sortable:hover { color:var(--accent); }
.stat-item .num { transition:color 0.3s; }
.preview-image { width:100%; border-radius:8px; margin-bottom:12px; max-height:50vh; object-fit:contain; }
.modal .btn-row { display:flex; gap:10px; margin-top:16px; flex-wrap:wrap; }
.modal .video-link-btn { display:inline-block; padding:8px 20px; background:var(--accent); color:#fff; border-radius:6px; text-decoration:none; font-size:14px; }
.modal .video-link-btn:hover { opacity:0.85; }
.modal .modal-info { color:var(--text2); font-size:13px; margin-bottom:12px; }
.modal .modal-tags { display:flex; gap:6px; flex-wrap:wrap; margin-top:6px; }
.export-bar { display:flex; gap:6px; }
.export-btn { padding:6px 14px; border:1px solid var(--border); border-radius:6px; background:var(--card2); color:var(--text); cursor:pointer; font-size:13px; transition:all 0.2s; }
.export-btn:hover { background:var(--accent); color:#fff; border-color:var(--accent); }
</style>
<script src="https://cdn.jsdelivr.net/npm/countup.js@2.8.0/dist/countUp.umd.js"></script>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
</head>
<body>

<div class="header">
  <h1>抖库 v4 - 抖音点赞分析报告</h1>
  <div class="meta">生成时间：<span id="gen-time"></span></div>
  <div class="stats-bar" id="stats-bar"></div>
</div>

<div class="nav">
  <button class="active" data-sec="overview">概览</button>
  <button data-sec="tracks">赛道</button>
  <button data-sec="authors">UP主</button>
  <button data-sec="videos">视频</button>
  <button data-sec="analytics">数据分析</button>
</div>

<div class="content">
  <div class="section active" id="sec-overview">
    <h2 style="color:#fff;margin-bottom:16px;">赛道分布</h2>
    <div class="track-grid" id="overview-tracks"></div>
  </div>

  <div class="section" id="sec-tracks">
    <div class="filter-bar">
      <input type="text" id="track-search" placeholder="搜索赛道..." oninput="filterTracks()">
      <select id="track-sort" onchange="filterTracks()">
        <option value="author_count">按UP主数</option>
        <option value="video_count">按视频数</option>
        <option value="total_followers">按粉丝数</option>
        <option value="avg_digg">按均赞</option>
      </select>
      <span class="result-count" id="track-count"></span>
    </div>
    <div class="track-grid" id="track-grid"></div>
  </div>

  <div class="section" id="sec-authors">
    <div class="filter-bar">
      <input type="text" id="author-search" placeholder="搜索UP主..." oninput="filterAuthors()">
      <select id="author-track-filter" onchange="filterAuthors()">
        <option value="">全部赛道</option>
      </select>
      <select id="author-sort" onchange="filterAuthors()">
        <option value="video_count">按被赞数</option>
        <option value="follower_count">按粉丝数</option>
        <option value="avg_digg">按均赞</option>
        <option value="avg_duration">按时长</option>
      </select>
      <span class="result-count" id="author-count"></span>
      <div class="export-bar">
        <button class="export-btn" onclick="exportCSV('authors')">CSV</button>
        <button class="export-btn" onclick="exportJSON('authors')">JSON</button>
      </div>
    </div>
    <div class="author-grid" id="author-grid"></div>
  </div>

  <div class="section" id="sec-videos">
    <div class="filter-bar">
      <input type="text" id="video-search" placeholder="搜索标题/描述..." oninput="filterVideos()">
      <select id="video-track-filter" onchange="filterVideos()">
        <option value="">全部赛道</option>
      </select>
      <select id="video-source-filter" onchange="filterVideos()">
        <option value="">点赞+收藏</option>
        <option value="likes">仅点赞</option>
        <option value="favorites">仅收藏</option>
      </select>
      <select id="video-sort" onchange="filterVideos()">
        <option value="digg">按点赞</option>
        <option value="duration">按时长</option>
        <option value="time">按时间</option>
      </select>
      <span class="result-count" id="video-count"></span>
      <div class="export-bar">
        <button class="export-btn" onclick="exportCSV('videos')">CSV</button>
        <button class="export-btn" onclick="exportJSON('videos')">JSON</button>
      </div>
    </div>
    <div class="scroll-table">
      <table class="video-table">
        <thead>
          <tr>
            <th>封面</th>
            <th class="sortable" data-sort="digg" onclick="sortByField('digg')">赞 <span class="th-sort"></span></th>
            <th>标题</th>
            <th>UP主</th>
            <th>赛道</th>
            <th class="sortable" data-sort="duration" onclick="sortByField('duration')">时长 <span class="th-sort"></span></th>
            <th class="sortable" data-sort="time" onclick="sortByField('time')">时间 <span class="th-sort"></span></th>
            <th>标签</th>
            <th>评论标签</th>
          </tr>
        </thead>
        <tbody id="video-tbody"></tbody>
      </table>
    </div>
  </div>

  <div class="section" id="sec-analytics">
    <div class="charts-grid">
      <div class="chart-card chart-card-wide">
        <h3 class="chart-title">赛道分布</h3>
        <div id="chart-track-pie" class="chart-container"></div>
      </div>
      <div class="chart-card">
        <h3 class="chart-title">UP主粉丝量 Top 15</h3>
        <div id="chart-author-followers" class="chart-container"></div>
      </div>
    </div>
    <div class="charts-grid">
      <div class="chart-card chart-card-wide">
        <h3 class="chart-title">视频点赞量分布</h3>
        <div id="chart-likes-dist" class="chart-container"></div>
      </div>
      <div class="chart-card">
        <h3 class="chart-title">赛道UP主数 vs 视频数</h3>
        <div id="chart-track-bar" class="chart-container"></div>
      </div>
    </div>
  </div>

</div>

<div class="modal-overlay" id="comment-modal" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <span class="close" onclick="closeModal()">&times;</span>
    <h3 id="modal-title">评论</h3>
    <div class="comments-list" id="modal-comments"></div>
  </div>
</div>

<div class="modal-overlay" id="video-preview-modal" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <span class="close" onclick="closeModal()">&times;</span>
    <img class="preview-image" id="preview-img" src="" alt="">
    <h3 id="preview-title"></h3>
    <div class="modal-info" id="preview-meta"></div>
    <div class="btn-row">
      <a class="video-link-btn" id="preview-video-link" href="#" target="_blank" rel="noopener">打开视频</a>
    </div>
  </div>
</div>

<script>
const STATS = __STATS__;
const TRACKS = __TRACKS__;
const AUTHORS = __AUTHORS__;
const VIDEOS = __VIDEOS__;
const COMMENTS = __COMMENTS__;
const MAX_AUTHORS = 200;
const MAX_VIDEOS = 500;

function fmtCount(n) {
  if (!n) return '0';
  if (n >= 10000) return (n/10000).toFixed(1) + 'w';
  if (n >= 1000) return (n/1000).toFixed(1) + 'k';
  return String(n);
}

function fmtDuration(s) {
  if (!s) return '-';
  var m = Math.floor(s/60);
  var sec = s % 60;
  return m > 0 ? m + ':' + String(sec).padStart(2,'0') : sec + 's';
}

function getDigg(v) {
  try { return JSON.parse(v.stats || '{}').digg || 0; } catch { return 0; }
}

document.getElementById('gen-time').textContent = STATS.generated_at;
var statsBar = document.getElementById('stats-bar');
var statsArr = [
  ['video', STATS.video_count],
  ['UP', STATS.author_count],
  ['likes', STATS.likes_count],
  ['favs', STATS.favorites_count],
  ['cmts', STATS.comment_count],
  ['track', STATS.tracks_count],
];
statsBar.innerHTML = statsArr.map(function(item) {
  return '<div class="stat-item"><div class="num kpi-num" data-value="' + item[1] + '">0</div><div class="label">' + item[0] + '</div></div>';
}).join('');

if (typeof CountUp !== 'undefined') {
  document.querySelectorAll('.kpi-num').forEach(function(el) {
    var endVal = parseInt(el.dataset.value) || 0;
    if (endVal === 0) { el.textContent = '0'; return; }
    var cu = new CountUp(el, endVal, { startVal: 0, duration: 1.8, useEasing: true, separator: '' });
    if (!cu.error) cu.start();
  });
}

document.querySelectorAll('.nav button').forEach(function(btn) {
  btn.addEventListener('click', function() {
    document.querySelectorAll('.section').forEach(function(s) { s.classList.remove('active'); });
    document.querySelectorAll('.nav button').forEach(function(b) { b.classList.remove('active'); });
    document.getElementById('sec-' + btn.dataset.sec).classList.add('active');
    btn.classList.add('active');
    if (btn.dataset.sec === 'authors') filterAuthors();
    if (btn.dataset.sec === 'videos') filterVideos();
  });
});

function renderTrackCard(t, maxCount) {
  var pct = maxCount > 0 ? (t.author_count / maxCount * 100) : 0;
  return '<div class="track-card" data-track="' + t.track + '">' +
    '<div class="name">' + t.track + '</div>' +
    '<div class="info">' + fmtCount(t.author_count) + ' UP · ' + fmtCount(t.video_count) + ' vids · ' + fmtCount(t.total_followers) + ' fans</div>' +
    '<div class="bar"><div class="bar-fill" style="width:' + pct + '%"></div></div></div>';
}

var maxTrackCount = Math.max.apply(null, TRACKS.map(function(t) { return t.author_count; }));
document.getElementById('overview-tracks').innerHTML = TRACKS.map(function(t) { return renderTrackCard(t, maxTrackCount); }).join('');

document.addEventListener('click', function(e) {
  var card = e.target.closest('.track-card');
  if (card && card.dataset.track) {
    document.querySelectorAll('.section').forEach(function(s) { s.classList.remove('active'); });
    document.querySelectorAll('.nav button').forEach(function(b) { b.classList.remove('active'); });
    document.getElementById('sec-videos').classList.add('active');
    document.querySelector('.nav button[data-sec="videos"]').classList.add('active');
    document.getElementById('video-track-filter').value = card.dataset.track;
    filterVideos();
    document.getElementById('author-track-filter').value = card.dataset.track;
  }
});

function filterTracks() {
  var q = document.getElementById('track-search').value.toLowerCase();
  var sortBy = document.getElementById('track-sort').value;
  var filtered = TRACKS.filter(function(t) { return t.track.toLowerCase().includes(q); });
  filtered.sort(function(a, b) { return (b[sortBy] || 0) - (a[sortBy] || 0); });
  var mx = filtered.length > 0 ? Math.max.apply(null, filtered.map(function(t) { return t.author_count; })) : 1;
  document.getElementById('track-grid').innerHTML = filtered.map(function(t) { return renderTrackCard(t, mx); }).join('');
  document.getElementById('track-count').textContent = filtered.length + ' tracks';
}
filterTracks();

var trackOpts = TRACKS.map(function(t) { return '<option value="' + t.track + '">' + t.track + '</option>'; }).join('');
document.getElementById('author-track-filter').innerHTML = '<option value="">All</option>' + trackOpts;
document.getElementById('video-track-filter').innerHTML = '<option value="">All</option>' + trackOpts;

var videoSortField = null;
var videoSortOrder = 'desc';

function sortByField(field) {
  if (videoSortField === field) {
    videoSortOrder = videoSortOrder === 'asc' ? 'desc' : 'asc';
  } else {
    videoSortField = field;
    videoSortOrder = 'asc';
  }
  updateSortIndicators();
  filterVideos();
}

function updateSortIndicators() {
  document.querySelectorAll('.video-table th.sortable .th-sort').forEach(function(el) {
    el.textContent = '';
    el.className = 'th-sort';
  });
  if (videoSortField) {
    var el = document.querySelector('.video-table th.sortable[data-sort="' + videoSortField + '"] .th-sort');
    if (el) {
      el.textContent = videoSortOrder === 'asc' ? '^' : 'v';
      el.className = 'th-sort ' + videoSortOrder;
    }
  }
}

function filterAuthors() {
  var q = document.getElementById('author-search').value.toLowerCase();
  var trackFilter = document.getElementById('author-track-filter').value;
  var sortBy = document.getElementById('author-sort').value;

  var filtered = AUTHORS.filter(function(a) {
    if (q && !a.nickname.toLowerCase().includes(q) && !(a.signature||'').toLowerCase().includes(q)) return false;
    if (trackFilter && a.portrait_track !== trackFilter && a.portrait_track_2 !== trackFilter) return false;
    return true;
  });
  filtered.sort(function(a, b) { return (b[sortBy] || 0) - (a[sortBy] || 0); });
  filtered = filtered.slice(0, MAX_AUTHORS);

  document.getElementById('author-grid').innerHTML = filtered.map(function(a) {
    var tags = [];
    try { tags = JSON.parse(a.portrait_tags || '[]'); } catch {}
    var tagHtml = tags.slice(0, 4).map(function(t) { return '<span class="tag-pill">' + t.tag + '(' + t.pct + '%)</span>'; }).join('');
    var track2 = a.portrait_track_2 ? '<span class="track-tag track-tag2">' + a.portrait_track_2 + '</span>' : '';
    var verify = a.verification_type > 0 ? ' verified' : '';
    var sig = a.signature ? '<div class="sig">' + a.signature + '</div>' : '';
    return '<div class="author-card"><div class="top">' +
      '<img class="author-avatar" src="' + (a.avatar||'') + '" alt="" onerror="this.style.display=\\'none\\'" loading="lazy">' +
      '<div class="info">' +
      '<div class="nickname">' + a.nickname + verify + ' <span class="track-tag">' + a.portrait_track + '</span>' + track2 + '</div>' +
      '<div class="nums"><span>fans' + fmtCount(a.follower_count) + '</span><span>vids' + a.video_count + '</span><span>avg' + fmtCount(a.avg_digg) + '</span><span>dur' + Math.round(a.avg_duration) + 's</span></div>' +
      '</div></div><div class="tags">' + tagHtml + '</div>' + sig + '</div>';
  }).join('');
  document.getElementById('author-count').textContent = filtered.length + ' UP';
}
filterAuthors();

function filterVideos() {
  var q = document.getElementById('video-search').value.toLowerCase();
  var trackFilter = document.getElementById('video-track-filter').value;
  var sourceFilter = document.getElementById('video-source-filter').value;

  var filtered = VIDEOS.filter(function(v) {
    if (q && !(v.title||'').toLowerCase().includes(q) && !v.author_name.toLowerCase().includes(q)) return false;
    if (trackFilter && v.author_track !== trackFilter && v.content_category !== trackFilter) return false;
    if (sourceFilter === 'likes' && !v.in_likes) return false;
    if (sourceFilter === 'favorites' && !v.in_favorites) return false;
    return true;
  });

  if (videoSortField) {
    filtered.sort(function(a, b) {
      var va, vb, cmp = 0;
      if (videoSortField === 'digg') { va = getDigg(a); vb = getDigg(b); cmp = va - vb; }
      else if (videoSortField === 'duration') { va = a.duration_sec || 0; vb = b.duration_sec || 0; cmp = va - vb; }
      else if (videoSortField === 'time') { cmp = (a.create_time || '').localeCompare(b.create_time || ''); }
      return videoSortOrder === 'asc' ? cmp : -cmp;
    });
  } else {
    filtered.sort(function(a, b) { return getDigg(b) - getDigg(a); });
  }
  filtered = filtered.slice(0, MAX_VIDEOS);

  document.getElementById('video-tbody').innerHTML = filtered.map(function(v) {
    var digg = getDigg(v);
    var vtags = []; try { vtags = JSON.parse(v.video_tags || '[]'); } catch {}
    var ctags = []; try { ctags = JSON.parse(v.comment_tags || '[]'); } catch {}
    var vtagHtml = vtags.slice(0, 3).map(function(t) { return '<span class="tag-pill">' + (t.tag_name||t.tag||'') + '</span>'; }).join('');
    var ctagHtml = ctags.slice(0, 4).map(function(t) { return '<span class="tag-pill' + (t.source==='sentiment'?' accent':'') + '">' + t.tag + '</span>'; }).join('');
    var hasComments = COMMENTS[v.aweme_id] && COMMENTS[v.aweme_id].length > 0;
    var commentBtn = hasComments ? ' <a href="#" onclick="showComments(\\'' + v.aweme_id + '\\',\\'' + (v.title||'').substring(0,30).replace(/'/g,"") + '\\');return false">' + COMMENTS[v.aweme_id].length + '</a>' : '';
    var track = v.author_track ? '<span class="tag-pill accent">' + v.author_track + '</span>' : '';
    return '<tr>' +
      '<td><img class="video-cover" src="' + (v.cover_url||'') + '" alt="" onerror="this.hidden=true" loading="lazy" data-cover="' + (v.cover_url||'') + '" data-video="' + (v.video_url||'') + '" data-title="' + (v.title||'').replace(/"/g,'&amp;quot;').substring(0,80) + '" data-author="' + v.author_name + '" data-duration="' + fmtDuration(v.duration_sec) + '" data-likes="' + fmtCount(digg) + '" data-downloaded="' + (v.is_downloaded||0) + '"></td>' +
      '<td>' + fmtCount(digg) + '</td>' +
      '<td class="title">' + (v.title || '-').substring(0,40) + '</td>' +
      '<td>' + v.author_name + '</td>' +
      '<td>' + track + '</td>' +
      '<td>' + fmtDuration(v.duration_sec) + '</td>' +
      '<td>' + (v.create_time||'-') + '</td>' +
      '<td>' + vtagHtml + '</td>' +
      '<td>' + ctagHtml + commentBtn + '</td>' +
      '</tr>';
  }).join('');
  document.getElementById('video-count').textContent = filtered.length + ' vids';
}
filterVideos();

function showComments(awemeId, title) {
  var comments = COMMENTS[awemeId] || [];
  document.getElementById('modal-title').textContent = title;
  document.getElementById('modal-comments').innerHTML = comments.slice(0, 20).map(function(c) {
    var ip = c.ip_location ? ' <span style="color:var(--text2);font-size:11px">' + c.ip_location + '</span>' : '';
    return '<div class="comment-item"><span class="digg">' + c.digg_count + '</span><span class="user">' + c.user_name + '</span>' + ip + '<div>' + c.content + '</div></div>';
  }).join('');
  document.getElementById('comment-modal').classList.add('show');
}

function closeModal() {
  document.getElementById('comment-modal').classList.remove('show');
  document.getElementById('video-preview-modal').classList.remove('show');
}

function openPreview(cover, video, title, author, duration, likes, isDownloaded) {
  var img = document.getElementById('preview-img');
  img.src = cover;
  img.onerror = function() { this.style.display = 'none'; };
  img.style.display = '';
  document.getElementById('preview-title').textContent = title;
  document.getElementById('preview-meta').textContent = author + ' | ' + duration + ' | ' + likes;
  
  var linkBtn = document.getElementById('preview-video-link');
  var btnRow = linkBtn.parentElement;
  var existingWarn = document.getElementById('preview-no-download-warn');
  if (existingWarn) existingWarn.remove();
  
  if (isDownloaded == 1) {
    linkBtn.href = video;
    linkBtn.style.display = '';
    linkBtn.textContent = 'Open Video';
  } else {
    linkBtn.href = '#';
    var warn = document.createElement('div');
    warn.id = 'preview-no-download-warn';
    warn.style.cssText = 'color:#f0c929;font-size:13px;padding:8px 0;';
    warn.textContent = 'not downloaded locally, may be 403';
    btnRow.insertBefore(warn, linkBtn);
    linkBtn.style.display = 'none';
  }
  document.getElementById('video-preview-modal').classList.add('show');
}

document.getElementById('video-tbody').addEventListener('click', function(e) {
  var img = e.target.closest('.video-cover');
  if (!img || !img.dataset.cover) return;
  openPreview(img.dataset.cover, img.dataset.video, img.dataset.title, img.dataset.author, img.dataset.duration, img.dataset.likes, img.dataset.downloaded);
});

function exportCSV(type) {
  var csv, filename;
  if (type === 'videos') {
    csv = 'title,author,digg,duration,time,url,track,tags\n';
    VIDEOS.forEach(function(v) {
      var st = {}; try { st = JSON.parse(v.stats || '{}'); } catch(e) {}
      var tg = []; try { tg = JSON.parse(v.video_tags || '[]'); } catch(e) {}
      csv += '"' + (v.title||'').replace(/"/g,'""') + '","' + v.author_name + '",' + (st.digg||0) + ',' + (v.duration_sec||0) + ',"' + (v.create_time||'') + '","' + (v.video_url||'') + '","' + (v.author_track||'') + '","' + tg.map(function(t){return (t.tag_name||t.tag||'');}).join(';') + '"\n';
    });
    filename = 'douku_videos.csv';
  } else {
    csv = 'name,fans,vids,track,track2,tags\n';
    AUTHORS.forEach(function(a) {
      csv += '"' + a.nickname + '",' + (a.follower_count||0) + ',' + (a.video_count||0) + ',"' + (a.portrait_track||'') + '","' + (a.portrait_track_2||'') + '","' + (a.portrait_tags||'') + '"\n';
    });
    filename = 'douku_authors.csv';
  }
  var blob = new Blob(['\ufeff' + csv], {type: 'text/csv;charset=utf-8;'});
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

function exportJSON(type) {
  var data, filename;
  if (type === 'videos') { data = VIDEOS; filename = 'douku_videos.json'; }
  else { data = AUTHORS; filename = 'douku_authors.json'; }
  var blob = new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'});
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

var chartsRendered = false;
document.querySelectorAll('.nav button').forEach(function(btn) {
  btn.addEventListener('click', function() {
    if (btn.dataset.sec === 'analytics' && !chartsRendered) {
      chartsRendered = true;
      showChartLoaders();
      setTimeout(renderCharts, 250);
    }
  });
});

function showChartLoaders() {
  var ids = ['chart-track-pie', 'chart-author-followers', 'chart-likes-dist', 'chart-track-bar'];
  ids.forEach(function(id) {
    var dom = document.getElementById(id);
    if (dom) {
      var inst = echarts.init(dom);
      inst.showLoading({ text: 'loading...', color: '#e94560', textColor: '#888', maskColor: 'rgba(15, 15, 15, 0.85)' });
    }
  });
}

function renderCharts() {
  renderTrackPie();
  renderAuthorFollowers();
  renderLikesDist();
  renderTrackBar();
}

function renderTrackPie() {
  var c = echarts.init(document.getElementById('chart-track-pie'));
  var data;
  var tracksFromDb = TRACKS.filter(function(t) { return t.author_count > 0; });
  if (tracksFromDb.length > 0) {
    data = tracksFromDb.map(function(t) { return { name: t.track, value: t.author_count }; });
  } else {
    var catMap = {};
    VIDEOS.forEach(function(v) {
      var cat = v.content_category || v.author_track || '?';
      catMap[cat] = (catMap[cat] || 0) + 1;
    });
    data = Object.keys(catMap).map(function(k) { return { name: k, value: catMap[k] }; });
  }
  c.setOption({
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    series: [{
      type: 'pie', radius: ['42%', '72%'], center: ['50%', '55%'],
      data: data,
      label: { color: '#aaa', fontSize: 11 },
      itemStyle: { borderRadius: 4, borderColor: '#0f0f0f', borderWidth: 2 }
    }]
  });
  c.hideLoading();
  window.addEventListener('resize', function() { c.resize(); });
}

function renderAuthorFollowers() {
  var c = echarts.init(document.getElementById('chart-author-followers'));
  var top = AUTHORS.slice().sort(function(a,b) { return (b.follower_count||0) - (a.follower_count||0); }).slice(0, 15).reverse();
  c.setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' },
      formatter: function(p) { return p[0].name + '<br/>fans: ' + fmtCount(p[0].value); }
    },
    grid: { left: '14%', right: '8%', top: 8, bottom: 16 },
    xAxis: { type: 'value', axisLabel: { color: '#888', fontSize: 10, formatter: function(v) { return v>=10000?(v/10000).toFixed(1)+'w':v; } } },
    yAxis: { type: 'category', data: top.map(function(a) { return a.nickname; }), axisLabel: { color: '#bbb', fontSize: 10, width: 80, overflow: 'truncate' } },
    series: [{
      type: 'bar', data: top.map(function(a) { return a.follower_count||0; }),
      itemStyle: { color: new echarts.graphic.LinearGradient(0,0,1,0, [{offset:0,color:'#0f3460'},{offset:1,color:'#e94560'}]) },
      barMaxWidth: 18
    }]
  });
  c.hideLoading();
  window.addEventListener('resize', function() { c.resize(); });
}

function renderLikesDist() {
  var c = echarts.init(document.getElementById('chart-likes-dist'));
  var likes = VIDEOS.map(function(v) { return getDigg(v); }).filter(function(v) { return v > 0; });
  var maxL = Math.max.apply(null, likes);
  var n = 12, sz = Math.ceil(maxL / n);
  var bins = {};
  for (var i = 0; i < n; i++) bins[i] = 0;
  likes.forEach(function(l) {
    var idx = Math.min(Math.floor(l / sz), n - 1);
    bins[idx]++;
  });
  var cats = [];
  for (var i = 0; i < n; i++) {
    var lo = i * sz, hi = lo + sz;
    cats.push((lo>=10000?(lo/10000).toFixed(0)+'w':lo) + '-' + (hi>=10000?(hi/10000).toFixed(0)+'w':hi));
  }
  c.setOption({
    tooltip: { trigger: 'axis' },
    grid: { left: '8%', right: '6%', top: 16, bottom: 40 },
    xAxis: { type: 'category', data: cats, axisLabel: { color: '#888', fontSize: 10, rotate: 30 } },
    yAxis: { type: 'value', name: 'vids', nameTextStyle: { color: '#888' }, axisLabel: { color: '#888' } },
    series: [{
      type: 'bar',
      data: Object.keys(bins).map(function(i) { return bins[i]; }),
      itemStyle: { color: new echarts.graphic.LinearGradient(0,0,0,1, [{offset:0,color:'#e94560'},{offset:1,color:'#0f3460'}]) },
      barMaxWidth: 40
    }]
  });
  c.hideLoading();
  window.addEventListener('resize', function() { c.resize(); });
}

function renderTrackBar() {
  var c = echarts.init(document.getElementById('chart-track-bar'));
  var top;
  var tracksFromDb = TRACKS.filter(function(t) { return t.author_count > 0; });
  if (tracksFromDb.length > 0) {
    top = tracksFromDb.sort(function(a,b) { return (b.author_count||0) - (a.author_count||0); }).slice(0, 12);
    top = top.map(function(t) { return { track: t.track, author_count: t.author_count, video_count: t.video_count }; });
  } else {
    var catMap = {};
    VIDEOS.forEach(function(v) {
      var cat = v.content_category || v.author_track || '?';
      if (!catMap[cat]) catMap[cat] = { video_count: 0, authors: {} };
      catMap[cat].video_count++;
      catMap[cat].authors[v.author_name] = 1;
    });
    top = Object.keys(catMap).map(function(k) {
      return { track: k, video_count: catMap[k].video_count, author_count: Object.keys(catMap[k].authors).length };
    }).sort(function(a,b) { return b.video_count - a.video_count; }).slice(0, 12);
  }
  top = top.reverse();
  c.setOption({
    tooltip: { trigger: 'axis' },
    legend: { data: ['UP','vids'], textStyle: { color: '#888', fontSize: 12 }, top: 0 },
    grid: { left: '12%', right: '8%', top: 36, bottom: 16 },
    xAxis: { type: 'category', data: top.map(function(t) { return t.track; }), axisLabel: { color: '#bbb', fontSize: 11 } },
    yAxis: { type: 'value', axisLabel: { color: '#888', fontSize: 10 } },
    series: [
      { name: 'UP', type: 'bar', data: top.map(function(t) { return t.author_count; }), itemStyle: { color: '#e94560' }, barMaxWidth: 16, barGap: '30%' },
      { name: 'vids', type: 'bar', data: top.map(function(t) { return t.video_count; }), itemStyle: { color: '#3282b8' }, barMaxWidth: 16 }
    ]
  });
  c.hideLoading();
  window.addEventListener('resize', function() { c.resize(); });
}

</script>
</body>
</html>"""


def build_html(data):
    """替换模板占位符"""
    html = HTML_TEMPLATE
    html = html.replace("__STATS__", json.dumps(data["stats"], ensure_ascii=False))
    html = html.replace("__TRACKS__", json.dumps(data["tracks"], ensure_ascii=False))
    html = html.replace("__AUTHORS__", json.dumps(data["authors"], ensure_ascii=False))
    html = html.replace("__VIDEOS__", json.dumps(data["videos"], ensure_ascii=False))
    html = html.replace("__COMMENTS__", json.dumps(data["comments"], ensure_ascii=False))
    return html


def generate_report(output_path=None, include_comments=True):
    """生成报告 - v4 多表数据源

    Returns:
        (output_path, stats_dict)
    """
    if output_path is None:
        output_path = DEFAULT_OUTPUT

    db_path = os.path.join(str(get_data_root()), 'douku_v4.db')
    init_db_v4(db_path)
    conn = get_conn_v4(db_path)

    data = load_data(conn, include_comments=include_comments)
    html = build_html(data)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)

    conn.close()
    return output_path, data["stats"]


def run_from_cli(args):
    """供 dytool_v4.py CLI 调用"""
    output_path = getattr(args, 'output', None)
    no_comments = getattr(args, 'no_comments', False)

    if output_path is None:
        output_path = os.path.join(str(get_data_root()), 'output', 'report.html')

    status = getattr(args, 'status', False)
    if status:
        db_path = os.path.join(str(get_data_root()), 'douku_v4.db')
        init_db_v4(db_path)
        conn = get_conn_v4(db_path)
        data = load_data(conn, include_comments=not no_comments)
        print("=" * 50)
        print("报告数据预览 (v4)")
        print("=" * 50)
        print(f"  视频: {data['stats']['video_count']}")
        print(f"  UP主: {data['stats']['author_count']}")
        print(f"  赛道: {data['stats']['tracks_count']}")
        print(f"  评论: {data['stats']['comment_count']}")
        print(f"  已下载: {data['stats']['downloaded_count']}")
        conn.close()
        return

    print("正在从 v4 多表加载数据...")
    path, stats = generate_report(output_path=output_path, include_comments=not no_comments)
    size_kb = os.path.getsize(path) / 1024
    print(f"\nv4 报告已生成: {path} ({size_kb:.0f} KB)")
    print(f"  视频: {stats['video_count']}  |  UP主: {stats['author_count']}")
    print(f"  赛道: {stats['tracks_count']}  |  评论: {stats['comment_count']}")


# 兼容旧版调用方式
def main():
    import sys
    import io as _io
    if sys.platform == 'win32':
        sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

    print("生成 v4 报告...")
    path, stats = generate_report()
    size_kb = os.path.getsize(path) / 1024
    print(f"OK {path} ({size_kb:.0f} KB)")

if __name__ == "__main__":
    main()