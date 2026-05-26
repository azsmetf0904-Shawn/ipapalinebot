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
ADMIN_USER_IDS  = [x.strip() for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip()]
ADMIN_PASSWORD  = os.environ.get("ADMIN_PASSWORD", "")
DATABASE_URL    = os.environ["DATABASE_URL"]   # Railway 自動注入
IMGBB_API_KEY   = os.environ.get("IMGBB_API_KEY", "")
APP_URL         = os.environ.get("APP_URL", "")
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY", "")

# ── 多機器人設定 ──
# 每個 Bot 用環境變數 BOT_<KEY>_TOKEN / BOT_<KEY>_SECRET / BOT_<KEY>_NAME / BOT_<KEY>_GROUPS 設定
# BOT_<KEY>_GROUPS 為逗號分隔的群組 ID（選填；也可在後台動態綁定）
# 向下相容：若只有舊版 LINE_CHANNEL_ACCESS_TOKEN，自動當成 "main" bot
def _load_bots() -> dict:
    bots = {}
    # 掃描所有 BOT_xxx_TOKEN 環境變數
    for k, v in os.environ.items():
        if k.startswith("BOT_") and k.endswith("_TOKEN") and v.strip():
            key = k[4:-6]  # 擷取中間的 KEY
            bots[key] = {
                "name":   os.environ.get(f"BOT_{key}_NAME", key),
                "token":  v.strip(),
                "secret": os.environ.get(f"BOT_{key}_SECRET", ""),
                "groups": [g.strip() for g in os.environ.get(f"BOT_{key}_GROUPS", "").split(",") if g.strip()],
            }
    # 向下相容：舊版單一 Token
    if not bots and os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"):
        bots["main"] = {
            "name":   os.environ.get("BOT_MAIN_NAME", "主要機器人"),
            "token":  os.environ["LINE_CHANNEL_ACCESS_TOKEN"],
            "secret": os.environ.get("LINE_CHANNEL_SECRET", ""),
            "groups": [x.strip() for x in os.environ.get("DEFAULT_GROUP_IDS", "").split(",") if x.strip()],
        }
    return bots

BOTS = _load_bots()

