import os, json, hashlib, hmac, base64, logging, requests
from zoneinfo import ZoneInfo
from datetime import datetime, date, timedelta
from flask import Flask, request, abort, jsonify, send_from_directory
from apscheduler.schedulers.background import BackgroundScheduler
import psycopg2
from psycopg2.extras import RealDictCursor
from urllib.parse import urlparse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='static')
scheduler = BackgroundScheduler(timezone="Asia/Taipei")
scheduler.start()

# ── 固定台灣時區，不允許外部覆蓋 ──
TZ = ZoneInfo("Asia/Taipei")

def now_tw():
    """永遠回傳台灣時間的 aware datetime"""
    return datetime.now(TZ)

def today_tw() -> date:
    return now_tw().date()

def isonow() -> str:
    return now_tw().isoformat()

# ── 環境變數 ──
LINE_CHANNEL_SECRET      = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
ADMIN_USER_IDS           = [x.strip() for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip()]
ADMIN_PASSWORD           = os.environ.get("ADMIN_PASSWORD", "ipapa2026")
DATABASE_URL             = os.environ["DATABASE_URL"]          # Railway 自動注入
DEFAULT_GROUP_IDS        = [x.strip() for x in os.environ.get("DEFAULT_GROUP_IDS", "").split(",") if x.strip()]
IMGBB_API_KEY            = os.environ.get("IMGBB_API_KEY", "")
APP_URL                  = os.environ.get("APP_URL", "")       # e.g. https://xxx.railway.app

HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
}

def fetch_group_name(group_id: str) -> str:
    """呼叫 LINE API 取得群組名稱，失敗回傳空字串"""
    try:
        r = requests.get(
            f"https://api.line.me/v2/bot/group/{group_id}/summary",
            headers=HEADERS, timeout=8
        )
        if r.status_code == 200:
            return r.json().get("groupName", "")
    except Exception as e:
        logger.warning(f"fetch_group_name {group_id}: {e}")
    return ""

