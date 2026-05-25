import os, json, hashlib, hmac, base64, sqlite3, logging, requests, tempfile
from zoneinfo import ZoneInfo
from datetime import datetime, date, timedelta
from flask import Flask, request, abort, jsonify, send_from_directory
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='static')
scheduler = BackgroundScheduler()
scheduler.start()

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
ADMIN_USER_IDS = [x.strip() for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip()]
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "ipapa2026")
DATABASE = os.environ.get("DATABASE_PATH", "bot.db")
DEFAULT_GROUP_IDS = [x.strip() for x in os.environ.get("DEFAULT_GROUP_IDS", "").split(",") if x.strip()]
IMGBB_API_KEY = os.environ.get("IMGBB_API_KEY", "")
TIMEZONE = os.environ.get("TIMEZONE", "Asia/Taipei")

def now_local():
    try:
        return datetime.now(ZoneInfo(TIMEZONE))
    except:
        return datetime.now(ZoneInfo("Asia/Taipei"))

def today_local():
    return now_local().date()

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
            remind_value INTEGER DEFAULT 30,
            remind_unit TEXT DEFAULT 'days',
            remind_interval_value INTEGER DEFAULT 7,
            remind_interval_unit TEXT DEFAULT 'days',
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
        CREATE TABLE IF NOT EXISTS scheduled_broadcasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            image_url TEXT DEFAULT '',
            interval_seconds REAL NOT NULL DEFAULT 86400,
            next_run TEXT NOT NULL,
            active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            group_count INTEGER DEFAULT 0
        );
    """)
    for cat in [("招商活動","#FF6B35"),("系統會議","#1A73E8"),("課程培訓","#06C755"),("其他","#9E9E9E")]:
        conn.execute("INSERT OR IGNORE INTO categories (name,color,created_at) VALUES (?,?,?)",
                     (cat[0], cat[1], now_local().isoformat()))
    conn.commit()
    conn.close()
    logger.info("DB initialized")

init_db()

# ── helpers ──
def unit_to_seconds(value, unit):
    m = {"seconds":1,"minutes":60,"hours":3600,"days":86400,"months":2592000,"years":31536000}
    return value * m.get(unit, 86400)

def seconds_to_timedelta(seconds):
    return timedelta(seconds=seconds)

def get_all_group_ids():
    conn = get_db()
    db_groups = [r["group_id"] for r in conn.execute("SELECT group_id FROM groups").fetchall()]
    conn.close()
    return list(set(db_groups + DEFAULT_GROUP_IDS))

def verify_signature(body, sig):
    h = hmac.new(LINE_CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(h).decode(), sig)

def push_to_groups(messages):
    groups = get_all_group_ids()
    logger.info(f"Pushing to {len(groups)} groups")
    ok = 0
    for gid in groups:
        r = requests.post("https://api.line.me/v2/bot/message/push", headers=HEADERS,
            json={"to": gid, "messages": messages})
        logger.info(f"Push {gid[:15]}: {r.status_code}")
        if r.status_code == 200: ok += 1
    return ok, len(groups)

def push_text(text):
    return push_to_groups([{"type":"text","text":text}])

def reply_message(reply_token, text):
    requests.post("https://api.line.me/v2/bot/message/reply", headers=HEADERS,
        json={"replyToken":reply_token,"messages":[{"type":"text","text":text}]})

def upload_image_to_imgbb(image_data, filename="image.jpg"):
    """上傳圖片到 imgbb，回傳公開 URL"""
    if not IMGBB_API_KEY:
        return None, "未設定 IMGBB_API_KEY"
    try:
        b64 = base64.b64encode(image_data).decode()
        r = requests.post("https://api.imgbb.com/1/upload",
            data={"key": IMGBB_API_KEY, "image": b64, "name": filename})
        data = r.json()
        if data.get("success"):
            return data["data"]["url"], None
        return None, data.get("error",{}).get("message","上傳失敗")
    except Exception as e:
        return None, str(e)

# ── 提醒排程 ──
def generate_reminders(course_id, course_date_str, remind_value, remind_unit, interval_value, interval_unit):
    conn = get_db()
    conn.execute("DELETE FROM course_reminders WHERE course_id=?", (course_id,))
    course_date = datetime.strptime(course_date_str, "%Y-%m-%d").date()
    before_seconds = unit_to_seconds(remind_value, remind_unit)
    interval_seconds = unit_to_seconds(interval_value, interval_unit)
    start = course_date - timedelta(seconds=before_seconds)
    dates = []
    d = start
    while d <= course_date:
        dates.append(d.isoformat())
        d += timedelta(seconds=max(interval_seconds, 86400))
    if course_date.isoformat() not in dates:
        dates.append(course_date.isoformat())
    for rd in dates:
        conn.execute("INSERT INTO course_reminders (course_id,remind_date,sent) VALUES (?,?,0)", (course_id, rd))
    conn.commit()
    conn.close()
    return dates

def check_and_send_reminders():
    today = today_local().isoformat()
    conn = get_db()
    rows = conn.execute("""
        SELECT cr.id, c.title, c.course_date, c.course_time, c.location, c.description, c.image_url,
               cat.name as category_name
        FROM course_reminders cr JOIN courses c ON cr.course_id=c.id
        LEFT JOIN categories cat ON c.category_id=cat.id
        WHERE cr.remind_date=? AND cr.sent=0
    """, (today,)).fetchall()
    for row in rows:
        cd = datetime.strptime(row["course_date"], "%Y-%m-%d").date()
        days_left = (cd - today_local()).days
        timing = "【今天上課】" if days_left==0 else f"【還有 {days_left} 天】"
        cat = f"[{row['category_name']}] " if row["category_name"] else ""
        text = f"📚 課程提醒 {timing}\n━━━━━━━━━━━━\n{cat}📌 {row['title']}\n📅 {row['course_date']} {row['course_time']}"
        if row["location"]: text += f"\n📍 {row['location']}"
        if row["description"]: text += f"\n📝 {row['description']}"
        msgs = []
        if row["image_url"]:
            msgs.append({"type":"image","originalContentUrl":row["image_url"],"previewImageUrl":row["image_url"]})
        msgs.append({"type":"text","text":text})
        ok, total = push_to_groups(msgs)
        if ok > 0:
            conn.execute("UPDATE course_reminders SET sent=1 WHERE id=?", (row["id"],))
    conn.commit()
    conn.close()

def check_scheduled_broadcasts():
    now = now_local().isoformat()
    conn = get_db()
    rows = conn.execute("SELECT * FROM scheduled_broadcasts WHERE active=1 AND next_run<=?", (now,)).fetchall()
    for row in rows:
        msgs = []
        if row["image_url"]:
            msgs.append({"type":"image","originalContentUrl":row["image_url"],"previewImageUrl":row["image_url"]})
        msgs.append({"type":"text","text":row["content"]})
        ok, total = push_to_groups(msgs)
        next_run = (now_local() + seconds_to_timedelta(row["interval_seconds"])).isoformat()
        conn.execute("UPDATE scheduled_broadcasts SET next_run=? WHERE id=?", (next_run, row["id"]))
        logger.info(f"Scheduled '{row['title']}' sent to {ok}/{total}")
    conn.commit()
    conn.close()

scheduler.add_job(check_and_send_reminders, "cron", hour=8, minute=0, id="daily_reminder")
scheduler.add_job(check_scheduled_broadcasts, "interval", minutes=15, id="sched_broadcast")

def check_admin(req):
    return req.headers.get("X-Admin-Pass") == ADMIN_PASSWORD

# ── Static HTML ──
@app.route("/admin")
def admin_page():
    return send_from_directory("static", "admin.html")

@app.route("/")
def index():
    groups = get_all_group_ids()
    return f'LINE 公告機器人 ✅ | 群組:{len(groups)} | <a href="/admin">管理後台</a>'

@app.route("/init-db")
def init_db_route():
    init_db()
    return "DB initialized OK"

# ── API ──

@app.route("/admin/timezone", methods=["GET"])
def get_timezone():
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    return jsonify({"timezone": TIMEZONE, "current_time": now_local().strftime("%Y-%m-%d %H:%M:%S %Z")})

@app.route("/admin/timezone", methods=["POST"])
def set_timezone():
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    tz = request.json.get("timezone","").strip()
    try:
        ZoneInfo(tz)  # validate
    except Exception:
        return jsonify({"ok":False,"error":"無效的時區"})
    # Write to env file for persistence hint (actual change needs Render env var)
    global TIMEZONE
    TIMEZONE = tz
    return jsonify({"ok":True,"timezone":tz,"current_time":now_local().strftime("%Y-%m-%d %H:%M:%S %Z")})

@app.route("/admin/groups")
def get_groups():
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    return jsonify({"count":len(get_all_group_ids()),"groups":get_all_group_ids()})

@app.route("/admin/categories", methods=["GET"])
def get_categories():
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    conn = get_db()
    rows = conn.execute("SELECT * FROM categories ORDER BY id").fetchall()
    conn.close()
    return jsonify({"categories":[dict(r) for r in rows]})

@app.route("/admin/categories", methods=["POST"])
def add_category():
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    d = request.json
    name = d.get("name","").strip()
    if not name: return jsonify({"ok":False,"error":"請填寫分類名稱"})
    try:
        conn = get_db()
        conn.execute("INSERT INTO categories (name,color,created_at) VALUES (?,?,?)",
                     (name, d.get("color","#06C755"), now_local().isoformat()))
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
    return jsonify({"courses":[dict(r) for r in rows]})

@app.route("/admin/courses", methods=["POST"])
def add_course():
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    d = request.json
    title = d.get("title","").strip()
    course_date = d.get("course_date","").replace("/","-")
    if not title or not course_date: return jsonify({"ok":False,"error":"請填寫課程名稱和日期"})
    rv = int(d.get("remind_value",30))
    ru = d.get("remind_unit","days")
    iv = int(d.get("remind_interval_value",7))
    iu = d.get("remind_interval_unit","days")
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO courses (category_id,title,course_date,course_time,location,description,image_url,remind_value,remind_unit,remind_interval_value,remind_interval_unit,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (d.get("category_id"), title, course_date, d.get("course_time","09:00"),
         d.get("location",""), d.get("description",""), d.get("image_url",""),
         rv, ru, iv, iu, now_local().isoformat())
    )
    cid = cur.lastrowid
    conn.commit()
    conn.close()
    dates = generate_reminders(cid, course_date, rv, ru, iv, iu)
    return jsonify({"ok":True,"course_id":cid,"remind_count":len(dates)})

@app.route("/admin/courses/<int:cid>", methods=["PUT"])
def edit_course(cid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    d = request.json
    title = d.get("title","").strip()
    course_date = d.get("course_date","").replace("/","-")
    if not title or not course_date: return jsonify({"ok":False,"error":"請填寫課程名稱和日期"})
    rv = int(d.get("remind_value",30))
    ru = d.get("remind_unit","days")
    iv = int(d.get("remind_interval_value",7))
    iu = d.get("remind_interval_unit","days")
    conn = get_db()
    conn.execute("""UPDATE courses SET category_id=?,title=?,course_date=?,course_time=?,location=?,
                    description=?,image_url=?,remind_value=?,remind_unit=?,remind_interval_value=?,remind_interval_unit=?
                    WHERE id=?""",
        (d.get("category_id"), title, course_date, d.get("course_time","09:00"),
         d.get("location",""), d.get("description",""), d.get("image_url",""),
         rv, ru, iv, iu, cid))
    conn.commit()
    conn.close()
    dates = generate_reminders(cid, course_date, rv, ru, iv, iu)
    return jsonify({"ok":True,"remind_count":len(dates)})

@app.route("/admin/courses/<int:cid>", methods=["DELETE"])
def delete_course(cid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    conn = get_db()
    conn.execute("DELETE FROM course_reminders WHERE course_id=?", (cid,))
    conn.execute("DELETE FROM courses WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    return jsonify({"ok":True})

@app.route("/admin/courses/<int:cid>/send-now", methods=["POST"])
def send_course_now(cid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    conn = get_db()
    row = conn.execute("SELECT c.*, cat.name as category_name FROM courses c LEFT JOIN categories cat ON c.category_id=cat.id WHERE c.id=?", (cid,)).fetchone()
    conn.close()
    if not row: return jsonify({"ok":False,"error":"找不到課程"})
    cd = datetime.strptime(row["course_date"], "%Y-%m-%d").date()
    days_left = (cd - today_local()).days
    timing = "【今天】" if days_left==0 else f"【還有{days_left}天】" if days_left>0 else "【已結束】"
    cat = f"[{row['category_name']}] " if row["category_name"] else ""
    text = f"📚 課程提醒 {timing}\n━━━━━━━━━━━━\n{cat}📌 {row['title']}\n📅 {row['course_date']} {row['course_time']}"
    if row["location"]: text += f"\n📍 {row['location']}"
    if row["description"]: text += f"\n📝 {row['description']}"
    msgs = []
    if row["image_url"]:
        msgs.append({"type":"image","originalContentUrl":row["image_url"],"previewImageUrl":row["image_url"]})
    msgs.append({"type":"text","text":text})
    ok, total = push_to_groups(msgs)
    return jsonify({"ok":ok,"total":total})

@app.route("/admin/send", methods=["POST"])
def admin_send():
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    d = request.json
    text = d.get("text","").strip()
    img_url = d.get("image_url","").strip()
    msgs = []
    if img_url: msgs.append({"type":"image","originalContentUrl":img_url,"previewImageUrl":img_url})
    if text: msgs.append({"type":"text","text":text})
    if not msgs: return jsonify({"ok":0,"total":0})
    ok, total = push_to_groups(msgs)
    return jsonify({"ok":ok,"total":total})

@app.route("/admin/upload-image", methods=["POST"])
def upload_image():
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    if "image" not in request.files:
        return jsonify({"ok":False,"error":"沒有收到圖片"})
    f = request.files["image"]
    image_data = f.read()
    url, err = upload_image_to_imgbb(image_data, f.filename)
    if url:
        return jsonify({"ok":True,"url":url})
    return jsonify({"ok":False,"error":err or "上傳失敗，請設定 IMGBB_API_KEY"})

@app.route("/admin/scheduled", methods=["GET"])
def get_scheduled():
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    conn = get_db()
    rows = conn.execute("SELECT * FROM scheduled_broadcasts ORDER BY created_at DESC").fetchall()
    conn.close()
    return jsonify({"schedules":[dict(r) for r in rows]})

@app.route("/admin/scheduled", methods=["POST"])
def add_scheduled():
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    d = request.json
    title = d.get("title","").strip()
    content_text = d.get("content","").strip()
    if not title or not content_text: return jsonify({"ok":False,"error":"請填寫標題和內容"})
    iv = float(d.get("interval_value", 1))
    iu = d.get("interval_unit","days")
    interval_seconds = unit_to_seconds(iv, iu)
    start_time = d.get("start_time", now_local().isoformat())
    try:
        next_run = datetime.fromisoformat(start_time).isoformat()
    except:
        next_run = now_local().isoformat()
    conn = get_db()
    conn.execute("INSERT INTO scheduled_broadcasts (title,content,image_url,interval_seconds,next_run,active,created_at) VALUES (?,?,?,?,?,1,?)",
        (title, content_text, d.get("image_url",""), interval_seconds, next_run, now_local().isoformat()))
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
        iv = float(d.get("interval_value",1))
        iu = d.get("interval_unit","days")
        conn.execute("UPDATE scheduled_broadcasts SET title=?,content=?,image_url=?,interval_seconds=? WHERE id=?",
            (d.get("title"), d.get("content"), d.get("image_url",""), unit_to_seconds(iv,iu), sid))
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
    msgs = []
    if row["image_url"]: msgs.append({"type":"image","originalContentUrl":row["image_url"],"previewImageUrl":row["image_url"]})
    msgs.append({"type":"text","text":row["content"]})
    ok, total = push_to_groups(msgs)
    next_run = (now_local() + seconds_to_timedelta(row["interval_seconds"])).isoformat()
    conn = get_db()
    conn.execute("UPDATE scheduled_broadcasts SET next_run=? WHERE id=?", (next_run, sid))
    conn.commit()
    conn.close()
    return jsonify({"ok":ok,"total":total})

@app.route("/admin/ai-parse", methods=["POST"])
def ai_parse_course():
    if not check_admin(request): return jsonify({"error":"unauthorized"}),401
    text = ""
    image_b64 = ""
    image_media_type = "image/jpeg"
    image_url = ""

    if request.content_type and "multipart" in request.content_type:
        text = request.form.get("text","").strip()
        if "image" in request.files:
            f = request.files["image"]
            image_data = f.read()
            image_b64 = base64.b64encode(image_data).decode()
            image_media_type = f.content_type or "image/jpeg"
    else:
        data = request.get_json() or {}
        text = data.get("text","").strip()
        image_b64 = data.get("image_b64","").strip()
        image_url = data.get("image_url","").strip()
        image_media_type = data.get("image_media_type","image/jpeg")

    if not text and not image_b64 and not image_url:
        return jsonify({"ok":False,"error":"請輸入課程描述或上傳圖片"})

    today = today_local().isoformat()
    prompt = f"""今天是 {today}。請從圖片或文字中提取課程資訊，只回傳 JSON：
{{"title":"課程名稱","course_date":"YYYY-MM-DD","course_time":"HH:MM","location":"地點或空字串","description":"說明或空字串","remind_value":30,"remind_unit":"days","remind_interval_value":7,"remind_interval_unit":"days"}}
相對日期請根據今天 {today} 計算。用戶輸入：{text}"""

    try:
        msg_content = []
        if image_b64:
            msg_content.append({"type":"image","source":{"type":"base64","media_type":image_media_type,"data":image_b64}})
        elif image_url:
            msg_content.append({"type":"image","source":{"type":"url","url":image_url}})
        msg_content.append({"type":"text","text":prompt})

        resp = requests.post("https://api.anthropic.com/v1/messages",
            headers={"Content-Type":"application/json"},
            json={"model":"claude-sonnet-4-20250514","max_tokens":600,
                  "messages":[{"role":"user","content":msg_content}]})
        result = resp.json()
        if "error" in result:
            return jsonify({"ok":False,"error":result["error"].get("message","API錯誤")})
        ai_text = result["content"][0]["text"].strip()
        if "```" in ai_text:
            ai_text = ai_text.split("```")[1]
            if ai_text.startswith("json"): ai_text = ai_text[4:]
        c = json.loads(ai_text.strip())
        return jsonify({"ok":True,"course":c})
    except Exception as e:
        logger.error(f"AI parse error: {e}")
        return jsonify({"ok":False,"error":str(e)})

@app.route("/admin/check-reminders", methods=["POST"])
def trigger_reminders():
    if request.headers.get("X-Admin-Pass") != ADMIN_PASSWORD: return jsonify({"error":"unauthorized"}),401
    check_and_send_reminders()
    check_scheduled_broadcasts()
    return jsonify({"ok":True,"date":today_local().isoformat()})

# ── Webhook ──
def handle_text(event):
    user_id = event["source"].get("userId","")
    reply_token = event["replyToken"]
    text = event["message"]["text"].strip()
    if event["source"]["type"] == "group":
        gid = event["source"]["groupId"]
        logger.info(f"GROUP MESSAGE: groupId={gid} userId={user_id}")
        conn = get_db()
        conn.execute("INSERT OR IGNORE INTO groups (group_id,joined_at) VALUES (?,?)", (gid, now_local().isoformat()))
        conn.commit()
        conn.close()
    if user_id not in ADMIN_USER_IDS: return
    if text.startswith("/公告 "):
        ok, total = push_text(f"📢 {text[4:].strip()}")
        reply_message(reply_token, f"✅ 已發送到 {ok}/{total} 個群組")
    elif text.startswith("/新增課程 ") or text.startswith("/加課 "):
        desc = text.split(" ",1)[1].strip()
        try:
            today = today_local().isoformat()
            prompt = f"今天是{today}。從以下文字提取課程資訊，只回傳JSON：{{\"title\":\"\",\"course_date\":\"YYYY-MM-DD\",\"course_time\":\"HH:MM\",\"location\":\"\",\"description\":\"\"}}\n用戶：{desc}"
            resp = requests.post("https://api.anthropic.com/v1/messages",
                headers={"Content-Type":"application/json"},
                json={"model":"claude-sonnet-4-20250514","max_tokens":300,
                      "messages":[{"role":"user","content":prompt}]})
            ai_text = resp.json()["content"][0]["text"].strip()
            if "```" in ai_text:
                ai_text = ai_text.split("```")[1]
                if ai_text.startswith("json"): ai_text = ai_text[4:]
            c = json.loads(ai_text.strip())
            conn = get_db()
            cur = conn.execute("INSERT INTO courses (title,course_date,course_time,location,description,image_url,remind_value,remind_unit,remind_interval_value,remind_interval_unit,created_at) VALUES (?,?,?,?,?,?,30,'days',7,'days',?)",
                (c["title"],c["course_date"],c.get("course_time","09:00"),c.get("location",""),c.get("description",""),"",now_local().isoformat()))
            cid = cur.lastrowid
            conn.commit()
            conn.close()
            dates = generate_reminders(cid, c["course_date"], 30, "days", 7, "days")
            reply_message(reply_token, f"✅ 課程已新增！\n📌 {c['title']}\n📅 {c['course_date']} {c.get('course_time','09:00')}\n📍 {c.get('location','未指定')}\n🔔 {len(dates)} 個提醒")
        except Exception as e:
            reply_message(reply_token, f"❌ AI 解析失敗\n{str(e)[:50]}")
    elif text == "/課程清單":
        conn = get_db()
        rows = conn.execute("SELECT title,course_date FROM courses ORDER BY course_date ASC LIMIT 10").fetchall()
        conn.close()
        if not rows: reply_message(reply_token, "目前沒有排程課程")
        else: reply_message(reply_token, "課程清單：\n" + "\n".join(f"📅 {r['course_date']} {r['title']}" for r in rows))
    elif text == "/群組清單":
        reply_message(reply_token, f"已連接 {len(get_all_group_ids())} 個群組")
    elif text in ("/說明","/help"):
        reply_message(reply_token,
            "📋 指令說明\n\n/公告 [內容] 立即發公告\n/新增課程 [描述] AI新增課程\n/課程清單 查看課程\n/群組清單 查看群組\n\n🌐 管理後台：\nhttps://ipapalinebot.onrender.com/admin")

def handle_join(event):
    if event["source"]["type"] == "group":
        gid = event["source"]["groupId"]
        conn = get_db()
        conn.execute("INSERT OR IGNORE INTO groups (group_id,joined_at) VALUES (?,?)", (gid, now_local().isoformat()))
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)))