# 向下相容：保留舊變數（用第一個 bot 填充，供 verify_signature 等使用）
_first_bot = next(iter(BOTS.values())) if BOTS else {}
LINE_CHANNEL_SECRET       = _first_bot.get("secret", os.environ.get("LINE_CHANNEL_SECRET", ""))
LINE_CHANNEL_ACCESS_TOKEN = _first_bot.get("token",  os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", ""))

def get_bot_headers(bot_key: str) -> dict:
    token = BOTS.get(bot_key, {}).get("token", LINE_CHANNEL_ACCESS_TOKEN)
    return {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}

# 預設用第一個 bot（向下相容 fetch_group_name 等舊函式）
HEADERS = get_bot_headers(next(iter(BOTS), "main")) if BOTS else {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
}

def gemini_call(prompt: str, image_b64: str = "", image_media_type: str = "image/jpeg",
               image_url: str = "", max_tokens: int = 800, _retry: int = 3) -> str:
    """呼叫 Gemini API，回傳純文字結果。遇到 429 自動重試最多 _retry 次。"""
    import time
    GEMINI_URL = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                  f"gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}")
    parts = []
    if image_b64:
        parts.append({"inline_data": {"mime_type": image_media_type, "data": image_b64}})
    elif image_url:
        img_resp = requests.get(image_url, timeout=10)
        b64 = base64.b64encode(img_resp.content).decode()
        mime = img_resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
        parts.append({"inline_data": {"mime_type": mime, "data": b64}})
    parts.append({"text": prompt})

    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "maxOutputTokens": 2048,
            "temperature": 0.1,
            "thinkingConfig": {"thinkingBudget": 0}   # 關閉 thinking，節省 token 與時間
        }
    }

    for attempt in range(1, _retry + 1):
        resp = requests.post(GEMINI_URL, headers={"Content-Type": "application/json"},
                             json=body, timeout=60)
        result = resp.json()

        if "error" in result:
            err = result["error"]
            code = err.get("code", 0)
            msg  = err.get("message", "Gemini API 錯誤")

            # 429 Rate Limit：等待後重試
            if code == 429 and attempt < _retry:
                wait = 45 * attempt   # 第1次等45秒，第2次等90秒
                logger.warning(f"Gemini 429 rate limit，{wait} 秒後重試（第 {attempt}/{_retry} 次）")
                time.sleep(wait)
                continue

            raise Exception(msg)

        # gemini-2.5-flash 回傳的 parts 可能含 thinking，找第一個純文字 part
        resp_parts = result["candidates"][0]["content"]["parts"]
        text = next((p["text"] for p in resp_parts if p.get("thought") is not True and "text" in p), None)
        if text is None:
            raise Exception("Gemini 回傳內容無法解析")
        return text.strip()

    raise Exception("Gemini API 重試次數已達上限，請稍後再試")


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
        ALTER TABLE groups ADD COLUMN IF NOT EXISTS bot_key    TEXT DEFAULT '';
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
            bot_key          TEXT DEFAULT '',
            created_at       TIMESTAMPTZ NOT NULL
        );
        ALTER TABLE scheduled_broadcasts ADD COLUMN IF NOT EXISTS bot_key TEXT DEFAULT '';
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
            bot_key     TEXT DEFAULT '',
            created_at  TIMESTAMPTZ NOT NULL
        );
        ALTER TABLE scheduled_announcements ADD COLUMN IF NOT EXISTS bot_key TEXT DEFAULT '';
        CREATE TABLE IF NOT EXISTS broadcast_schedule_entries (
            id           SERIAL PRIMARY KEY,
            source_type  TEXT NOT NULL,
            source_id    INTEGER NOT NULL,
            send_at      TIMESTAMPTZ NOT NULL,
            sent         BOOLEAN DEFAULT FALSE,
            sent_time    TIMESTAMPTZ,
            group_count  INTEGER DEFAULT 0,
            bot_key      TEXT DEFAULT '',
            created_at   TIMESTAMPTZ NOT NULL
        );
        ALTER TABLE broadcast_schedule_entries ADD COLUMN IF NOT EXISTS bot_key TEXT DEFAULT '';
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

def get_all_group_ids(bot_key: str = "") -> list[str]:
    """取得指定 bot 應發送的群組 ID 清單。"""
    conn = get_db()
    cur = conn.cursor()
    if bot_key and bot_key in BOTS:
        cur.execute("SELECT group_id FROM groups WHERE active=TRUE AND (bot_key=%s OR bot_key IS NULL OR bot_key='')", (bot_key,))
    else:
        cur.execute("SELECT group_id FROM groups WHERE active=TRUE")
    db_groups = [r["group_id"] for r in cur.fetchall()]
    cur.close(); conn.close()
    static = BOTS.get(bot_key, {}).get("groups", []) if bot_key else []
    return list(set(db_groups + static))

def verify_signature(body: bytes, sig: str, secret: str = "") -> bool:
    s = secret or LINE_CHANNEL_SECRET
    h = hmac.new(s.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(h).decode(), sig)

def find_bot_by_signature(body: bytes, sig: str) -> str:
    """逐一用所有 Bot 的 Secret 驗證簽名，回傳符合的 bot_key；找不到回傳空字串"""
    for key, bot in BOTS.items():
        secret = bot.get("secret", "")
        if secret and verify_signature(body, sig, secret):
            return key
    # fallback：向下相容單一 Secret 環境
    if LINE_CHANNEL_SECRET and verify_signature(body, sig, LINE_CHANNEL_SECRET):
        return next(iter(BOTS), "")
    return ""