# ── PostgreSQL 連線 ──
def get_db():
    """每次請求建立新連線（Railway 不需 connection pool 設定）"""
    url = urlparse(DATABASE_URL)
    conn = psycopg2.connect(
        host=url.hostname,
        port=url.port or 5432,
        dbname=url.path.lstrip("/"),
        user=url.username,
        password=url.password,
        sslmode="require",
        cursor_factory=RealDictCursor,
        connect_timeout=10,
    )
    conn.autocommit = False
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            group_id   TEXT PRIMARY KEY,
            joined_at  TIMESTAMPTZ NOT NULL,
            group_name TEXT DEFAULT '',
            group_type TEXT DEFAULT 'general',
            active     BOOLEAN DEFAULT TRUE
        );
        ALTER TABLE groups ADD COLUMN IF NOT EXISTS group_name TEXT DEFAULT '';
        ALTER TABLE groups ADD COLUMN IF NOT EXISTS group_type TEXT DEFAULT 'general';
        ALTER TABLE groups ADD COLUMN IF NOT EXISTS active     BOOLEAN DEFAULT TRUE;
        CREATE TABLE IF NOT EXISTS categories (
            id         SERIAL PRIMARY KEY,
            name       TEXT NOT NULL UNIQUE,
            color      TEXT DEFAULT '#06C755',
            created_at TIMESTAMPTZ NOT NULL
        );
        CREATE TABLE IF NOT EXISTS courses (
            id                    SERIAL PRIMARY KEY,
            category_id           INTEGER DEFAULT NULL REFERENCES categories(id) ON DELETE SET NULL,
            title                 TEXT NOT NULL,
            course_date           DATE NOT NULL,
            course_time           TEXT DEFAULT '09:00',
            location              TEXT DEFAULT '',
            description           TEXT DEFAULT '',
            image_url             TEXT DEFAULT '',
            remind_value          INTEGER DEFAULT 30,
            remind_unit           TEXT DEFAULT 'days',
            remind_interval_value INTEGER DEFAULT 7,
            remind_interval_unit  TEXT DEFAULT 'days',
            created_at            TIMESTAMPTZ NOT NULL
        );
        CREATE TABLE IF NOT EXISTS course_reminders (
            id          SERIAL PRIMARY KEY,
            course_id   INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
            remind_date DATE NOT NULL,
            sent        BOOLEAN DEFAULT FALSE
        );
        CREATE TABLE IF NOT EXISTS scheduled_broadcasts (
            id               SERIAL PRIMARY KEY,
            title            TEXT NOT NULL,
            content          TEXT NOT NULL,
            image_url        TEXT DEFAULT '',
            interval_seconds REAL NOT NULL DEFAULT 86400,
            next_run         TIMESTAMPTZ NOT NULL,
            active           BOOLEAN DEFAULT TRUE,
            created_at       TIMESTAMPTZ NOT NULL
        );
        CREATE TABLE IF NOT EXISTS announcements (
            id          SERIAL PRIMARY KEY,
            content     TEXT NOT NULL,
            sent_at     TIMESTAMPTZ NOT NULL,
            group_count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS scheduled_announcements (
            id          SERIAL PRIMARY KEY,
            title       TEXT NOT NULL DEFAULT '',
            content     TEXT NOT NULL,
            image_url   TEXT DEFAULT '',
            send_at     TIMESTAMPTZ NOT NULL,
            sent        BOOLEAN DEFAULT FALSE,
            sent_time   TIMESTAMPTZ,
            group_count INTEGER DEFAULT 0,
            created_at  TIMESTAMPTZ NOT NULL
        );
        CREATE TABLE IF NOT EXISTS broadcast_schedule_entries (
            id           SERIAL PRIMARY KEY,
            source_type  TEXT NOT NULL,
            source_id    INTEGER NOT NULL,
            send_at      TIMESTAMPTZ NOT NULL,
            sent         BOOLEAN DEFAULT FALSE,
            sent_time    TIMESTAMPTZ,
            group_count  INTEGER DEFAULT 0,
            created_at   TIMESTAMPTZ NOT NULL
        );
    """)
    # 預設分類
    for name, color in [("招商活動","#FF6B35"),("系統會議","#1A73E8"),("課程培訓","#06C755"),("其他","#9E9E9E")]:
        cur.execute("INSERT INTO categories (name,color,created_at) VALUES (%s,%s,%s) ON CONFLICT (name) DO NOTHING",
                    (name, color, isonow()))
    conn.commit()
    cur.close()
    conn.close()
    logger.info("DB initialized (PostgreSQL)")

init_db()

# ── 工具函式 ──
def unit_to_seconds(value, unit: str) -> float:
    mapping = {"seconds":1,"minutes":60,"hours":3600,"days":86400,"weeks":604800,"months":2592000,"years":31536000}
    return float(value) * mapping.get(unit, 86400)

def get_all_group_ids() -> list[str]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT group_id FROM groups WHERE active = TRUE")
    db_groups = [r["group_id"] for r in cur.fetchall()]
    cur.close(); conn.close()
    return list(set(db_groups + DEFAULT_GROUP_IDS))

def verify_signature(body: bytes, sig: str) -> bool:
    h = hmac.new(LINE_CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(h).decode(), sig)

def push_to_groups(messages: list) -> tuple[int, int]:
    groups = get_all_group_ids()
    ok = 0
    for gid in groups:
        r = requests.post("https://api.line.me/v2/bot/message/push", headers=HEADERS,
            json={"to": gid, "messages": messages}, timeout=10)
        logger.info(f"Push {gid[:20]}: {r.status_code}")
        if r.status_code == 200:
            ok += 1
    return ok, len(groups)

def push_text(text: str):
    return push_to_groups([{"type": "text", "text": text}])

def reply_message(reply_token: str, text: str):
    requests.post("https://api.line.me/v2/bot/message/reply", headers=HEADERS,
        json={"replyToken": reply_token, "messages": [{"type": "text", "text": text}]}, timeout=10)

def upload_image_to_imgbb(image_data: bytes, filename="image.jpg") -> tuple[str | None, str | None]:
    if not IMGBB_API_KEY:
        return None, "未設定 IMGBB_API_KEY"
    try:
        b64 = base64.b64encode(image_data).decode()
        r = requests.post("https://api.imgbb.com/1/upload",
            data={"key": IMGBB_API_KEY, "image": b64, "name": filename}, timeout=15)
        data = r.json()
        if data.get("success"):
            return data["data"]["url"], None
        return None, data.get("error", {}).get("message", "上傳失敗")
    except Exception as e:
        return None, str(e)

# ── 提醒排程生成 ──
def generate_reminders(course_id: int, course_date_str: str,
                       remind_value: int, remind_unit: str,
                       interval_value: int, interval_unit: str) -> list[str]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM course_reminders WHERE course_id=%s", (course_id,))

    course_date = datetime.strptime(course_date_str, "%Y-%m-%d").date()
    before_td   = timedelta(seconds=unit_to_seconds(remind_value, remind_unit))
    interval_td = timedelta(seconds=max(unit_to_seconds(interval_value, interval_unit), 86400))

    dates = []
    d = course_date - before_td
    while d <= course_date:
        dates.append(d)
        d += interval_td
    if course_date not in dates:
        dates.append(course_date)

    for rd in dates:
        cur.execute("INSERT INTO course_reminders (course_id,remind_date,sent) VALUES (%s,%s,FALSE)",
                    (course_id, rd.isoformat()))
    conn.commit()
    cur.close(); conn.close()
    return [d.isoformat() for d in dates]

# ── 每日 08:00 台灣時間觸發提醒 ──
def check_and_send_reminders():
    today = today_tw().isoformat()
    logger.info(f"[Reminder] Checking reminders for {today} (TW)")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT cr.id, c.title, c.course_date::text, c.course_time, c.location,
               c.description, c.image_url, cat.name AS category_name
        FROM course_reminders cr
        JOIN courses c ON cr.course_id = c.id
        LEFT JOIN categories cat ON c.category_id = cat.id
        WHERE cr.remind_date = %s AND cr.sent = FALSE
    """, (today,))
    rows = cur.fetchall()

    for row in rows:
        cd = datetime.strptime(row["course_date"], "%Y-%m-%d").date()
        days_left = (cd - today_tw().date()).days
        timing = "【今天上課】" if days_left == 0 else f"【還有 {days_left} 天】"
        cat = f"[{row['category_name']}] " if row["category_name"] else ""
        text = (f"📚 課程提醒 {timing}\n━━━━━━━━━━━━\n"
                f"{cat}📌 {row['title']}\n"
                f"📅 {row['course_date']} {row['course_time']}")
        if row["location"]:    text += f"\n📍 {row['location']}"
        if row["description"]: text += f"\n📝 {row['description']}"

        msgs = []
        if row["image_url"]:
            msgs.append({"type":"image","originalContentUrl":row["image_url"],"previewImageUrl":row["image_url"]})
        msgs.append({"type":"text","text":text})

        ok, _ = push_to_groups(msgs)
        if ok > 0:
            cur.execute("UPDATE course_reminders SET sent=TRUE WHERE id=%s", (row["id"],))

    conn.commit()
    cur.close(); conn.close()
    logger.info(f"[Reminder] Processed {len(rows)} reminders")

