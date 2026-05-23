import os, json, hashlib, hmac, base64, sqlite3, logging
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
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "ipapa2026")
DATABASE = os.environ.get("DATABASE_PATH", "bot.db")

HEADERS = {"Content-Type": "application/json", "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}

# ── DB ──
def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS groups (
            group_id TEXT PRIMARY KEY, joined_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS courses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            course_date TEXT NOT NULL,
            course_time TEXT DEFAULT '09:00',
            location TEXT DEFAULT '',
            description TEXT DEFAULT '',
            remind_days_before INTEGER DEFAULT 30,
            remind_interval_days INTEGER DEFAULT 7,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS course_reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL,
            remind_date TEXT NOT NULL,
            sent INTEGER DEFAULT 0,
            FOREIGN KEY(course_id) REFERENCES courses(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            group_count INTEGER DEFAULT 0
        );
    """)
    conn.commit()
    conn.close()

# ── LINE ──
def verify_signature(body, sig):
    h = hmac.new(LINE_CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(h).decode(), sig)

def push_to_groups(text):
    conn = get_db()
    groups = conn.execute("SELECT group_id FROM groups").fetchall()
    conn.close()
    logger.info(f"Pushing to {len(groups)} groups")
    ok = 0
    for g in groups:
        r = requests.post("https://api.line.me/v2/bot/message/push", headers=HEADERS,
            json={"to": g["group_id"], "messages": [{"type": "text", "text": text}]})
        logger.info(f"Push {g['group_id'][:10]}: {r.status_code} {r.text[:100]}")
        if r.status_code == 200:
            ok += 1
    return ok, len(groups)

def reply_message(reply_token, text):
    requests.post("https://api.line.me/v2/bot/message/reply", headers=HEADERS,
        json={"replyToken": reply_token, "messages": [{"type": "text", "text": text}]})

# ── 提醒排程 ──
def generate_reminders(course_id, course_date_str, days_before=30, interval=7):
    conn = get_db()
    conn.execute("DELETE FROM course_reminders WHERE course_id=?", (course_id,))
    course_date = datetime.strptime(course_date_str, "%Y-%m-%d").date()
    start = course_date - timedelta(days=days_before)
    dates = []
    d = start
    while d <= course_date:
        dates.append(d.isoformat())
        d += timedelta(days=interval)
    if course_date.isoformat() not in dates:
        dates.append(course_date.isoformat())
    for rd in dates:
        conn.execute("INSERT INTO course_reminders (course_id, remind_date, sent) VALUES (?,?,0)", (course_id, rd))
    conn.commit()
    conn.close()
    return dates

def check_and_send_reminders():
    today = date.today().isoformat()
    conn = get_db()
    rows = conn.execute("""
        SELECT cr.id, c.title, c.course_date, c.course_time, c.location, c.description
        FROM course_reminders cr JOIN courses c ON cr.course_id=c.id
        WHERE cr.remind_date=? AND cr.sent=0
    """, (today,)).fetchall()
    for row in rows:
        cd = datetime.strptime(row["course_date"], "%Y-%m-%d").date()
        days_left = (cd - date.today()).days
        timing = "【今天上課】" if days_left == 0 else f"【還有 {days_left} 天】"
        text = f"📚 課程提醒 {timing}\n━━━━━━━━━━━━\n📌 {row['title']}\n📅 {row['course_date']} {row['course_time']}"
        if row["location"]: text += f"\n📍 {row['location']}"
        if row["description"]: text += f"\n📝 {row['description']}"
        ok, total = push_to_groups(text)
        if ok > 0:
            conn.execute("UPDATE course_reminders SET sent=1 WHERE id=?", (row["id"],))
            logger.info(f"Reminder sent to {ok}/{total} groups")
    conn.commit()
    conn.close()

scheduler.add_job(check_and_send_reminders, "cron", hour=8, minute=0, id="daily_reminder")

# ── 管理網頁 ──
ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ipapa 公告管理</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:#f0f4f8;color:#333}
.header{background:#06C755;color:#fff;padding:16px 24px;display:flex;align-items:center;gap:12px}
.header h1{font-size:20px}.header p{font-size:13px;opacity:.85}
.container{max-width:860px;margin:24px auto;padding:0 16px}
.card{background:#fff;border-radius:12px;padding:24px;margin-bottom:20px;box-shadow:0 2px 8px rgba(0,0,0,.08)}
.card h2{font-size:15px;color:#06C755;margin-bottom:16px;padding-bottom:8px;border-bottom:2px solid #e8f5e9}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.full{grid-column:1/-1}
label{font-size:13px;color:#666;font-weight:500;display:block;margin-bottom:4px}
input,textarea,select{width:100%;border:1px solid #ddd;border-radius:8px;padding:8px 12px;font-size:14px}
textarea{min-height:70px;resize:vertical}
.row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.hint{font-size:12px;color:#888;background:#f8f9fa;padding:8px 12px;border-radius:8px;margin-top:8px;line-height:1.6}
.btn{padding:10px 20px;border:none;border-radius:8px;cursor:pointer;font-size:14px;font-weight:600}
.btn-green{background:#06C755;color:#fff}.btn-blue{background:#1a73e8;color:#fff}.btn-red{background:#e53935;color:#fff;padding:6px 12px;font-size:13px}
.btn:hover{opacity:.85}
table{width:100%;border-collapse:collapse;font-size:14px}
th{background:#f8f9fa;padding:10px 12px;text-align:left;font-weight:600;color:#555}
td{padding:10px 12px;border-bottom:1px solid #f0f0f0;vertical-align:middle}
.badge{display:inline-block;padding:2px 8px;border-radius:12px;font-size:12px;font-weight:600}
.badge-green{background:#e8f5e9;color:#2e7d32}.badge-gray{background:#f5f5f5;color:#777}.badge-orange{background:#fff3e0;color:#e65100}
.alert{padding:12px 16px;border-radius:8px;margin-bottom:16px;font-size:14px}
.alert-ok{background:#e8f5e9;color:#2e7d32}.alert-err{background:#ffebee;color:#c62828}
.sub{font-size:12px;color:#999}
@media(max-width:600px){.grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="header">
  <span style="font-size:28px">📢</span>
  <div><h1>ipapa 課程公告管理</h1><p>LINE 群組自動提醒系統</p></div>
</div>
<div class="container">
<div id="alert-box"></div>

<!-- 新增課程 -->
<div class="card">
  <h2>➕ 新增課程</h2>
  <div class="grid">
    <div class="full"><label>課程名稱 *</label><input id="f-title" placeholder="例：2026 春季瑜珈課程"></div>
    <div><label>課程日期 *</label><input id="f-date" type="date"></div>
    <div><label>上課時間</label><input id="f-time" type="time" value="09:00"></div>
    <div class="full"><label>地點</label><input id="f-loc" placeholder="例：台南市XX教室"></div>
    <div class="full"><label>課程說明（選填）</label><textarea id="f-desc" placeholder="報名連結、注意事項..."></textarea></div>
    <div>
      <label>提前幾天開始提醒</label>
      <input id="f-before" type="number" value="30" min="1" max="365">
    </div>
    <div>
      <label>每隔幾天發一次</label>
      <input id="f-interval" type="number" value="7" min="1" max="30">
    </div>
  </div>
  <div id="preview-hint" class="hint" style="display:none"></div>
  <div style="margin-top:16px">
    <button class="btn btn-green" onclick="addCourse()">✅ 新增課程</button>
  </div>
</div>

<!-- 課程列表 -->
<div class="card">
  <h2>📅 課程列表</h2>
  <div id="course-list"><p style="color:#aaa;text-align:center;padding:20px">載入中...</p></div>
</div>

<!-- 立即發公告 -->
<div class="card">
  <h2>📣 立即發公告到所有群組</h2>
  <label>公告內容</label>
  <textarea id="manual-msg" placeholder="輸入公告內容..." style="min-height:90px;margin-top:4px"></textarea>
  <div style="margin-top:12px;display:flex;align-items:center;gap:12px">
    <button class="btn btn-blue" onclick="sendManual()">立即發送</button>
    <span id="group-count" class="sub"></span>
  </div>
</div>

</div>
<script>
let pw = '';

function init() {
  pw = prompt('請輸入管理密碼：') || '';
  if (!pw) { document.body.innerHTML = '<p style="padding:40px;color:#999">已取消</p>'; return; }
  loadCourses();
  loadGroupCount();
  setupPreview();
}

function showAlert(msg, type='ok') {
  const el = document.getElementById('alert-box');
  el.innerHTML = `<div class="alert alert-${type}">${msg}</div>`;
  setTimeout(() => el.innerHTML='', 4000);
}

async function api(path, method='GET', body=null) {
  const opts = { method, headers: {'Content-Type':'application/json','X-Admin-Pass': pw} };
  if (body) opts.body = JSON.stringify(body);
  try {
    const r = await fetch(path, opts);
    return await r.json();
  } catch(e) {
    return {error: e.message};
  }
}

async function loadGroupCount() {
  const data = await api('/admin/groups');
  if (data.count !== undefined)
    document.getElementById('group-count').textContent = `目前已加入 ${data.count} 個群組`;
}

function setupPreview() {
  ['f-date','f-before','f-interval'].forEach(id => {
    document.getElementById(id).addEventListener('input', updatePreview);
  });
}

function updatePreview() {
  const dateVal = document.getElementById('f-date').value;
  const before = parseInt(document.getElementById('f-before').value) || 30;
  const interval = parseInt(document.getElementById('f-interval').value) || 7;
  const hint = document.getElementById('preview-hint');
  if (!dateVal) { hint.style.display='none'; return; }
  const courseDate = new Date(dateVal);
  const start = new Date(courseDate); start.setDate(start.getDate() - before);
  const dates = [];
  let d = new Date(start);
  while (d <= courseDate) {
    dates.push(d.toISOString().slice(0,10).replace(/-/g,'/').slice(5));
    d.setDate(d.getDate() + interval);
  }
  const last = courseDate.toISOString().slice(0,10).replace(/-/g,'/').slice(5);
  if (!dates.includes(last)) dates.push(last+'（當天）');
  hint.style.display='block';
  hint.innerHTML = `📅 預計發送 <strong>${dates.length}</strong> 次：${dates.join(' → ')}`;
}

async function loadCourses() {
  const data = await api('/admin/courses');
  const el = document.getElementById('course-list');
  if (data.error === 'unauthorized') { el.innerHTML='<p style="color:red">密碼錯誤，請重新整理頁面</p>'; return; }
  if (!data.courses || !data.courses.length) { el.innerHTML='<p style="color:#aaa;text-align:center;padding:20px">尚未新增任何課程</p>'; return; }
  const today = new Date().toISOString().slice(0,10);
  let html = '<table><tr><th>課程名稱</th><th>日期時間</th><th>地點</th><th>提醒</th><th>狀態</th><th>操作</th></tr>';
  for (const c of data.courses) {
    const isPast = c.course_date < today;
    const isToday = c.course_date === today;
    const badge = isPast ? '<span class="badge badge-gray">已結束</span>'
      : isToday ? '<span class="badge badge-orange">今天</span>'
      : '<span class="badge badge-green">排程中</span>';
    html += `<tr>
      <td><strong>${c.title}</strong>${c.description?'<br><span class="sub">'+c.description.slice(0,25)+'</span>':''}</td>
      <td>${c.course_date}<br><span class="sub">${c.course_time}</span></td>
      <td>${c.location||'-'}</td>
      <td><span class="sub">共${c.remind_count}次<br>已發${c.sent_count||0}次</span></td>
      <td>${badge}</td>
      <td><button class="btn btn-red" onclick="deleteCourse(${c.id})">刪除</button></td>
    </tr>`;
  }
  html += '</table>';
  el.innerHTML = html;
}

async function addCourse() {
  const title = document.getElementById('f-title').value.trim();
  const courseDate = document.getElementById('f-date').value;
  if (!title || !courseDate) { showAlert('請填寫課程名稱和日期', 'err'); return; }
  const data = await api('/admin/courses', 'POST', {
    title, course_date: courseDate,
    course_time: document.getElementById('f-time').value,
    location: document.getElementById('f-loc').value,
    description: document.getElementById('f-desc').value,
    remind_days_before: parseInt(document.getElementById('f-before').value)||30,
    remind_interval_days: parseInt(document.getElementById('f-interval').value)||7,
  });
  if (data.ok) {
    showAlert(`✅ 課程已新增，產生 ${data.remind_count} 個提醒日期`);
    ['f-title','f-date','f-loc','f-desc'].forEach(id => document.getElementById(id).value='');
    document.getElementById('preview-hint').style.display='none';
    loadCourses();
  } else {
    showAlert(data.error||'新增失敗', 'err');
  }
}

async function deleteCourse(id) {
  if (!confirm('確定刪除？相關提醒排程也會一併刪除')) return;
  const data = await api(`/admin/courses/${id}`, 'DELETE');
  if (data.ok) { showAlert('已刪除課程'); loadCourses(); }
  else showAlert('刪除失敗', 'err');
}

async function sendManual() {
  const text = document.getElementById('manual-msg').value.trim();
  if (!text) { showAlert('請輸入公告內容', 'err'); return; }
  const data = await api('/admin/send', 'POST', { text: '📢 ' + text });
  if (data.error) { showAlert('發送失敗：'+data.error, 'err'); return; }
  showAlert(`✅ 已發送到 ${data.ok}/${data.total} 個群組`);
  document.getElementById('manual-msg').value='';
}

init();
</script>
</body>
</html>"""

def check_admin(req):
    return req.headers.get("X-Admin-Pass") == ADMIN_PASSWORD

@app.route("/admin")
def admin_page():
    return render_template_string(ADMIN_HTML)

@app.route("/admin/groups")
def get_groups():
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) as c FROM groups").fetchone()["c"]
    conn.close()
    return jsonify({"count": count})

@app.route("/admin/courses", methods=["GET"])
def get_courses():
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    conn = get_db()
    rows = conn.execute("""
        SELECT c.*, COUNT(cr.id) as remind_count, SUM(cr.sent) as sent_count
        FROM courses c LEFT JOIN course_reminders cr ON c.id=cr.course_id
        GROUP BY c.id ORDER BY c.course_date ASC
    """).fetchall()
    conn.close()
    return jsonify({"courses": [dict(r) for r in rows]})

@app.route("/admin/courses", methods=["POST"])
def add_course():
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    d = request.json
    title = d.get("title","").strip()
    course_date = d.get("course_date","")
    if not title or not course_date: return jsonify({"ok":False,"error":"請填寫課程名稱和日期"})
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO courses (title,course_date,course_time,location,description,remind_days_before,remind_interval_days,created_at) VALUES (?,?,?,?,?,?,?,?)",
        (title, course_date, d.get("course_time","09:00"), d.get("location",""), d.get("description",""),
         d.get("remind_days_before",30), d.get("remind_interval_days",7), datetime.now().isoformat())
    )
    course_id = cur.lastrowid
    conn.commit()
    conn.close()
    dates = generate_reminders(course_id, course_date, d.get("remind_days_before",30), d.get("remind_interval_days",7))
    return jsonify({"ok":True,"course_id":course_id,"remind_count":len(dates)})

