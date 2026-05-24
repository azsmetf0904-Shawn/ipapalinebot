import os, json, hashlib, hmac, base64, sqlite3, logging, requests, tempfile
from datetime import datetime, date, timedelta
import pytz  # 引入時區套件
from flask import Flask, request, abort, jsonify, send_from_directory
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── 時區設定 ──
TW_TZ = pytz.timezone('Asia/Taipei')

def get_tw_now():
    """獲取台灣當前的 datetime（帶時區資訊）"""
    return datetime.now(TW_TZ)

def get_tw_today():
    """獲取台灣當前的 date"""
    return datetime.now(TW_TZ).date()

app = Flask(__name__, static_folder='static')

# 初始化排程器並強制指定台灣時區
scheduler = BackgroundScheduler(timezone=TW_TZ)
scheduler.start()

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
ADMIN_USER_IDS = [x.strip() for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip()]
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "ipapa2026")
DATABASE = os.environ.get("DATABASE_PATH", "bot.db")
DEFAULT_GROUP_IDS = [x.strip() for x in os.environ.get("DEFAULT_GROUP_IDS", "").split(",") if x.strip()]
IMGBB_API_KEY = os.environ.get("IMGBB_API_KEY", "")

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
                     (cat[0], cat[1], get_tw_now().isoformat()))
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
    today = get_tw_today().isoformat()  # 使用台灣當前日期
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
        days_left = (cd - get_tw_today()).days  # 使用台灣當前日期計算差距
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
    now = get_tw_now().isoformat()  # 使用台灣當前的 ISO 時間比對
    conn = get_db()
    rows = conn.execute("SELECT * FROM scheduled_broadcasts WHERE active=1 AND next_run<=?", (now,)).fetchall()
    for row in rows:
        msgs = []
        if row["image_url"]:
            msgs.append({"type":"image","originalContentUrl":row["image_url"],"previewImageUrl":row["image_url"]})
        msgs.append({"type":"text","text":row["content"]})
        ok, total = push_to_groups(msgs)
        # 更新下一次執行時間，同樣基於台灣當前時間進行累加
        next_run = (get_tw_now() + seconds_to_timedelta(row["interval_seconds"])).isoformat()
        conn.execute("UPDATE scheduled_broadcasts SET next_run=? WHERE id=?", (next_run, row["id"]))
        logger.info(f"Scheduled '{row['title']}' sent to {ok}/{total}")
    conn.commit()
    conn.close()

# 這裡因為上面初始化 Scheduler 時已經帶入 timezone=TW_TZ，所以每天早上 8:00 會準時以台灣時間觸發
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
def add_