# ── 排程廣播（每 15 分鐘檢查） ──
def check_scheduled_broadcasts():
    now = now_tw()  # aware datetime，帶台灣時區
    conn = get_db()
    cur = conn.cursor()
    # 用 NOW() AT TIME ZONE 比對，確保時區一致
    cur.execute("SELECT * FROM scheduled_broadcasts WHERE active=TRUE AND next_run <= %s", (now,))
    rows = cur.fetchall()
    logger.info(f"[Broadcast] Checking at {now.isoformat()}, found {len(rows)} due")

    for row in rows:
        msgs = []
        if row["image_url"]:
            msgs.append({"type":"image","originalContentUrl":row["image_url"],"previewImageUrl":row["image_url"]})
        msgs.append({"type":"text","text":row["content"]})
        ok, total = push_to_groups(msgs)
        next_run = (now_tw() + timedelta(seconds=row["interval_seconds"])).isoformat()
        cur.execute("UPDATE scheduled_broadcasts SET next_run=%s WHERE id=%s", (next_run, row["id"]))
        logger.info(f"[Broadcast] '{row['title']}' sent {ok}/{total}, next_run={next_run}")

    conn.commit()
    cur.close(); conn.close()

# ── 一次性排程公告（每 5 分鐘檢查） ──
def check_scheduled_announcements():
    now = now_tw()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM scheduled_announcements WHERE sent=FALSE AND send_at <= %s", (now,))
    rows = cur.fetchall()
    logger.info(f"[SchedAnn] Checking at {now.isoformat()}, found {len(rows)} due")
    for row in rows:
        msgs = []
        if row["image_url"]:
            msgs.append({"type":"image","originalContentUrl":row["image_url"],"previewImageUrl":row["image_url"]})
        msgs.append({"type":"text","text":row["content"]})
        ok, total = push_to_groups(msgs)
        cur.execute(
            "UPDATE scheduled_announcements SET sent=TRUE, sent_time=%s, group_count=%s WHERE id=%s",
            (now_tw().isoformat(), total, row["id"])
        )
        logger.info(f"[SchedAnn] id={row['id']} sent {ok}/{total}")
    conn.commit(); cur.close(); conn.close()

# 排程：固定台灣時間 08:00 + 每 15 分鐘廣播檢查 + 每 5 分鐘一次性公告
scheduler.add_job(check_and_send_reminders, "cron", hour=8, minute=0,
                  timezone="Asia/Taipei", id="daily_reminder", replace_existing=True)
scheduler.add_job(check_scheduled_broadcasts, "interval", minutes=15,
                  id="sched_broadcast", replace_existing=True)
scheduler.add_job(check_scheduled_announcements, "interval", minutes=5,
                  id="sched_announce", replace_existing=True)

# ── 指定日期時間發送排程（broadcast_schedule_entries）──
def check_broadcast_schedule_entries():
    now = now_tw()
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM broadcast_schedule_entries WHERE sent=FALSE AND send_at <= %s", (now,))
    rows = cur.fetchall()
    if rows:
        logger.info(f"[BcastEntry] {len(rows)} entries due at {now.isoformat()}")
    for row in rows:
        src_type = row["source_type"]
        src_id   = row["source_id"]
        msgs = []
        try:
            if src_type == "course":
                cur2 = conn.cursor()
                cur2.execute("""
                    SELECT c.*, cat.name AS category_name FROM courses c
                    LEFT JOIN categories cat ON c.category_id=cat.id WHERE c.id=%s
                """, (src_id,))
                c = cur2.fetchone(); cur2.close()
                if c:
                    cd = datetime.strptime(str(c["course_date"]), "%Y-%m-%d").date()
                    days_left = (cd - today_tw()).days
                    timing = "【今天上課】" if days_left==0 else f"【還有 {days_left} 天】" if days_left>0 else "【已結束】"
                    cat = f"[{c['category_name']}] " if c["category_name"] else ""
                    text = (f"📚 課程提醒 {timing}\n━━━━━━━━━━━━\n"
                            f"{cat}📌 {c['title']}\n"
                            f"📅 {c['course_date']} {c['course_time']}")
                    if c["location"]:    text += f"\n📍 {c['location']}"
                    if c["description"]: text += f"\n📝 {c['description']}"
                    if c["image_url"]:
                        msgs.append({"type":"image","originalContentUrl":c["image_url"],"previewImageUrl":c["image_url"]})
                    msgs.append({"type":"text","text":text})
            elif src_type == "broadcast":
                cur2 = conn.cursor()
                cur2.execute("SELECT * FROM scheduled_broadcasts WHERE id=%s", (src_id,))
                b = cur2.fetchone(); cur2.close()
                if b:
                    if b["image_url"]:
                        msgs.append({"type":"image","originalContentUrl":b["image_url"],"previewImageUrl":b["image_url"]})
                    msgs.append({"type":"text","text":b["content"]})
        except Exception as e:
            logger.error(f"[BcastEntry] build msg error: {e}")
        if msgs:
            ok, total = push_to_groups(msgs)
            cur.execute(
                "UPDATE broadcast_schedule_entries SET sent=TRUE, sent_time=%s, group_count=%s WHERE id=%s",
                (now_tw().isoformat(), total, row["id"])
            )
            logger.info(f"[BcastEntry] id={row['id']} {src_type}#{src_id} sent {ok}/{total}")
    conn.commit(); cur.close(); conn.close()

scheduler.add_job(check_broadcast_schedule_entries, "interval", minutes=5,
                  id="bcast_entries", replace_existing=True)