@app.route("/admin/courses/<int:cid>", methods=["DELETE"])
def delete_course(cid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    conn = get_db()
    conn.execute("DELETE FROM course_reminders WHERE course_id=?", (cid,))
    conn.execute("DELETE FROM courses WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    return jsonify({"ok":True})

@app.route("/admin/send", methods=["POST"])
def admin_send():
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    text = request.json.get("text","")
    ok, total = push_to_groups(text)
    conn = get_db()
    conn.execute("INSERT INTO announcements (content,sent_at,group_count) VALUES (?,?,?)",
                 (text, datetime.now().isoformat(), ok))
    conn.commit()
    conn.close()
    return jsonify({"ok":ok,"total":total})

@app.route("/admin/check-reminders", methods=["POST"])
def trigger_reminders():
    if request.headers.get("X-Admin-Pass") != ADMIN_PASSWORD:
        return jsonify({"error":"unauthorized"}), 401
    check_and_send_reminders()
    return jsonify({"ok":True,"date":date.today().isoformat()})

# ── Webhook ──
def handle_text(event):
    user_id = event["source"].get("userId","")
    reply_token = event["replyToken"]
    text = event["message"]["text"].strip()
    if event["source"]["type"] == "group":
        gid = event["source"]["groupId"]
        conn = get_db()
        conn.execute("INSERT OR IGNORE INTO groups (group_id,joined_at) VALUES (?,?)",
                     (gid, datetime.now().isoformat()))
        conn.commit()
        conn.close()
    if user_id not in ADMIN_USER_IDS: return
    if text.startswith("/公告 "):
        ok, total = push_to_groups(f"📢 {text[4:].strip()}")
        reply_message(reply_token, f"✅ 已發送到 {ok}/{total} 個群組")
    elif text == "/群組清單":
        conn = get_db()
        n = conn.execute("SELECT COUNT(*) as c FROM groups").fetchone()["c"]
        conn.close()
        reply_message(reply_token, f"已加入 {n} 個群組")
    elif text in ("/說明","/help"):
        reply_message(reply_token,
            "📋 指令\n\n/公告 [內容] 立即發公告\n/群組清單 查看群組數\n\n"
            "課程排程管理：\nhttps://ipapalinebot.onrender.com/admin")

def handle_join(event):
    if event["source"]["type"] == "group":
        conn = get_db()
        conn.execute("INSERT OR IGNORE INTO groups (group_id,joined_at) VALUES (?,?)",
                     (event["source"]["groupId"], datetime.now().isoformat()))
        conn.commit()
        conn.close()

def handle_leave(event):
    if event["source"]["type"] == "group":
        conn = get_db()
        conn.execute("DELETE FROM groups WHERE group_id=?", (event["source"]["groupId"],))
        conn.commit()
        conn.close()

@app.route("/webhook", methods=["POST"])
def webhook():
    sig = request.headers.get("X-Line-Signature","")
    body = request.get_data()
    if not verify_signature(body, sig): abort(400)
    for event in json.loads(body).get("events",[]):
        t = event.get("type")
        if t == "message" and event["message"]["type"] == "text": handle_text(event)
        elif t == "join": handle_join(event)
        elif t == "leave": handle_leave(event)
    return "OK"

@app.route("/")
def index():
    return 'LINE 公告機器人運行中 ✅ | <a href="/admin">管理後台</a>'

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)))
