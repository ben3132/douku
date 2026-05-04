"""
分类呈现 - 生成可交互的静态 HTML 报告
浏览器打开即可筛选/分组/搜索，无需服务器

用法:
  python generate_report.py                  # 生成完整报告
  python generate_report.py --output my.html # 指定输出文件
  python generate_report.py --no-comments    # 不含评论数据(更小)
"""

import os
import sys
import json
import argparse
import io
from collections import Counter
from datetime import datetime

if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db_utils import init_db, get_conn

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'likes.db')
DEFAULT_OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'output', 'report.html')


def format_count(n):
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    elif n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def load_data(conn, include_comments=True):
    """从数据库加载所有数据"""
    tracks = [dict(r) for r in conn.execute("""
        SELECT portrait_track as track, COUNT(*) as author_count,
               SUM(video_count) as video_count,
               SUM(follower_count) as total_followers,
               AVG(avg_digg) as avg_digg
        FROM authors WHERE portrait_track != ''
        GROUP BY portrait_track
        ORDER BY author_count DESC
    """).fetchall()]

    authors = [dict(r) for r in conn.execute("""
        SELECT id, sec_uid, nickname, avatar, signature, ip_location,
               follower_count, following_count, aweme_count, favoriting_count,
               verification_type, verification_label,
               portrait_track, portrait_track_2, video_count, avg_digg, avg_duration,
               portrait_tags
        FROM authors
        WHERE portrait_track != ''
        ORDER BY video_count DESC, follower_count DESC
    """).fetchall()]

    videos = [dict(r) for r in conn.execute("""
        SELECT v.aweme_id, v.title, v.desc, v.duration_sec, v.create_time,
               v.video_tags, v.hashtags, v.stats, v.video_url, v.cover_url,
               v.in_likes, v.in_favorites, v.is_downloaded,
               v.comment_tags, v.comment_fetched,
               a.nickname as author_name, a.portrait_track as author_track,
               a.follower_count as author_followers, a.sec_uid
        FROM videos v
        JOIN authors a ON v.author_id = a.id
        ORDER BY json_extract(v.stats, '$.digg') DESC
    """).fetchall()]

    comments_by_video = {}
    if include_comments:
        rows = conn.execute("""
            SELECT aweme_id, cid, content, user_name, digg_count, is_hot, ip_location
            FROM comments ORDER BY digg_count DESC
        """).fetchall()
        for r in rows:
            aid = r["aweme_id"]
            if aid not in comments_by_video:
                comments_by_video[aid] = []
            comments_by_video[aid].append(dict(r))

    stats = {
        "video_count": conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0],
        "author_count": conn.execute("SELECT COUNT(*) FROM authors").fetchone()[0],
        "likes_count": conn.execute("SELECT COUNT(*) FROM videos WHERE in_likes=1").fetchone()[0],
        "favorites_count": conn.execute("SELECT COUNT(*) FROM videos WHERE in_favorites=1").fetchone()[0],
        "downloaded_count": conn.execute("SELECT COUNT(*) FROM videos WHERE is_downloaded=1").fetchone()[0],
        "comment_count": conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0],
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


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>抖音点赞分析报告</title>
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

@media (max-width:768px) {
  .content { padding:16px; }
  .header { padding:20px 16px; }
  .stats-bar { gap:8px; }
  .stat-item { min-width:80px; padding:8px 12px; }
  .stat-item .num { font-size:18px; }
  .author-grid { grid-template-columns:1fr; }
  .track-grid { grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); }
}
</style>
</head>
<body>

<div class="header">
  <h1>📊 抖音点赞分析报告</h1>
  <div class="meta">生成时间：<span id="gen-time"></span></div>
  <div class="stats-bar" id="stats-bar"></div>
</div>

<div class="nav">
  <button class="active" data-sec="overview">📊 概览</button>
  <button data-sec="tracks">🏷️ 赛道</button>
  <button data-sec="authors">👤 UP主</button>
  <button data-sec="videos">🎬 视频</button>
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
    </div>
    <div class="scroll-table">
      <table class="video-table">
        <thead>
          <tr>
            <th>赞</th>
            <th>标题</th>
            <th>UP主</th>
            <th>赛道</th>
            <th>时长</th>
            <th>标签</th>
            <th>评论标签</th>
          </tr>
        </thead>
        <tbody id="video-tbody"></tbody>
      </table>
    </div>
  </div>