def check_admin(req) -> bool:
    return req.headers.get("X-Admin-Pass") == ADMIN_PASSWORD

# ── Static ──
@app.route("/admin")
def admin_page():
    return send_from_directory("static", "admin.html")

@app.route("/")
def index():
    groups = get_all_group_ids()
    tw = now_tw().strftime("%Y-%m-%d %H:%M:%S")
    return (f'LINE 公告機器人 ✅<br>'
            f'台灣時間：{tw}<br>'
            f'群組：{len(groups)}<br>'
            f'<a href="/admin">管理後台</a>')

@app.route("/health")
def health():
    """Railway health check endpoint"""
    return jsonify({"status":"ok","tw_time":now_tw().strftime("%Y-%m-%d %H:%M:%S %Z")})

@app.route("/init-db")
def init_db_route():
    init_db()
    return "DB initialized OK"

# ── Admin API ──

@app.route("/admin/info", methods=["GET"])
def admin_info():
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    return jsonify({
        "timezone": "Asia/Taipei",
        "current_time": now_tw().strftime("%Y-%m-%d %H:%M:%S"),
        "groups": len(get_all_group_ids())
    })

@app.route("/admin/groups")
def get_groups():
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT group_id, joined_at, group_name, group_type, active FROM groups ORDER BY joined_at DESC")
    rows = cur.fetchall(); cur.close(); conn.close()
    result = []
    for r in rows:
        d = dict(r)
        if d.get("joined_at"): d["joined_at"] = str(d["joined_at"])
        result.append(d)
    return jsonify({"count": len(result), "groups": result})

@app.route("/admin/groups/<gid>", methods=["PUT"])
def update_group(gid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    d = request.json or {}
    conn = get_db(); cur = conn.cursor()
    fields, vals = [], []
    if "active" in d:
        fields.append("active=%s"); vals.append(bool(d["active"]))
    if "group_name" in d:
        fields.append("group_name=%s"); vals.append(str(d["group_name"]).strip())
    if "group_type" in d:
        fields.append("group_type=%s"); vals.append(str(d["group_type"]).strip())
    if not fields:
        cur.close(); conn.close()
        return jsonify({"ok": False, "error": "no fields to update"})
    vals.append(gid)
    cur.execute(f"UPDATE groups SET {', '.join(fields)} WHERE group_id=%s", vals)
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})

@app.route("/admin/groups/<gid>", methods=["DELETE"])
def delete_group(gid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM groups WHERE group_id=%s", (gid,))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})

@app.route("/admin/categories", methods=["GET"])
def get_categories():
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM categories ORDER BY id")
    rows = cur.fetchall()
    cur.close(); conn.close()
    return jsonify({"categories": [dict(r) for r in rows]})

@app.route("/admin/categories", methods=["POST"])
def add_category():
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    d = request.json
    name = d.get("name","").strip()
    if not name: return jsonify({"ok":False,"error":"請填寫分類名稱"})
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("INSERT INTO categories (name,color,created_at) VALUES (%s,%s,%s)",
                    (name, d.get("color","#06C755"), isonow()))
        conn.commit(); cur.close(); conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

@app.route("/admin/categories/<int:cid>", methods=["DELETE"])
def delete_category(cid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE courses SET category_id=NULL WHERE category_id=%s", (cid,))
    cur.execute("DELETE FROM categories WHERE id=%s", (cid,))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})

@app.route("/admin/courses", methods=["GET"])
def get_courses():
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT c.*, cat.name AS category_name, cat.color AS category_color,
               COUNT(cr.id) AS remind_count,
               SUM(CASE WHEN cr.sent THEN 1 ELSE 0 END) AS sent_count
        FROM courses c
        LEFT JOIN categories cat ON c.category_id = cat.id
        LEFT JOIN course_reminders cr ON c.id = cr.course_id
        GROUP BY c.id, cat.name, cat.color
        ORDER BY c.course_date ASC
    """)
    rows = cur.fetchall()
    cur.close(); conn.close()
    result = []
    for r in rows:
        d = dict(r)
        if d.get("course_date"): d["course_date"] = str(d["course_date"])
        result.append(d)
    return jsonify({"courses": result})

@app.route("/admin/courses", methods=["POST"])
def add_course():
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    d = request.json
    title = d.get("title","").strip()
    course_date = d.get("course_date","").replace("/","-")
    if not title or not course_date: return jsonify({"ok":False,"error":"請填寫課程名稱和日期"})
    rv = int(d.get("remind_value", 30))
    ru = d.get("remind_unit", "days")
    iv = int(d.get("remind_interval_value", 7))
    iu = d.get("remind_interval_unit", "days")
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO courses
          (category_id,title,course_date,course_time,location,description,image_url,
           remind_value,remind_unit,remind_interval_value,remind_interval_unit,created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
    """, (d.get("category_id"), title, course_date, d.get("course_time","09:00"),
          d.get("location",""), d.get("description",""), d.get("image_url",""),
          rv, ru, iv, iu, isonow()))
    cid = cur.fetchone()["id"]
    conn.commit(); cur.close(); conn.close()
    dates = generate_reminders(cid, course_date, rv, ru, iv, iu)
    return jsonify({"ok":True,"course_id":cid,"remind_count":len(dates)})

