import os, json, hashlib, hmac, base64, sqlite3, logging, requests
from datetime import datetime, date, timedelta
from flask import Flask, request, abort, jsonify, render_template_string

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
ADMIN_USER_IDS = [x.strip() for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip()]
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "ipapa2026")
DATABASE = os.environ.get("DATABASE_PATH", "bot.db")
DEFAULT_GROUP_IDS = [x.strip() for x in os.environ.get("DEFAULT_GROUP_IDS", "").split(",") if x.strip()]

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
            category_id INTEGER DEFAULT NULL,
            title TEXT NOT NULL,
            course_date TEXT NOT NULL,
            course_time TEXT DEFAULT '09:00',
            location TEXT DEFAULT '',
            description TEXT DEFAULT '',
            image_url TEXT DEFAULT '',
            remind_days_before INTEGER DEFAULT 30,
            remind_interval_days INTEGER DEFAULT 7,
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
        CREATE TABLE IF NOT EXISTS announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            group_count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS scheduled_broadcasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            image_url TEXT DEFAULT '',
            interval_hours REAL NOT NULL DEFAULT 24,
            start_time TEXT NOT NULL,
            next_run TEXT NOT NULL,
            active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        );
    """)
    # 加入預設分類
    for cat in [("招商活動", "#FF6B35"), ("系統會議", "#1A73E8"), ("課程培訓", "#06C755"), ("其他", "#9E9E9E")]:
        conn.execute("INSERT OR IGNORE INTO categories (name, color, created_at) VALUES (?,?,?)",
                     (cat[0], cat[1], datetime.now().isoformat()))
    conn.commit()
    conn.close()
    logger.info("DB initialized")

init_db()

# ── LINE ──
def verify_signature(body, sig):
    h = hmac.new(LINE_CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(h).decode(), sig)

def get_all_group_ids():
    """取得所有群組 ID（DB + 環境變數）"""
    conn = get_db()
    db_groups = [r["group_id"] for r in conn.execute("SELECT group_id FROM groups").fetchall()]
    conn.close()
    all_groups = list(set(db_groups + DEFAULT_GROUP_IDS))
    return all_groups

def push_to_groups(messages):
    """推播到所有群組，messages 是 LINE message objects 的 list"""
    groups = get_all_group_ids()
    logger.info(f"Pushing to {len(groups)} groups: {groups}")
    ok = 0
    for gid in groups:
        r = requests.post("https://api.line.me/v2/bot/message/push", headers=HEADERS,
            json={"to": gid, "messages": messages})
        logger.info(f"Push {gid[:15]}: {r.status_code} {r.text[:100]}")
        if r.status_code == 200:
            ok += 1
    return ok, len(groups)

def push_text_to_groups(text):
    return push_to_groups([{"type": "text", "text": text}])

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
        SELECT cr.id, c.title, c.course_date, c.course_time, c.location, c.description, c.image_url,
               cat.name as category_name
        FROM course_reminders cr
        JOIN courses c ON cr.course_id=c.id
        LEFT JOIN categories cat ON c.category_id=cat.id
        WHERE cr.remind_date=? AND cr.sent=0
    """, (today,)).fetchall()
    for row in rows:
        cd = datetime.strptime(row["course_date"], "%Y-%m-%d").date()
        days_left = (cd - date.today()).days
        timing = "【今天上課】" if days_left == 0 else f"【還有 {days_left} 天】"
        cat = f"[{row['category_name']}] " if row["category_name"] else ""
        text = f"📚 課程提醒 {timing}\n━━━━━━━━━━━━\n{cat}📌 {row['title']}\n📅 {row['course_date']} {row['course_time']}"
        if row["location"]: text += f"\n📍 {row['location']}"
        if row["description"]: text += f"\n📝 {row['description']}"

        messages = []
        if row["image_url"]:
            messages.append({"type": "image", "originalContentUrl": row["image_url"], "previewImageUrl": row["image_url"]})
        messages.append({"type": "text", "text": text})

        ok, total = push_to_groups(messages)
        if ok > 0:
            conn.execute("UPDATE course_reminders SET sent=1 WHERE id=?", (row["id"],))
    conn.commit()
    conn.close()

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
.container{max-width:900px;margin:24px auto;padding:0 16px}
.card{background:#fff;border-radius:12px;padding:24px;margin-bottom:20px;box-shadow:0 2px 8px rgba(0,0,0,.08)}
.card h2{font-size:15px;color:#06C755;margin-bottom:16px;padding-bottom:8px;border-bottom:2px solid #e8f5e9;cursor:pointer;display:flex;justify-content:space-between;align-items:center}
.card-body{transition:all .3s}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.full{grid-column:1/-1}
label{font-size:13px;color:#666;font-weight:500;display:block;margin-bottom:4px}
input,textarea,select{width:100%;border:1px solid #ddd;border-radius:8px;padding:8px 12px;font-size:14px}
textarea{min-height:70px;resize:vertical}
.hint{font-size:12px;color:#888;background:#f8f9fa;padding:8px 12px;border-radius:8px;margin-top:8px;line-height:1.6}
.btn{padding:10px 20px;border:none;border-radius:8px;cursor:pointer;font-size:14px;font-weight:600;transition:opacity .2s}
.btn-green{background:#06C755;color:#fff}.btn-blue{background:#1a73e8;color:#fff}
.btn-red{background:#e53935;color:#fff;padding:6px 12px;font-size:12px}
.btn-gray{background:#f5f5f5;color:#333;padding:6px 12px;font-size:12px}
.btn:hover{opacity:.85}
.cat-section{margin-bottom:16px;border:1px solid #e8f5e9;border-radius:10px;overflow:hidden}
.cat-header{background:#e8f5e9;padding:12px 16px;cursor:pointer;display:flex;justify-content:space-between;align-items:center;font-weight:600}
.cat-header .dot{width:10px;height:10px;border-radius:50%;display:inline-block;margin-right:8px}
.cat-body{display:none;padding:0}
.cat-body.open{display:block}
table{width:100%;border-collapse:collapse;font-size:14px}
th{background:#f8f9fa;padding:10px 12px;text-align:left;font-weight:600;color:#555}
td{padding:10px 12px;border-bottom:1px solid #f0f0f0;vertical-align:middle}
.badge{display:inline-block;padding:2px 8px;border-radius:12px;font-size:12px;font-weight:600}
.badge-green{background:#e8f5e9;color:#2e7d32}.badge-gray{background:#f5f5f5;color:#777}.badge-orange{background:#fff3e0;color:#e65100}
.alert{padding:12px 16px;border-radius:8px;margin-bottom:16px;font-size:14px}
.alert-ok{background:#e8f5e9;color:#2e7d32}.alert-err{background:#ffebee;color:#c62828}
.sub{font-size:12px;color:#999}
.img-preview{width:80px;height:60px;object-fit:cover;border-radius:6px;display:none}
.cat-tags{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}
.cat-tag{padding:4px 12px;border-radius:20px;font-size:13px;cursor:pointer;border:2px solid transparent}
.cat-tag.active{border-color:#333}
@media(max-width:600px){.grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="header">
  <span style="font-size:28px">📢</span>
  <div style="flex:1"><h1>ipapa 課程公告管理</h1><p id="grp-count">載入中...</p></div><button onclick="doLogout()" style="background:rgba(255,255,255,.2);border:none;color:#fff;padding:6px 14px;border-radius:8px;cursor:pointer;font-size:13px">登出</button>
</div>

<!-- 登入畫面 -->
<div id="login-screen" style="display:none;position:fixed;inset:0;background:#f0f4f8;z-index:9999;align-items:center;justify-content:center;flex-direction:column;gap:16px">
  <div style="background:#fff;border-radius:16px;padding:40px;box-shadow:0 4px 20px rgba(0,0,0,.1);text-align:center;width:320px">
    <div style="font-size:48px;margin-bottom:12px">📢</div>
    <h2 style="color:#06C755;margin-bottom:4px">ipapa 管理後台</h2>
    <p style="color:#999;font-size:13px;margin-bottom:24px">請輸入管理密碼</p>
    <input id="pw-input" type="password" placeholder="管理密碼" style="width:100%;margin-bottom:12px;text-align:center;font-size:16px"
      onkeydown="if(event.key==='Enter')doLogin()">
    <p id="login-err" style="color:#e53935;font-size:13px;margin-bottom:8px;min-height:18px"></p>
    <button class="btn btn-green" style="width:100%" onclick="doLogin()">登入</button>
  </div>
</div>
<div class="container">
<div id="alert-box"></div>

<!-- 立即發公告 -->
<div class="card">
  <h2 onclick="toggle('send-body')">📣 立即發公告 <span>▼</span></h2>
  <div id="send-body" class="card-body">
    <div class="grid" style="margin-bottom:12px">
      <div class="full"><label>公告內容</label>
        <textarea id="manual-msg" placeholder="輸入公告內容..." style="min-height:80px"></textarea>
      </div>
      <div class="full"><label>附加圖片（選填，貼上圖片網址）</label>
        <input id="manual-img" type="url" placeholder="https://example.com/image.jpg" oninput="previewImg('manual-img','manual-img-preview')">
        <img id="manual-img-preview" class="img-preview" style="margin-top:6px">
      </div>
    </div>
    <button class="btn btn-blue" onclick="sendManual()">立即發送到所有群組</button>
  </div>
</div>

<!-- 分類管理 -->
<div class="card">
  <h2 onclick="toggle('cat-body')">🏷️ 分類管理 <span>▼</span></h2>
  <div id="cat-body" class="card-body">
    <div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap">
      <input id="new-cat-name" placeholder="新分類名稱" style="width:160px">
      <input id="new-cat-color" type="color" value="#06C755" style="width:48px;padding:2px;height:38px">
      <button class="btn btn-green" style="padding:8px 16px" onclick="addCategory()">新增分類</button>
    </div>
    <div id="cat-list"></div>
  </div>
</div>

<!-- 新增課程 -->
<div class="card">
  <h2 onclick="toggle('add-body')">➕ 新增課程 <span>▼</span></h2>
  <div id="add-body" class="card-body">
    <div class="grid">
      <div class="full"><label>課程名稱 *</label><input id="f-title" placeholder="例：2026 春季瑜珈課程"></div>
      <div class="full"><label>選擇分類</label><div id="cat-selector" class="cat-tags"></div></div>
      <div><label>課程日期 *</label><input id="f-date" type="date"></div>
      <div><label>上課時間</label><input id="f-time" type="time" value="09:00"></div>
      <div class="full"><label>地點</label><input id="f-loc" placeholder="例：台南市XX教室"></div>
      <div class="full"><label>課程說明（選填）</label><textarea id="f-desc" placeholder="報名連結、注意事項..."></textarea></div>
      <div class="full">
        <label>課程圖片（選填，貼上圖片網址）</label>
        <input id="f-img" type="url" placeholder="https://example.com/image.jpg" oninput="previewImg('f-img','f-img-preview')">
        <img id="f-img-preview" class="img-preview" style="margin-top:6px">
      </div>
      <div><label>提前幾天開始提醒</label><input id="f-before" type="number" value="30" min="1" max="365"></div>
      <div><label>每隔幾天發一次</label><input id="f-interval" type="number" value="7" min="1" max="30"></div>
      <div class="full"><div id="preview-hint" class="hint" style="display:none"></div></div>
    </div>
    <button class="btn btn-green" style="margin-top:16px" onclick="addCourse()">✅ 新增課程</button>
  </div>
</div>



<!-- 定時發送 -->
<div class="card">
  <h2 onclick="toggle('sched-body')">⏰ 定時發送管理 <span>▼</span></h2>
  <div id="sched-body" class="card-body">
    <p style="font-size:13px;color:#888;margin-bottom:16px">設定自動定時發送的公告，例如每天早安訊息、每週通知等</p>
    
    <!-- 新增定時發送 -->
    <div style="border:1px solid #e8f5e9;border-radius:10px;padding:16px;margin-bottom:16px">
      <p style="font-weight:600;color:#06C755;margin-bottom:12px">➕ 新增定時發送</p>
      <div class="grid">
        <div class="full"><label>名稱（備註用）</label><input id="s-title" placeholder="例：每日早安訊息"></div>
        <div class="full"><label>公告內容</label><textarea id="s-content" placeholder="例：📢 早安！今天也請準時上工 ☀️" style="min-height:70px"></textarea></div>
        <div class="full"><label>圖片網址（選填）</label><input id="s-img" type="url" placeholder="https://example.com/image.jpg"></div>
        <div>
          <label>發送間隔</label>
          <select id="s-interval">
            <option value="1">每 1 小時</option>
            <option value="2">每 2 小時</option>
            <option value="4">每 4 小時</option>
            <option value="6">每 6 小時</option>
            <option value="12">每 12 小時</option>
            <option value="24" selected>每 24 小時（每天）</option>
            <option value="48">每 2 天</option>
            <option value="72">每 3 天</option>
            <option value="168">每週</option>
            <option value="custom">自訂</option>
          </select>
        </div>
        <div id="custom-interval-div" style="display:none">
          <label>自訂間隔（小時）</label>
          <input id="s-custom-hours" type="number" min="0.5" step="0.5" value="24" placeholder="小時數">
        </div>
        <div>
          <label>第一次發送時間</label>
          <input id="s-start" type="datetime-local">
        </div>
      </div>
      <button class="btn btn-green" style="margin-top:12px" onclick="addScheduled()">➕ 新增定時發送</button>
    </div>

    <!-- 定時發送列表 -->
    <div id="sched-list"><p style="color:#aaa;text-align:center;padding:16px">載入中...</p></div>
  </div>
</div>
<!-- AI 新增課程 -->
<div class="card">
  <h2 onclick="toggle('ai-body')">🤖 AI 智慧新增課程 <span>▼</span></h2>
  <div id="ai-body" class="card-body">
    <p style="font-size:13px;color:#888;margin-bottom:12px">用自然語言描述課程，AI 自動解析並填入表單</p>
    <div style="display:flex;gap:8px;margin-bottom:12px">
      <textarea id="ai-input" placeholder="例：下個月20號早上十點在台北信義區有招商說明會，請幫我設定提醒" style="flex:1;min-height:70px"></textarea>
    </div>
    <button class="btn btn-green" onclick="aiParse()" id="ai-btn">🤖 AI 解析</button>
    <div id="ai-result" style="display:none;margin-top:16px;padding:16px;background:#f8f9fa;border-radius:8px;font-size:14px"></div>
  </div>
</div>
<!-- 課程列表（依分類展開） -->
<div class="card">
  <h2>📅 課程列表</h2>
  <div id="course-list">載入中...</div>
</div>

</div>
<script>
let pw='', categories=[], selectedCatId=null;

function init(){
  // 從網址參數或 cookie 取得密碼
  const urlParams = new URLSearchParams(window.location.search);
  const urlPw = urlParams.get('pw');
  const cookiePw = getCookie('admin_pw');
  pw = urlPw || cookiePw || '';
  
  if(pw){
    api('/admin/groups').then(data => {
      if(data.error === 'unauthorized'){
        pw = '';
        document.getElementById('login-screen').style.display='flex';
      } else {
        setCookie('admin_pw', pw, 7);
        document.getElementById('login-screen').style.display='none';
        loadAll();
        setupPreview();
      }
    });
  } else {
    document.getElementById('login-screen').style.display='flex';
  }
}

function getCookie(name){
  const v = document.cookie.match('(^|;) ?'+name+'=([^;]*)(;|$)');
  return v ? v[2] : '';
}

function setCookie(name, value, days){
  const d = new Date();
  d.setTime(d.getTime() + days*24*60*60*1000);
  document.cookie = name+'='+value+';expires='+d.toUTCString()+';path=/';
}

function doLogin(){
  const input = document.getElementById('pw-input').value.trim();
  if(!input) return;
  pw = input;
  api('/admin/groups').then(data => {
    if(data.error === 'unauthorized'){
      document.getElementById('login-err').textContent = '密碼錯誤，請再試一次';
      pw = '';
    } else {
      setCookie('admin_pw', pw, 7);
      document.getElementById('login-screen').style.display='none';
      loadAll();
      setupPreview();
    }
  });
}

function doLogout(){
  document.cookie = 'admin_pw=;expires=Thu, 01 Jan 1970 00:00:00 UTC;path=/';
  location.href='/admin';
}

function toggle(id){
  const el=document.getElementById(id);
  el.style.display=el.style.display==='none'?'block':'none';
}

function showAlert(msg,type='ok'){
  const el=document.getElementById('alert-box');
  el.innerHTML=`<div class="alert alert-${type}">${msg}</div>`;
  setTimeout(()=>el.innerHTML='',4000);
}

async function api(path,method='GET',body=null){
  const opts={method,headers:{'Content-Type':'application/json','X-Admin-Pass':pw}};
  if(body) opts.body=JSON.stringify(body);
  try{const r=await fetch(path,opts);return await r.json();}
  catch(e){return{error:e.message};}
}

function previewImg(inputId,previewId){
  const url=document.getElementById(inputId).value.trim();
  const img=document.getElementById(previewId);
  if(url){img.src=url;img.style.display='block';}
  else{img.style.display='none';}
}

async function loadAll(){
  await loadCategories();
  await loadCourses();
  loadGroupCount();
  loadScheduled();
}

async function loadGroupCount(){
  const data=await api('/admin/groups');
  if(data.count!==undefined){
    document.getElementById('grp-count').textContent=`已連接 ${data.count} 個群組`;
  } else {
    document.getElementById('grp-count').textContent='LINE 公告機器人';
  }
}

async function loadCategories(){
  const data=await api('/admin/categories');
  if(!data.categories){return;}
  categories=data.categories;
  // 分類管理列表
  const catList=document.getElementById('cat-list');
  if(!categories.length){catList.innerHTML='<p class="sub">尚無分類</p>';return;}
  catList.innerHTML=categories.map(c=>`
    <span style="display:inline-flex;align-items:center;gap:6px;background:#f5f5f5;border-radius:20px;padding:4px 12px;margin:4px">
      <span style="width:10px;height:10px;border-radius:50%;background:${c.color};display:inline-block"></span>
      ${c.name}
      <button class="btn btn-red" style="padding:2px 6px;font-size:11px" onclick="deleteCategory(${c.id})">✕</button>
    </span>
  `).join('');
  // 新增課程的分類選擇器
  const sel=document.getElementById('cat-selector');
  sel.innerHTML='<span class="cat-tag '+(selectedCatId===null?'active':'')+'" onclick="selectCat(null,this)" style="background:#f5f5f5">不分類</span>'
    +categories.map(c=>`<span class="cat-tag ${selectedCatId===c.id?'active':''}" onclick="selectCat(${c.id},this)" style="background:${c.color}20;color:${c.color}">${c.name}</span>`).join('');
}

function selectCat(id,el){
  selectedCatId=id;
  document.querySelectorAll('.cat-tag').forEach(t=>t.classList.remove('active'));
  el.classList.add('active');
}

async function addCategory(){
  const name=document.getElementById('new-cat-name').value.trim();
  const color=document.getElementById('new-cat-color').value;
  if(!name){showAlert('請輸入分類名稱','err');return;}
  const data=await api('/admin/categories','POST',{name,color});
  if(data.ok){document.getElementById('new-cat-name').value='';loadAll();}
  else showAlert(data.error||'新增失敗','err');
}

async function deleteCategory(id){
  if(!confirm('確定刪除此分類？該分類的課程將變為未分類'))return;
  const data=await api(`/admin/categories/${id}`,'DELETE');
  if(data.ok){loadAll();}
}

function setupPreview(){
  ['f-date','f-before','f-interval'].forEach(id=>{
    const el=document.getElementById(id);
    if(el) el.addEventListener('input',updatePreview);
  });
}

function updatePreview(){
  const dateVal=document.getElementById('f-date').value;
  const before=parseInt(document.getElementById('f-before').value)||30;
  const interval=parseInt(document.getElementById('f-interval').value)||7;
  const hint=document.getElementById('preview-hint');
  if(!dateVal){hint.style.display='none';return;}
  const courseDate=new Date(dateVal);
  const start=new Date(courseDate);start.setDate(start.getDate()-before);
  const dates=[];let d=new Date(start);
  while(d<=courseDate){
    dates.push(d.toISOString().slice(5,10));
    d.setDate(d.getDate()+interval);
  }
  const last=courseDate.toISOString().slice(5,10);
  if(!dates.includes(last))dates.push(last+'(當天)');
  hint.style.display='block';
  hint.innerHTML=`📅 預計發送 <strong>${dates.length}</strong> 次：${dates.join(' → ')}`;
}

async function loadCourses(){
  const data=await api('/admin/courses');
  const el=document.getElementById('course-list');
  if(data.error==='unauthorized'){el.innerHTML='<p style="color:red">密碼錯誤</p>';return;}
  if(!data.courses||!data.courses.length){el.innerHTML='<p style="color:#aaa;text-align:center;padding:20px">尚未新增任何課程</p>';return;}
  const today=new Date().toISOString().slice(0,10);
  // 依分類分組
  const grouped={};
  data.courses.forEach(c=>{
    const key=c.category_name||'未分類';
    const color=c.category_color||'#9E9E9E';
    if(!grouped[key])grouped[key]={color,courses:[]};
    grouped[key].courses.push(c);
  });
  let html='';
  for(const [catName,group] of Object.entries(grouped)){
    html+=`<div class="cat-section">
      <div class="cat-header" onclick="this.nextElementSibling.classList.toggle('open')">
        <span><span class="dot" style="background:${group.color}"></span>${catName}（${group.courses.length} 堂）</span>
        <span>▼</span>
      </div>
      <div class="cat-body open">
        <table><tr><th>課程名稱</th><th>日期</th><th>地點</th><th>提醒</th><th>狀態</th><th>操作</th></tr>`;
    for(const c of group.courses){
      const isPast=c.course_date<today;
      const isToday=c.course_date===today;
      const badge=isPast?'<span class="badge badge-gray">已結束</span>':isToday?'<span class="badge badge-orange">今天</span>':'<span class="badge badge-green">排程中</span>';
      const imgTag=c.image_url?`<img src="${c.image_url}" style="width:40px;height:30px;object-fit:cover;border-radius:4px;margin-left:4px">`:''
      html+=`<tr>
        <td><strong>${c.title}</strong>${imgTag}${c.description?'<br><span class="sub">'+c.description.slice(0,25)+'</span>':''}</td>
        <td>${c.course_date}<br><span class="sub">${c.course_time}</span></td>
        <td>${c.location||'-'}</td>
        <td><span class="sub">${c.remind_count}次/已發${c.sent_count||0}</span></td>
        <td>${badge}</td>
        <td style="display:flex;gap:4px;flex-wrap:wrap">
          <button class="btn btn-gray" onclick="openEdit(${JSON.stringify(c).split('&quot;').join('&amp;quot;').split('"').join('&quot;')})">編輯</button>
          <button class="btn btn-blue" style="padding:6px 10px;font-size:12px" onclick="sendCourseNow(${c.id})">立即發送</button>
          <button class="btn btn-red" onclick="deleteCourse(${c.id})">刪除</button>
        </td>
      </tr>`;
    }
    html+='</table></div></div>';
  }
  el.innerHTML=html;
}

async function addCourse(){
  const title=document.getElementById('f-title').value.trim();
  let courseDate=document.getElementById('f-date').value.split('/').join('-');
  if(!title||!courseDate){showAlert('請填寫課程名稱和日期','err');return;}
  const data=await api('/admin/courses','POST',{
    title, course_date:courseDate,
    category_id:selectedCatId,
    course_time:document.getElementById('f-time').value,
    location:document.getElementById('f-loc').value,
    description:document.getElementById('f-desc').value,
    image_url:document.getElementById('f-img').value.trim(),
    remind_days_before:parseInt(document.getElementById('f-before').value)||30,
    remind_interval_days:parseInt(document.getElementById('f-interval').value)||7,
  });
  if(data.ok){
    showAlert(`✅ 課程已新增，產生 ${data.remind_count} 個提醒日期`);
    ['f-title','f-date','f-loc','f-desc','f-img'].forEach(id=>document.getElementById(id).value='');
    document.getElementById('f-img-preview').style.display='none';
    document.getElementById('preview-hint').style.display='none';
    selectedCatId=null;
    loadAll();
  } else showAlert(data.error||'新增失敗','err');
}

async function deleteCourse(id){
  if(!confirm('確定刪除？'))return;
  const data=await api(`/admin/courses/${id}`,'DELETE');
  if(data.ok){showAlert('已刪除');loadCourses();}
}

async function sendManual(){
  const text=document.getElementById('manual-msg').value.trim();
  const imgUrl=document.getElementById('manual-img').value.trim();
  if(!text&&!imgUrl){showAlert('請輸入公告內容','err');return;}
  const data=await api('/admin/send','POST',{text:text?'📢 '+text:'',image_url:imgUrl});
  if(data.error){showAlert('發送失敗：'+data.error,'err');return;}
  showAlert(`✅ 已發送到 ${data.ok}/${data.total} 個群組`);
  document.getElementById('manual-msg').value='';
  document.getElementById('manual-img').value='';
  document.getElementById('manual-img-preview').style.display='none';
}





// 自訂間隔顯示
document.getElementById('s-interval').addEventListener('change', function(){
  document.getElementById('custom-interval-div').style.display = this.value==='custom' ? 'block' : 'none';
});

// 預設第一次發送時間為明天早上8點
(function(){
  const d = new Date();
  d.setDate(d.getDate()+1);
  d.setHours(8,0,0,0);
  const pad = n => String(n).padStart(2,'0');
  document.getElementById('s-start').value = 
    `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
})();

async function loadScheduled() {
  const data = await api('/admin/scheduled');
  const el = document.getElementById('sched-list');
  if (!data.schedules || !data.schedules.length) {
    el.innerHTML = '<p style="color:#aaa;text-align:center;padding:16px">尚未設定定時發送</p>';
    return;
  }
  const intervalLabel = h => {
    if (h < 1) return `每 ${h*60} 分鐘`;
    if (h === 1) return '每 1 小時';
    if (h === 24) return '每天';
    if (h === 168) return '每週';
    return `每 ${h} 小時`;
  };
  let html = '<table><tr><th>名稱</th><th>間隔</th><th>下次發送</th><th>狀態</th><th>操作</th></tr>';
  for (const s of data.schedules) {
    const nextRun = new Date(s.next_run).toLocaleString('zh-TW');
    const statusBadge = s.active 
      ? '<span class="badge badge-green">啟用中</span>'
      : '<span class="badge badge-gray">已暫停</span>';
    html += `<tr>
      <td><strong>${s.title}</strong><br><span class="sub">${s.content.slice(0,30)}${s.content.length>30?'...':''}</span></td>
      <td><span class="sub">${intervalLabel(s.interval_hours)}</span></td>
      <td><span class="sub">${nextRun}</span></td>
      <td>${statusBadge}</td>
      <td style="display:flex;gap:4px;flex-wrap:wrap">
        <button class="btn btn-blue" style="padding:6px 10px;font-size:12px" onclick="sendScheduledNow(${s.id})">立即發</button>
        <button class="btn btn-gray" style="padding:6px 10px;font-size:12px" onclick="toggleScheduled(${s.id},${s.active?0:1})">${s.active?'暫停':'啟用'}</button>
        <button class="btn btn-red" onclick="deleteScheduled(${s.id})">刪除</button>
      </td>
    </tr>`;
  }
  html += '</table>';
  el.innerHTML = html;
}

async function addScheduled() {
  const title = document.getElementById('s-title').value.trim();
  const content_text = document.getElementById('s-content').value.trim();
  if (!title || !content_text) { showAlert('請填寫名稱和內容', 'err'); return; }
  const intervalSel = document.getElementById('s-interval').value;
  const interval_hours = intervalSel === 'custom' 
    ? parseFloat(document.getElementById('s-custom-hours').value) 
    : parseFloat(intervalSel);
  const startVal = document.getElementById('s-start').value;
  const data = await api('/admin/scheduled', 'POST', {
    title, content: '📢 ' + content_text,
    image_url: document.getElementById('s-img').value.trim(),
    interval_hours,
    start_time: startVal ? new Date(startVal).toISOString() : new Date().toISOString(),
  });
  if (data.ok) {
    showAlert('✅ 定時發送已設定');
    document.getElementById('s-title').value = '';
    document.getElementById('s-content').value = '';
    document.getElementById('s-img').value = '';
    loadScheduled();
  } else showAlert(data.error || '新增失敗', 'err');
}

async function sendScheduledNow(id) {
  if (!confirm('立即發送此定時公告？')) return;
  const data = await api(`/admin/scheduled/${id}/send-now`, 'POST');
  showAlert(`✅ 已發送到 ${data.ok}/${data.total} 個群組`);
}

async function toggleScheduled(id, active) {
  await api(`/admin/scheduled/${id}`, 'PUT', { active });
  loadScheduled();
}

async function deleteScheduled(id) {
  if (!confirm('確定刪除此定時發送？')) return;
  await api(`/admin/scheduled/${id}`, 'DELETE');
  showAlert('已刪除');
  loadScheduled();
}

async function aiParse() {
  const text = document.getElementById('ai-input').value.trim();
  if (!text) { showAlert('請輸入課程描述', 'err'); return; }
  const btn = document.getElementById('ai-btn');
  btn.textContent = '⏳ AI 解析中...';
  btn.disabled = true;
  
  const data = await api('/admin/ai-parse', 'POST', { text });
  btn.textContent = '🤖 AI 解析';
  btn.disabled = false;
  
  if (!data.ok) { showAlert(data.error || 'AI 解析失敗', 'err'); return; }
  
  const c = data.course;
  const resultDiv = document.getElementById('ai-result');
  resultDiv.style.display = 'block';
  resultDiv.innerHTML = `
    <p style="font-weight:600;color:#06C755;margin-bottom:12px">✅ AI 解析結果，請確認後新增：</p>
    <div class="grid">
      <div><label>課程名稱</label><input id="ai-title" value="${c.title||''}"></div>
      <div><label>日期</label><input id="ai-date" type="date" value="${c.course_date||''}"></div>
      <div><label>時間</label><input id="ai-time" type="time" value="${c.course_time||'09:00'}"></div>
      <div><label>地點</label><input id="ai-loc" value="${c.location||''}"></div>
      <div class="full"><label>說明</label><input id="ai-desc" value="${c.description||''}"></div>
      <div><label>提前幾天提醒</label><input id="ai-before" type="number" value="${c.remind_days_before||30}"></div>
      <div><label>每隔幾天</label><input id="ai-interval" type="number" value="${c.remind_interval_days||7}"></div>
    </div>
    <button class="btn btn-green" style="margin-top:12px" onclick="aiConfirmAdd()">✅ 確認新增到排程</button>
    <button class="btn btn-gray" style="margin-top:12px;margin-left:8px" onclick="document.getElementById('ai-result').style.display='none'">取消</button>
  `;
}

async function aiConfirmAdd() {
  const courseDate = document.getElementById('ai-date').value.split('/').join('-');
  const data = await api('/admin/courses', 'POST', {
    title: document.getElementById('ai-title').value.trim(),
    course_date: courseDate,
    course_time: document.getElementById('ai-time').value,
    location: document.getElementById('ai-loc').value,
    description: document.getElementById('ai-desc').value,
    image_url: '',
    category_id: selectedCatId,
    remind_days_before: parseInt(document.getElementById('ai-before').value)||30,
    remind_interval_days: parseInt(document.getElementById('ai-interval').value)||7,
  });
  if (data.ok) {
    showAlert(`✅ 課程已新增，產生 ${data.remind_count} 個提醒日期`);
    document.getElementById('ai-input').value = '';
    document.getElementById('ai-result').style.display = 'none';
    loadCourses();
  } else {
    showAlert(data.error || '新增失敗', 'err');
  }
}

async function sendCourseNow(id) {
  if (!confirm('確定立即發送此課程提醒到所有群組？')) return;
  const data = await api(`/admin/courses/${id}/send-now`, 'POST');
  if (data.error) { showAlert('發送失敗：' + data.error, 'err'); return; }
  showAlert(`✅ 已發送到 ${data.ok}/${data.total} 個群組`);
}

let editCatId = null;

function openEdit(c) {
  if (typeof c === 'string') c = JSON.parse(c);
  editCatId = c.category_id || null;
  document.getElementById('e-id').value = c.id;
  document.getElementById('e-title').value = c.title;
  document.getElementById('e-date').value = c.course_date;
  document.getElementById('e-time').value = c.course_time;
  document.getElementById('e-loc').value = c.location || '';
  document.getElementById('e-desc').value = c.description || '';
  document.getElementById('e-before').value = c.remind_days_before || 30;
  document.getElementById('e-interval').value = c.remind_interval_days || 7;
  const imgUrl = c.image_url || '';
  document.getElementById('e-img').value = imgUrl;
  const preview = document.getElementById('e-img-preview');
  if (imgUrl) { preview.src = imgUrl; preview.style.display = 'block'; }
  else preview.style.display = 'none';
  // 分類選擇器
  const sel = document.getElementById('e-cat-selector');
  sel.innerHTML = '<span class="cat-tag '+(editCatId===null?'active':'')+'" onclick="selectEditCat(null,this)" style="background:#f5f5f5">不分類</span>'
    + categories.map(cat=>`<span class="cat-tag ${editCatId===cat.id?'active':''}" onclick="selectEditCat(${cat.id},this)" style="background:${cat.color}20;color:${cat.color}">${cat.name}</span>`).join('');
  // 預覽排程
  ['e-date','e-before','e-interval'].forEach(id => {
    document.getElementById(id).addEventListener('input', updateEditPreview);
  });
  updateEditPreview();
  document.getElementById('edit-modal').style.display = 'block';
}

function closeEdit() {
  document.getElementById('edit-modal').style.display = 'none';
}

function selectEditCat(id, el) {
  editCatId = id;
  document.querySelectorAll('#e-cat-selector .cat-tag').forEach(t=>t.classList.remove('active'));
  el.classList.add('active');
}

function updateEditPreview() {
  const dateVal = document.getElementById('e-date').value;
  const before = parseInt(document.getElementById('e-before').value) || 30;
  const interval = parseInt(document.getElementById('e-interval').value) || 7;
  const hint = document.getElementById('e-preview-hint');
  if (!dateVal) { hint.style.display='none'; return; }
  const courseDate = new Date(dateVal);
  const start = new Date(courseDate); start.setDate(start.getDate()-before);
  const dates=[]; let d=new Date(start);
  while(d<=courseDate) { dates.push(d.toISOString().slice(5,10)); d.setDate(d.getDate()+interval); }
  const last = courseDate.toISOString().slice(5,10);
  if(!dates.includes(last)) dates.push(last+'(當天)');
  hint.style.display='block';
  hint.innerHTML=`📅 預計發送 <strong>${dates.length}</strong> 次：${dates.join(' → ')}`;
}

async function saveEdit() {
  const id = document.getElementById('e-id').value;
  const title = document.getElementById('e-title').value.trim();
  let courseDate = document.getElementById('e-date').value.split('/').join('-');
  if (!title || !courseDate) { showAlert('請填寫課程名稱和日期','err'); return; }
  const data = await api(`/admin/courses/${id}`, 'PUT', {
    title, course_date: courseDate,
    category_id: editCatId,
    course_time: document.getElementById('e-time').value,
    location: document.getElementById('e-loc').value,
    description: document.getElementById('e-desc').value,
    image_url: document.getElementById('e-img').value.trim(),
    remind_days_before: parseInt(document.getElementById('e-before').value)||30,
    remind_interval_days: parseInt(document.getElementById('e-interval').value)||7,
  });
  if (data.ok) {
    showAlert(`✅ 已更新，重新產生 ${data.remind_count} 個提醒日期`);
    closeEdit();
    loadCourses();
  } else {
    showAlert(data.error||'儲存失敗','err');
  }
}

init();
</script>

<!-- 編輯課程 Modal -->
<div id="edit-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:999;overflow-y:auto">
  <div style="background:#fff;max-width:600px;margin:40px auto;border-radius:12px;padding:24px;position:relative">
    <h2 style="color:#06C755;margin-bottom:16px;font-size:16px">✏️ 編輯課程</h2>
    <input id="e-id" type="hidden">
    <div class="grid">
      <div class="full"><label>課程名稱 *</label><input id="e-title"></div>
      <div class="full"><label>分類</label><div id="e-cat-selector" class="cat-tags"></div></div>
      <div><label>課程日期 *</label><input id="e-date" type="date"></div>
      <div><label>上課時間</label><input id="e-time" type="time"></div>
      <div class="full"><label>地點</label><input id="e-loc"></div>
      <div class="full"><label>課程說明</label><textarea id="e-desc"></textarea></div>
      <div class="full">
        <label>課程圖片網址</label>
        <input id="e-img" type="url" oninput="previewImg('e-img','e-img-preview')">
        <img id="e-img-preview" class="img-preview" style="margin-top:6px">
      </div>
      <div><label>提前幾天提醒</label><input id="e-before" type="number" min="1" max="365"></div>
      <div><label>每隔幾天發一次</label><input id="e-interval" type="number" min="1" max="30"></div>
      <div class="full"><div id="e-preview-hint" class="hint" style="display:none"></div></div>
    </div>
    <div style="display:flex;gap:10px;margin-top:16px">
      <button class="btn btn-green" onclick="saveEdit()">✅ 儲存變更</button>
      <button class="btn btn-gray" onclick="closeEdit()">取消</button>
    </div>
  </div>
</div>
</body>
</html>"""

def check_admin(req):
    return req.headers.get("X-Admin-Pass") == ADMIN_PASSWORD

@app.route("/admin")
def admin_page():
    return render_template_string(ADMIN_HTML)

@app.route("/admin/groups")
def get_groups():
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    groups = get_all_group_ids()
    return jsonify({"count": len(groups), "groups": groups})

@app.route("/admin/categories", methods=["GET"])
def get_categories():
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    conn = get_db()
    rows = conn.execute("SELECT * FROM categories ORDER BY id").fetchall()
    conn.close()
    return jsonify({"categories": [dict(r) for r in rows]})

@app.route("/admin/categories", methods=["POST"])
def add_category():
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    d = request.json
    name = d.get("name","").strip()
    if not name: return jsonify({"ok":False,"error":"請填寫分類名稱"})
    try:
        conn = get_db()
        conn.execute("INSERT INTO categories (name,color,created_at) VALUES (?,?,?)",
                     (name, d.get("color","#06C755"), datetime.now().isoformat()))
        conn.commit()
        conn.close()
        return jsonify({"ok":True})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

@app.route("/admin/categories/<int:cid>", methods=["DELETE"])
def delete_category(cid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    conn = get_db()
    conn.execute("UPDATE courses SET category_id=NULL WHERE category_id=?", (cid,))
    conn.execute("DELETE FROM categories WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    return jsonify({"ok":True})

@app.route("/admin/courses", methods=["GET"])
def get_courses():
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    conn = get_db()
    rows = conn.execute("""
        SELECT c.*, cat.name as category_name, cat.color as category_color,
               COUNT(cr.id) as remind_count, SUM(cr.sent) as sent_count
        FROM courses c
        LEFT JOIN categories cat ON c.category_id=cat.id
        LEFT JOIN course_reminders cr ON c.id=cr.course_id
        GROUP BY c.id ORDER BY c.course_date ASC
    """).fetchall()
    conn.close()
    return jsonify({"courses": [dict(r) for r in rows]})

@app.route("/admin/courses", methods=["POST"])
def add_course():
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    d = request.json
    title = d.get("title","").strip()
    course_date = d.get("course_date","").replace("/","-")
    if not title or not course_date: return jsonify({"ok":False,"error":"請填寫課程名稱和日期"})
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO courses (category_id,title,course_date,course_time,location,description,image_url,remind_days_before,remind_interval_days,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (d.get("category_id"), title, course_date, d.get("course_time","09:00"),
         d.get("location",""), d.get("description",""), d.get("image_url",""),
         d.get("remind_days_before",30), d.get("remind_interval_days",7), datetime.now().isoformat())
    )
    course_id = cur.lastrowid
    conn.commit()
    conn.close()
    dates = generate_reminders(course_id, course_date, d.get("remind_days_before",30), d.get("remind_interval_days",7))
    return jsonify({"ok":True,"course_id":course_id,"remind_count":len(dates)})


@app.route("/admin/courses/<int:cid>", methods=["PUT"])
def edit_course(cid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    d = request.json
    title = d.get("title","").strip()
    course_date = d.get("course_date","").replace("/","-")
    if not title or not course_date: return jsonify({"ok":False,"error":"請填寫課程名稱和日期"})
    conn = get_db()
    conn.execute("""UPDATE courses SET category_id=?,title=?,course_date=?,course_time=?,
                    location=?,description=?,image_url=?,remind_days_before=?,remind_interval_days=?
                    WHERE id=?""",
        (d.get("category_id"), title, course_date, d.get("course_time","09:00"),
         d.get("location",""), d.get("description",""), d.get("image_url",""),
         d.get("remind_days_before",30), d.get("remind_interval_days",7), cid))
    conn.commit()
    conn.close()
    dates = generate_reminders(cid, course_date, d.get("remind_days_before",30), d.get("remind_interval_days",7))
    return jsonify({"ok":True,"remind_count":len(dates)})


@app.route("/admin/courses/<int:cid>/send-now", methods=["POST"])
def send_course_now(cid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    conn = get_db()
    row = conn.execute("""
        SELECT c.*, cat.name as category_name
        FROM courses c LEFT JOIN categories cat ON c.category_id=cat.id
        WHERE c.id=?
    """, (cid,)).fetchone()
    conn.close()
    if not row: return jsonify({"ok":False,"error":"找不到課程"})
    cd = datetime.strptime(row["course_date"], "%Y-%m-%d").date()
    days_left = (cd - date.today()).days
    if days_left < 0:
        timing = "【已結束】"
    elif days_left == 0:
        timing = "【今天上課】"
    else:
        timing = f"【還有 {days_left} 天】"
    cat = f"[{row['category_name']}] " if row["category_name"] else ""
    text = f"📚 課程提醒 {timing}\n━━━━━━━━━━━━\n{cat}📌 {row['title']}\n📅 {row['course_date']} {row['course_time']}"
    if row["location"]: text += f"\n📍 {row['location']}"
    if row["description"]: text += f"\n📝 {row['description']}"
    messages = []
    if row["image_url"]:
        messages.append({"type":"image","originalContentUrl":row["image_url"],"previewImageUrl":row["image_url"]})
    messages.append({"type":"text","text":text})
    ok, total = push_to_groups(messages)
    return jsonify({"ok":ok,"total":total})

@app.route("/admin/courses/<int:cid>", methods=["DELETE"])
def delete_course(cid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    conn = get_db()
    conn.execute("DELETE FROM course_reminders WHERE course_id=?", (cid,))
    conn.execute("DELETE FROM courses WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    return jsonify({"ok":True})

@app.route("/admin/send", methods=["POST"])
def admin_send():
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    d = request.json
    text = d.get("text","").strip()
    img_url = d.get("image_url","").strip()
    messages = []
    if img_url:
        messages.append({"type":"image","originalContentUrl":img_url,"previewImageUrl":img_url})
    if text:
        messages.append({"type":"text","text":text})
    if not messages: return jsonify({"ok":0,"total":0})
    ok, total = push_to_groups(messages)
    conn = get_db()
    conn.execute("INSERT INTO announcements (content,sent_at,group_count) VALUES (?,?,?)",
                 (text or img_url, datetime.now().isoformat(), ok))
    conn.commit()
    conn.close()
    return jsonify({"ok":ok,"total":total})


@app.route("/admin/ai-parse", methods=["POST"])
def ai_parse_course():
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    text = request.json.get("text","").strip()
    if not text: return jsonify({"ok":False,"error":"請輸入課程描述"})
    
    today = date.today().isoformat()
    prompt = f"""今天是 {today}。
請從以下文字中提取課程資訊，回傳 JSON 格式（只回傳 JSON，不要其他文字）：
{{
  "title": "課程名稱",
  "course_date": "YYYY-MM-DD",
  "course_time": "HH:MM",
  "location": "地點或空字串",
  "description": "說明或空字串",
  "remind_days_before": 30,
  "remind_interval_days": 7
}}

用戶輸入：{text}

注意：
- 如果是「下個月15號」請計算實際日期
- 時間預設 09:00
- remind_days_before 預設 30
- remind_interval_days 預設 7"""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json"},
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 500,
                "messages": [{"role": "user", "content": prompt}]
            }
        )
        result = resp.json()
        ai_text = result["content"][0]["text"].strip()
        # 清除可能的 markdown
        if "```" in ai_text:
            ai_text = ai_text.split("```")[1]
            if ai_text.startswith("json"):
                ai_text = ai_text[4:]
        course_data = json.loads(ai_text.strip())
        return jsonify({"ok": True, "course": course_data})
    except Exception as e:
        logger.error(f"AI parse error: {e}")
        return jsonify({"ok": False, "error": f"AI 解析失敗：{str(e)}"})


@app.route("/admin/scheduled", methods=["GET"])
def get_scheduled():
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    conn = get_db()
    rows = conn.execute("SELECT * FROM scheduled_broadcasts ORDER BY created_at DESC").fetchall()
    conn.close()
    return jsonify({"schedules": [dict(r) for r in rows]})

@app.route("/admin/scheduled", methods=["POST"])
def add_scheduled():
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    d = request.json
    title = d.get("title","").strip()
    content_text = d.get("content","").strip()
    if not title or not content_text: return jsonify({"ok":False,"error":"請填寫標題和內容"})
    interval_hours = float(d.get("interval_hours", 24))
    now = datetime.now()
    start_time = d.get("start_time", now.isoformat())
    # 計算第一次發送時間
    next_run = datetime.fromisoformat(start_time).isoformat()
    conn = get_db()
    conn.execute(
        "INSERT INTO scheduled_broadcasts (title,content,image_url,interval_hours,start_time,next_run,active,created_at) VALUES (?,?,?,?,?,?,1,?)",
        (title, content_text, d.get("image_url",""), interval_hours, start_time, next_run, now.isoformat())
    )
    conn.commit()
    conn.close()
    return jsonify({"ok":True})

@app.route("/admin/scheduled/<int:sid>", methods=["PUT"])
def update_scheduled(sid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    d = request.json
    conn = get_db()
    if "active" in d:
        conn.execute("UPDATE scheduled_broadcasts SET active=? WHERE id=?", (d["active"], sid))
    else:
        conn.execute("""UPDATE scheduled_broadcasts SET title=?,content=?,image_url=?,interval_hours=? WHERE id=?""",
            (d.get("title"), d.get("content"), d.get("image_url",""), float(d.get("interval_hours",24)), sid))
    conn.commit()
    conn.close()
    return jsonify({"ok":True})

@app.route("/admin/scheduled/<int:sid>", methods=["DELETE"])
def delete_scheduled(sid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    conn = get_db()
    conn.execute("DELETE FROM scheduled_broadcasts WHERE id=?", (sid,))
    conn.commit()
    conn.close()
    return jsonify({"ok":True})

@app.route("/admin/scheduled/<int:sid>/send-now", methods=["POST"])
def send_scheduled_now(sid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    conn = get_db()
    row = conn.execute("SELECT * FROM scheduled_broadcasts WHERE id=?", (sid,)).fetchone()
    conn.close()
    if not row: return jsonify({"ok":False,"error":"找不到排程"})
    messages = []
    if row["image_url"]:
        messages.append({"type":"image","originalContentUrl":row["image_url"],"previewImageUrl":row["image_url"]})
    messages.append({"type":"text","text":row["content"]})
    ok, total = push_to_groups(messages)
    # 更新下次發送時間
    next_run = (datetime.now() + timedelta(hours=row["interval_hours"])).isoformat()
    conn = get_db()
    conn.execute("UPDATE scheduled_broadcasts SET next_run=? WHERE id=?", (next_run, sid))
    conn.commit()
    conn.close()
    return jsonify({"ok":ok,"total":total})

@app.route("/admin/check-reminders", methods=["POST"])
def trigger_reminders():
    if request.headers.get("X-Admin-Pass") != ADMIN_PASSWORD:
        return jsonify({"error":"unauthorized"}),401
    check_and_send_reminders()
    return jsonify({"ok":True,"date":date.today().isoformat()})

@app.route("/init-db")
def init_db_route():
    init_db()
    return "DB initialized OK"

# ── Webhook ──
def handle_text(event):
    user_id = event["source"].get("userId","")
    reply_token = event["replyToken"]
    text = event["message"]["text"].strip()
    if event["source"]["type"] == "group":
        gid = event["source"]["groupId"]
        logger.info(f"GROUP MESSAGE: groupId={gid} userId={user_id}")
        conn = get_db()
        conn.execute("INSERT OR IGNORE INTO groups (group_id,joined_at) VALUES (?,?)",
                     (gid, datetime.now().isoformat()))
        conn.commit()
        conn.close()
    if user_id not in ADMIN_USER_IDS: return
    if text.startswith("/公告 "):
        ok, total = push_text_to_groups(f"📢 {text[4:].strip()}")
        reply_message(reply_token, f"✅ 已發送到 {ok}/{total} 個群組")
    elif text.startswith("/新增課程 ") or text.startswith("/加課 "):
        # AI 解析課程
        desc = text.split(" ", 1)[1].strip()
        reply_message(reply_token, "⏳ AI 解析中，請稍候...")
        try:
            today = date.today().isoformat()
            prompt = f"""今天是 {today}。
請從以下文字提取課程資訊，只回傳 JSON：
{{"title":"課程名稱","course_date":"YYYY-MM-DD","course_time":"HH:MM","location":"地點","description":"說明","remind_days_before":30,"remind_interval_days":7}}
用戶輸入：{desc}"""
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"Content-Type": "application/json"},
                json={"model":"claude-sonnet-4-20250514","max_tokens":500,
                      "messages":[{"role":"user","content":prompt}]}
            )
            ai_text = resp.json()["content"][0]["text"].strip()
            if "```" in ai_text:
                ai_text = ai_text.split("```")[1]
                if ai_text.startswith("json"): ai_text = ai_text[4:]
            c = json.loads(ai_text.strip())
            conn = get_db()
            cur = conn.execute(
                "INSERT INTO courses (title,course_date,course_time,location,description,image_url,remind_days_before,remind_interval_days,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (c["title"], c["course_date"], c.get("course_time","09:00"),
                 c.get("location",""), c.get("description",""), "",
                 c.get("remind_days_before",30), c.get("remind_interval_days",7),
                 datetime.now().isoformat())
            )
            course_id = cur.lastrowid
            conn.commit()
            conn.close()
            dates = generate_reminders(course_id, c["course_date"],
                                       c.get("remind_days_before",30),
                                       c.get("remind_interval_days",7))
            reply_message(reply_token,
                f"✅ 課程已新增！\n\n"
                f"📌 {c['title']}\n"
                f"📅 {c['course_date']} {c.get('course_time','09:00')}\n"
                f"📍 {c.get('location','') or '未指定'}\n\n"
                f"🔔 已設定 {len(dates)} 個提醒日期")
        except Exception as e:
            reply_message(reply_token, f"❌ AI 解析失敗，請至網頁後台手動新增\n{str(e)[:50]}")
    elif text == "/課程清單":
        conn = get_db()
        rows = conn.execute("SELECT title, course_date FROM courses ORDER BY course_date ASC LIMIT 10").fetchall()
        conn.close()
        if not rows:
            reply_message(reply_token, "目前沒有排程課程")
        else:
            lines = [f"📅 {r['course_date']} {r['title']}" for r in rows]
            reply_message(reply_token, "課程清單：\n" + "\n".join(lines))
    elif text == "/群組清單":
        groups = get_all_group_ids()
        reply_message(reply_token, f"已連接 {len(groups)} 個群組")
    elif text in ("/說明","/help"):
        reply_message(reply_token,
            "📋 指令說明\n\n"
            "/公告 [內容] 立即發公告\n"
            "/新增課程 [描述] AI智慧新增課程\n"
            "  例：/新增課程 下個月15號早上十點台北招商說明會\n"
            "/課程清單 查看所有課程\n"
            "/群組清單 查看群組\n\n"
            "🌐 網頁後台：\nhttps://ipapalinebot.onrender.com/admin")

def handle_join(event):
    if event["source"]["type"] == "group":
        gid = event["source"]["groupId"]
        logger.info(f"Bot joined group: {gid}")
        conn = get_db()
        conn.execute("INSERT OR IGNORE INTO groups (group_id,joined_at) VALUES (?,?)",
                     (gid, datetime.now().isoformat()))
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
    groups = get_all_group_ids()
    return f'LINE 公告機器人運行中 ✅ | 群組數：{len(groups)} | <a href="/admin">管理後台</a>'

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)))