</div>

<div class="modal-overlay" id="comment-modal" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <span class="close" onclick="closeModal()">✕</span>
    <h3 id="modal-title">评论详情</h3>
    <div class="comments-list" id="modal-comments"></div>
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
  if (n >= 10000) return (n/10000).toFixed(1) + '万';
  if (n >= 1000) return (n/1000).toFixed(1) + 'k';
  return String(n);
}

function fmtDuration(s) {
  if (!s) return '-';
  const m = Math.floor(s/60);
  const sec = s % 60;
  return m > 0 ? m + ':' + String(sec).padStart(2,'0') : sec + 's';
}

function getDigg(v) {
  try { return JSON.parse(v.stats || '{}').digg || 0; } catch { return 0; }
}

// Init stats bar
document.getElementById('gen-time').textContent = STATS.generated_at;
const statsBar = document.getElementById('stats-bar');
[
  ['🎬 视频', STATS.video_count],
  ['👤 UP主', STATS.author_count],
  ['❤️ 点赞', STATS.likes_count],
  ['⭐ 收藏', STATS.favorites_count],
  ['💬 评论', STATS.comment_count],
  ['🏷️ 赛道', STATS.tracks_count],
].forEach(function(item) {
  statsBar.innerHTML += '<div class="stat-item"><div class="num">' + fmtCount(item[1]) + '</div><div class="label">' + item[0] + '</div></div>';
});

// Navigation
document.querySelectorAll('.nav button').forEach(function(btn) {
  btn.addEventListener('click', function() {
    document.querySelectorAll('.section').forEach(function(s) { s.classList.remove('active'); });
    document.querySelectorAll('.nav button').forEach(function(b) { b.classList.remove('active'); });
    document.getElementById('sec-' + btn.dataset.sec).classList.add('active');
    btn.classList.add('active');
    if (btn.dataset.sec === 'authors') renderAuthors();
    if (btn.dataset.sec === 'videos') renderVideos();
  });
});

// Track grid
function renderTrackCard(t, maxCount) {
  const pct = maxCount > 0 ? (t.author_count / maxCount * 100) : 0;
  return '<div class="track-card" data-track="' + t.track + '">' +
    '<div class="name">' + t.track + '</div>' +
    '<div class="info">' + fmtCount(t.author_count) + ' UP主 · ' + fmtCount(t.video_count) + ' 视频 · ' + fmtCount(t.total_followers) + ' 粉丝</div>' +
    '<div class="bar"><div class="bar-fill" style="width:' + pct + '%"></div></div></div>';
}

const maxTrackCount = Math.max(...TRACKS.map(function(t) { return t.author_count; }));
document.getElementById('overview-tracks').innerHTML = TRACKS.map(function(t) { return renderTrackCard(t, maxTrackCount); }).join('');

// Track click → filter
document.addEventListener('click', function(e) {
  const card = e.target.closest('.track-card');
  if (card && card.dataset.track) {
    filterByTrack(card.dataset.track);
  }
});

function filterByTrack(track) {
  // Switch to authors tab and filter
  document.querySelectorAll('.section').forEach(function(s) { s.classList.remove('active'); });
  document.querySelectorAll('.nav button').forEach(function(b) { b.classList.remove('active'); });
  document.getElementById('sec-authors').classList.add('active');
  document.querySelector('.nav button[data-sec="authors"]').classList.add('active');
  document.getElementById('author-track-filter').value = track;
  filterAuthors();
  // Also set video filter
  document.getElementById('video-track-filter').value = track;
}

