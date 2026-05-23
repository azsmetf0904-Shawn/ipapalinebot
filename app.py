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
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin1234")
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
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            color TEXT DEFAULT '#06C755',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS courses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            category_id INTEGER DEFAULT NULL,
            course_date TEXT NOT NULL,
            course_time TEXT DEFAULT '09:00',
            location TEXT DEFAULT '',
            description TEXT DEFAULT '',
            remind_days_before INTEGER DEFAULT 30,
            remind_interval TEXT DEFAULT 'weekly',
            created_at TEXT NOT NULL,
            FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS course_reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL,
            remind_date TEXT NOT NULL,
            sent INTEGER DEFAULT 0,
            FOREIGN KEY(course_id) REFERENCES courses(id) ON DELETE CASCADE
        );
    """)
    # 預設分類
    for name, color in [("瑜珈", "#06C755"), ("舞蹈", "#1976d2"), ("運動", "#f57c00"), ("其他", "#9e9e9e")]:
        try:
            conn.execute("INSERT INTO categories (name, color, created_at) VALUES (?,?,?)",
                         (name, color, datetime.now().isoformat()))
        except:
            pass
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
    ok = sum(1 for g in groups if _push(g["group_id"], text))
    return ok, len(groups)

def _push(target_id, text):
    r = requests.post("https://api.line.me/v2/bot/message/push", headers=HEADERS,
                      json={"to": target_id, "messages": [{"type": "text", "text": text}]})
    return r.status_code == 200

def reply_message(token, text):
    requests.post("https://api.line.me/v2/bot/message/reply", headers=HEADERS,
                  json={"replyToken": token, "messages": [{"type": "text", "text": text}]})

# ── 提醒排程 ──

def generate_reminders(course_id, course_date_str, days_before=30, interval="weekly"):
    conn = get_db()
    conn.execute("DELETE FROM course_reminders WHERE course_id=?", (course_id,))
    cd = datetime.strptime(course_date_str, "%Y-%m-%d").date()
    start = cd - timedelta(days=days_before)
    step = timedelta(days=7 if interval == "weekly" else 1)
    dates, d = [], start
    while d < cd:
        dates.append(d.isoformat()); d += step
    dates.append(cd.isoformat())
    for rd in dates:
        conn.execute("INSERT INTO course_reminders (course_id, remind_date, sent) VALUES (?,?,0)", (course_id, rd))
    conn.commit(); conn.close()
    return dates

def check_and_send_reminders():
    today = date.today().isoformat()
    conn = get_db()
    rows = conn.execute("""
        SELECT cr.id, c.title, c.course_date, c.course_time, c.location, c.description,
               cat.name as cat_name
        FROM course_reminders cr
        JOIN courses c ON cr.course_id = c.id
        LEFT JOIN categories cat ON c.category_id = cat.id
        WHERE cr.remind_date=? AND cr.sent=0
    """, (today,)).fetchall()
    for row in rows:
        cd = datetime.strptime(row["course_date"], "%Y-%m-%d").date()
        dl = (cd - date.today()).days
        timing = "【今天上課！】" if dl==0 else "【明天上課！】" if dl==1 else f"【還有{dl}天】" if dl<=7 else f"【還有約{round(dl/7)}週】"
        cat = f"[{row['cat_name']}] " if row["cat_name"] else ""
        text = f"📚 課程提醒 {timing}\n━━━━━━━━━━━━\n📌 {cat}{row['title']}\n📅 {row['course_date']} {row['course_time']}\n"
        if row["location"]: text += f"📍 {row['location']}\n"
        if row["description"]: text += f"📝 {row['description']}\n"
        ok, total = push_to_groups(text)
        conn.execute("UPDATE course_reminders SET sent=1 WHERE id=?", (row["id"],))
        logger.info(f"Reminder sent {ok}/{total}")
    conn.commit(); conn.close()

scheduler.add_job(check_and_send_reminders, "cron", hour=8, minute=0, id="daily_reminder")

# ── 網頁 ──

ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>課程公告管理</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#f0f4f8;color:#333}
.header{background:#06C755;color:#fff;padding:16px 24px;display:flex;align-items:center;gap:12px;box-shadow:0 2px 8px rgba(0,0,0,.15)}
.header h1{font-size:20px}
.container{max-width:980px;margin:24px auto;padding:0 16px}
.card{background:#fff;border-radius:12px;padding:22px;margin-bottom:18px;box-shadow:0 2px 8px rgba(0,0,0,.07)}
.card-title{font-size:15px;color:#06C755;font-weight:700;margin-bottom:14px;border-bottom:2px solid #e8f5e9;padding-bottom:8px;display:flex;align-items:center;justify-content:space-between}
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.form-group{display:flex;flex-direction:column;gap:5px}
.form-group.full{grid-column:1/-1}
label{font-size:13px;color:#555;font-weight:600}
label .hint{font-weight:400;color:#aaa;margin-left:4px}
input,textarea,select{border:1.5px solid #e0e0e0;border-radius:8px;padding:9px 12px;font-size:14px;width:100%;transition:border-color .2s}
input:focus,textarea:focus,select:focus{outline:none;border-color:#06C755}
textarea{resize:vertical;min-height:60px}
.remind-box{background:#f8fff9;border:1.5px solid #c8e6c9;border-radius:10px;padding:14px;margin-top:4px}
.remind-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:14px}
.remind-row input[type=number]{width:76px}
.remind-row select{width:110px}
.preview-box{background:#f1f8e9;border-radius:8px;padding:9px 13px;font-size:12px;color:#558b2f;margin-top:10px;line-height:1.8}
.btn{padding:9px 18px;border:none;border-radius:8px;cursor:pointer;font-size:13px;font-weight:700;transition:opacity .15s,transform .1s}
.btn:hover{opacity:.85;transform:translateY(-1px)}
.btn-green{background:#06C755;color:#fff}
.btn-red{background:#f44336;color:#fff}
.btn-blue{background:#1976d2;color:#fff}
.btn-orange{background:#ff9800;color:#fff}
.btn-gray{background:#9e9e9e;color:#fff}
.btn-sm{padding:5px 11px;font-size:12px}
/* 分類區塊 */
.cat-section{margin-bottom:12px;border:1.5px solid #e8e8e8;border-radius:10px;overflow:hidden}
.cat-header{display:flex;align-items:center;gap:10px;padding:12px 16px;cursor:pointer;background:#fafafa;user-select:none;transition:background .15s}
.cat-header:hover{background:#f0f0f0}
.cat-dot{width:12px;height:12px;border-radius:50%;flex-shrink:0}
.cat-name{font-weight:700;font-size:15px;flex:1}
.cat-count{font-size:12px;color:#aaa;margin-right:6px}
.cat-arrow{font-size:12px;color:#aaa;transition:transform .25s}
.cat-arrow.open{transform:rotate(90deg)}
.cat-body{display:none;border-top:1.5px solid #e8e8e8}
.cat-body.open{display:block}
table{width:100%;border-collapse:collapse;font-size:14px}
th{background:#f5f5f5;padding:10px 12px;text-align:left;font-weight:700;color:#555;border-bottom:2px solid #eee}
td{padding:11px 12px;border-bottom:1px solid #f0f0f0;vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:#fafffe}
.badge{display:inline-block;padding:3px 9px;border-radius:10px;font-size:11px;font-weight:700}
.badge-green{background:#e8f5e9;color:#2e7d32}
.badge-orange{background:#fff3e0;color:#e65100}
.badge-gray{background:#f5f5f5;color:#777}
.sub{font-size:12px;color:#aaa;margin-top:2px}
.actions{display:flex;gap:5px;flex-wrap:wrap}
.alert{padding:12px 16px;border-radius:8px;margin-bottom:14px;font-size:14px;font-weight:500}
.alert-success{background:#e8f5e9;color:#2e7d32;border-left:4px solid #06C755}
.alert-error{background:#ffebee;color:#c62828;border-left:4px solid #f44336}
.empty{text-align:center;padding:28px;color:#bbb;font-size:14px}
/* 分類管理 */
.cat-list{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px}
.cat-tag{display:flex;align-items:center;gap:6px;padding:6px 12px;border-radius:20px;font-size:13px;font-weight:600;color:#fff}
.cat-tag button{background:rgba(255,255,255,.3);border:none;border-radius:50%;width:18px;height:18px;cursor:pointer;font-size:11px;color:#fff;line-height:1;display:flex;align-items:center;justify-content:center}
.add-cat-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.add-cat-row input{max-width:180px}
.color-picker{width:44px;height:36px;border:1.5px solid #e0e0e0;border-radius:8px;padding:2px;cursor:pointer}
@media(max-width:600px){.form-grid{grid-template-columns:1fr}.remind-row{flex-direction:column;align-items:flex-start}}
</style>
</head>
<body>
<div class="header">
  <span style="font-size:30px">📢</span>
  <div><h1>ipapa 課程公告管理</h1><div style="font-size:13px;opacity:.9">LINE 群組自動提醒系統</div></div>
</div>
<div class="container">
<div id="alert"></div>

<!-- 分類管理 -->
<div class="card">
  <div class="card-title">🏷️ 課程分類管理</div>
  <div class="cat-list" id="cat-tags"></div>
  <div class="add-cat-row">
    <input id="new-cat-name" type="text" placeholder="新增分類名稱" style="max-width:180px">
    <input id="new-cat-color" type="color" class="color-picker" value="#06C755" title="選擇顏色">
    <button class="btn btn-green" onclick="addCategory()">＋ 新增分類</button>
  </div>
</div>

<!-- 新增課程 -->
<div class="card">
  <div class="card-title">➕ 新增課程</div>
  <div class="form-grid">
    <div class="form-group full">
      <label>課程名稱 *</label>
      <input id="f-title" type="text" placeholder="例：春季瑜珈初階班">
    </div>
    <div class="form-group">
      <label>課程分類</label>
      <select id="f-cat"><option value="">— 不分類 —</option></select>
    </div>
    <div class="form-group">
      <label>課程日期 *</label>
      <input id="f-date" type="date" onchange="updatePreview()">
    </div>
    <div class="form-group">
      <label>上課時間</label>
      <input id="f-time" type="time" value="09:00">
    </div>
    <div class="form-group">
      <label>地點 <span class="hint">（選填）</span></label>
      <input id="f-location" type="text" placeholder="例：台北市中山區XX教室">
    </div>
    <div class="form-group full">
      <label>課程說明 <span class="hint">（選填）</span></label>
      <textarea id="f-desc" placeholder="報名連結、注意事項..."></textarea>
    </div>
    <div class="form-group full">
      <label>⏰ 提醒排程設定</label>
      <div class="remind-box">
        <div class="remind-row">
          <span>課程開始前</span>
          <input type="number" id="f-days" value="30" min="1" max="365" onchange="updatePreview()">
          <span>天開始提醒，每</span>
          <select id="f-interval" onchange="updatePreview()">
            <option value="weekly">週</option>
            <option value="daily">天</option>
          </select>
          <span>發送一次</span>
        </div>
        <div class="preview-box" id="remind-preview">請先選擇課程日期</div>
      </div>
    </div>
  </div>
  <div style="margin-top:14px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
    <button class="btn btn-green" onclick="addCourse()">✅ 新增課程</button>
    <span style="font-size:12px;color:#aaa">新增後自動產生提醒排程</span>
  </div>
</div>

<!-- 課程列表（依分類展開/收起） -->
<div class="card">
  <div class="card-title">📅 課程列表（依分類）</div>
  <div id="course-list"><div class="empty">載入中...</div></div>
</div>

<!-- 立即發公告 -->
<div class="card">
  <div class="card-title">📣 立即發公告</div>
  <textarea id="manual-msg" placeholder="輸入公告內容..." style="min-height:80px;width:100%"></textarea>
  <button class="btn btn-blue" style="margin-top:10px" onclick="sendManual()">立即發送到所有群組</button>
</div>
</div>

<script>
const adminPass = prompt('請輸入管理密碼：');
if (!adminPass) { document.body.innerHTML='<p style="padding:40px;color:#999">已取消</p>'; }

let categories = [];

function showAlert(msg, type='success') {
  const el = document.getElementById('alert');
  el.innerHTML = `<div class="alert alert-${type}">${msg}</div>`;
  setTimeout(() => el.innerHTML='', 5000);
}

async function api(path, method='GET', body=null) {
  const opts = { method, headers: {'Content-Type':'application/json','X-Admin-Pass':adminPass} };
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  return r.json();
}

// ── 分類 ──

async function loadCategories() {
  const data = await api('/admin/categories');
  if (!data.categories) return;
  categories = data.categories;
  renderCategoryTags();
  renderCategorySelect();
}

function renderCategoryTags() {
  const el = document.getElementById('cat-tags');
  if (!categories.length) { el.innerHTML='<span style="color:#aaa;font-size:13px">尚無分類</span>'; return; }
  el.innerHTML = categories.map(c => `
    <div class="cat-tag" style="background:${c.color}">
      <span>${c.name}</span>
      <button onclick="deleteCategory(${c.id},'${c.name}')" title="刪除">✕</button>
    </div>`).join('');
}

function renderCategorySelect() {
  const sel = document.getElementById('f-cat');
  sel.innerHTML = '<option value="">— 不分類 —</option>' +
    categories.map(c => `<option value="${c.id}">${c.name}</option>`).join('');
}

async function addCategory() {
  const name = document.getElementById('new-cat-name').value.trim();
  const color = document.getElementById('new-cat-color').value;
  if (!name) { showAlert('請輸入分類名稱','error'); return; }
  const data = await api('/admin/categories','POST',{name,color});
  if (data.ok) { document.getElementById('new-cat-name').value=''; await loadCategories(); loadCourses(); }
  else showAlert(data.error||'新增失敗','error');
}

async function deleteCategory(id, name) {
  if (!confirm(`刪除分類「${name}」？該分類的課程將變為未分類`)) return;
  const data = await api(`/admin/categories/${id}`,'DELETE');
  if (data.ok) { await loadCategories(); loadCourses(); }
}

// ── 提醒預覽 ──

function updatePreview() {
  const dv = document.getElementById('f-date').value;
  const days = parseInt(document.getElementById('f-days').value)||30;
  const iv = document.getElementById('f-interval').value;
  const box = document.getElementById('remind-preview');
  if (!dv) { box.textContent='請先選擇課程日期'; return; }
  const cd = new Date(dv), start = new Date(dv);
  start.setDate(start.getDate()-days);
  const step = iv==='weekly'?7:1, dates=[];
  let d=new Date(start);
  while(d<cd){dates.push(d.toLocaleDateString('zh-TW',{month:'numeric',day:'numeric'}));d.setDate(d.getDate()+step);}
  dates.push(cd.toLocaleDateString('zh-TW',{month:'numeric',day:'numeric'})+'（當天）');
  box.innerHTML=`📅 預計發送 <strong>${dates.length}</strong> 次：${dates.join(' → ')}`;
}

// ── 課程 ──

async function loadCourses() {
  const data = await api('/admin/courses');
  if (!data.courses) { document.getElementById('course-list').innerHTML='<div class="empty" style="color:#f44336">密碼錯誤</div>'; return; }

  // 依分類分組
  const groups = {};
  // 先加所有分類（含空的）
  groups['__none__'] = { name:'未分類', color:'#9e9e9e', courses:[] };
  categories.forEach(c => groups[c.id] = { name:c.name, color:c.color, courses:[] });

  data.courses.forEach(c => {
    const key = c.category_id || '__none__';
    if (!groups[key]) groups[key] = { name:'未分類', color:'#9e9e9e', courses:[] };
    groups[key].courses.push(c);
  });

  const today = new Date().toISOString().slice(0,10);
  let html = '';

  for (const [key, group] of Object.entries(groups)) {
    if (!group.courses.length) continue;
    const sectionId = 'sec-'+key;
    html += `
    <div class="cat-section">
      <div class="cat-header" onclick="toggleSection('${sectionId}')">
        <div class="cat-dot" style="background:${group.color}"></div>
        <div class="cat-name">${group.name}</div>
        <div class="cat-count">${group.courses.length} 堂課</div>
        <div class="cat-arrow open" id="arr-${sectionId}">▶</div>
      </div>
      <div class="cat-body open" id="${sectionId}">
        <table>
          <tr><th>課程名稱</th><th>日期</th><th>提醒設定</th><th>進度</th><th>狀態</th><th>操作</th></tr>`;
    for (const c of group.courses) {
      const isPast = c.course_date < today;
      const isToday = c.course_date === today;
      const badge = isPast ? '<span class="badge badge-gray">已結束</span>'
                  : isToday ? '<span class="badge badge-orange">今天</span>'
                  : '<span class="badge badge-green">即將開課</span>';
      const ivLabel = c.remind_interval==='daily'?'每天':'每週';
      const prog = c.remind_count>0 ? `${c.sent_count||0}/${c.remind_count}次` : '無';
      const loc = c.location ? `<div class="sub">📍${c.location}</div>` : '';
      html += `<tr>
        <td><strong>${c.title}</strong>${loc}</td>
        <td>${c.course_date}<div class="sub">${c.course_time}</div></td>
        <td><div class="sub">提前${c.remind_days_before}天</div><div class="sub">${ivLabel}</div></td>
        <td><div class="sub">${prog}</div></td>
        <td>${badge}</td>
        <td class="actions">
          <button class="btn btn-orange btn-sm" onclick="sendNow(${c.id},'${c.title.replace(/'/g,"\\'")}','${c.course_date}','${c.course_time}','${(c.location||'').replace(/'/g,"\\'")}')">提醒</button>
          <button class="btn btn-red btn-sm" onclick="deleteCourse(${c.id},'${c.title.replace(/'/g,"\\'")}')">刪除</button>
        </td>
      </tr>`;
    }
    html += `</table></div></div>`;
  }

  document.getElementById('course-list').innerHTML = html || '<div class="empty">尚未新增任何課程</div>';
}

function toggleSection(id) {
  const body = document.getElementById(id);
  const arr = document.getElementById('arr-'+id);
  const isOpen = body.classList.contains('open');
  body.classList.toggle('open', !isOpen);
  arr.classList.toggle('open', !isOpen);
}

async function addCourse() {
  const title = document.getElementById('f-title').value.trim();
  const courseDate = document.getElementById('f-date').value;
  if (!title||!courseDate) { showAlert('❌ 請填寫課程名稱和日期','error'); return; }
  const catId = document.getElementById('f-cat').value;
  const data = await api('/admin/courses','POST',{
    title, course_date:courseDate,
    course_time:document.getElementById('f-time').value,
    location:document.getElementById('f-location').value,
    description:document.getElementById('f-desc').value,
    category_id: catId ? parseInt(catId) : null,
    remind_days_before:parseInt(document.getElementById('f-days').value)||30,
    remind_interval:document.getElementById('f-interval').value,
  });
  if (data.ok) {
    showAlert(`✅ 已新增「${title}」，共 ${data.remind_count} 個提醒`);
    ['f-title','f-location','f-desc'].forEach(id=>document.getElementById(id).value='');
    document.getElementById('f-date').value='';
    document.getElementById('remind-preview').textContent='請先選擇課程日期';
    loadCourses();
  } else showAlert(data.error||'新增失敗','error');
}

async function deleteCourse(id, title) {
  if (!confirm(`確定刪除「${title}」？`)) return;
  const data = await api(`/admin/courses/${id}`,'DELETE');
  if (data.ok) { showAlert('已刪除'); loadCourses(); }
}

async function sendNow(id, title, dt, time, loc) {
  const days = Math.round((new Date(dt)-new Date())/86400000);
  const timing = days===0?'【今天上課！】':days>0?`【還有${days}天】`:'【已結束】';
  const locStr = loc ? `\n📍 ${loc}` : '';
  const text = `📚 課程提醒 ${timing}\n━━━━━━━━━━━━\n📌 ${title}\n📅 ${dt} ${time}${locStr}`;
  const data = await api('/admin/send','POST',{text});
  showAlert(`✅ 已發送到 ${data.ok}/${data.total} 個群組`);
}

async function sendManual() {
  const text = document.getElementById('manual-msg').value.trim();
  if (!text) { showAlert('❌ 請輸入公告內容','error'); return; }
  const data = await api('/admin/send','POST',{text:'📢 '+text});
  showAlert(`✅ 已發送到 ${data.ok}/${data.total} 個群組`);
  document.getElementById('manual-msg').value='';
}

(async()=>{ await loadCategories(); await loadCourses(); })();
</script>
</body>
</html>"""