def push_to_groups(messages: list, bot_key: str = "") -> tuple[int, int, str]:
    """回傳 (ok_count, total_count, error_hint)
    bot_key: 指定用哪個機器人帳號發送；空字串 = 向下相容（用第一個 bot）
    """
    if not bot_key:
        bot_key = next(iter(BOTS), "")
    headers = get_bot_headers(bot_key)
    groups  = get_all_group_ids(bot_key)
    ok = 0
    errors = []
    for gid in groups:
        try:
            r = requests.post("https://api.line.me/v2/bot/message/push", headers=headers,
                json={"to": gid, "messages": messages}, timeout=10)
            if r.status_code == 200:
                ok += 1
                logger.info(f"[{bot_key}] Push {gid[:20]}: OK")
            else:
                try:
                    err_body = r.json()
                    err_msg = err_body.get("message", "")
                except Exception:
                    err_msg = r.text[:100]
                logger.error(f"[{bot_key}] Push {gid[:20]}: {r.status_code} {err_msg}")
                if r.status_code == 403 or "not a member" in err_msg.lower():
                    hint = "機器人不在群組中（請重新邀請）"
                elif r.status_code == 401:
                    hint = f"Token 無效（{bot_key}），請更新環境變數"
                elif r.status_code == 429:
                    hint = f"推播次數已達免費上限（{bot_key} 每月 200 則）"
                else:
                    hint = f"LINE API 錯誤 {r.status_code}: {err_msg[:60]}"
                errors.append(hint)
        except Exception as e:
            logger.error(f"[{bot_key}] Push {gid[:20]}: exception {e}")
            errors.append(str(e)[:80])
    error_hint = errors[0] if errors and ok == 0 else ""
    return ok, len(groups), error_hint

def push_text(text: str, bot_key: str = "") -> tuple[int, int, str]:
    return push_to_groups([{"type": "text", "text": text}], bot_key=bot_key)