// Tracks tab
function filterTracks() {
  const q = document.getElementById('track-search').value.toLowerCase();
  const sortBy = document.getElementById('track-sort').value;
  let filtered = TRACKS.filter(function(t) { return t.track.toLowerCase().includes(q); });
  filtered.sort(function(a, b) { return (b[sortBy] || 0) - (a[sortBy] || 0); });
  const mx = filtered.length > 0 ? Math.max(...filtered.map(function(t) { return t.author_count; })) : 1;
  document.getElementById('track-grid').innerHTML = filtered.map(function(t) { return renderTrackCard(t, mx); }).join('');
  document.getElementById('track-count').textContent = filtered.length + ' 个赛道';
}
filterTracks();

// Populate track filters
const trackOpts = TRACKS.map(function(t) { return '<option value="' + t.track + '">' + t.track + '</option>'; }).join('');
document.getElementById('author-track-filter').innerHTML = '<option value="">全部赛道</option>' + trackOpts;
document.getElementById('video-track-filter').innerHTML = '<option value="">全部赛道</option>' + trackOpts;

// Authors tab
function filterAuthors() {
  const q = document.getElementById('author-search').value.toLowerCase();
  const trackFilter = document.getElementById('author-track-filter').value;
  const sortBy = document.getElementById('author-sort').value;

  let filtered = AUTHORS.filter(function(a) {
    if (q && !a.nickname.toLowerCase().includes(q) && !(a.signature||'').toLowerCase().includes(q)) return false;
    if (trackFilter && a.portrait_track !== trackFilter && a.portrait_track_2 !== trackFilter) return false;
    return true;
  });
  filtered.sort(function(a, b) { return (b[sortBy] || 0) - (a[sortBy] || 0); });
  filtered = filtered.slice(0, MAX_AUTHORS);

  document.getElementById('author-grid').innerHTML = filtered.map(function(a) {
    let tags = [];
    try { tags = JSON.parse(a.portrait_tags || '[]'); } catch {}
    const tagHtml = tags.slice(0, 4).map(function(t) { return '<span class="tag-pill">' + t.tag + '(' + t.pct + '%)</span>'; }).join('');
    const track2 = a.portrait_track_2 ? '<span class="track-tag track-tag2">' + a.portrait_track_2 + '</span>' : '';
    const verify = a.verification_type > 0 ? ' ✅' : '';
    const sig = a.signature ? '<div class="sig">' + a.signature + '</div>' : '';
    return '<div class="author-card"><div class="top"><div class="info">' +
      '<div class="nickname">' + a.nickname + verify + ' <span class="track-tag">' + a.portrait_track + '</span>' + track2 + '</div>' +
      '<div class="nums"><span>粉丝' + fmtCount(a.follower_count) + '</span><span>被赞' + a.video_count + '条</span><span>均赞' + fmtCount(a.avg_digg) + '</span><span>均长' + Math.round(a.avg_duration) + 's</span></div>' +
      '</div></div><div class="tags">' + tagHtml + '</div>' + sig + '</div>';
  }).join('');
  document.getElementById('author-count').textContent = filtered.length + ' 位UP主';
}
filterAuthors();