def check_admin(req):
    return req.headers.get("X-Admin-Pass") == ADMIN_PASSWORD

@app.route("/admin")
def admin_page():
    return render_template_string(ADMIN_HTML)

# ── 分類 API ──

@app.route("/admin/categories", methods=["GET"])
def get_categories():
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    conn = get_db()
    cats = conn.execute("SELECT * FROM categories ORDER BY name").fetchall()
    conn.close()
    return jsonify({"categories":[dict(r) for r in cats]})

@app.route("/admin/categories", methods=["POST"])
def add_category():
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    data = request.json
    name = data.get("name","").strip()
    color = data.get("color","#06C755")
    if not name: return jsonify({"ok":False,"error":"請輸入分類名稱"})
    try:
        conn = get_db()
        conn.execute("INSERT INTO categories (name,color,created_at) VALUES (?,?,?)",
                     (name, color, datetime.now().isoformat()))
        conn.commit(); conn.close()
        return jsonify({"ok":True})
    except Exception as e:
        return jsonify({"ok":False,"error":"分類名稱重複"})

@app.route("/admin/categories/<int:cat_id>", methods=["DELETE"])
def delete_category(cat_id):
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    conn = get_db()
    conn.execute("UPDATE courses SET category_id=NULL WHERE category_id=?", (cat_id,))
    conn.execute("DELETE FROM categories WHERE id=?", (cat_id,))
    conn.commit(); conn.close()
    return jsonify({"ok":True})