@app.route("/admin/courses/<int:cid>", methods=["PUT"])
def edit_course(cid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    d = request.json
    title = d.get("title","").strip()
    course_date = d.get("course_date","").replace("/","-")
    if not title or not course_date: return jsonify({"ok":False,"error":"請填寫課程名稱和日期"})
    rv = int(d.get("remind_value",30))
    ru = d.get("remind_unit","days")
    iv = int(d.get("remind_interval_value",7))
    iu = d.get("remind_interval_unit","days")
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        UPDATE courses SET category_id=%s,title=%s,course_date=%s,course_time=%s,
        location=%s,description=%s,image_url=%s,remind_value=%s,remind_unit=%s,
        remind_interval_value=%s,remind_interval_unit=%s WHERE id=%s
    """, (d.get("category_id"), title, course_date, d.get("course_time","09:00"),
          d.get("location",""), d.get("description",""), d.get("image_url",""),
          rv, ru, iv, iu, cid))
    conn.commit(); cur.close(); conn.close()
    dates = generate_reminders(cid, course_date, rv, ru, iv, iu)
    return jsonify({"ok":True,"remind_count":len(dates)})

@app.route("/admin/courses/<int:cid>", methods=["DELETE"])
def delete_course(cid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM courses WHERE id=%s", (cid,))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})

@app.route("/admin/courses/<int:cid>/send-now", methods=["POST"])
def send_course_now(cid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT c.*, cat.name AS category_name FROM courses c
        LEFT JOIN categories cat ON c.category_id=cat.id WHERE c.id=%s
    """, (cid,))
    row = cur.fetchone(); cur.close(); conn.close()
    if not row: return jsonify({"ok":False,"error":"找不到課程"})
    cd = datetime.strptime(str(row["course_date"]), "%Y-%m-%d").date()
    days_left = (cd - today_tw()).days
    timing = "【今天】" if days_left==0 else f"【還有{days_left}天】" if days_left>0 else "【已結束】"
    cat = f"[{row['category_name']}] " if row["category_name"] else ""
    text = (f"📚 課程提醒 {timing}\n━━━━━━━━━━━━\n"
            f"{cat}📌 {row['title']}\n"
            f"📅 {row['course_date']} {row['course_time']}")
    if row["location"]:    text += f"\n📍 {row['location']}"
    if row["description"]: text += f"\n📝 {row['description']}"
    msgs = []
    if row["image_url"]:
        msgs.append({"type":"image","originalContentUrl":row["image_url"],"previewImageUrl":row["image_url"]})
    msgs.append({"type":"text","text":text})
    ok, total = push_to_groups(msgs)
    return jsonify({"ok":ok,"total":total})

@app.route("/admin/send", methods=["POST"])
def admin_send():
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    d = request.json
    text = d.get("text","").strip()
    img_url = d.get("image_url","").strip()
    msgs = []
    if img_url: msgs.append({"type":"image","originalContentUrl":img_url,"previewImageUrl":img_url})
    if text:    msgs.append({"type":"text","text":text})
    if not msgs: return jsonify({"ok":0,"total":0})
    ok, total = push_to_groups(msgs)
    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT INTO announcements (content,sent_at,group_count) VALUES (%s,%s,%s)",
                (text, isonow(), total))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok":ok,"total":total})

@app.route("/admin/scheduled-announcements", methods=["GET"])
def get_scheduled_announcements():
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM scheduled_announcements ORDER BY send_at DESC LIMIT 50")
    rows = cur.fetchall(); cur.close(); conn.close()
    result = []
    for r in rows:
        d = dict(r)
        if d.get("send_at"): d["send_at"] = str(d["send_at"])
        if d.get("sent_time"): d["sent_time"] = str(d["sent_time"])
        if d.get("created_at"): d["created_at"] = str(d["created_at"])
        result.append(d)
    return jsonify({"announcements": result})