def reply_message(reply_token: str, text: str, bot_key: str = ""):
    """回覆訊息：指定 bot_key 可確保多 Bot 環境使用正確的 Token"""
    headers = get_bot_headers(bot_key) if bot_key else HEADERS
    requests.post("https://api.line.me/v2/bot/message/reply", headers=headers,
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
    try:
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

            ok, _, _ = push_to_groups(msgs)
            if ok > 0:
                cur.execute("UPDATE course_reminders SET sent=TRUE WHERE id=%s", (row["id"],))

        conn.commit()
        logger.info(f"[Reminder] Processed {len(rows)} reminders")
    finally:
        cur.close(); conn.close()

# ── 排程廣播（每 15 分鐘檢查） ──
def check_scheduled_broadcasts():
    now = now_tw()  # aware datetime，帶台灣時區
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM scheduled_broadcasts WHERE active=TRUE AND next_run <= %s", (now,))
        rows = cur.fetchall()
        logger.info(f"[Broadcast] Checking at {now.isoformat()}, found {len(rows)} due")

        for row in rows:
            msgs = []
            if row["image_url"]:
                msgs.append({"type":"image","originalContentUrl":row["image_url"],"previewImageUrl":row["image_url"]})
            msgs.append({"type":"text","text":row["content"]})
            ok, total, _err = push_to_groups(msgs, bot_key=row.get("bot_key",""))
            next_run = (now_tw() + timedelta(seconds=row["interval_seconds"])).isoformat()
            cur.execute("UPDATE scheduled_broadcasts SET next_run=%s WHERE id=%s", (next_run, row["id"]))
            logger.info(f"[Broadcast] '{row['title']}' sent {ok}/{total}, next_run={next_run}")

        conn.commit()
    finally:
        cur.close(); conn.close()

# ── 一次性排程公告（每 5 分鐘檢查） ──
def check_scheduled_announcements():
    now = now_tw()
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM scheduled_announcements WHERE sent=FALSE AND send_at <= %s", (now,))
        rows = cur.fetchall()
        logger.info(f"[SchedAnn] Checking at {now.isoformat()}, found {len(rows)} due")
        for row in rows:
            msgs = []
            if row["image_url"]:
                msgs.append({"type":"image","originalContentUrl":row["image_url"],"previewImageUrl":row["image_url"]})
            msgs.append({"type":"text","text":row["content"]})
            ok, total, _err = push_to_groups(msgs, bot_key=row.get("bot_key",""))
            cur.execute(
                "UPDATE scheduled_announcements SET sent=TRUE, sent_time=%s, group_count=%s WHERE id=%s",
                (now_tw().isoformat(), total, row["id"])
            )
            logger.info(f"[SchedAnn] id={row['id']} sent {ok}/{total}")
        conn.commit()
    finally:
        cur.close(); conn.close()

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
    try:
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
                ok, total, _err = push_to_groups(msgs, bot_key=row.get("bot_key",""))
                cur.execute(
                    "UPDATE broadcast_schedule_entries SET sent=TRUE, sent_time=%s, group_count=%s WHERE id=%s",
                    (now_tw().isoformat(), total, row["id"])
                )
                logger.info(f"[BcastEntry] id={row['id']} {src_type}#{src_id} sent {ok}/{total}")
        conn.commit()
    finally:
        cur.close(); conn.close()

scheduler.add_job(check_broadcast_schedule_entries, "interval", minutes=5,
                  id="bcast_entries", replace_existing=True)

def check_admin(req) -> bool:
    return req.headers.get("X-Admin-Pass") == ADMIN_PASSWORD

# ── Static ──
@app.route("/admin")
def admin_page():
    # admin.html 放在 static/ 資料夾
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
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    init_db()
    return "DB initialized OK"

# ── Admin API ──

@app.route("/admin/info", methods=["GET"])
def admin_info():
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    return jsonify({
        "timezone": "Asia/Taipei",
        "current_time": now_tw().strftime("%Y-%m-%d %H:%M:%S"),
        "groups": len(get_all_group_ids()),
        "bots": {k: {"name": v["name"], "group_count": len(get_all_group_ids(k))} for k, v in BOTS.items()}
    })

@app.route("/admin/bots", methods=["GET"])
def get_bots():
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    result = []
    for k, v in BOTS.items():
        result.append({
            "key": k,
            "name": v["name"],
            "group_count": len(get_all_group_ids(k)),
        })
    return jsonify({"bots": result})

@app.route("/admin/groups")
def get_groups():
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT group_id, joined_at, group_name, group_type, active, bot_key FROM groups ORDER BY joined_at DESC")
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
    if "bot_key" in d:
        fields.append("bot_key=%s"); vals.append(str(d["bot_key"]).strip())
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
    d2 = request.json or {}
    bot_key = d2.get("bot_key","").strip()
    ok, total, _err = push_to_groups(msgs, bot_key=bot_key)
    resp = {"ok": ok, "total": total}
    if _err: resp["error"] = _err
    return jsonify(resp)

@app.route("/admin/send", methods=["POST"])
def admin_send():
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    d = request.json
    text    = d.get("text","").strip()
    img_url = d.get("image_url","").strip()
    bot_key = d.get("bot_key","").strip()
    msgs = []
    if img_url: msgs.append({"type":"image","originalContentUrl":img_url,"previewImageUrl":img_url})
    if text:    msgs.append({"type":"text","text":text})
    if not msgs: return jsonify({"ok":0,"total":0})
    ok, total, _err = push_to_groups(msgs, bot_key=bot_key)

    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT INTO announcements (content,sent_at,group_count) VALUES (%s,%s,%s)",
                (text, isonow(), total))
    conn.commit(); cur.close(); conn.close()
    resp = {"ok":ok,"total":total}
    if _err: resp["error"] = _err
    return jsonify(resp)

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
    bot_key = d.get("bot_key","").strip()
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO scheduled_announcements (title,content,image_url,send_at,sent,bot_key,created_at)
        VALUES (%s,%s,%s,%s,FALSE,%s,%s)
    """, (d.get("title","").strip(), content, d.get("image_url","").strip(), send_at.isoformat(), bot_key, isonow()))
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
    ok, total, _err = push_to_groups(msgs, bot_key=row.get("bot_key",""))
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
    ok, total, _err = push_to_groups(msgs, bot_key=row.get("bot_key",""))
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
        next_run = datetime.fromisoformat(start_time).isoformat()
    except Exception:
        next_run = isonow()
    conn = get_db(); cur = conn.cursor()
    bot_key = d.get("bot_key","").strip()
    cur.execute("""
        INSERT INTO scheduled_broadcasts (title,content,image_url,interval_seconds,next_run,active,bot_key,created_at)
        VALUES (%s,%s,%s,%s,%s,TRUE,%s,%s)
    """, (title, content_text, d.get("image_url",""), interval_seconds, next_run, bot_key, isonow()))
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
    ok, total, _err = push_to_groups(msgs, bot_key=row.get("bot_key",""))
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
    prompt = (
        f"今天是 {today}（台灣時間）。請從圖片或文字中提取課程資訊。\n"
        f"重要規則：\n"
        f"- 若圖片或文字中有多個活動，請選擇用戶文字指定的那個；若未指定則選第一個未過期的活動。\n"
        f"- 只回傳一個 JSON 物件，不要任何其他文字、說明或 markdown。\n"
        f"- 相對日期（例如「下週五」）請根據今天 {today} 計算實際日期。\n"
        f"- 若圖片中有多個活動但用戶有在文字中指定某個活動名稱，優先選那個。\n"
        f"JSON 格式如下（直接輸出，不加任何前綴）：\n"
        f'{{"title":"課程或活動名稱","course_date":"YYYY-MM-DD","course_time":"HH:MM",'
        f'"location":"地點或空字串","description":"說明或空字串",'
        f'"remind_value":30,"remind_unit":"days","remind_interval_value":7,"remind_interval_unit":"days"}}\n'
        f'用戶輸入：{text}'
    )
    try:
        ai_text = gemini_call(prompt, image_b64=image_b64,
                              image_media_type=image_media_type,
                              image_url=image_url, max_tokens=600)
        # 清除 markdown code block
        if "```" in ai_text:
            ai_text = ai_text.split("```")[1]
            if ai_text.startswith("json"): ai_text = ai_text[4:]
        # 擷取第一個完整 JSON 物件
        start = ai_text.find("{")
        end   = ai_text.rfind("}") + 1
        if start == -1 or end == 0:
            raise Exception("AI 未回傳有效 JSON")
        c = json.loads(ai_text[start:end].strip())
        return jsonify({"ok":True,"course":c})
    except Exception as e:
        logger.error(f"AI parse error: {e}")
        return jsonify({"ok":False,"error":str(e)})


@app.route("/admin/ai-parse-multi", methods=["POST"])
def ai_parse_multi():
    """AI 解析圖片或文字中的所有活動，批次新增為課程"""
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    text = ""
    image_b64 = ""
    image_media_type = "image/jpeg"

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
        image_media_type = data.get("image_media_type","image/jpeg")

    if not text and not image_b64:
        return jsonify({"ok":False,"error":"請輸入描述或上傳圖片"})

    today = today_tw().isoformat()
    prompt = (
        f"今天是 {today}（台灣時間）。請從圖片或文字中提取所有活動/課程資訊。\n"
        f"重要規則：\n"
        f"- 找出圖片或文字中的每一個活動，全部列出，不要遺漏。\n"
        f"- 只回傳一個 JSON 陣列，不要任何其他文字、說明或 markdown。\n"
        f"- 相對日期請根據今天 {today} 計算實際日期。\n"
        f"- 若某個活動沒有明確時間，course_time 填 '09:00'。\n"
        f"- 若某個活動沒有明確地點，location 填空字串。\n"
        f"JSON 格式（陣列，每個活動一個物件）：\n"
        f'[{{"title":"活動名稱","course_date":"YYYY-MM-DD","course_time":"HH:MM",'
        f'"location":"地點或空字串","description":"說明或空字串",'
        f'"remind_value":30,"remind_unit":"days","remind_interval_value":7,"remind_interval_unit":"days"}}]\n'
        f'用戶輸入：{text}'
    )

    try:
        ai_text = gemini_call(prompt, image_b64=image_b64,
                              image_media_type=image_media_type, max_tokens=2048)
        # 清除 markdown
        if "```" in ai_text:
            ai_text = ai_text.split("```")[1]
            if ai_text.startswith("json"): ai_text = ai_text[4:]
        # 擷取 JSON 陣列
        start = ai_text.find("[")
        end   = ai_text.rfind("]") + 1
        if start == -1 or end == 0:
            raise Exception("AI 未回傳有效 JSON 陣列")
        courses = json.loads(ai_text[start:end].strip())
        if not isinstance(courses, list) or len(courses) == 0:
            raise Exception("AI 未解析出任何活動")

        # 批次新增課程
        saved = []
        errors = []
        conn = get_db(); cur = conn.cursor()
        for c in courses:
            try:
                title       = str(c.get("title","")).strip()
                course_date = str(c.get("course_date","")).replace("/","-").strip()
                if not title or not course_date:
                    errors.append(f"略過：{c}")
                    continue
                rv = int(c.get("remind_value", 30))
                ru = c.get("remind_unit", "days")
                iv = int(c.get("remind_interval_value", 7))
                iu = c.get("remind_interval_unit", "days")
                cur.execute("""
                    INSERT INTO courses
                      (category_id,title,course_date,course_time,location,description,image_url,
                       remind_value,remind_unit,remind_interval_value,remind_interval_unit,created_at)
                    VALUES (%s,%s,%s,%s,%s,%s,'',  %s,%s,%s,%s,%s) RETURNING id
                """, (None, title, course_date, c.get("course_time","09:00"),
                      c.get("location",""), c.get("description",""),
                      rv, ru, iv, iu, isonow()))
                cid = cur.fetchone()["id"]
                dates = generate_reminders(cid, course_date, rv, ru, iv, iu)
                saved.append({"id": cid, "title": title, "course_date": course_date,
                              "remind_count": len(dates)})
            except Exception as ce:
                errors.append(f"{c.get('title','?')}：{str(ce)[:60]}")
        conn.commit(); cur.close(); conn.close()

        return jsonify({"ok": True, "saved": saved, "errors": errors,
                        "total": len(courses), "saved_count": len(saved)})
    except Exception as e:
        logger.error(f"AI parse multi error: {e}")
        return jsonify({"ok":False,"error":str(e)})


@app.route("/admin/ai-parse-broadcast", methods=["POST"])
def ai_parse_broadcast():
    """AI 解析圖片或文字，自動生成排程廣播內容與提醒設定"""
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
        return jsonify({"ok":False,"error":"請輸入描述或上傳圖片"})

    today = today_tw().isoformat()
    prompt = (
        f"今天是 {today}（台灣時間）。請從圖片或文字中提取活動/廣播資訊，只回傳 JSON，不要任何其他文字：\n"
        f'{{"title":"活動或廣播的標題","content":"推播給群組的完整廣播內容（請寫得清楚完整，包含時間地點等重要資訊）",'
        f'"event_date":"YYYY-MM-DD（活動日期，若無則留空字串）",'
        f'"event_time":"HH:MM（活動時間，若無則留空字串）",'
        f'"remind_days_before":30,"remind_interval_days":7,'
        f'"interval_value":1,"interval_unit":"days",'
        f'"start_time":"YYYY-MM-DDTHH:MM（第一次發送時間，若有活動日期則自動往前推 remind_days_before 天，若無活動日期則填今天）"}}\n'
        f'說明：\n'
        f'- remind_days_before：提前幾天開始提醒（若用戶有指定就用，否則預設 30）\n'
        f'- remind_interval_days：每幾天提醒一次（若用戶有指定就用，否則預設 7）\n'
        f'- interval_value + interval_unit：排程發送間隔（和 remind_interval_days 對應，unit 固定填 "days"）\n'
        f'- start_time：第一次發送時間，格式 YYYY-MM-DDTHH:MM，根據 event_date 往前推 remind_days_before 天計算\n'
        f'- content 要包含完整資訊，可以加上 emoji 讓訊息更生動\n'
        f'相對日期（例如「下週五」）請根據今天 {today} 計算。\n'
        f'用戶輸入：{text}'
    )

    try:
        ai_text = gemini_call(prompt, image_b64=image_b64,
                              image_media_type=image_media_type,
                              image_url=image_url, max_tokens=800)
        if "```" in ai_text:
            ai_text = ai_text.split("```")[1]
            if ai_text.startswith("json"): ai_text = ai_text[4:]
        c = json.loads(ai_text.strip())

        st = c.get("start_time","")
        if st:
            try:
                dt = datetime.fromisoformat(st)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=TZ)
                c["start_time"] = dt.isoformat()
            except Exception:
                c["start_time"] = isonow()
        else:
            c["start_time"] = isonow()

        return jsonify({"ok":True,"broadcast":c})
    except Exception as e:
        logger.error(f"AI parse broadcast error: {e}")
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
def handle_text(event, bot_key: str = ""):
    user_id = event["source"].get("userId","")
    reply_token = event["replyToken"]
    text = event["message"]["text"].strip()

    if event["source"]["type"] == "group":
        gid = event["source"]["groupId"]
        conn = get_db()
        cur = conn.cursor()
        try:
            # 若群組已存在且有名稱則不重複呼叫 LINE API
            cur.execute("SELECT group_name FROM groups WHERE group_id=%s", (gid,))
            row = cur.fetchone()
            if row is None:
                headers = get_bot_headers(bot_key) if bot_key else HEADERS
                try:
                    r = requests.get(f"https://api.line.me/v2/bot/group/{gid}/summary",
                                     headers=headers, timeout=8)
                    group_name = r.json().get("groupName","") if r.status_code==200 else ""
                except Exception:
                    group_name = ""
                cur.execute("""
                    INSERT INTO groups (group_id, joined_at, group_name, bot_key)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (group_id) DO UPDATE SET group_name = EXCLUDED.group_name, bot_key = EXCLUDED.bot_key
                """, (gid, isonow(), group_name, bot_key))
            elif not row["group_name"]:
                group_name = fetch_group_name(gid)
                cur.execute("UPDATE groups SET group_name=%s WHERE group_id=%s", (group_name, gid))
            conn.commit()
        finally:
            cur.close(); conn.close()

    if user_id not in ADMIN_USER_IDS:
        return

    if text.startswith("/公告 "):
        ok, total, _err = push_text(f"📢 {text[4:].strip()}", bot_key=bot_key)
        reply_message(reply_token, f"✅ 已發送到 {ok}/{total} 個群組", bot_key=bot_key)

    elif text.startswith("/新增課程 ") or text.startswith("/加課 "):
        desc = text.split(" ",1)[1].strip()
        try:
            today = today_tw().isoformat()
            prompt = (f"今天是{today}（台灣時間）。從以下文字提取課程資訊，只回傳JSON：\n"
                      f'{{"title":"","course_date":"YYYY-MM-DD","course_time":"HH:MM","location":"","description":""}}\n'
                      f"用戶：{desc}")
            ai_text = gemini_call(prompt, max_tokens=300)
            if "```" in ai_text:
                ai_text = ai_text.split("```")[1]
                if ai_text.startswith("json"): ai_text = ai_text[4:]
            c = json.loads(ai_text.strip())
            conn = get_db(); cur = conn.cursor()
            try:
                cur.execute("""
                    INSERT INTO courses
                      (title,course_date,course_time,location,description,image_url,
                       remind_value,remind_unit,remind_interval_value,remind_interval_unit,created_at)
                    VALUES (%s,%s,%s,%s,%s,'',30,'days',7,'days',%s) RETURNING id
                """, (c["title"],c["course_date"],c.get("course_time","09:00"),
                      c.get("location",""),c.get("description",""),isonow()))
                cid = cur.fetchone()["id"]
                conn.commit()
            finally:
                cur.close(); conn.close()
            dates = generate_reminders(cid, c["course_date"], 30, "days", 7, "days")
            reply_message(reply_token,
                f"✅ 課程已新增！\n📌 {c['title']}\n📅 {c['course_date']} {c.get('course_time','09:00')}\n"
                f"📍 {c.get('location','未指定')}\n🔔 {len(dates)} 個提醒",
                bot_key=bot_key)
        except Exception as e:
            reply_message(reply_token, f"❌ AI 解析失敗\n{str(e)[:80]}", bot_key=bot_key)

    elif text == "/課程清單":
        conn = get_db(); cur = conn.cursor()
        try:
            cur.execute("SELECT title, course_date::text FROM courses ORDER BY course_date ASC LIMIT 10")
            rows = cur.fetchall()
        finally:
            cur.close(); conn.close()
        if not rows:
            reply_message(reply_token, "目前沒有排程課程", bot_key=bot_key)
        else:
            reply_message(reply_token, "課程清單：\n" + "\n".join(f"📅 {r['course_date']} {r['title']}" for r in rows), bot_key=bot_key)

    elif text == "/群組清單":
        reply_message(reply_token, f"已連接 {len(get_all_group_ids(bot_key))} 個群組", bot_key=bot_key)

    elif text in ("/說明", "/help"):
        url = APP_URL or "（請設定 APP_URL 環境變數）"
        reply_message(reply_token,
            f"📋 指令說明\n\n"
            f"/公告 [內容] — 立即發公告\n"
            f"/新增課程 [描述] — AI 新增課程\n"
            f"/課程清單 — 查看課程\n"
            f"/群組清單 — 查看群組\n\n"
            f"🌐 管理後台：\n{url}/admin",
            bot_key=bot_key)

def _get_chat_id_and_name(event, headers) -> tuple[str, str]:
    """從 join/leave event 取得 chat id 與名稱，支援 group 和 room（多人聊天室）"""
    src = event["source"]
    src_type = src.get("type","")
    if src_type == "group":
        gid = src["groupId"]
        try:
            r = requests.get(f"https://api.line.me/v2/bot/group/{gid}/summary",
                             headers=headers, timeout=8)
            name = r.json().get("groupName","") if r.status_code==200 else ""
        except Exception:
            name = ""
        return gid, name
    elif src_type == "room":
        gid = src["roomId"]
        return gid, ""   # room API 不提供名稱
    return "", ""

def handle_join(event, bot_key: str = ""):
    headers = get_bot_headers(bot_key) if bot_key else HEADERS
    gid, group_name = _get_chat_id_and_name(event, headers)
    if not gid:
        return
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO groups (group_id, joined_at, group_name, bot_key)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (group_id) DO UPDATE SET group_name = EXCLUDED.group_name, bot_key = EXCLUDED.bot_key
    """, (gid, isonow(), group_name, bot_key))
    conn.commit(); cur.close(); conn.close()
    logger.info(f"Chat joined: {gid} name={group_name!r} bot={bot_key!r}")

def handle_leave(event):
    src = event["source"]
    src_type = src.get("type","")
    if src_type == "group":
        gid = src["groupId"]
    elif src_type == "room":
        gid = src["roomId"]
    else:
        return
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM groups WHERE group_id=%s", (gid,))
    conn.commit(); cur.close(); conn.close()
    logger.info(f"Chat left: {gid}")

@app.route("/webhook", methods=["POST"])
def webhook():
    sig = request.headers.get("X-Line-Signature","")
    body = request.get_data()
    bot_key = find_bot_by_signature(body, sig)
    if not bot_key:
        abort(400)
    for event in json.loads(body).get("events",[]):
        t = event.get("type")
        try:
            if t == "message" and event["message"]["type"] == "text":
                handle_text(event, bot_key=bot_key)
            elif t == "join":
                handle_join(event, bot_key=bot_key)
            elif t == "leave":
                handle_leave(event)
        except Exception as e:
            logger.error(f"Event handler error: {e}")
    return "OK"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
