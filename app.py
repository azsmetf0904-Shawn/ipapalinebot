import os
import json
import hashlib
import hmac
import base64
import sqlite3
import logging
from datetime import datetime, date, timedelta
from flask import Flask, request, abort, jsonify, render_template_string
from apscheduler.schedulers.background import BackgroundScheduler
import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
scheduler = BackgroundScheduler()
scheduler.start()

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
ADMIN_USER_IDS = [x.strip() for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip()]
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin1234")
DATABASE = os.environ.get("DATABASE_PATH", "bot.db")

HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
}

# ───────── DB ─────────

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS groups (
            group_id TEXT PRIMARY KEY,
            joined_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            group_count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS courses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            course_date TEXT NOT NULL,
            course_time TEXT DEFAULT '09:00',
            location TEXT DEFAULT '',
            description TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS course_reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL,
            remind_date TEXT NOT NULL,
            sent INTEGER DEFAULT 0,
            FOREIGN KEY(course_id) REFERENCES courses(id) ON DELETE CASCADE
        );
    """)
    conn.commit()
    conn.close()

# ───────── LINE ─────────

def verify_signature(body, signature):
    hash_val = hmac.new(LINE_CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(hash_val).decode(), signature)

def broadcast(text):
    resp = requests.post(
        "https://api.line.me/v2/bot/message/broadcast",
        headers=HEADERS,
        json={"messages": [{"type": "text", "text": text}]}
    )
    logger.info(f"Broadcast: {resp.status_code}")
    return resp.status_code == 200

def push_to_groups(text):
    conn = get_db()
    groups = conn.execute("SELECT group_id FROM groups").fetchall()
    conn.close()
    ok = sum(1 for g in groups if push_message(g["group_id"], text))
    return ok, len(groups)

def push_message(target_id, text):
    resp = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers=HEADERS,
        json={"to": target_id, "messages": [{"type": "text", "text": text}]}
    )
    return resp.status_code == 200

def reply_message(reply_token, text):
    requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers=HEADERS,
        json={"replyToken": reply_token, "messages": [{"type": "text", "text": text}]}
    )

# ───────── 課程提醒排程 ─────────

def generate_reminders(course_id, course_date_str):
    """課程日期前1個月，每週產生一次提醒日期"""
    conn = get_db()
    conn.execute("DELETE FROM course_reminders WHERE course_id=?", (course_id,))
    course_date = datetime.strptime(course_date_str, "%Y-%m-%d").date()
    start_date = course_date - timedelta(days=30)
    remind_dates = []
    d = start_date
    while d <= course_date:
        remind_dates.append(d.isoformat())
        d += timedelta(days=7)
    # 確保課程當天也有提醒
    if course_date.isoformat() not in remind_dates:
        remind_dates.append(course_date.isoformat())
    for rd in remind_dates:
        conn.execute(
            "INSERT INTO course_reminders (course_id, remind_date, sent) VALUES (?, ?, 0)",
            (course_id, rd)
        )
    conn.commit()
    conn.close()
    return remind_dates

def check_and_send_reminders():
    """每天由 scheduler 或 GitHub Actions 呼叫，檢查今天要發的提醒"""
    today = date.today().isoformat()
    conn = get_db()
    rows = conn.execute("""
        SELECT cr.id, cr.course_id, c.title, c.course_date, c.course_time, c.location, c.description
        FROM course_reminders cr
        JOIN courses c ON cr.course_id = c.id
        WHERE cr.remind_date = ? AND cr.sent = 0
    """, (today,)).fetchall()

    for row in rows:
        course_date = datetime.strptime(row["course_date"], "%Y-%m-%d").date()
        days_left = (course_date - date.today()).days
        if days_left == 0:
            timing = "【今天】"
        elif days_left <= 7:
            timing = f"【還有 {days_left} 天】"
        else:
            weeks_left = days_left // 7
            timing = f"【還有約 {weeks_left} 週】"

        text = (
            f"📚 課程提醒 {timing}\n"
            f"━━━━━━━━━━━━\n"
            f"📌 {row['title']}\n"
            f"📅 {row['course_date']} {row['course_time']}\n"
        )
        if row["location"]:
            text += f"📍 {row['location']}\n"
        if row["description"]:
            text += f"📝 {row['description']}\n"

        ok, total = push_to_groups(text)
        if ok > 0:
            conn.execute("UPDATE course_reminders SET sent=1 WHERE id=?", (row["id"],))
            logger.info(f"Reminder sent for course {row['course_id']} to {ok}/{total} groups")

    conn.commit()
    conn.close()

# 每天早上 8:00 自動檢查
scheduler.add_job(check_and_send_reminders, "cron", hour=8, minute=0, id="daily_reminder")

# ───────── 網頁管理介面 ─────────

ADMIN_HTML = """
<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>課程公告管理</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, sans-serif; background: #f0f4f8; color: #333; }
  .header { background: #06C755; color: white; padding: 16px 24px; display: flex; align-items: center; gap: 12px; }
  .header h1 { font-size: 20px; }
  .container { max-width: 900px; margin: 24px auto; padding: 0 16px; }
  .card { background: white; border-radius: 12px; padding: 24px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
  .card h2 { font-size: 16px; color: #06C755; margin-bottom: 16px; border-bottom: 2px solid #e8f5e9; padding-bottom: 8px; }
  .form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .form-group { display: flex; flex-direction: column; gap: 4px; }
  .form-group.full { grid-column: 1 / -1; }
  label { font-size: 13px; color: #666; font-weight: 500; }
  input, textarea { border: 1px solid #ddd; border-radius: 8px; padding: 8px 12px; font-size: 14px; width: 100%; }
  textarea { resize: vertical; min-height: 60px; }
  .btn { padding: 10px 20px; border: none; border-radius: 8px; cursor: pointer; font-size: 14px; font-weight: 600; }
  .btn-green { background: #06C755; color: white; }
  .btn-red { background: #ff4444; color: white; }
  .btn-blue { background: #007bff; color: white; }
  .btn:hover { opacity: 0.85; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th { background: #f8f9fa; padding: 10px 12px; text-align: left; font-weight: 600; color: #555; }
  td { padding: 10px 12px; border-bottom: 1px solid #f0f0f0; vertical-align: middle; }
  tr:hover td { background: #fafffe; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 12px; font-weight: 600; }
  .badge-green { background: #e8f5e9; color: #2e7d32; }
  .badge-orange { background: #fff3e0; color: #e65100; }
  .badge-gray { background: #f5f5f5; color: #666; }
  .remind-list { font-size: 12px; color: #888; }
  .actions { display: flex; gap: 6px; }
  .alert { padding: 12px 16px; border-radius: 8px; margin-bottom: 16px; font-size: 14px; }
  .alert-success { background: #e8f5e9; color: #2e7d32; }
  .alert-error { background: #ffebee; color: #c62828; }
  @media(max-width:600px) { .form-grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<div class="header">
  <span style="font-size:28px">📢</span>
  <div>
    <h1>ipapa 課程公告管理</h1>
    <div style="font-size:13px;opacity:0.9">LINE 群組自動提醒系統</div>
  </div>
</div>
<div class="container">

  <div id="alert"></div>

  <!-- 新增課程 -->
  <div class="card">
    <h2>➕ 新增課程</h2>
    <div class="form-grid">
      <div class="form-group full">
        <label>課程名稱 *</label>
        <input id="f-title" type="text" placeholder="例：2026 春季瑜珈課程">
      </div>
      <div class="form-group">
        <label>課程日期 *</label>
        <input id="f-date" type="date">
      </div>
      <div class="form-group">
        <label>上課時間</label>
        <input id="f-time" type="time" value="09:00">
      </div>
      <div class="form-group full">
        <label>地點</label>
        <input id="f-location" type="text" placeholder="例：台北市中山區XX教室">
      </div>
      <div class="form-group full">
        <label>課程說明</label>
        <textarea id="f-desc" placeholder="報名連結、注意事項..."></textarea>
      </div>
    </div>
    <div style="margin-top:16px;display:flex;gap:10px;align-items:center">
      <button class="btn btn-green" onclick="addCourse()">新增課程</button>
      <span style="font-size:13px;color:#888">新增後系統自動產生提醒排程（課程前1個月每週發送）</span>
    </div>
  </div>

  <!-- 課程列表 -->
  <div class="card">
    <h2>📅 課程列表</h2>
    <div id="course-list">載入中...</div>
  </div>

  <!-- 立即發公告 -->
  <div class="card">
    <h2>📣 立即發公告</h2>
    <div class="form-group">
      <label>公告內容</label>
      <textarea id="manual-msg" placeholder="輸入要發送到所有群組的公告..." style="min-height:80px"></textarea>
    </div>
    <button class="btn btn-blue" style="margin-top:12px" onclick="sendManual()">立即發送</button>
  </div>

</div>

<script>
const pass = prompt('請輸入管理密碼：');
if (!pass) { document.body.innerHTML = '<p style="padding:40px">已取消</p>'; }

function showAlert(msg, type='success') {
  const el = document.getElementById('alert');
  el.innerHTML = `<div class="alert alert-${type}">${msg}</div>`;
  setTimeout(() => el.innerHTML = '', 4000);
}

async function api(path, method='GET', body=null) {
  const opts = { method, headers: {'Content-Type':'application/json','X-Admin-Pass': pass} };
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  return r.json();
}

async function loadCourses() {
  const data = await api('/admin/courses');
  if (!data.courses) { document.getElementById('course-list').innerHTML = '<p style="color:red">密碼錯誤</p>'; return; }
  if (!data.courses.length) {
    document.getElementById('course-list').innerHTML = '<p style="color:#aaa;text-align:center;padding:20px">尚未新增任何課程</p>';
    return;
  }
  const today = new Date().toISOString().slice(0,10);
  let html = '<table><tr><th>課程名稱</th><th>日期</th><th>地點</th><th>提醒次數</th><th>狀態</th><th>操作</th></tr>';
  for (const c of data.courses) {
    const isPast = c.course_date < today;
    const isToday = c.course_date === today;
    const badge = isPast ? '<span class="badge badge-gray">已結束</span>'
                : isToday ? '<span class="badge badge-orange">今天</span>'
                : '<span class="badge badge-green">進行中</span>';
    html += `<tr>
      <td><strong>${c.title}</strong>${c.description ? '<br><span class="remind-list">'+c.description.slice(0,30)+'</span>' : ''}</td>
      <td>${c.course_date}<br><span class="remind-list">${c.course_time}</span></td>
      <td>${c.location || '-'}</td>
      <td><span class="remind-list">${c.remind_count} 次（已發 ${c.sent_count}）</span></td>
      <td>${badge}</td>
      <td class="actions">
        <button class="btn btn-blue" style="padding:6px 10px;font-size:12px" onclick="sendNow(${c.id},'${c.title}','${c.course_date}','${c.course_time}','${c.location}')">立即提醒</button>
        <button class="btn btn-red" style="padding:6px 10px;font-size:12px" onclick="deleteCourse(${c.id})">刪除</button>
      </td>
    </tr>`;
  }
  html += '</table>';
  document.getElementById('course-list').innerHTML = html;
}

async function addCourse() {
  const title = document.getElementById('f-title').value.trim();
  const date = document.getElementById('f-date').value;
  if (!title || !date) { showAlert('請填寫課程名稱和日期', 'error'); return; }
  const data = await api('/admin/courses', 'POST', {
    title, course_date: date,
    course_time: document.getElementById('f-time').value,
    location: document.getElementById('f-location').value,
    description: document.getElementById('f-desc').value,
  });
  if (data.ok) {
    showAlert(`✅ 已新增課程，產生 ${data.remind_count} 個提醒日期`);
    document.getElementById('f-title').value = '';
    document.getElementById('f-date').value = '';
    document.getElementById('f-location').value = '';
    document.getElementById('f-desc').value = '';
    loadCourses();
  } else {
    showAlert(data.error || '新增失敗', 'error');
  }
}

async function deleteCourse(id) {
  if (!confirm('確定刪除？相關提醒排程也會一併刪除')) return;
  const data = await api(`/admin/courses/${id}`, 'DELETE');
  if (data.ok) { showAlert('已刪除'); loadCourses(); }
}

async function sendNow(id, title, date, time, location) {
  const text = `📚 課程提醒\\n━━━━━━━━━━━━\\n📌 ${title}\\n📅 ${date} ${time}${location ? '\\n📍 '+location : ''}`;
  const data = await api('/admin/send', 'POST', { text });
  showAlert(`✅ 已發送到 ${data.ok}/${data.total} 個群組`);
}

async function sendManual() {
  const text = document.getElementById('manual-msg').value.trim();
  if (!text) { showAlert('請輸入公告內容', 'error'); return; }
  const data = await api('/admin/send', 'POST', { text: '📢 ' + text });
  showAlert(`✅ 已發送到 ${data.ok}/${data.total} 個群組`);
  document.getElementById('manual-msg').value = '';
}

loadCourses();
</script>
</body>
</html>
"""

def check_admin(req):
    return req.headers.get("X-Admin-Pass") == ADMIN_PASSWORD

# ───────── API 路由 ─────────

@app.route("/admin")
def admin_page():
    return render_template_string(ADMIN_HTML)

@app.route("/admin/courses", methods=["GET"])
def get_courses():
    if not check_admin(request):
        return jsonify({"error": "unauthorized"}), 401
    conn = get_db()
    courses = conn.execute("""
        SELECT c.*,
            COUNT(cr.id) as remind_count,
            SUM(cr.sent) as sent_count
        FROM courses c
        LEFT JOIN course_reminders cr ON c.id = cr.course_id
        GROUP BY c.id
        ORDER BY c.course_date ASC
    """).fetchall()
    conn.close()
    return jsonify({"courses": [dict(r) for r in courses]})

@app.route("/admin/courses", methods=["POST"])
def add_course():
    if not check_admin(request):
        return jsonify({"error": "unauthorized"}), 401
    data = request.json
    title = data.get("title", "").strip()
    course_date = data.get("course_date", "")
    if not title or not course_date:
        return jsonify({"ok": False, "error": "請填寫課程名稱和日期"})
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO courses (title, course_date, course_time, location, description, created_at) VALUES (?,?,?,?,?,?)",
        (title, course_date, data.get("course_time","09:00"),
         data.get("location",""), data.get("description",""), datetime.now().isoformat())
    )
    course_id = cur.lastrowid
    conn.commit()
    conn.close()
    remind_dates = generate_reminders(course_id, course_date)
    return jsonify({"ok": True, "course_id": course_id, "remind_count": len(remind_dates)})

@app.route("/admin/courses/<int:course_id>", methods=["DELETE"])
def delete_course(course_id):
    if not check_admin(request):
        return jsonify({"error": "unauthorized"}), 401
    conn = get_db()
    conn.execute("DELETE FROM course_reminders WHERE course_id=?", (course_id,))
    conn.execute("DELETE FROM courses WHERE id=?", (course_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/admin/send", methods=["POST"])
def admin_send():
    if not check_admin(request):
        return jsonify({"error": "unauthorized"}), 401
    text = request.json.get("text", "")
    ok, total = push_to_groups(text)
    return jsonify({"ok": ok, "total": total})

@app.route("/admin/check-reminders", methods=["POST"])
def trigger_reminders():
    """GitHub Actions 每天呼叫此端點觸發提醒檢查"""
    token = request.headers.get("X-Admin-Pass")
    if token != ADMIN_PASSWORD:
        return jsonify({"error": "unauthorized"}), 401
    check_and_send_reminders()
    return jsonify({"ok": True, "date": date.today().isoformat()})

# ───────── LINE Webhook ─────────

def handle_text_message(event):
    user_id = event["source"].get("userId", "")
    reply_token = event["replyToken"]
    text = event["message"]["text"].strip()
    source_type = event["source"]["type"]

    if source_type == "group":
        group_id = event["source"]["groupId"]
        conn = get_db()
        conn.execute("INSERT OR IGNORE INTO groups (group_id, joined_at) VALUES (?,?)",
                     (group_id, datetime.now().isoformat()))
        conn.commit()
        conn.close()

    if user_id not in ADMIN_USER_IDS:
        return

    if text.startswith("/公告 "):
        content = text[4:].strip()
        ok, total = push_to_groups(f"📢 {content}")
        reply_message(reply_token, f"✅ 已發送到 {ok}/{total} 個群組")
    elif text == "/群組清單":
        conn = get_db()
        groups = conn.execute("SELECT group_id, joined_at FROM groups").fetchall()
        conn.close()
        if not groups:
            reply_message(reply_token, "尚未加入任何群組")
        else:
            reply_message(reply_token, f"已加入 {len(groups)} 個群組")
    elif text in ("/說明", "/help"):
        reply_message(reply_token,
            "📋 指令說明\n\n"
            "/公告 [內容] - 立即發公告\n"
            "/群組清單 - 查看群組\n\n"
            "課程管理請至網頁後台：\n"
            "https://ipapalinebot.onrender.com/admin"
        )

def handle_join(event):
    if event["source"]["type"] == "group":
        group_id = event["source"]["groupId"]
        conn = get_db()
        conn.execute("INSERT OR IGNORE INTO groups (group_id, joined_at) VALUES (?,?)",
                     (group_id, datetime.now().isoformat()))
        conn.commit()
        conn.close()

def handle_leave(event):
    if event["source"]["type"] == "group":
        group_id = event["source"]["groupId"]
        conn = get_db()
        conn.execute("DELETE FROM groups WHERE group_id=?", (group_id,))
        conn.commit()
        conn.close()

@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data()
    if not verify_signature(body, signature):
        abort(400)
    data = json.loads(body)
    for event in data.get("events", []):
        t = event.get("type")
        if t == "message" and event["message"]["type"] == "text":
            handle_text_message(event)
        elif t == "join":
            handle_join(event)
        elif t == "leave":
            handle_leave(event)
    return "OK"

@app.route("/")
def index():
    return "LINE 公告機器人運行中 ✅ | <a href='/admin'>管理後台</a>"

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