@app.route("/admin/scheduled-announcements", methods=["POST"])
def add_scheduled_announcement():
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    d = request.json
    content = d.get("content","").strip()
    send_at_str = d.get("send_at","").strip()
    if not content: return jsonify({"ok":False,"error":"請填寫公告內容"})
    if not send_at_str: return jsonify({"ok":False,"error":"請選擇發送時間"})
    try:
        send_at = datetime.fromisoformat(send_at_str)
        if send_at.tzinfo is None:
            send_at = send_at.replace(tzinfo=TZ)
    except Exception:
        return jsonify({"ok":False,"error":"時間格式錯誤"})
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO scheduled_announcements (title,content,image_url,send_at,sent,created_at)
        VALUES (%s,%s,%s,%s,FALSE,%s)
    """, (d.get("title","").strip(), content, d.get("image_url","").strip(), send_at.isoformat(), isonow()))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})

@app.route("/admin/scheduled-announcements/<int:aid>", methods=["DELETE"])
def delete_scheduled_announcement(aid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM scheduled_announcements WHERE id=%s AND sent=FALSE", (aid,))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})

@app.route("/admin/scheduled-announcements/<int:aid>/send-now", methods=["POST"])
def send_scheduled_announcement_now(aid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM scheduled_announcements WHERE id=%s", (aid,))
    row = cur.fetchone()
    if not row: cur.close(); conn.close(); return jsonify({"ok":False,"error":"找不到公告"})
    msgs = []
    if row["image_url"]:
        msgs.append({"type":"image","originalContentUrl":row["image_url"],"previewImageUrl":row["image_url"]})
    msgs.append({"type":"text","text":row["content"]})
    ok, total = push_to_groups(msgs)
    cur.execute("UPDATE scheduled_announcements SET sent=TRUE, sent_time=%s, group_count=%s WHERE id=%s",
                (isonow(), total, aid))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok":ok,"total":total})

# ── Broadcast Schedule Entries ──
@app.route("/admin/broadcast-entries", methods=["GET"])
def get_broadcast_entries():
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    src_type = request.args.get("source_type","")
    src_id   = request.args.get("source_id","")
    conn = get_db(); cur = conn.cursor()
    q = "SELECT * FROM broadcast_schedule_entries"
    params = []
    conds = []
    if src_type: conds.append("source_type=%s"); params.append(src_type)
    if src_id:   conds.append("source_id=%s");   params.append(int(src_id))
    if conds: q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY send_at ASC"
    cur.execute(q, params)
    rows = cur.fetchall(); cur.close(); conn.close()
    result = []
    for r in rows:
        d = dict(r)
        if d.get("send_at"):   d["send_at"]   = str(d["send_at"])
        if d.get("sent_time"): d["sent_time"]  = str(d["sent_time"])
        if d.get("created_at"):d["created_at"] = str(d["created_at"])
        result.append(d)
    return jsonify({"entries": result})

@app.route("/admin/broadcast-entries", methods=["POST"])
def add_broadcast_entries():
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    d = request.json
    src_type = d.get("source_type","").strip()
    src_id   = d.get("source_id")
    send_ats = d.get("send_ats", [])   # list of ISO datetime strings
    if not src_type or not src_id or not send_ats:
        return jsonify({"ok":False,"error":"請填寫 source_type、source_id 及發送時間"})
    conn = get_db(); cur = conn.cursor()
    count = 0
    for sa in send_ats:
        try:
            dt = datetime.fromisoformat(sa)
            # 若為 naive datetime（前端送台灣本地時間），補上台灣時區
            # 確保帶台灣時區（+08:00），讓 PostgreSQL 正確存 TIMESTAMPTZ
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ)
            cur.execute("""
                INSERT INTO broadcast_schedule_entries
                  (source_type, source_id, send_at, sent, created_at)
                VALUES (%s,%s,%s::timestamptz,FALSE,%s)
            """, (src_type, int(src_id), dt.isoformat(), isonow()))
            count += 1
        except Exception as e:
            logger.warning(f"[BcastEntry] skip invalid send_at {sa}: {e}")
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True, "added": count})

@app.route("/admin/broadcast-entries/<int:eid>", methods=["DELETE"])
def delete_broadcast_entry(eid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM broadcast_schedule_entries WHERE id=%s AND sent=FALSE", (eid,))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})

@app.route("/admin/broadcast-entries/<int:eid>/send-now", methods=["POST"])
def send_broadcast_entry_now(eid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM broadcast_schedule_entries WHERE id=%s", (eid,))
    row = cur.fetchone()
    if not row: cur.close(); conn.close(); return jsonify({"ok":False,"error":"找不到排程"})
    # Reuse the same build-message logic
    src_type = row["source_type"]; src_id = row["source_id"]
    msgs = []
    if src_type == "course":
        cur.execute("""SELECT c.*, cat.name AS category_name FROM courses c
                       LEFT JOIN categories cat ON c.category_id=cat.id WHERE c.id=%s""", (src_id,))
        c = cur.fetchone()
        if c:
            cd = datetime.strptime(str(c["course_date"]), "%Y-%m-%d").date()
            days_left = (cd - today_tw()).days
            timing = "【今天上課】" if days_left==0 else f"【還有 {days_left} 天】" if days_left>0 else "【已結束】"
            cat = f"[{c['category_name']}] " if c["category_name"] else ""
            text = (f"📚 課程提醒 {timing}\n━━━━━━━━━━━━\n{cat}📌 {c['title']}\n📅 {c['course_date']} {c['course_time']}")
            if c["location"]:    text += f"\n📍 {c['location']}"
            if c["description"]: text += f"\n📝 {c['description']}"
            if c["image_url"]:   msgs.append({"type":"image","originalContentUrl":c["image_url"],"previewImageUrl":c["image_url"]})
            msgs.append({"type":"text","text":text})
    elif src_type == "broadcast":
        cur.execute("SELECT * FROM scheduled_broadcasts WHERE id=%s", (src_id,))
        b = cur.fetchone()
        if b:
            if b["image_url"]: msgs.append({"type":"image","originalContentUrl":b["image_url"],"previewImageUrl":b["image_url"]})
            msgs.append({"type":"text","text":b["content"]})
    if not msgs: cur.close(); conn.close(); return jsonify({"ok":False,"error":"無法組建訊息"})
    ok, total = push_to_groups(msgs)
    cur.execute("UPDATE broadcast_schedule_entries SET sent=TRUE, sent_time=%s, group_count=%s WHERE id=%s",
                (isonow(), total, eid))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok":ok,"total":total})

@app.route("/admin/upload-image", methods=["POST"])
def upload_image():
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    if "image" not in request.files: return jsonify({"ok":False,"error":"沒有收到圖片"})
    f = request.files["image"]
    url, err = upload_image_to_imgbb(f.read(), f.filename)
    return jsonify({"ok":True,"url":url}) if url else jsonify({"ok":False,"error":err})

@app.route("/admin/scheduled", methods=["GET"])
def get_scheduled():
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM scheduled_broadcasts ORDER BY created_at DESC")
    rows = cur.fetchall(); cur.close(); conn.close()
    result = []
    for r in rows:
        d = dict(r)
        if d.get("next_run"): d["next_run"] = str(d["next_run"])
        if d.get("created_at"): d["created_at"] = str(d["created_at"])
        result.append(d)
    return jsonify({"schedules": result})

@app.route("/admin/scheduled", methods=["POST"])
def add_scheduled():
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    d = request.json
    title = d.get("title","").strip()
    content_text = d.get("content","").strip()
    if not title or not content_text: return jsonify({"ok":False,"error":"請填寫標題和內容"})
    iv = float(d.get("interval_value", 1))
    iu = d.get("interval_unit","days")
    interval_seconds = unit_to_seconds(iv, iu)
    start_time = d.get("start_time","")
    try:
        dt = datetime.fromisoformat(start_time)
        # 若為 naive datetime（前端送台灣本地時間），補上台灣時區
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        next_run = dt.isoformat()
    except Exception:
        next_run = isonow()
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO scheduled_broadcasts (title,content,image_url,interval_seconds,next_run,active,created_at)
        VALUES (%s,%s,%s,%s,%s,TRUE,%s)
    """, (title, content_text, d.get("image_url",""), interval_seconds, next_run, isonow()))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})