// Videos tab
function filterVideos() {
  const q = document.getElementById('video-search').value.toLowerCase();
  const trackFilter = document.getElementById('video-track-filter').value;
  const sourceFilter = document.getElementById('video-source-filter').value;
  const sortBy = document.getElementById('video-sort').value;

  let filtered = VIDEOS.filter(function(v) {
    if (q && !(v.title||'').toLowerCase().includes(q) && !v.author_name.toLowerCase().includes(q)) return false;
    if (trackFilter && v.author_track !== trackFilter) return false;
    if (sourceFilter === 'likes' && !v.in_likes) return false;
    if (sourceFilter === 'favorites' && !v.in_favorites) return false;
    return true;
  });

  filtered.sort(function(a, b) {
    if (sortBy === 'digg') return getDigg(b) - getDigg(a);
    if (sortBy === 'duration') return (b.duration_sec||0) - (a.duration_sec||0);
    if (sortBy === 'time') return (b.create_time||'').localeCompare(a.create_time||'');
    return 0;
  });
  filtered = filtered.slice(0, MAX_VIDEOS);

  document.getElementById('video-tbody').innerHTML = filtered.map(function(v) {
    const digg = getDigg(v);
    let vtags = []; try { vtags = JSON.parse(v.video_tags || '[]'); } catch {}
    let ctags = []; try { ctags = JSON.parse(v.comment_tags || '[]'); } catch {}
    const vtagHtml = vtags.slice(0, 3).map(function(t) { return '<span class="tag-pill">' + (t.tag_name||t.tag||'') + '</span>'; }).join('');
    const ctagHtml = ctags.slice(0, 4).map(function(t) { return '<span class="tag-pill' + (t.source==='sentiment'?' accent':'') + '">' + t.tag + '</span>'; }).join('');
    const hasComments = COMMENTS[v.aweme_id] && COMMENTS[v.aweme_id].length > 0;
    const commentBtn = hasComments ? ' <a href="#" onclick="showComments(\'' + v.aweme_id + '\',\'' + (v.title||'').substring(0,30).replace(/'/g,"") + '\');return false">💬' + COMMENTS[v.aweme_id].length + '</a>' : '';
    const track = v.author_track ? '<span class="tag-pill accent">' + v.author_track + '</span>' : '';
    return '<tr>' +
      '<td>' + fmtCount(digg) + '</td>' +
      '<td class="title">' + (v.title || '-').substring(0,40) + '</td>' +
      '<td>' + v.author_name + '</td>' +
      '<td>' + track + '</td>' +
      '<td>' + fmtDuration(v.duration_sec) + '</td>' +
      '<td>' + vtagHtml + '</td>' +
      '<td>' + ctagHtml + commentBtn + '</td>' +
      '</tr>';
  }).join('');
  document.getElementById('video-count').textContent = filtered.length + ' 个视频';
}
filterVideos();

// Comments modal
function showComments(awemeId, title) {
  const comments = COMMENTS[awemeId] || [];
  document.getElementById('modal-title').textContent = '💬 ' + title;
  document.getElementById('modal-comments').innerHTML = comments.slice(0, 20).map(function(c) {
    const ip = c.ip_location ? ' <span style="color:var(--text2);font-size:11px">· ' + c.ip_location + '</span>' : '';
    return '<div class="comment-item"><span class="digg">👍' + c.digg_count + '</span><span class="user">' + c.user_name + '</span>' + ip + '<div>' + c.content + '</div></div>';
  }).join('');
  document.getElementById('comment-modal').classList.add('show');
}

function closeModal() {
  document.getElementById('comment-modal').classList.remove('show');
}
</script>
</body>
</html>"""


def build_html(data):
    """生成 HTML 报告"""
    html = HTML_TEMPLATE
    html = html.replace("__STATS__", json.dumps(data["stats"], ensure_ascii=False))
    html = html.replace("__TRACKS__", json.dumps(data["tracks"], ensure_ascii=False))
    html = html.replace("__AUTHORS__", json.dumps(data["authors"], ensure_ascii=False))
    html = html.replace("__VIDEOS__", json.dumps(data["videos"], ensure_ascii=False))
    html = html.replace("__COMMENTS__", json.dumps(data["comments"], ensure_ascii=False))
    return html


def main():
    parser = argparse.ArgumentParser(description="生成分类呈现HTML报告")
    parser.add_argument("--output", "-o", default=DEFAULT_OUTPUT, help="输出文件路径")
    parser.add_argument("--no-comments", action="store_true", help="不含评论数据")
    args = parser.parse_args()

    init_db()
    conn = get_conn()

    print("📂 正在加载数据...")
    data = load_data(conn, include_comments=not args.no_comments)

    print(f"  视频: {data['stats']['video_count']}")
    print(f"  UP主: {data['stats']['author_count']}")
    print(f"  赛道: {data['stats']['tracks_count']}")
    print(f"  评论: {data['stats']['comment_count']}")

    print("🔧 正在生成HTML...")
    html = build_html(data)

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, 'w', encoding='utf-8') as f:
        f.write(html)

    size_kb = os.path.getsize(args.output) / 1024
    print(f"✅ 报告已生成: {args.output} ({size_kb:.0f} KB)")
    print(f"   浏览器打开即可查看")

    conn.close()


if __name__ == "__main__":
    main()