# ── 課程 API ──

@app.route("/admin/courses", methods=["GET"])
def get_courses():
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    conn = get_db()
    courses = conn.execute("""
        SELECT c.*, COUNT(cr.id) as remind_count, COALESCE(SUM(cr.sent),0) as sent_count
        FROM courses c
        LEFT JOIN course_reminders cr ON c.id=cr.course_id
        GROUP BY c.id ORDER BY c.course_date ASC
    """).fetchall()
    conn.close()
    return jsonify({"courses":[dict(r) for r in courses]})

@app.route("/admin/courses", methods=["POST"])
def add_course():
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    d = request.json
    title = d.get("title","").strip()
    course_date = d.get("course_date","")
    if not title or not course_date: return jsonify({"ok":False,"error":"請填寫課程名稱和日期"})
    days_before = int(d.get("remind_days_before",30))
    interval = d.get("remind_interval","weekly")
    cat_id = d.get("category_id") or None
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO courses (title,category_id,course_date,course_time,location,description,remind_days_before,remind_interval,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (title, cat_id, course_date, d.get("course_time","09:00"),
         d.get("location",""), d.get("description",""), days_before, interval, datetime.now().isoformat())
    )
    course_id = cur.lastrowid
    conn.commit(); conn.close()
    remind_dates = generate_reminders(course_id, course_date, days_before, interval)
    return jsonify({"ok":True,"course_id":course_id,"remind_count":len(remind_dates)})