@app.route("/admin/scheduled/<int:sid>", methods=["PUT"])
def update_scheduled(sid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    d = request.json
    conn = get_db(); cur = conn.cursor()
    if "active" in d:
        cur.execute("UPDATE scheduled_broadcasts SET active=%s WHERE id=%s", (bool(d["active"]), sid))
    else:
        iv = float(d.get("interval_value",1))
        iu = d.get("interval_unit","days")
        cur.execute("""
            UPDATE scheduled_broadcasts SET title=%s,content=%s,image_url=%s,interval_seconds=%s WHERE id=%s
        """, (d.get("title"), d.get("content"), d.get("image_url",""), unit_to_seconds(iv,iu), sid))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})

@app.route("/admin/scheduled/<int:sid>", methods=["DELETE"])
def delete_scheduled(sid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM scheduled_broadcasts WHERE id=%s", (sid,))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})

@app.route("/admin/scheduled/<int:sid>/send-now", methods=["POST"])
def send_scheduled_now(sid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM scheduled_broadcasts WHERE id=%s", (sid,))
    row = cur.fetchone()
    if not row: cur.close(); conn.close(); return jsonify({"ok":False,"error":"找不到排程"})
    msgs = []
    if row["image_url"]:
        msgs.append({"type":"image","originalContentUrl":row["image_url"],"previewImageUrl":row["image_url"]})
    msgs.append({"type":"text","text":row["content"]})
    ok, total = push_to_groups(msgs)
    next_run = (now_tw() + timedelta(seconds=row["interval_seconds"])).isoformat()
    cur.execute("UPDATE scheduled_broadcasts SET next_run=%s WHERE id=%s", (next_run, sid))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok":ok,"total":total})

@app.route("/admin/ai-parse", methods=["POST"])
def ai_parse_course():
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    text = ""
    image_b64 = ""
    image_media_type = "image/jpeg"
    image_url = ""

    if request.content_type and "multipart" in request.content_type:
        text = request.form.get("text","").strip()
        if "image" in request.files:
            f = request.files["image"]
            image_b64 = base64.b64encode(f.read()).decode()
            image_media_type = f.content_type or "image/jpeg"
    else:
        data = request.get_json() or {}
        text = data.get("text","").strip()
        image_b64 = data.get("image_b64","").strip()
        image_url = data.get("image_url","").strip()
        image_media_type = data.get("image_media_type","image/jpeg")

    if not text and not image_b64 and not image_url:
        return jsonify({"ok":False,"error":"請輸入課程描述或上傳圖片"})

    today = today_tw().isoformat()
    prompt = (f"今天是 {today}（台灣時間）。請從圖片或文字中提取課程資訊，只回傳 JSON，不要任何其他文字：\n"
              f'{{"title":"課程名稱","course_date":"YYYY-MM-DD","course_time":"HH:MM",'
              f'"location":"地點或空字串","description":"說明或空字串",'
              f'"remind_value":30,"remind_unit":"days","remind_interval_value":7,"remind_interval_unit":"days"}}\n'
              f'相對日期（例如「下週五」）請根據今天 {today} 計算實際日期。\n'
              f'用戶輸入：{text}')
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
                  "messages":[{"role":"user","content":msg_content}]}, timeout=30)
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
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    check_and_send_reminders()
    check_scheduled_broadcasts()
    check_scheduled_announcements()
    check_broadcast_schedule_entries()
    # 回傳目前待發的 broadcast entries 數量，方便除錯
    conn = get_db(); cur = conn.cursor()
    now = now_tw()
    cur.execute("SELECT COUNT(*) as total FROM broadcast_schedule_entries WHERE sent=FALSE")
    pending_total = cur.fetchone()["total"]
    cur.execute("SELECT COUNT(*) as due FROM broadcast_schedule_entries WHERE sent=FALSE AND send_at <= %s", (now,))
    pending_due = cur.fetchone()["due"]
    cur.close(); conn.close()
    return jsonify({
        "ok": True,
        "tw_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "tw_date": today_tw().isoformat(),
        "pending_entries": int(pending_total),
        "due_entries": int(pending_due),
    })

@app.route("/admin/groups/sync-names", methods=["POST"])
def sync_group_names():
    """一鍵同步所有群組名稱（呼叫 LINE API）"""
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT group_id FROM groups")
    rows = cur.fetchall()
    updated = 0
    for row in rows:
        gid = row["group_id"]
        name = fetch_group_name(gid)
        if name:
            cur.execute("UPDATE groups SET group_name=%s WHERE group_id=%s", (name, gid))
            updated += 1
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True, "total": len(rows), "updated": updated})

@app.route("/admin/groups/<gid>/sync-name", methods=["POST"])
def sync_one_group_name(gid):
    """同步單一群組名稱"""
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    name = fetch_group_name(gid)
    if not name:
        return jsonify({"ok": False, "error": "無法取得群組名稱（機器人可能已離開該群組）"})
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE groups SET group_name=%s WHERE group_id=%s", (name, gid))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True, "group_name": name})