@app.route("/admin/courses/<int:course_id>", methods=["DELETE"])
def delete_course(course_id):
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    conn = get_db()
    conn.execute("DELETE FROM course_reminders WHERE course_id=?", (course_id,))
    conn.execute("DELETE FROM courses WHERE id=?", (course_id,))
    conn.commit(); conn.close()
    return jsonify({"ok":True})

@app.route("/admin/send", methods=["POST"])
def admin_send():
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    text = request.json.get("text","")
    ok, total = push_to_groups(text)
    return jsonify({"ok":ok,"total":total})

@app.route("/admin/check-reminders", methods=["POST"])
def trigger_reminders():
    if request.headers.get("X-Admin-Pass") != ADMIN_PASSWORD: return jsonify({"error":"unauthorized"}),401
    check_and_send_reminders()
    return jsonify({"ok":True,"date":date.today().isoformat()})

# ── LINE Webhook ──

def handle_text_message(event):
    user_id = event["source"].get("userId","")
    reply_token = event["replyToken"]
    text = event["message"]["text"].strip()
    if event["source"]["type"] == "group":
        gid = event["source"]["groupId"]
        conn = get_db()
        conn.execute("INSERT OR IGNORE INTO groups (group_id,joined_at) VALUES (?,?)", (gid, datetime.now().isoformat()))
        conn.commit(); conn.close()
    if user_id not in ADMIN_USER_IDS: return
    if text.startswith("/公告 "):
        ok, total = push_to_groups(f"📢 {text[4:].strip()}")
        reply_message(reply_token, f"✅ 已發送到 {ok}/{total} 個群組")
    elif text in ("/說明","/help"):
        reply_message(reply_token, "📋 指令\n/公告 [內容] 立即發公告\n\n課程管理：\nhttps://ipapalinebot.onrender.com/admin")

def handle_join(event):
    if event["source"]["type"] == "group":
        gid = event["source"]["groupId"]
        conn = get_db()
        conn.execute("INSERT OR IGNORE INTO groups (group_id,joined_at) VALUES (?,?)", (gid, datetime.now().isoformat()))
        conn.commit(); conn.close()

def handle_leave(event):
    if event["source"]["type"] == "group":
        gid = event["source"]["groupId"]
        conn = get_db()
        conn.execute("DELETE FROM groups WHERE group_id=?", (gid,))
        conn.commit(); conn.close()

@app.route("/webhook", methods=["POST"])
def webhook():
    sig = request.headers.get("X-Line-Signature","")
    body = request.get_data()
    if not verify_signature(body, sig): abort(400)
    for event in json.loads(body).get("events",[]):
        t = event.get("type")
        if t=="message" and event["message"]["type"]=="text": handle_text_message(event)
        elif t=="join": handle_join(event)
        elif t=="leave": handle_leave(event)
    return "OK"

@app.route("/")
def index():
    return 'LINE 公告機器人 ✅ | <a href="/admin">管理後台</a>'

if __name__=="__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)))