# ── Webhook ──
def handle_text(event):
    user_id = event["source"].get("userId","")
    reply_token = event["replyToken"]
    text = event["message"]["text"].strip()

    if event["source"]["type"] == "group":
        gid = event["source"]["groupId"]
        conn = get_db(); cur = conn.cursor()
        # 若群組已存在且有名稱則不重複呼叫 LINE API
        cur.execute("SELECT group_name FROM groups WHERE group_id=%s", (gid,))
        row = cur.fetchone()
        if row is None:
            group_name = fetch_group_name(gid)
            cur.execute("""
                INSERT INTO groups (group_id, joined_at, group_name)
                VALUES (%s, %s, %s)
                ON CONFLICT (group_id) DO UPDATE SET group_name = EXCLUDED.group_name
            """, (gid, isonow(), group_name))
        elif not row["group_name"]:
            group_name = fetch_group_name(gid)
            cur.execute("UPDATE groups SET group_name=%s WHERE group_id=%s", (group_name, gid))
        conn.commit(); cur.close(); conn.close()

    if user_id not in ADMIN_USER_IDS:
        return

    if text.startswith("/公告 "):
        ok, total = push_text(f"📢 {text[4:].strip()}")
        reply_message(reply_token, f"✅ 已發送到 {ok}/{total} 個群組")

    elif text.startswith("/新增課程 ") or text.startswith("/加課 "):
        desc = text.split(" ",1)[1].strip()
        try:
            today = today_tw().isoformat()
            prompt = (f"今天是{today}（台灣時間）。從以下文字提取課程資訊，只回傳JSON：\n"
                      f'{{"title":"","course_date":"YYYY-MM-DD","course_time":"HH:MM","location":"","description":""}}\n'
                      f"用戶：{desc}")
            resp = requests.post("https://api.anthropic.com/v1/messages",
                headers={"Content-Type":"application/json"},
                json={"model":"claude-sonnet-4-20250514","max_tokens":300,
                      "messages":[{"role":"user","content":prompt}]}, timeout=30)
            ai_text = resp.json()["content"][0]["text"].strip()
            if "```" in ai_text:
                ai_text = ai_text.split("```")[1]
                if ai_text.startswith("json"): ai_text = ai_text[4:]
            c = json.loads(ai_text.strip())
            conn = get_db(); cur = conn.cursor()
            cur.execute("""
                INSERT INTO courses
                  (title,course_date,course_time,location,description,image_url,
                   remind_value,remind_unit,remind_interval_value,remind_interval_unit,created_at)
                VALUES (%s,%s,%s,%s,%s,'',30,'days',7,'days',%s) RETURNING id
            """, (c["title"],c["course_date"],c.get("course_time","09:00"),
                  c.get("location",""),c.get("description",""),isonow()))
            cid = cur.fetchone()["id"]
            conn.commit(); cur.close(); conn.close()
            dates = generate_reminders(cid, c["course_date"], 30, "days", 7, "days")
            reply_message(reply_token,
                f"✅ 課程已新增！\n📌 {c['title']}\n📅 {c['course_date']} {c.get('course_time','09:00')}\n"
                f"📍 {c.get('location','未指定')}\n🔔 {len(dates)} 個提醒")
        except Exception as e:
            reply_message(reply_token, f"❌ AI 解析失敗\n{str(e)[:80]}")

    elif text == "/課程清單":
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT title, course_date::text FROM courses ORDER BY course_date ASC LIMIT 10")
        rows = cur.fetchall(); cur.close(); conn.close()
        if not rows:
            reply_message(reply_token, "目前沒有排程課程")
        else:
            reply_message(reply_token, "課程清單：\n" + "\n".join(f"📅 {r['course_date']} {r['title']}" for r in rows))

    elif text == "/群組清單":
        reply_message(reply_token, f"已連接 {len(get_all_group_ids())} 個群組")

    elif text in ("/說明", "/help"):
        url = APP_URL or "（請設定 APP_URL 環境變數）"
        reply_message(reply_token,
            f"📋 指令說明\n\n"
            f"/公告 [內容] — 立即發公告\n"
            f"/新增課程 [描述] — AI 新增課程\n"
            f"/課程清單 — 查看課程\n"
            f"/群組清單 — 查看群組\n\n"
            f"🌐 管理後台：\n{url}/admin")

def handle_join(event):
    if event["source"]["type"] == "group":
        gid = event["source"]["groupId"]
        group_name = fetch_group_name(gid)
        conn = get_db(); cur = conn.cursor()
        cur.execute("""
            INSERT INTO groups (group_id, joined_at, group_name)
            VALUES (%s, %s, %s)
            ON CONFLICT (group_id) DO UPDATE SET group_name = EXCLUDED.group_name
        """, (gid, isonow(), group_name))
        conn.commit(); cur.close(); conn.close()
        logger.info(f"Group joined: {gid} name={group_name!r}")

def handle_leave(event):
    if event["source"]["type"] == "group":
        gid = event["source"]["groupId"]
        conn = get_db(); cur = conn.cursor()
        cur.execute("DELETE FROM groups WHERE group_id=%s", (gid,))
        conn.commit(); cur.close(); conn.close()

@app.route("/webhook", methods=["POST"])
def webhook():
    sig = request.headers.get("X-Line-Signature","")
    body = request.get_data()
    if not verify_signature(body, sig):
        abort(400)
    for event in json.loads(body).get("events",[]):
        t = event.get("type")
        try:
            if t == "message" and event["message"]["type"] == "text":
                handle_text(event)
            elif t == "join":
                handle_join(event)
            elif t == "leave":
                handle_leave(event)
        except Exception as e:
            logger.error(f"Event handler error: {e}")
    return "OK"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
