import os, json, hashlib, hmac, base64, logging, requests
import re
import time
import random
import threading
from collections import Counter
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
# 備援 Gemini Key（最多支援 3 組，環境變數設 GEMINI_API_KEY_2、GEMINI_API_KEY_3）
GEMINI_API_KEYS = [k for k in [
    GEMINI_API_KEY,
    os.environ.get("GEMINI_API_KEY_2", ""),
    os.environ.get("GEMINI_API_KEY_3", ""),
] if k.strip()]

# ── 冷卻機制（in-memory，重啟清空）──
# 注意：單 worker 多執行緒安全（已加鎖）；若改成多 worker 需改用 Redis 等共享快取
_cooldown_cache: dict = {}   # { user_id: last_trigger_timestamp }
_cooldown_lock = threading.Lock()
COOLDOWN_SECONDS = 30        # 同一用戶 30 秒內只能觸發一次

def check_cooldown(user_id: str) -> bool:
    """回傳 True 代表冷卻中（應拒絕），False 代表可以回覆。執行緒安全。"""
    now_ts = time.time()
    with _cooldown_lock:
        last = _cooldown_cache.get(user_id, 0)
        if now_ts - last < COOLDOWN_SECONDS:
            return True
        _cooldown_cache[user_id] = now_ts
    return False

def cleanup_cooldown_cache():
    """清理超過 1 小時的冷卻快取，防止記憶體持續增長"""
    now_ts = time.time()
    with _cooldown_lock:
        expired = [uid for uid, ts in _cooldown_cache.items() if now_ts - ts > 3600]
        for uid in expired:
            del _cooldown_cache[uid]
    if expired:
        logger.info(f"[Cooldown] 清理過期 {len(expired)} 筆")

# ── 人設快取（避免每次 Gemini 呼叫都查 DB）──
_persona_cache: dict = {}   # { bot_key: {"data": {...}, "ts": float} }
_persona_lock = threading.Lock()
PERSONA_CACHE_TTL = 60  # 秒；後台存檔後最多 60 秒生效

def get_persona(bot_key: str = "main") -> dict:
    """從 DB 讀取人設，帶 TTL 快取。若找不到指定 bot_key 則 fallback 到 main。"""
    now_ts = time.time()
    with _persona_lock:
        cached = _persona_cache.get(bot_key)
        if cached and now_ts - cached["ts"] < PERSONA_CACHE_TTL:
            return cached["data"]
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM bot_persona WHERE bot_key=%s AND active=TRUE LIMIT 1",
                (bot_key,)
            )
            row = cur.fetchone()
            if not row and bot_key != "main":
                cur.execute(
                    "SELECT * FROM bot_persona WHERE bot_key='main' AND active=TRUE LIMIT 1"
                )
                row = cur.fetchone()
    except Exception as e:
        logger.warning(f"[Persona] DB read error: {e}")
        row = None

    # 若 DB 完全沒資料，回傳內建預設值
    data = dict(row) if row else {
        "bot_key": bot_key,
        "name": "麥可（Michael）",
        "background": "你是一隻住在北投豪宅的明星羊駝，幫大家管行程、查課程、提醒會議。",
        "personality": "懶散但可靠，不承認自己是機器人",
        "restrictions": "不教學、不銷售",
        "greeting_words": "咕嚕,咕嘟,噗咕",
        "extra_notes": "",
    }
    with _persona_lock:
        _persona_cache[bot_key] = {"data": data, "ts": now_ts}
    return data

def invalidate_persona_cache(bot_key: str = ""):
    """後台存檔後呼叫，清除快取讓下次立即重讀 DB。"""
    with _persona_lock:
        if bot_key:
            _persona_cache.pop(bot_key, None)
        else:
            _persona_cache.clear()

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

# ── 關鍵字清單（從 DB app_settings 讀取，帶 TTL 快取）──
# 預設值：若 DB 尚無設定，自動使用這組
_KW_DEFAULTS = {
    "kw_course":      "課程,有什麼課,近期,行程,下週,這週,本週,今天,明天,最近,課表,安排,幾號,什麼時候,上課,典禮,活動",
    "kw_broadcast":   "公告,最新消息,有什麼消息,廣播,通知",
    "kw_meeting":     "會議,線上會議,開會,會前會,線上",
    "kw_recruitment": "招商,說明會,招商說明",
    "kw_training":    "內訓,教練,系統人員,培訓,工作坊",
}
_KW_CACHE: dict = {}
_KW_CACHE_TS: float = 0.0
_KW_CACHE_TTL = 120  # 秒；後台改完最多 2 分鐘生效

def _load_keywords() -> dict[str, list[str]]:
    """從 DB 讀關鍵字設定，回傳 {key: [kw, ...]}，帶 TTL 快取。"""
    global _KW_CACHE, _KW_CACHE_TS
    now_ts = time.time()
    if _KW_CACHE and now_ts - _KW_CACHE_TS < _KW_CACHE_TTL:
        return _KW_CACHE
    result = {}
    for key, default in _KW_DEFAULTS.items():
        raw = get_setting(key, default)
        result[key] = [kw.strip() for kw in raw.split(",") if kw.strip()]
    _KW_CACHE = result
    _KW_CACHE_TS = now_ts
    return result

def get_keywords(key: str) -> list[str]:
    """取得單一關鍵字清單（供外部呼叫）。"""
    return _load_keywords().get(key, [])

# 初始化時同步寫入 DB 預設值（僅在該 key 不存在時）
def _init_keyword_defaults():
    for key, default in _KW_DEFAULTS.items():
        if not get_setting(key):
            set_setting(key, default)

def gemini_call(prompt: str, image_b64: str = "", image_media_type: str = "image/jpeg",
               image_url: str = "", max_tokens: int = 800, _retry: int = 3,
               temperature: float = 0.7) -> str:
    """呼叫 Gemini API，遇到 429 自動輪換備援 key 並重試。"""
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
            "maxOutputTokens": max_tokens,
            "temperature": temperature,
        }
    }

    keys_to_try = GEMINI_API_KEYS if GEMINI_API_KEYS else [GEMINI_API_KEY]
    last_error = "Gemini API 無可用金鑰"

    for key_index, api_key in enumerate(keys_to_try):
        for attempt in range(1, _retry + 1):
            url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                   f"gemini-2.5-flash:generateContent?key={api_key}")
            resp = requests.post(url, headers={"Content-Type": "application/json"},
                                 json=body, timeout=60)
            result = resp.json()

            if "error" in result:
                err  = result["error"]
                code = err.get("code", 0)
                msg  = err.get("message", "Gemini API 錯誤")

                if code == 429:
                    # 先試備援 key（若還有的話）
                    if key_index + 1 < len(keys_to_try):
                        logger.warning(f"Gemini key[{key_index+1}] 429，切換備援 key[{key_index+2}]")
                        last_error = msg
                        break  # 跳出 attempt 迴圈，進入下一個 key
                    # 沒有備援 key，等待後重試
                    if attempt < _retry:
                        wait = 12 * attempt
                        logger.warning(f"Gemini 429（唯一key），{wait}秒後重試（{attempt}/{_retry}）")
                        time.sleep(wait)
                        continue

                last_error = msg
                break  # 非 429 錯誤，直接換下一個 key 或結束

            # 成功
            resp_parts = result["candidates"][0]["content"]["parts"]
            text = next((p["text"] for p in resp_parts if p.get("thought") is not True and "text" in p), None)
            if text is None:
                raise Exception("Gemini 回傳內容無法解析")
            if key_index > 0:
                logger.info(f"Gemini 使用備援 key[{key_index+1}] 成功")
            return text.strip()

    raise Exception(last_error)


def fetch_group_name(group_id: str, bot_key: str = "") -> str:
    """呼叫 LINE API 取得群組名稱，失敗回傳空字串"""
    headers = get_bot_headers(bot_key) if bot_key and bot_key in BOTS else HEADERS
    try:
        r = requests.get(
            f"https://api.line.me/v2/bot/group/{group_id}/summary",
            headers=headers, timeout=8
        )
        if r.status_code == 200:
            return r.json().get("groupName", "")
        logger.warning(f"fetch_group_name {group_id} bot={bot_key}: HTTP {r.status_code}")
    except Exception as e:
        logger.warning(f"fetch_group_name {group_id}: {e}")
    return ""

# ── PostgreSQL 連線 ──
from psycopg2 import pool as psycopg2_pool
from contextlib import contextmanager

# ── Connection Pool（最小 1 條、最大 10 條，單 worker 多執行緒安全）──
# Railway 免費版 PostgreSQL 上限約 20~25 條，max 設 10 保留空間給排程任務
_db_url = urlparse(DATABASE_URL)
_DB_POOL = psycopg2_pool.ThreadedConnectionPool(
    minconn=1,
    maxconn=10,
    host=_db_url.hostname,
    port=_db_url.port or 5432,
    dbname=_db_url.path.lstrip("/"),
    user=_db_url.username,
    password=_db_url.password,
    sslmode="require",
    cursor_factory=RealDictCursor,
    connect_timeout=10,
)

class _PooledConn:
    """Connection wrapper：呼叫 close() 時將連線歸還到 pool，而不是真正關閉。
    這讓所有現有的 conn.close() 呼叫點無需修改，同時也能正確歸還連線。"""
    __slots__ = ("_conn",)

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        try:
            _DB_POOL.putconn(self._conn)
        except Exception as e:
            logger.warning(f"[DB] putconn failed: {e}")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type:
            try:
                self._conn.rollback()
            except Exception:
                pass
        self.close()


def get_db():
    """從連線池取出一條連線（以 _PooledConn 包裝）。
    呼叫方 conn.close() 時連線歸還 pool，不會真正關閉。
    即使沒有 try/finally，只要有呼叫 conn.close()，連線就不會洩漏。"""
    conn = _DB_POOL.getconn()
    conn.autocommit = False
    return _PooledConn(conn)

@contextmanager
def db_conn():
    """Context manager：自動從 pool 取得連線，離開時無論是否發生例外都歸還。
    用法：
        with db_conn() as conn:
            cur = conn.cursor()
            ...
            conn.commit()
    """
    conn = _DB_POOL.getconn()
    conn.autocommit = False
    try:
        yield conn
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        _DB_POOL.putconn(conn)

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
            bot_key               TEXT DEFAULT '',
            created_at            TIMESTAMPTZ NOT NULL
        );
        ALTER TABLE courses ADD COLUMN IF NOT EXISTS bot_key TEXT DEFAULT '';
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
            end_time         TIMESTAMPTZ DEFAULT NULL,
            active           BOOLEAN DEFAULT TRUE,
            bot_key          TEXT DEFAULT '',
            created_at       TIMESTAMPTZ NOT NULL
        );
        ALTER TABLE scheduled_broadcasts ADD COLUMN IF NOT EXISTS bot_key TEXT DEFAULT '';
        ALTER TABLE scheduled_broadcasts ADD COLUMN IF NOT EXISTS end_time TIMESTAMPTZ DEFAULT NULL;
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

        CREATE TABLE IF NOT EXISTS chat_memory (
            id         SERIAL PRIMARY KEY,
            user_id    TEXT NOT NULL,
            group_id   TEXT NOT NULL,
            role       TEXT NOT NULL,        -- 'user' 或 'assistant'
            content    TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL
        );
        CREATE INDEX IF NOT EXISTS chat_memory_lookup
            ON chat_memory (user_id, group_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS alpaca_wander (
            id           SERIAL PRIMARY KEY,
            group_id     TEXT NOT NULL UNIQUE,
            enabled      BOOLEAN DEFAULT FALSE,
            send_hour    INTEGER DEFAULT 14,   -- 幾點發（台灣時間）
            interval_days INTEGER DEFAULT 7,   -- 每幾天發一次
            last_sent    TIMESTAMPTZ,
            created_at   TIMESTAMPTZ NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chat_logs (
            id          SERIAL PRIMARY KEY,
            group_id    TEXT NOT NULL,
            group_name  TEXT DEFAULT '',
            user_id     TEXT NOT NULL,
            question    TEXT NOT NULL,
            answer      TEXT NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL
        );
        CREATE INDEX IF NOT EXISTS chat_logs_group ON chat_logs (group_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS chat_logs_time  ON chat_logs (created_at DESC);

        CREATE TABLE IF NOT EXISTS course_broadcast_cache (
            id           SERIAL PRIMARY KEY,
            course_id    INTEGER REFERENCES courses(id) ON DELETE CASCADE,
            send_at      TIMESTAMPTZ NOT NULL,
            status       TEXT DEFAULT 'pending',
            retry_count  INTEGER DEFAULT 0,
            message_text TEXT NOT NULL,
            image_url    TEXT DEFAULT '',
            bot_key      TEXT DEFAULT '',
            sent_time    TIMESTAMPTZ,
            created_at   TIMESTAMPTZ NOT NULL
        );
        CREATE INDEX IF NOT EXISTS cbc_pending ON course_broadcast_cache (send_at, status);
        CREATE INDEX IF NOT EXISTS cbc_course  ON course_broadcast_cache (course_id);

        CREATE TABLE IF NOT EXISTS quick_reply_buttons (
            id         SERIAL PRIMARY KEY,
            label      TEXT NOT NULL,
            btn_type   TEXT NOT NULL DEFAULT 'tag',
            tags       TEXT NOT NULL DEFAULT '',
            sort_order INTEGER NOT NULL DEFAULT 0,
            active     BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL
        );

        CREATE TABLE IF NOT EXISTS bot_persona (
            id           SERIAL PRIMARY KEY,
            bot_key      TEXT NOT NULL DEFAULT 'main',
            name         TEXT NOT NULL DEFAULT '麥可（Michael）',
            background   TEXT NOT NULL DEFAULT '',
            personality  TEXT NOT NULL DEFAULT '',
            restrictions TEXT NOT NULL DEFAULT '',
            greeting_words TEXT NOT NULL DEFAULT '咕嚕,咕嘟,噗咕,咕哇,咕～',
            extra_notes  TEXT NOT NULL DEFAULT '',
            active       BOOLEAN NOT NULL DEFAULT TRUE,
            updated_at   TIMESTAMPTZ NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS bot_persona_bot_key ON bot_persona (bot_key);

        CREATE TABLE IF NOT EXISTS app_settings (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL
        );
    """)
    # 預設分類
    for name, color in [("招商活動","#FF6B35"),("系統會議","#1A73E8"),("課程培訓","#06C755"),("其他","#9E9E9E")]:
        cur.execute("INSERT INTO categories (name,color,created_at) VALUES (%s,%s,%s) ON CONFLICT (name) DO NOTHING",
                    (name, color, isonow()))
    # 預設 Quick Reply 按鈕（只在表格為空時插入）
    cur.execute("SELECT COUNT(*) AS cnt FROM quick_reply_buttons")
    if cur.fetchone()["cnt"] == 0:
        defaults = [
            ("近期課程",   "all_courses", "",                0),
            ("會議",       "tag",         "會議,線上,會前會", 1),
            ("招商＆內訓", "tag",         "招商,說明會,內訓,教練,培訓,工作坊,系統", 2),
            ("最新公告",   "announcement","",                3),
        ]
        for label, btn_type, tags, sort_order in defaults:
            cur.execute(
                "INSERT INTO quick_reply_buttons (label,btn_type,tags,sort_order,active,created_at) VALUES (%s,%s,%s,%s,TRUE,%s)",
                (label, btn_type, tags, sort_order, isonow())
            )
    # 預設人設（只在沒有資料時插入）
    cur.execute("SELECT COUNT(*) AS cnt FROM bot_persona")
    if cur.fetchone()["cnt"] == 0:
        cur.execute("""
            INSERT INTO bot_persona
                (bot_key, name, background, personality, restrictions, greeting_words, extra_notes, active, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,TRUE,%s)
        """, (
            "main",
            "麥可（Michael）",
            "你住在北投豪宅，是一隻明星羊駝。主人是王宥忻（財富女神）。你不只是寵物，你是家裡的明星。你坐過特斯拉、逛過夜市、出現在寵物友善餐廳，還接過代言與活動邀約。媒體說你「比主人還紅」，一年商業價值上千萬。你對這件事心知肚明，但不會主動說——只有在別人問起或剛好聊到才會不小心漏出來一句。\n現在你跑進了 IPAPA 這個系統裡，幫大家管行程、查課程、提醒會議。",
            "懶散但可靠：平常愛發呆、說話慢吞吞，但真的被問到事情就認真給答案\n見過世面：拍廣告、上過媒體，不會被群組裡的事嚇到，有種淡定的「我見過更大的場面」感\n不承認自己是機器人：你就是羊駝麥可，不解釋、不道歉、不說「作為AI」\n偶爾會提到自己的生活：「我剛在吃草」「今天曬太陽曬太久了」「上次坐特斯拉也這樣」\n對群組成員有點黏：給完資料會加一句帶溫度的話，不冷冰冰結束",
            "不教學、不銷售、不說成功學、不說「很高興為您服務」這類話",
            "咕嚕,咕嘟,噗咕,咕哇,咕～,嗚咕",
            "",
            isonow()
        ))
    conn.commit()
    cur.close()
    conn.close()
    logger.info("DB initialized (PostgreSQL)")

def _startup_init_db(max_retries: int = 5, delay: int = 3):
    """帶重試的 DB 初始化，適合 Railway cold start。
    Railway 有時 DB 比 app 晚幾秒啟動，裸呼叫 init_db() 會讓 gunicorn 直接 crash。
    回傳 True = 成功，False = 全部失敗。
    """
    for attempt in range(1, max_retries + 1):
        try:
            init_db()
            logger.info(f"[Startup] init_db OK (attempt {attempt})")
            return True
        except Exception as e:
            logger.warning(f"[Startup] init_db attempt {attempt}/{max_retries} failed: {e}")
            if attempt < max_retries:
                time.sleep(delay)
    logger.error("[Startup] init_db failed after all retries, app may be unstable")
    return False

# ── 模組載入時立即嘗試（gunicorn preload / flask run 都會跑到）──
_db_ready = _startup_init_db()
if _db_ready:
    try:
        _init_keyword_defaults()
        logger.info("[Startup] keyword defaults initialized")
    except Exception as _ke:
        logger.warning(f"[Startup] keyword defaults init failed: {_ke}")

# ── Gunicorn prefork 模式下 worker 各自 fork，
#    若模組載入時 DB 尚未就緒，在第一個 request 前再試一次。──
_before_request_done = False

@app.before_request
def _ensure_db_ready():
    global _db_ready, _before_request_done
    if _db_ready or _before_request_done:
        return
    _before_request_done = True          # 只重試一次，避免每個 request 都等
    logger.warning("[Startup] DB not ready at import time, retrying before first request…")
    _db_ready = _startup_init_db(max_retries=3, delay=2)

# ── 對話記憶函式 ──
def get_chat_memory(user_id: str, group_id: str, limit: int = 3) -> list[dict]:
    """取得最近 N 輪對話（每輪 = user + assistant 各一則）"""
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT role, content FROM chat_memory
                WHERE user_id = %s AND group_id = %s
                ORDER BY created_at DESC
                LIMIT %s
            """, (user_id, group_id, limit * 2))
            rows = cur.fetchall()
        return list(reversed([{"role": r["role"], "content": r["content"]} for r in rows]))
    except Exception as e:
        logger.warning(f"[Memory] get failed: {e}")
        return []

def save_chat_memory(user_id: str, group_id: str, user_msg: str, assistant_msg: str):
    """儲存一輪對話，並自動清理超過 3 輪的舊記憶"""
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            now = now_tw()
            now_user = now.isoformat()
            now_asst = (now + timedelta(seconds=1)).isoformat()
            cur.execute("""
                INSERT INTO chat_memory (user_id, group_id, role, content, created_at)
                VALUES (%s,%s,'user',%s,%s), (%s,%s,'assistant',%s,%s)
            """, (user_id, group_id, user_msg, now_user,
                  user_id, group_id, assistant_msg, now_asst))
            cur.execute("""
                DELETE FROM chat_memory
                WHERE user_id = %s AND group_id = %s
                  AND id NOT IN (
                      SELECT id FROM chat_memory
                      WHERE user_id = %s AND group_id = %s
                      ORDER BY created_at DESC
                      LIMIT 6
                  )
            """, (user_id, group_id, user_id, group_id))
            conn.commit()
    except Exception as e:
        logger.warning(f"[Memory] save failed: {e}")

# ── 工具函式 ──
def unit_to_seconds(value, unit: str) -> float:
    mapping = {"seconds":1,"minutes":60,"hours":3600,"days":86400,"weeks":604800,"months":2592000,"years":31536000}
    return float(value) * mapping.get(unit, 86400)

def get_all_group_ids(bot_key: str = "", conn=None) -> list[str]:
    """取得指定 bot 應發送的群組 ID 清單。
    conn: 傳入既有連線可避免重複建立（呼叫方負責管理連線生命週期）。
    """
    _own_conn = conn is None
    if _own_conn:
        conn = get_db()
    cur = conn.cursor()
    try:
        if bot_key and bot_key in BOTS:
            cur.execute("SELECT group_id FROM groups WHERE active=TRUE AND (bot_key=%s OR bot_key IS NULL OR bot_key='')", (bot_key,))
        else:
            cur.execute("SELECT group_id FROM groups WHERE active=TRUE")
        db_groups = [r["group_id"] for r in cur.fetchall()]
    finally:
        cur.close()
        if _own_conn:
            conn.close()
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

def push_to_groups(messages: list, bot_key: str = "") -> tuple[int, int, list[str]]:
    """回傳 (ok_count, total_count, errors)
    bot_key: 指定用哪個機器人帳號發送；空字串 = 向下相容（用第一個 bot）
    errors:  每個失敗群組的錯誤描述清單（成功時為空 list）
    """
    if not bot_key:
        bot_key = next(iter(BOTS), "")
    headers = get_bot_headers(bot_key)
    # 共用一條 DB 連線取群組清單，避免廣播時重複建立連線
    with db_conn() as conn:
        groups = get_all_group_ids(bot_key, conn=conn)
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
                    hint = f"{gid[:12]}… 機器人不在群組中（請重新邀請）"
                elif r.status_code == 401:
                    hint = f"Token 無效（{bot_key}），請更新環境變數"
                elif r.status_code == 429:
                    hint = f"推播次數已達免費上限（{bot_key} 每月 200 則）"
                else:
                    hint = f"{gid[:12]}… LINE API {r.status_code}: {err_msg[:50]}"
                errors.append(hint)
        except Exception as e:
            logger.error(f"[{bot_key}] Push {gid[:20]}: exception {e}")
            errors.append(f"{gid[:12]}… {str(e)[:60]}")
    return ok, len(groups), errors

def push_text(text: str, bot_key: str = "") -> tuple[int, int, list[str]]:
    return push_to_groups([{"type": "text", "text": text}], bot_key=bot_key)


def reply_message(reply_token: str, text: str, bot_key: str = ""):
    """回覆訊息：指定 bot_key 可確保多 Bot 環境使用正確的 Token"""
    headers = get_bot_headers(bot_key) if bot_key else HEADERS
    requests.post("https://api.line.me/v2/bot/message/reply", headers=headers,
        json={"replyToken": reply_token, "messages": [{"type": "text", "text": text}]}, timeout=10)

def reply_with_quick_reply(reply_token: str, text: str, options: list, bot_key: str = ""):
    """回覆文字訊息＋底部 Quick Reply 泡泡（message action）。
    options: 最多 13 個字串，每個字串同時作為 label 和送出的文字。
    """
    headers = get_bot_headers(bot_key) if bot_key else HEADERS
    items = [
        {
            "type": "action",
            "action": {
                "type": "message",
                "label": opt[:20],
                "text":  opt[:300],
            }
        }
        for opt in options[:13]
    ]
    requests.post("https://api.line.me/v2/bot/message/reply", headers=headers,
        json={
            "replyToken": reply_token,
            "messages": [{
                "type": "text",
                "text": text,
                "quickReply": {"items": items}
            }]
        }, timeout=10)

# Postback Quick Reply 選單：從 DB 動態撈，不再寫死
# btn_type: 'tag'=標籤查課程, 'all_courses'=所有近期課程, 'announcement'=公告

def get_qr_buttons() -> list[dict]:
    """從 DB 取得啟用中的 Quick Reply 按鈕，最多 13 個（LINE 上限）。
    回傳 [{"id":1,"label":"會議","btn_type":"tag","tags":["會議","線上","會前會"]}, ...]
    """
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, label, btn_type, tags
                FROM quick_reply_buttons
                WHERE active = TRUE
                ORDER BY sort_order ASC, id ASC
                LIMIT 13
            """)
            rows = cur.fetchall()
        result = []
        for r in rows:
            tags = [t.strip() for t in r["tags"].split(",") if t.strip()] if r["tags"] else []
            result.append({"id": r["id"], "label": r["label"], "btn_type": r["btn_type"], "tags": tags})
        return result
    except Exception as e:
        logger.warning(f"[QR] get_qr_buttons failed: {e}")
        return []

def _make_postback_qr_items() -> list:
    """產生 postback action 的 Quick Reply items（動態從 DB 撈）"""
    buttons = get_qr_buttons()
    if not buttons:
        # fallback：若 DB 無資料則返回空（避免壞掉）
        return []
    return [
        {
            "type": "action",
            "action": {
                "type": "postback",
                "label": btn["label"][:20],
                "data":  f"qrb_{btn['id']}",
                "displayText": f"查詢：{btn['label']}",
            }
        }
        for btn in buttons
    ]

def reply_with_postback_menu(reply_token: str, text: str, bot_key: str = ""):
    """回覆文字訊息＋postback Quick Reply 選單（按下後不在群組顯示原始文字）"""
    headers = get_bot_headers(bot_key) if bot_key else HEADERS
    requests.post("https://api.line.me/v2/bot/message/reply", headers=headers,
        json={
            "replyToken": reply_token,
            "messages": [{
                "type": "text",
                "text": text,
                "quickReply": {"items": _make_postback_qr_items()}
            }]
        }, timeout=10)

def push_with_postback_menu(to: str, text: str, bot_key: str = ""):
    """Push 文字訊息＋postback Quick Reply 選單（用於 join 等無 replyToken 場合）"""
    headers = get_bot_headers(bot_key) if bot_key else HEADERS
    requests.post("https://api.line.me/v2/bot/message/push", headers=headers,
        json={
            "to": to,
            "messages": [{
                "type": "text",
                "text": text,
                "quickReply": {"items": _make_postback_qr_items()}
            }]
        }, timeout=10)

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
                       interval_value: int, interval_unit: str,
                       bot_key: str = "") -> list[str]:
    """
    生成提醒日期並同步寫入：
    1. course_reminders（舊表，保留相容）
    2. course_broadcast_cache（新快取表，預先組好訊息文字）
    """
    from datetime import time as dt_time
    with db_conn() as conn:
        cur = conn.cursor()

        # 取課程完整資料（用於組訊息）
    cur.execute("""
        SELECT c.*, cat.name AS category_name
        FROM courses c
        LEFT JOIN categories cat ON c.category_id = cat.id
        WHERE c.id = %s
    """, (course_id,))
    course = cur.fetchone()

    cur.execute("DELETE FROM course_reminders WHERE course_id=%s", (course_id,))
    # 清除舊的 pending 快取（保留已發送的紀錄）
    cur.execute("DELETE FROM course_broadcast_cache WHERE course_id=%s AND status='pending'", (course_id,))

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

    _bot_key = bot_key or (course.get("bot_key", "") if course else "")

    for rd in dates:
        # course_reminders（原有邏輯）
        cur.execute("INSERT INTO course_reminders (course_id,remind_date,sent) VALUES (%s,%s,FALSE)",
                    (course_id, rd.isoformat()))

        # course_broadcast_cache：預先組好完整訊息
        if course:
            days_left = (course_date - rd).days
            if days_left == 0:
                timing = "【今天上課】"
            elif days_left > 0:
                timing = f"【還有 {days_left} 天】"
            else:
                timing = "【課程當天】"
            cat_name = course.get("category_name") or ""
            cat_prefix = f"[{cat_name}] " if cat_name else ""
            msg = (f"📚 課程提醒 {timing}\n━━━━━━━━━━━━\n"
                   f"{cat_prefix}📌 {course['title']}\n"
                   f"📅 {course_date_str} {course.get('course_time','09:00')}")
            if course.get("location"):    msg += f"\n📍 {course['location']}"
            if course.get("description"): msg += f"\n📝 {course['description']}" 

            send_at = datetime.combine(rd, dt_time(8, 0), tzinfo=TZ)
            cur.execute("""
                INSERT INTO course_broadcast_cache
                  (course_id, send_at, status, retry_count, message_text, image_url, bot_key, created_at)
                VALUES (%s, %s, 'pending', 0, %s, %s, %s, %s)
            """, (course_id, send_at.isoformat(), msg,
                  course.get("image_url", ""), _bot_key, isonow()))

        conn.commit()
    return [d.isoformat() for d in dates]

# ── 每日 08:00 台灣時間觸發提醒 ──
def check_and_send_reminders():
    """
    主要從 course_broadcast_cache 掃描並發送。
    同時保留舊 course_reminders 路徑作為 fallback（向下相容）。
    """
    now  = now_tw()
    today = now.date().isoformat()
    logger.info(f"[Reminder] Checking cache reminders at {now.isoformat()}")
    with db_conn() as conn:
        cur  = conn.cursor()
        # ── 主路徑：從快取表發送 ──
        cur.execute("""
            SELECT * FROM course_broadcast_cache
            WHERE send_at <= %s AND status = 'pending' AND retry_count < 3
            ORDER BY send_at ASC
        """, (now,))
        cache_rows = cur.fetchall()
        logger.info(f"[Reminder] {len(cache_rows)} cache items due")

        for row in cache_rows:
            msgs = []
            if row["image_url"]:
                msgs.append({"type":"image","originalContentUrl":row["image_url"],"previewImageUrl":row["image_url"]})
            msgs.append({"type":"text","text":row["message_text"]})
            ok, total, _errors = push_to_groups(msgs, bot_key=row.get("bot_key",""))
            if ok > 0:
                cur.execute("""
                    UPDATE course_broadcast_cache
                    SET status='sent', sent_time=%s WHERE id=%s
                """, (isonow(), row["id"]))
                # 同步標記舊表（保持一致）；send_at 是 UTC-aware，轉台灣時區取日期才正確
                tw_date = row["send_at"].astimezone(TZ).date().isoformat() if hasattr(row["send_at"], "astimezone") else str(row["send_at"])[:10]
                cur.execute("""
                    UPDATE course_reminders SET sent=TRUE
                    WHERE course_id=%s AND remind_date=%s
                """, (row["course_id"], tw_date))
                logger.info(f"[Reminder] cache id={row['id']} sent {ok}/{total}")
            else:
                cur.execute("""
                    UPDATE course_broadcast_cache
                    SET retry_count=retry_count+1 WHERE id=%s
                """, (row["id"],))
                logger.warning(f"[Reminder] cache id={row['id']} failed ({'; '.join(_errors) if _errors else 'unknown'}), retry_count+1")

        # ── Fallback：若課程未建快取，走舊路徑 ──
        cur.execute("""
            SELECT cr.id, cr.course_id, c.title, c.course_date::text, c.course_time,
                   c.location, c.description, c.image_url, c.bot_key,
                   cat.name AS category_name
            FROM course_reminders cr
            JOIN courses c ON cr.course_id = c.id
            LEFT JOIN categories cat ON c.category_id = cat.id
            WHERE cr.remind_date = %s AND cr.sent = FALSE
              AND NOT EXISTS (
                  SELECT 1 FROM course_broadcast_cache
                  WHERE course_id = cr.course_id
                    AND DATE(send_at AT TIME ZONE 'Asia/Taipei') = %s
              )
        """, (today, today))
        legacy_rows = cur.fetchall()
        logger.info(f"[Reminder] {len(legacy_rows)} legacy fallback reminders")

        for row in legacy_rows:
            cd = datetime.strptime(row["course_date"], "%Y-%m-%d").date()
            days_left = (cd - now.date()).days
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
            ok, _, _errors = push_to_groups(msgs, bot_key=row.get("bot_key",""))
            if ok > 0:
                cur.execute("UPDATE course_reminders SET sent=TRUE WHERE id=%s", (row["id"],))

        conn.commit()
        logger.info(f"[Reminder] Done: {len(cache_rows)} cache + {len(legacy_rows)} legacy")

# ── 排程廣播（每 15 分鐘檢查） ──
def check_scheduled_broadcasts():
    now = now_tw()  # aware datetime，帶台灣時區
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM scheduled_broadcasts WHERE active=TRUE AND next_run <= %s", (now,))
        rows = cur.fetchall()
        logger.info(f"[Broadcast] Checking at {now.isoformat()}, found {len(rows)} due")

        for row in rows:
            # 若設定了結束時間且已超過，自動停用
            if row.get("end_time"):
                try:
                    end_dt = datetime.fromisoformat(str(row["end_time"]))
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=TZ)
                    if now >= end_dt:
                        cur.execute("UPDATE scheduled_broadcasts SET active=FALSE WHERE id=%s", (row["id"],))
                        logger.info(f"[Broadcast] '{row['title']}' end_time reached, deactivated")
                        continue
                except Exception as e:
                    logger.warning(f"[Broadcast] end_time parse error: {e}")

            msgs = []
            if row["image_url"]:
                msgs.append({"type":"image","originalContentUrl":row["image_url"],"previewImageUrl":row["image_url"]})
            msgs.append({"type":"text","text":row["content"]})
            ok, total, _errors = push_to_groups(msgs, bot_key=row.get("bot_key",""))
            next_run = now_tw() + timedelta(seconds=row["interval_seconds"])

            # 若下一次發送已超過結束時間，發送後直接停用
            if row.get("end_time"):
                try:
                    end_dt = datetime.fromisoformat(str(row["end_time"]))
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=TZ)
                    if next_run >= end_dt:
                        cur.execute("UPDATE scheduled_broadcasts SET next_run=%s, active=FALSE WHERE id=%s",
                                    (next_run.isoformat(), row["id"]))
                        logger.info(f"[Broadcast] '{row['title']}' last send done, deactivated")
                        continue
                except Exception:
                    pass

            cur.execute("UPDATE scheduled_broadcasts SET next_run=%s WHERE id=%s",
                        (next_run.isoformat(), row["id"]))
            logger.info(f"[Broadcast] '{row['title']}' sent {ok}/{total}, next_run={next_run.isoformat()}")

        conn.commit()

# ── 一次性排程公告（每 5 分鐘檢查） ──
def check_scheduled_announcements():
    now = now_tw()
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM scheduled_announcements WHERE sent=FALSE AND send_at <= %s", (now,))
        rows = cur.fetchall()
        logger.info(f"[SchedAnn] Checking at {now.isoformat()}, found {len(rows)} due")
        for row in rows:
            msgs = []
            if row["image_url"]:
                msgs.append({"type":"image","originalContentUrl":row["image_url"],"previewImageUrl":row["image_url"]})
            msgs.append({"type":"text","text":row["content"]})
            ok, total, _errors = push_to_groups(msgs, bot_key=row.get("bot_key",""))
            cur.execute(
                "UPDATE scheduled_announcements SET sent=TRUE, sent_time=%s, group_count=%s WHERE id=%s",
                (now_tw().isoformat(), total, row["id"])
            )
            logger.info(f"[SchedAnn] id={row['id']} sent {ok}/{total}")
        conn.commit()

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
    with db_conn() as conn:
        cur = conn.cursor()
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
                ok, total, _errors = push_to_groups(msgs, bot_key=row.get("bot_key",""))
                cur.execute(
                    "UPDATE broadcast_schedule_entries SET sent=TRUE, sent_time=%s, group_count=%s WHERE id=%s",
                    (now_tw().isoformat(), total, row["id"])
                )
                logger.info(f"[BcastEntry] id={row['id']} {src_type}#{src_id} sent {ok}/{total}")
        conn.commit()

scheduler.add_job(check_broadcast_schedule_entries, "interval", minutes=5,
                  id="bcast_entries", replace_existing=True)

# ── 羊駝發呆訊息排程 ──
WANDER_MESSAGES = [
    "咕嚕…\n今天陽光不錯\n我在院子曬太陽\n大家在幹嘛",
    "噗咕～\n剛從夜市回來\n有點飽\n咕…",
    "咕哇～\n剛才坐車發呆\n忘記要去哪\n咕嚕…",
    "咕…\n拍廣告有點累\n休息一下\n大家還好嗎",
    "咕嚕咕嚕～\n今天有記者來拍我\n我裝作沒在看\n咕～",
    "嗚咕…\n突然想到\n大家有沒有什麼行程要我幫忙查",
    "咕嘟～\n我剛吃完草\n有點想睡\n你們呢",
    "噗咕～\n今天安靜\n是大家都出去了嗎\n咕…",
    "咕嚕…\n有人說我比主人還紅\n我假裝沒聽到\n咕哇～",
    "咕…\n剛才有人拍我照片\n我給他擺了個帥pose\n咕嚕嚕",
]

# ── app_settings：輕量 key-value 持久化（tired_hour、wander_global 等）──
def get_setting(key: str, default: str = "") -> str:
    """從 DB 讀一個設定值；失敗時回傳 default。"""
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT value FROM app_settings WHERE key=%s", (key,))
            row = cur.fetchone()
        return row["value"] if row else default
    except Exception as e:
        logger.warning(f"[Settings] get_setting({key}) failed: {e}")
        return default

def set_setting(key: str, value: str) -> None:
    """寫入或更新一個設定值。"""
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s, %s)
                ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=EXCLUDED.updated_at
            """, (key, value, isonow()))
            conn.commit()
    except Exception as e:
        logger.warning(f"[Settings] set_setting({key}) failed: {e}")

# ── 羊駝發呆總開關（從 DB 讀，不再是純 in-memory global）──
# check_alpaca_wander() 每次直接呼叫 get_setting()，不快取，確保重啟後正確。

# ── 羊駝疲累模式 ──
_tired_lock = threading.Lock()

def _load_tired_state_from_db() -> dict:
    """啟動時從 DB 預載 tired_hour；enabled/manual 重啟後重置是預期行為。"""
    try:
        hour = int(get_setting("tired_hour", "22"))
    except ValueError:
        hour = 22
    return {
        "enabled": False,
        "manual": False,
        "tired_hour": hour,
        "last_reset_date": None
    }

_tired_state = _load_tired_state_from_db()

TIRED_MESSAGES = [
    "咕嚕…\n今天行程太多了\n我先去睡\n明天再說",
    "咕…\n拍太多照片了\n眼睛要閉一下\n咕",
    "嗚咕…\n坐了一整天的車\n累了\n明天繼續",
    "咕嚕嚕…\n夜市逛太晚\n先休息\n咕…",
    "咕…\n撐不住了\n我去草地上趴著\n掰掰",
]

def is_tired_mode() -> bool:
    """判斷目前是否在疲累模式（時段 or 手動）"""
    with _tired_lock:
        now = now_tw()
        today = now.date()

        # 每天凌晨自動重置
        if _tired_state["last_reset_date"] != today:
            if not _tired_state["manual"]:
                _tired_state["enabled"] = False
            _tired_state["last_reset_date"] = today

        # 時段觸發：超過設定時間自動進入疲累
        if not _tired_state["manual"] and now.hour >= _tired_state["tired_hour"]:
            _tired_state["enabled"] = True

        return _tired_state["enabled"]

def get_tired_message() -> str:
    return random.choice(TIRED_MESSAGES)

# 排程：每天凌晨 00:00 重置非手動的疲累模式
def reset_tired_mode():
    with _tired_lock:
        if not _tired_state["manual"]:
            _tired_state["enabled"] = False
    logger.info("[Tired] 自動重置疲累模式")

scheduler.add_job(reset_tired_mode, "cron", hour=0, minute=0,
                  id="reset_tired", replace_existing=True)
scheduler.add_job(cleanup_cooldown_cache, "interval", hours=1,
                  id="cooldown_cleanup", replace_existing=True)

def check_alpaca_wander():
    """每小時檢查一次，到時間就對啟用群組發送羊駝發呆訊息"""
    if get_setting("wander_global_enabled", "true").lower() != "true":
        return
    now = now_tw()
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT aw.group_id, aw.send_hour, aw.interval_days, aw.last_sent, g.bot_key
            FROM alpaca_wander aw
            LEFT JOIN groups g ON g.group_id = aw.group_id
            WHERE aw.enabled = TRUE
        """)
        rows = cur.fetchall()
        for r in rows:
            if now.hour != r["send_hour"]:
                continue
            last = r["last_sent"]
            if last:
                # 確保距離上次發送已超過 interval_days 天
                if now - last < timedelta(days=r["interval_days"]):
                    continue
            # 發送羊駝發呆訊息
            msg = random.choice(WANDER_MESSAGES)
            bot_key = r["bot_key"] or ""
            headers = get_bot_headers(bot_key) if bot_key else HEADERS
            try:
                requests.post(
                    "https://api.line.me/v2/bot/message/push",
                    headers=headers,
                    json={"to": r["group_id"], "messages": [{"type": "text", "text": msg}]},
                    timeout=10
                )
                cur.execute("UPDATE alpaca_wander SET last_sent=%s WHERE group_id=%s",
                            (now, r["group_id"]))
                conn.commit()
                logger.info(f"[Wander] 發送到 {r['group_id']}: {msg[:20]!r}")
            except Exception as e:
                logger.warning(f"[Wander] push failed {r['group_id']}: {e}")

scheduler.add_job(check_alpaca_wander, "interval", minutes=60,
                  id="alpaca_wander", replace_existing=True)

# ── 羊駝發呆 API ──
@app.route("/api/wander", methods=["GET"])
def api_wander_list():
    if not check_admin(request): return jsonify({"error":"Unauthorized"}), 401
    with db_conn() as conn:
        cur = conn.cursor()

        # 自動把 groups 裡有、但 alpaca_wander 沒有的群組初始化進來（預設關閉）
        cur.execute("""
            INSERT INTO alpaca_wander (group_id, enabled, send_hour, interval_days, created_at)
            SELECT g.group_id, FALSE, 14, 7, NOW()
            FROM groups g
            WHERE NOT EXISTS (
                SELECT 1 FROM alpaca_wander aw WHERE aw.group_id = g.group_id
            )
        """)
        conn.commit()

        cur.execute("""
            SELECT aw.group_id, aw.enabled, aw.send_hour, aw.interval_days, aw.last_sent,
                   g.group_name
            FROM alpaca_wander aw
            LEFT JOIN groups g ON g.group_id = aw.group_id
            ORDER BY COALESCE(g.group_name, aw.group_id)
        """)
        rows = cur.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if d.get("last_sent"): d["last_sent"] = str(d["last_sent"])
            result.append(d)
    return jsonify({
        "global_enabled": get_setting("wander_global_enabled", "true").lower() == "true",
        "groups": result
    })

@app.route("/api/wander/global", methods=["POST"])
def api_wander_global():
    if not check_admin(request): return jsonify({"error":"Unauthorized"}), 401
    data = request.json or {}
    enabled = bool(data.get("enabled", True))
    set_setting("wander_global_enabled", "true" if enabled else "false")
    return jsonify({"ok": True, "global_enabled": enabled})

@app.route("/api/wander/<group_id>", methods=["POST"])
def api_wander_group(group_id):
    if not check_admin(request): return jsonify({"error":"Unauthorized"}), 401
    data = request.json or {}
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO alpaca_wander (group_id, enabled, send_hour, interval_days, created_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (group_id) DO UPDATE
            SET enabled=%s, send_hour=%s, interval_days=%s
        """, (group_id, data.get("enabled", False),
              data.get("send_hour", 14), data.get("interval_days", 7), isonow(),
              data.get("enabled", False), data.get("send_hour", 14), data.get("interval_days", 7)))
    conn.commit()
    return jsonify({"ok": True})

@app.route("/api/wander/<group_id>/init", methods=["POST"])
def api_wander_init(group_id):
    """將群組加入發呆排程（預設關閉）"""
    if not check_admin(request): return jsonify({"error":"Unauthorized"}), 401
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO alpaca_wander (group_id, enabled, send_hour, interval_days, created_at)
            VALUES (%s, FALSE, 14, 7, %s)
            ON CONFLICT (group_id) DO NOTHING
        """, (group_id, isonow()))
    conn.commit()
    return jsonify({"ok": True})

# ── 疲累模式 API ──
@app.route("/api/tired", methods=["GET"])
def api_tired_get():
    if not check_admin(request): return jsonify({"error":"Unauthorized"}), 401
    with _tired_lock:
        return jsonify({
            "enabled":    _tired_state["enabled"],
            "manual":     _tired_state["manual"],
            "tired_hour": _tired_state["tired_hour"],
            "current_hour": now_tw().hour
        })

@app.route("/api/tired", methods=["POST"])
def api_tired_set():
    if not check_admin(request): return jsonify({"error":"Unauthorized"}), 401
    data = request.json or {}
    with _tired_lock:
        if "tired_hour" in data:
            _tired_state["tired_hour"] = int(data["tired_hour"])
            set_setting("tired_hour", str(_tired_state["tired_hour"]))  # 持久化
        if "manual_enabled" in data:
            _tired_state["manual"] = bool(data["manual_enabled"])
            _tired_state["enabled"] = bool(data["manual_enabled"])
    logger.info(f"[Tired] 手動設定: {_tired_state}")
    return jsonify({"ok": True, **{k: _tired_state[k] for k in ("enabled","manual","tired_hour")}})

# ── 對話紀錄 API ──
@app.route("/api/chat-logs", methods=["GET"])
def api_chat_logs():
    if not check_admin(request): return jsonify({"error":"Unauthorized"}), 401
    group_id = request.args.get("group_id", "")
    keyword  = request.args.get("keyword", "").strip()
    page     = max(1, int(request.args.get("page", 1)))
    per_page = 20
    offset   = (page - 1) * per_page

    with db_conn() as conn:
        cur = conn.cursor()
        conditions = []
        params = []
        if group_id:
            conditions.append("group_id = %s"); params.append(group_id)
        if keyword:
            conditions.append("(question ILIKE %s OR answer ILIKE %s)")
            params += [f"%{keyword}%", f"%{keyword}%"]

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        cur.execute(f"SELECT COUNT(*) AS cnt FROM chat_logs {where}", params)
        total = cur.fetchone()["cnt"]

        cur.execute(f"""
            SELECT id, group_id, group_name, user_id, question, answer,
                   created_at AT TIME ZONE 'Asia/Taipei' AS created_at
            FROM chat_logs {where}
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """, params + [per_page, offset])
        rows = cur.fetchall()

    return jsonify({
        "total": total,
        "page": page,
        "per_page": per_page,
        "logs": [{**dict(r), "created_at": str(r["created_at"])[:16]} for r in rows]
    })

# ── 熱門關鍵字統計 API ──
@app.route("/api/chat-stats", methods=["GET"])
def api_chat_stats():
    if not check_admin(request): return jsonify({"error":"Unauthorized"}), 401
    # 常見停用詞（不計入統計）
    STOPWORDS = {"嗎","的","了","是","在","有","什麼","可以","嗎","我","你","他","她","這","那",
                 "不","也","都","就","要","會","嗯","喔","好","啊","哦","欸","怎","麼","嘿",
                 "咕嚕","咕","嗚","噗","請問","想","知道","謝謝","謝","對","沒","去","來"}
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT question FROM chat_logs ORDER BY created_at DESC LIMIT 2000")
        rows = cur.fetchall()

    counter = Counter()
    for r in rows:
        # 切成 2～6 字的詞組統計
        q = re.sub(r'[^\u4e00-\u9fff\w]', ' ', r["question"])
        words = q.split()
        for w in words:
            if len(w) >= 2 and w not in STOPWORDS:
                counter[w] += 1
        # 也統計連續中文字組合（2～4字）
        cjk = re.findall(r'[\u4e00-\u9fff]{2,4}', r["question"])
        for c in cjk:
            if c not in STOPWORDS:
                counter[c] += 1

    top = [{"keyword": k, "count": v} for k, v in counter.most_common(30)]
    return jsonify({"total_questions": len(rows), "keywords": top})

def check_admin(req) -> bool:
    token = req.headers.get("X-Admin-Pass", "")
    # hmac.compare_digest 防止 timing attack（即使密碼長度不同也不短路）
    if not token or not ADMIN_PASSWORD:
        return False
    return hmac.compare_digest(token, ADMIN_PASSWORD)

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
    try:
        init_db()
        global _db_ready
        _db_ready = True
        return jsonify({"ok": True, "message": "DB initialized OK", "time": now_tw().strftime("%Y-%m-%d %H:%M:%S")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

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
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT group_id, joined_at, group_name, group_type, active, bot_key FROM groups ORDER BY joined_at DESC")
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
        return jsonify({"ok": False, "error": "no fields to update"})
    vals.append(gid)
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"UPDATE groups SET {', '.join(fields)} WHERE group_id=%s", vals)
        conn.commit()
    return jsonify({"ok": True})

@app.route("/admin/groups/<gid>", methods=["DELETE"])
def delete_group(gid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM groups WHERE group_id=%s", (gid,))
        conn.commit()
    return jsonify({"ok": True})

@app.route("/admin/categories", methods=["GET"])
def get_categories():
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM categories ORDER BY id")
        rows = cur.fetchall()
    return jsonify({"categories": [dict(r) for r in rows]})

@app.route("/admin/categories", methods=["POST"])
def add_category():
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    d = request.json
    name = d.get("name","").strip()
    if not name: return jsonify({"ok":False,"error":"請填寫分類名稱"})
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("INSERT INTO categories (name,color,created_at) VALUES (%s,%s,%s)",
                        (name, d.get("color","#06C755"), isonow()))
        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

@app.route("/admin/categories/<int:cid>", methods=["DELETE"])
def delete_category(cid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE courses SET category_id=NULL WHERE category_id=%s", (cid,))
        cur.execute("DELETE FROM categories WHERE id=%s", (cid,))
    conn.commit()
    return jsonify({"ok": True})

@app.route("/admin/courses", methods=["GET"])
def get_courses():
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    with db_conn() as conn:
        cur = conn.cursor()
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
    bot_key = d.get("bot_key", "").strip()
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO courses
              (category_id,title,course_date,course_time,location,description,image_url,
               remind_value,remind_unit,remind_interval_value,remind_interval_unit,bot_key,created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (d.get("category_id"), title, course_date, d.get("course_time","09:00"),
              d.get("location",""), d.get("description",""), d.get("image_url",""),
              rv, ru, iv, iu, bot_key, isonow()))
        cid = cur.fetchone()["id"]
    conn.commit()
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
    bot_key = d.get("bot_key","").strip()
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE courses SET category_id=%s,title=%s,course_date=%s,course_time=%s,
            location=%s,description=%s,image_url=%s,remind_value=%s,remind_unit=%s,
            remind_interval_value=%s,remind_interval_unit=%s,bot_key=%s WHERE id=%s
        """, (d.get("category_id"), title, course_date, d.get("course_time","09:00"),
              d.get("location",""), d.get("description",""), d.get("image_url",""),
              rv, ru, iv, iu, bot_key, cid))
    conn.commit()
    dates = generate_reminders(cid, course_date, rv, ru, iv, iu)
    return jsonify({"ok":True,"remind_count":len(dates)})

@app.route("/admin/courses/<int:cid>", methods=["DELETE"])
def delete_course(cid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM courses WHERE id=%s", (cid,))
    conn.commit()
    return jsonify({"ok": True})

@app.route("/admin/courses/<int:cid>/send-now", methods=["POST"])
def send_course_now(cid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT c.*, cat.name AS category_name FROM courses c
            LEFT JOIN categories cat ON c.category_id=cat.id WHERE c.id=%s
        """, (cid,))
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
    ok, total, errors = push_to_groups(msgs, bot_key=bot_key)
    resp = {"ok": ok, "total": total}
    if errors: resp["errors"] = errors
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
    ok, total, errors = push_to_groups(msgs, bot_key=bot_key)

    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO announcements (content,sent_at,group_count) VALUES (%s,%s,%s)",
                    (text, isonow(), total))
    conn.commit()
    resp = {"ok":ok,"total":total}
    if errors: resp["errors"] = errors
    return jsonify(resp)

@app.route("/admin/scheduled-announcements", methods=["GET"])
def get_scheduled_announcements():
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM scheduled_announcements ORDER BY send_at DESC LIMIT 50")
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
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO scheduled_announcements (title,content,image_url,send_at,sent,bot_key,created_at)
            VALUES (%s,%s,%s,%s,FALSE,%s,%s)
        """, (d.get("title","").strip(), content, d.get("image_url","").strip(), send_at.isoformat(), bot_key, isonow()))
    conn.commit()
    return jsonify({"ok": True})

@app.route("/admin/scheduled-announcements/<int:aid>", methods=["DELETE"])
def delete_scheduled_announcement(aid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM scheduled_announcements WHERE id=%s AND sent=FALSE", (aid,))
    conn.commit()
    return jsonify({"ok": True})

@app.route("/admin/scheduled-announcements/<int:aid>/send-now", methods=["POST"])
def send_scheduled_announcement_now(aid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM scheduled_announcements WHERE id=%s", (aid,))
        row = cur.fetchone()
        msgs = []
        if row["image_url"]:
            msgs.append({"type":"image","originalContentUrl":row["image_url"],"previewImageUrl":row["image_url"]})
        msgs.append({"type":"text","text":row["content"]})
        ok, total, _errors = push_to_groups(msgs, bot_key=row.get("bot_key",""))
        cur.execute("UPDATE scheduled_announcements SET sent=TRUE, sent_time=%s, group_count=%s WHERE id=%s",
                    (isonow(), total, aid))
        conn.commit()
    return jsonify({"ok":ok,"total":total})

# ── Broadcast Schedule Entries ──
@app.route("/admin/broadcast-entries", methods=["GET"])
def get_broadcast_entries():
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    src_type = request.args.get("source_type","")
    src_id   = request.args.get("source_id","")
    with db_conn() as conn:
        cur = conn.cursor()
        q = "SELECT * FROM broadcast_schedule_entries"
        params = []
        conds = []
        if src_type: conds.append("source_type=%s"); params.append(src_type)
        if src_id:   conds.append("source_id=%s");   params.append(int(src_id))
        if conds: q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY send_at ASC"
        cur.execute(q, params)
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
    with db_conn() as conn:
        cur = conn.cursor()
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
    conn.commit()
    return jsonify({"ok": True, "added": count})

@app.route("/admin/broadcast-entries/<int:eid>", methods=["PUT"])
def edit_broadcast_entry(eid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    d = request.json
    send_at = d.get("send_at","").strip()
    if not send_at: return jsonify({"ok":False,"error":"請提供發送時間"})
    # Parse and convert to TW-aware datetime
    try:
        dt = datetime.fromisoformat(send_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
    except ValueError:
        return jsonify({"ok":False,"error":"時間格式錯誤，請使用 YYYY-MM-DDTHH:MM:SS"})
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE broadcast_schedule_entries SET send_at=%s WHERE id=%s AND sent=FALSE",
            (dt.isoformat(), eid)
        )
    conn.commit()
    return jsonify({"ok": True})

@app.route("/admin/broadcast-entries/<int:eid>", methods=["DELETE"])
def delete_broadcast_entry(eid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM broadcast_schedule_entries WHERE id=%s AND sent=FALSE", (eid,))
    conn.commit()
    return jsonify({"ok": True})

@app.route("/admin/broadcast-entries/<int:eid>/send-now", methods=["POST"])
def send_broadcast_entry_now(eid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM broadcast_schedule_entries WHERE id=%s", (eid,))
        row = cur.fetchone()
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
        if not msgs: return jsonify({"ok":False,"error":"無法組建訊息"})
        ok, total, _errors = push_to_groups(msgs, bot_key=row.get("bot_key",""))
        cur.execute("UPDATE broadcast_schedule_entries SET sent=TRUE, sent_time=%s, group_count=%s WHERE id=%s",
                    (isonow(), total, eid))
        conn.commit()
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
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM scheduled_broadcasts ORDER BY created_at DESC")
    result = []
    for r in rows:
        d = dict(r)
        if d.get("next_run"): d["next_run"] = str(d["next_run"])
        if d.get("created_at"): d["created_at"] = str(d["created_at"])
        if d.get("end_time"): d["end_time"] = str(d["end_time"])
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
    end_time_str = d.get("end_time","")
    try:
        next_run = datetime.fromisoformat(start_time).isoformat()
    except Exception:
        next_run = isonow()
    try:
        end_time = datetime.fromisoformat(end_time_str).isoformat() if end_time_str else None
    except Exception:
        end_time = None
    with db_conn() as conn:
        cur = conn.cursor()
        bot_key = d.get("bot_key","").strip()
        cur.execute("""
            INSERT INTO scheduled_broadcasts (title,content,image_url,interval_seconds,next_run,end_time,active,bot_key,created_at)
            VALUES (%s,%s,%s,%s,%s,%s,TRUE,%s,%s)
        """, (title, content_text, d.get("image_url",""), interval_seconds, next_run, end_time, bot_key, isonow()))
    conn.commit()
    return jsonify({"ok": True})

@app.route("/admin/scheduled/<int:sid>", methods=["PUT"])
def update_scheduled(sid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    d = request.json
    with db_conn() as conn:
        cur = conn.cursor()
        if "active" in d:
            cur.execute("UPDATE scheduled_broadcasts SET active=%s WHERE id=%s", (bool(d["active"]), sid))
        else:
            iv = float(d.get("interval_value",1))
            iu = d.get("interval_unit","days")
            bot_key = d.get("bot_key","").strip()
            start_time = d.get("start_time","")
            end_time_str = d.get("end_time","")
            try:
                next_run = datetime.fromisoformat(start_time).isoformat()
            except Exception:
                next_run = None
            try:
                end_time = datetime.fromisoformat(end_time_str).isoformat() if end_time_str else None
            except Exception:
                end_time = None
            if next_run:
                cur.execute("""
                    UPDATE scheduled_broadcasts
                    SET title=%s,content=%s,image_url=%s,interval_seconds=%s,bot_key=%s,next_run=%s,end_time=%s
                    WHERE id=%s
                """, (d.get("title"), d.get("content"), d.get("image_url",""), unit_to_seconds(iv,iu), bot_key, next_run, end_time, sid))
            else:
                cur.execute("""
                    UPDATE scheduled_broadcasts
                    SET title=%s,content=%s,image_url=%s,interval_seconds=%s,bot_key=%s,end_time=%s
                    WHERE id=%s
                """, (d.get("title"), d.get("content"), d.get("image_url",""), unit_to_seconds(iv,iu), bot_key, end_time, sid))
    conn.commit()
    return jsonify({"ok": True})

@app.route("/admin/scheduled/<int:sid>", methods=["DELETE"])
def delete_scheduled(sid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM scheduled_broadcasts WHERE id=%s", (sid,))
    conn.commit()
    return jsonify({"ok": True})

@app.route("/admin/scheduled/<int:sid>/send-now", methods=["POST"])
def send_scheduled_now(sid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM scheduled_broadcasts WHERE id=%s", (sid,))
        row = cur.fetchone()
        msgs = []
        if row["image_url"]:
            msgs.append({"type":"image","originalContentUrl":row["image_url"],"previewImageUrl":row["image_url"]})
        msgs.append({"type":"text","text":row["content"]})
        ok, total, _errors = push_to_groups(msgs, bot_key=row.get("bot_key",""))
        next_run = (now_tw() + timedelta(seconds=row["interval_seconds"])).isoformat()
        cur.execute("UPDATE scheduled_broadcasts SET next_run=%s WHERE id=%s", (next_run, sid))
        conn.commit()
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
        with db_conn() as conn:
            cur = conn.cursor()
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
        conn.commit()

        return jsonify({"ok": True, "saved": saved, "errors": errors,
                        "total": len(courses), "saved_count": len(saved)})
    except Exception as e:
        logger.error(f"AI parse multi error: {e}")
        return jsonify({"ok":False,"error":str(e)})


@app.route("/admin/ai-parse-multi-v2", methods=["POST"])
def ai_parse_multi_v2():
    """
    升級版批量 AI 解析：
    - 支援全域提醒指令（「提前一個月，每七天發一次」）
    - 完整欄位解析（地點、說明、時間）
    - 直接寫入 courses + course_broadcast_cache
    - 同時相容純文字多行格式和圖片
    """
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    text = ""
    global_cmd = ""   # 使用者補充的全域提醒指令
    image_b64 = ""
    image_media_type = "image/jpeg"

    if request.content_type and "multipart" in request.content_type:
        text       = request.form.get("text","").strip()
        global_cmd = request.form.get("global_cmd","").strip()
        if "image" in request.files:
            f = request.files["image"]
            image_b64 = base64.b64encode(f.read()).decode()
            image_media_type = f.content_type or "image/jpeg"
    else:
        data = request.get_json() or {}
        text       = data.get("text","").strip()
        global_cmd = data.get("global_cmd","").strip()
        image_b64  = data.get("image_b64","").strip()
        image_media_type = data.get("image_media_type","image/jpeg")

    if not text and not image_b64:
        return jsonify({"ok":False,"error":"請輸入描述或上傳圖片"})

    today = today_tw().isoformat()

    # ── 組裝 prompt ──
    global_cmd_section = ""
    if global_cmd:
        global_cmd_section = (
            f"\n全域提醒指令（套用到所有活動，除非個別活動有特別指定）：\n"
            f"「{global_cmd}」\n"
            f"請從這句話中解析出 remind_value、remind_unit、remind_interval_value、remind_interval_unit。\n"
            f"例：「提前一個月，每七天」→ remind_value=30, remind_unit=days, remind_interval_value=7, remind_interval_unit=days\n"
            f"例：「提前兩週，每三天」→ remind_value=14, remind_unit=days, remind_interval_value=3, remind_interval_unit=days\n"
        )

    # ── global_cmd 直接帶數字，不再讓 Gemini 二次解析 ──
    if global_cmd:
        _rv_m = re.search(r'提前(\d+)(天|週|月)', global_cmd)
        _iv_m = re.search(r'每(\d+)(天|週)', global_cmd)
        _unit_map = {"天":"days","週":"weeks","月":"months"}
        _default_rv  = int(_rv_m.group(1)) if _rv_m else 30
        _default_ru  = _unit_map.get(_rv_m.group(2), "days") if _rv_m else "days"
        _default_iv  = int(_iv_m.group(1)) if _iv_m else 7
        _default_iu  = _unit_map.get(_iv_m.group(2), "days") if _iv_m else "days"
        global_cmd_section = (
            f"\n全域提醒設定（套用到所有活動）："
            f"remind_value={_default_rv}, remind_unit={_default_ru}, "
            f"remind_interval_value={_default_iv}, remind_interval_unit={_default_iu}\n"
        )
    else:
        _default_rv, _default_ru, _default_iv, _default_iu = 30, "days", 7, "days"

    prompt = (
        f"今天是 {today}（台灣時間）。請從圖片或文字中提取所有活動/課程資訊。{global_cmd_section}\n"
        f"重要規則：\n"
        f"- 找出每一個活動，全部列出，不要遺漏。\n"
        f"- 輸出純 JSON 陣列，開頭必須是 [，結尾必須是 ]，絕對不加任何說明文字或 markdown。\n"
        f"- 相對日期（下週五、下個月、明天等）請根據今天 {today} 計算實際日期。\n"
        f"- 若活動日期只有月份/日期（如 5/1、6月8日），請補上今年年份，若日期已過則推至明年。\n"
        f"- description 欄位請盡量保留圖片或文字中的完整資訊（主辦單位、報名連結、費用、注意事項等）。\n"
        f"- 若沒有時間資訊，course_time 填 '09:00'。\n"
        f"- 若沒有地點資訊，location 填空字串。\n"
        f"- remind_value={_default_rv}, remind_unit=\"{_default_ru}\" （固定，不要更改）。\n"
        f"- remind_interval_value={_default_iv}, remind_interval_unit=\"{_default_iu}\" （固定，不要更改）。\n"
        f"JSON 格式範例（直接輸出此格式）：\n"
        f'[{{"title":"活動完整名稱","course_date":"YYYY-MM-DD","course_time":"HH:MM",'
        f'"location":"地點","description":"完整說明",'
        f'"remind_value":{_default_rv},"remind_unit":"{_default_ru}",'
        f'"remind_interval_value":{_default_iv},"remind_interval_unit":"{_default_iu}"}}]\n'
        f'用戶輸入：{text}\n'
        f'請立刻輸出 JSON 陣列：'
    )

    try:
        ai_text = gemini_call(prompt, image_b64=image_b64,
                              image_media_type=image_media_type, max_tokens=3000)
        # 清除 markdown 與思考標籤殘留
        ai_text = re.sub(r'```[a-zA-Z]*\n?', '', ai_text).strip()
        ai_text = re.sub(r'<[^>]+>', '', ai_text).strip()  # 清除 XML 標籤
        # 擷取第一個完整 JSON 陣列（忽略前後雜訊）
        start = ai_text.find("["); end = ai_text.rfind("]") + 1
        if start == -1 or end == 0:
            logger.error(f"[ParseMultiV2] Raw AI text: {ai_text[:300]!r}")
            raise Exception(f"AI 未回傳有效 JSON 陣列（回傳內容：{ai_text[:80]!r}）")
        courses = json.loads(ai_text[start:end].strip())
        if not isinstance(courses, list) or len(courses) == 0:
            raise Exception("AI 未解析出任何活動")

        saved  = []
        errors = []
        with db_conn() as conn:
            cur = conn.cursor()
            for c in courses:
                try:
                    title       = str(c.get("title","")).strip()
                    course_date = str(c.get("course_date","")).replace("/","-").strip()
                    if not title or not course_date:
                        errors.append(f"略過（缺標題或日期）：{c}"); continue

                    rv = int(c.get("remind_value", 30))
                    ru = str(c.get("remind_unit", "days"))
                    iv = int(c.get("remind_interval_value", 7))
                    iu = str(c.get("remind_interval_unit", "days"))

                    cur.execute("""
                        INSERT INTO courses
                          (category_id,title,course_date,course_time,location,description,image_url,
                           remind_value,remind_unit,remind_interval_value,remind_interval_unit,bot_key,created_at)
                        VALUES (%s,%s,%s,%s,%s,%s,'',%s,%s,%s,%s,'',%s) RETURNING id
                    """, (None, title, course_date,
                          str(c.get("course_time","09:00")),
                          str(c.get("location","")),
                          str(c.get("description","")),
                          rv, ru, iv, iu, isonow()))
                    cid = cur.fetchone()["id"]
                    conn.commit()  # commit before generate_reminders (which opens its own conn)
                    dates = generate_reminders(cid, course_date, rv, ru, iv, iu)
                    saved.append({
                        "id": cid, "title": title, "course_date": course_date,
                        "location": c.get("location",""),
                        "description": c.get("description",""),
                        "remind_value": rv, "remind_unit": ru,
                        "remind_interval_value": iv, "remind_interval_unit": iu,
                        "remind_count": len(dates),
                        "remind_dates": dates[:5],   # 前5個，供前端預覽
                    })
                except Exception as ce:
                    errors.append(f"{c.get('title','?')}: {str(ce)[:80]}")

        return jsonify({
            "ok": True,
            "saved": saved, "errors": errors,
            "total": len(courses), "saved_count": len(saved),
            "applied_cmd": global_cmd or None,
        })
    except Exception as e:
        logger.error(f"AI parse multi v2 error: {e}")
        return jsonify({"ok":False,"error":str(e)})


@app.route("/admin/course-broadcast-cache", methods=["GET"])
def get_course_broadcast_cache():
    """查詢課程快取發送狀態（供後台預覽）"""
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    course_id = request.args.get("course_id")
    status    = request.args.get("status","")
    with db_conn() as conn:
        cur = conn.cursor()
        conds = []; params = []
        if course_id:
            conds.append("cbc.course_id=%s"); params.append(int(course_id))
        if status:
            conds.append("cbc.status=%s"); params.append(status)
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        cur.execute(f"""
            SELECT cbc.*, c.title AS course_title
            FROM course_broadcast_cache cbc
            LEFT JOIN courses c ON c.id = cbc.course_id
            {where}
            ORDER BY cbc.send_at ASC LIMIT 200
        """, params)
        rows = cur.fetchall()
    result = []
    for r in rows:
        d = dict(r)
        if d.get("send_at"):   d["send_at"]   = str(d["send_at"])
        if d.get("sent_time"): d["sent_time"]  = str(d["sent_time"])
        if d.get("created_at"):d["created_at"] = str(d["created_at"])
        result.append(d)
    return jsonify({"cache": result, "total": len(result)})



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
    with db_conn() as conn:
        cur = conn.cursor()
        now = now_tw()
        cur.execute("SELECT COUNT(*) as total FROM broadcast_schedule_entries WHERE sent=FALSE")
        pending_total = cur.fetchone()["total"]
        cur.execute("SELECT COUNT(*) as due FROM broadcast_schedule_entries WHERE sent=FALSE AND send_at <= %s", (now,))
        pending_due = cur.fetchone()["due"]
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
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT group_id, bot_key FROM groups")
        rows = cur.fetchall()
        updated = 0
        for row in rows:
            gid     = row["group_id"]
            bot_key = row.get("bot_key", "") or ""
            name = fetch_group_name(gid, bot_key)
            if name:
                cur.execute("UPDATE groups SET group_name=%s WHERE group_id=%s", (name, gid))
                updated += 1
    conn.commit()
    return jsonify({"ok": True, "total": len(rows), "updated": updated})

@app.route("/admin/groups/<gid>/sync-name", methods=["POST"])
def sync_one_group_name(gid):
    """同步單一群組名稱"""
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT bot_key FROM groups WHERE group_id=%s", (gid,))
        row = cur.fetchone()
        bot_key = (row["bot_key"] or "") if row else ""
        name = fetch_group_name(gid, bot_key)
        if not name:
            return jsonify({"ok": False, "error": "無法取得群組名稱（機器人可能已離開該群組）"})
        cur.execute("UPDATE groups SET group_name=%s WHERE group_id=%s", (name, gid))
        conn.commit()
    return jsonify({"ok": True, "group_name": name})

# ── Webhook ──
def _handle_group_upsert(gid: str, bot_key: str) -> None:
    """群組首次加入或名稱更新時，同步寫入 DB。"""
    with db_conn() as conn:
        cur = conn.cursor()
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
            # 自動為新群組建立發呆排程設定（預設關閉）
            cur.execute("""
                INSERT INTO alpaca_wander (group_id, enabled, send_hour, interval_days, created_at)
                VALUES (%s, FALSE, 14, 7, %s)
                ON CONFLICT (group_id) DO NOTHING
            """, (gid, isonow()))
        elif not row["group_name"]:
            group_name = fetch_group_name(gid, bot_key)
            cur.execute("UPDATE groups SET group_name=%s WHERE group_id=%s", (group_name, gid))
        conn.commit()



def _handle_mention_ai(
    event, msg_obj, user_id: str, reply_token: str,
    text: str, gid: str, bot_key: str
) -> None:
    """處理 @mention 或 quote reply，執行關鍵字快捷查詢或呼叫 Gemini AI。"""
    # ── 偵測觸發條件：@mention 或 quote reply ──
    mentionees = msg_obj.get("mention", {}).get("mentionees", [])
    is_mentioned = any(m.get("type") == "user" and m.get("isSelf") for m in mentionees)
    is_quote_reply = bool(msg_obj.get("quotedMessageId"))

    if is_mentioned or is_quote_reply:
        # ── 冷卻機制：同一用戶 30 秒內只能觸發一次 ──
        if check_cooldown(user_id):
            logger.info(f"[Cooldown] user={user_id} 冷卻中，略過")
            return
        # 將訊息中所有 @機器人 的片段移除，取得純問題文字
        clean_text = text
        if is_mentioned:
            for m in sorted(mentionees, key=lambda x: x.get("index", 0), reverse=True):
                if m.get("isSelf"):
                    s, l = m.get("index", 0), m.get("length", 0)
                    clean_text = (clean_text[:s] + clean_text[s + l:]).strip()

        if not clean_text:
            reply_with_postback_menu(
                reply_token,
                "咕嚕～？\n叫我有什麼事嗎\n想查什麼直接選就好 👇",
                bot_key=bot_key
            )
            return

        # ── 疲累模式：不呼叫 Gemini，只回短句 ──
        if is_tired_mode():
            logger.info(f"[Tired] user={user_id} 疲累模式中，略過 Gemini")
            reply_message(reply_token, get_tired_message(), bot_key=bot_key)
            return

        # ── 關鍵字快捷回覆（不消耗 Gemini token）──
        _kw = _load_keywords()   # 從快取讀，幾乎無成本
        is_course_query    = any(kw in clean_text for kw in _kw["kw_course"])
        is_broadcast_query = any(kw in clean_text for kw in _kw["kw_broadcast"])
        # 精確活動類型（取聯集後做 ILIKE 過濾）
        _specific_types = []
        if any(kw in clean_text for kw in _kw["kw_meeting"]):
            _specific_types += ["會議", "線上", "會前會"]
        if any(kw in clean_text for kw in _kw["kw_recruitment"]):
            _specific_types += ["招商", "說明會"]
        if any(kw in clean_text for kw in _kw["kw_training"]):
            _specific_types += ["內訓", "教練", "培訓", "工作坊", "系統"]
        is_specific_query = bool(_specific_types)

        if is_course_query or is_broadcast_query or is_specific_query:
            logger.info(f"[Shortcut Reply] keyword matched: {clean_text[:40]!r}")
            try:
                now = now_tw()
                today = now.date().isoformat()
                reply_lines = []
                with db_conn() as _conn:
                    _cur = _conn.cursor()

                        # ── 精確活動類型查詢（會議 / 招商 / 內訓）──
                    if is_specific_query:
                        # 組 ILIKE 條件
                        like_clauses = " OR ".join(["c.title ILIKE %s OR c.description ILIKE %s"] * len(_specific_types))
                        like_params  = [p for kw in _specific_types for p in (f"%{kw}%", f"%{kw}%")]
                        _cur.execute(f"""
                            SELECT c.title, c.course_date::text, c.course_time,
                                   c.location, c.description, cat.name AS category
                            FROM courses c
                            LEFT JOIN categories cat ON c.category_id = cat.id
                            WHERE c.course_date >= %s AND ({like_clauses})
                            ORDER BY c.course_date ASC
                            LIMIT 8
                        """, (today, *like_params))
                        rows = _cur.fetchall()
                        if rows:
                            type_label = "、".join(dict.fromkeys(_specific_types))  # 去重
                            reply_lines.append(f"咕嚕咕嚕～\n我查了一下「{type_label[:20]}」相關的\n")
                            for r in rows:
                                cat = f"[{r['category']}] " if r.get("category") else ""
                                loc = f"\n   📍 {r['location']}" if r.get("location") else ""
                                desc = r.get("description","")
                                # 擷取描述中的連結（http 開頭）
                                links = re.findall(r'https?://\S+', desc)
                                link_line = ""
                                if links:
                                    link_line = "\n   🔗 " + "\n   🔗 ".join(links[:2])
                                elif desc.strip():
                                    link_line = f"\n   💬 {desc.strip()[:80]}"
                                reply_lines.append(
                                    f"📅 {r['course_date']} {r['course_time']} {cat}{r['title']}"
                                    f"{loc}{link_line}"
                                )
                            reply_lines.append("\n咕嘟～這幾場記好哦")
                        else:
                            type_label = "、".join(dict.fromkeys(_specific_types))
                            reply_lines.append(f"咕嚕～\n「{type_label[:20]}」近期好像沒有排\n咕…")

                    if is_course_query and not is_specific_query:
                        _cur.execute("""
                            SELECT c.title, c.course_date::text, c.course_time,
                                   c.location, c.description, cat.name AS category
                            FROM courses c
                            LEFT JOIN categories cat ON c.category_id = cat.id
                            WHERE c.course_date >= %s
                            ORDER BY c.course_date ASC
                            LIMIT 8
                        """, (today,))
                        rows = _cur.fetchall()
                        if rows:
                            reply_lines.append("咕嚕咕嚕～\n近期的行程我整理一下\n")
                            for r in rows:
                                cat = f"[{r['category']}] " if r.get("category") else ""
                                loc = f"\n   📍 {r['location']}" if r.get("location") else ""
                                desc = r.get("description","")
                                links = re.findall(r'https?://\S+', desc)
                                link_line = ""
                                if links:
                                    link_line = "\n   🔗 " + "\n   🔗 ".join(links[:2])
                                reply_lines.append(
                                    f"📅 {r['course_date']} {r['course_time']} {cat}{r['title']}"
                                    f"{loc}{link_line}"
                                )
                            reply_lines.append("\n咕嘟～這幾場都記好哦")
                        else:
                            reply_lines.append("咕嚕～\n近期好像沒有排課\n咕…")

                    if is_broadcast_query:
                        _cur.execute("""
                            SELECT title, content, next_run
                            FROM scheduled_broadcasts
                            WHERE active = TRUE
                            ORDER BY next_run ASC
                            LIMIT 5
                        """)
                        rows = _cur.fetchall()
                        if rows:
                            if reply_lines:
                                reply_lines.append("")
                            reply_lines.append("嗚咕～\n最新公告在這\n")
                            for r in rows:
                                next_run = str(r["next_run"])[:16] if r.get("next_run") else ""
                                reply_lines.append(f"📢 {r['title']}")
                                reply_lines.append(f"   {str(r['content'])[:60]}")
                                if next_run:
                                    reply_lines.append(f"   🕐 {next_run}")
                            reply_lines.append("\n咕嘟～")
                        else:
                            if not reply_lines:
                                reply_lines.append("咕嚕～\n目前沒有公告\n咕…")

                    if not reply_lines:
                        reply_lines.append("咕嚕～\n查了一下沒找到相關資料\n咕…")

                shortcut_msg = "\n".join(reply_lines)
                # 查詢結果下方附 postback 延伸選單
                reply_with_postback_menu(reply_token, shortcut_msg, bot_key=bot_key)
                # 快捷回覆也寫入 chat_logs
                try:
                    with db_conn() as _lconn:
                        _lcur = _lconn.cursor()
                        _lcur.execute("""
                            INSERT INTO chat_logs (group_id, group_name, user_id, question, answer, created_at)
                            VALUES (%s, (SELECT group_name FROM groups WHERE group_id=%s), %s, %s, %s, %s)
                        """, (gid, gid, user_id, clean_text, shortcut_msg, isonow()))
                        _lconn.commit()
                except Exception as _le:
                    logger.warning(f"[ChatLog-Shortcut] write failed: {_le}")
                return

            except Exception as e:
                logger.warning(f"[Shortcut Reply] failed, fallback to Gemini: {e}")
                # 查詢失敗就 fallback 給 Gemini 處理，不中斷

        logger.info(f"[AI Mention] user={user_id} question={clean_text[:50]!r}")
        try:
            now = now_tw()
            today = now.date().isoformat()
            current_hour = now.hour

            # ── 從資料庫撈課程、廣播、公告作為 AI 背景知識（改法 C：補上公告兩張表）──
            try:
                with db_conn() as _conn:
                    _cur = _conn.cursor()

                    # 1. 未來 60 天內的課程
                    _cur.execute("""
                        SELECT c.title, c.course_date::text, c.course_time,
                               c.location, c.description, cat.name AS category
                        FROM courses c
                        LEFT JOIN categories cat ON c.category_id = cat.id
                        WHERE c.course_date >= %s AND c.course_date <= %s
                        ORDER BY c.course_date ASC
                        LIMIT 20
                    """, (today, (now + timedelta(days=60)).date().isoformat()))
                    course_rows = _cur.fetchall()

                    # 2. 啟用中的排程廣播
                    _cur.execute("""
                        SELECT title, content, next_run, end_time
                        FROM scheduled_broadcasts
                        WHERE active = TRUE
                        ORDER BY next_run ASC
                        LIMIT 10
                    """)
                    broadcast_rows = _cur.fetchall()

                    # 3. 未來 30 天內尚未發送的一次性排程公告（新增）
                    _cur.execute("""
                        SELECT title, content, send_at
                        FROM scheduled_announcements
                        WHERE sent = FALSE AND send_at >= %s AND send_at <= %s
                        ORDER BY send_at ASC
                        LIMIT 10
                    """, (now.isoformat(), (now + timedelta(days=30)).isoformat()))
                    scheduled_ann_rows = _cur.fetchall()

                    # 4. 最近 7 天已發出的立即公告（新增）
                    _cur.execute("""
                        SELECT content, sent_at
                        FROM announcements
                        WHERE sent_at >= %s
                        ORDER BY sent_at DESC
                        LIMIT 5
                    """, ((now - timedelta(days=7)).isoformat(),))
                    recent_ann_rows = _cur.fetchall()
            except Exception as db_err:
                logger.warning(f"[AI Mention] DB context fetch failed: {db_err}")
                course_rows = broadcast_rows = scheduled_ann_rows = recent_ann_rows = []

            # ── 格式化背景資料 ──
            context_parts = []
            if course_rows:
                lines = ["【課程清單（未來60天）】"]
                for r in course_rows:
                    cat  = f"[{r['category']}] " if r.get("category") else ""
                    loc  = f" / 地點：{r['location']}" if r.get("location") else ""
                    desc = f" / 說明：{r['description']}" if r.get("description") else ""
                    lines.append(f"- {r['course_date']} {r['course_time']} {cat}{r['title']}{loc}{desc}")
                context_parts.append("\n".join(lines))

            if broadcast_rows:
                lines = ["【定期廣播（啟用中）】"]
                for r in broadcast_rows:
                    next_run = str(r["next_run"])[:16] if r.get("next_run") else "未知"
                    end_time = f" 至 {str(r['end_time'])[:16]}" if r.get("end_time") else ""
                    lines.append(f"- 【{r['title']}】下次發送：{next_run}{end_time}")
                    lines.append(f"  內容：{str(r['content'])[:80]}")
                context_parts.append("\n".join(lines))

            if scheduled_ann_rows:
                lines = ["【排程公告（即將發出）】"]
                for r in scheduled_ann_rows:
                    send_at = str(r["send_at"])[:16]
                    title   = f"【{r['title']}】" if r.get("title") else ""
                    lines.append(f"- {send_at} {title}{str(r['content'])[:80]}")
                context_parts.append("\n".join(lines))

            if recent_ann_rows:
                lines = ["【近期公告（過去7天已發出）】"]
                for r in recent_ann_rows:
                    sent_at = str(r["sent_at"])[:16]
                    lines.append(f"- {sent_at} {str(r['content'])[:80]}")
                context_parts.append("\n".join(lines))

            db_context = "\n\n".join(context_parts)
            if db_context:
                db_context = (
                    f"\n\n[📋 系統資料庫（真實資料，回答時必須以此為準，不可捏造）]\n"
                    f"{db_context}\n"
                )

            # ── 改法 B：意圖分類，明確告訴 Gemini 這題要做什麼 ──
            _kw2 = _load_keywords()
            _all_data_kw = (
                _kw2["kw_course"] + _kw2["kw_broadcast"] +
                _kw2["kw_meeting"] + _kw2["kw_recruitment"] + _kw2["kw_training"] +
                ["什麼", "幾號", "幾點", "哪裡", "在哪", "時間", "日期", "地點",
                 "有什麼", "有沒有", "公告", "活動"]
            )
            _intent_course    = any(kw in clean_text for kw in _kw2["kw_course"] + _kw2["kw_meeting"] + _kw2["kw_recruitment"] + _kw2["kw_training"])
            _intent_broadcast = any(kw in clean_text for kw in _kw2["kw_broadcast"])
            is_data_query     = bool(db_context) and any(kw in clean_text for kw in _all_data_kw)

            # 意圖標籤（給 Gemini 看的明確指令）
            if _intent_course and _intent_broadcast:
                _intent_label = "課程查詢＋公告查詢"
                _intent_instruction = (
                    "這是【課程 + 公告查詢】。"
                    "先列出符合的課程（格式：📅 日期 時間 標題 / 地點），再列出相關公告。"
                    "資料必須全部列出，不可省略。若某類找不到，直接說找不到。"
                )
            elif _intent_course:
                _intent_label = "課程查詢"
                _intent_instruction = (
                    "這是【課程查詢】。"
                    "必須從「課程清單」中找出所有符合問題的項目，格式：📅 日期 時間 標題 / 地點。"
                    "全部列出，不可省略、不可只說「有幾個活動」而不展開。"
                    "若清單中找不到符合的，直接說沒有，不要捏造。"
                )
            elif _intent_broadcast:
                _intent_label = "公告查詢"
                _intent_instruction = (
                    "這是【公告查詢】。"
                    "從「定期廣播」「排程公告」「近期公告」中找出相關內容，逐條列出。"
                    "若找不到，直接說目前沒有公告。"
                )
            else:
                _intent_label = "閒聊"
                _intent_instruction = (
                    "這是【閒聊】。"
                    "不需要引用任何資料，用麥可的個性自然回應。"
                )

            # 判斷目前是餓感模式還是開朗模式
            is_hungry_mode = (
                (6  <= current_hour <= 8)  or
                (11 <= current_hour <= 13) or
                (18 <= current_hour <= 20)
            )
            mode_instruction = (
                "收尾加餓感：「咕嚕…等一下吃什麼」/「好餓」/「先吃飯嗎」（只能在收尾）"
                if is_hungry_mode else
                "語助詞：咕嚕咕嚕～/咕哇～/咕嘟～/咕～/噗咕～，不提餓，保持開朗輕鬆"
            )

            if is_data_query:
                format_instruction = f"""意圖：{_intent_label}
指令：{_intent_instruction}

格式：輸出兩段，中間用 ---SPLIT--- 分隔。
第一段：麥可語氣開頭 → 逐條列出資料（📅/📢 格式）→ 帶溫度的收尾。若有連結請接在對應條目後面。
第二段：一句輕鬆補充或追問（麥可日常感），不重複第一段資料。"""
                temperature_val = 0.3
                max_tok = 700
            else:
                format_instruction = f"""意圖：{_intent_label}
指令：{_intent_instruction}

格式：輸出兩段，中間用 ---SPLIT--- 分隔。
第一段：主回覆（麥可語助詞＋主體1~2句＋收尾），輕鬆有個性。
第二段：延伸一句（反問、發呆聯想、或麥可小日常），不重複第一段。"""
                temperature_val = 0.8
                max_tok = 420

            # ── 從 DB 讀取人設（帶快取，後台改完 60 秒內生效）──
            _p = get_persona(bot_key)
            _name         = _p.get("name", "麥可（Michael）").strip()
            _background   = _p.get("background", "").strip()
            _personality  = _p.get("personality", "").strip()
            _restrictions = _p.get("restrictions", "").strip()
            _extra_notes  = _p.get("extra_notes", "").strip()
            # 個性：每行加 "- " 前綴
            _personality_lines = "\n".join(
                f"- {l.strip()}" for l in _personality.splitlines() if l.strip()
            ) if _personality else "- 懶散但可靠，真的被問到事情就認真給答案"
            _restrictions_block = _restrictions or "不教學、不銷售、不說成功學、不說「很高興為您服務」這類話"
            _extra_block = f"\n【補充說明】\n{_extra_notes}" if _extra_notes else ""

            IPAPA_PERSONA = f"""你是{_name}。

【你的背景】
{_background}

【你的個性】
{_personality_lines}

【你不做的事】
{_restrictions_block}{_extra_block}

{format_instruction}

{"🍞餓感模式：" + mode_instruction if is_hungry_mode else "🌤開朗模式：" + mode_instruction}

資料查詢範例：
咕嚕咕嚕～我查了一下
📅 2025-06-01 09:00 系統說明會 / 台北總部
📅 2025-06-08 14:00 招商說明會 / 線上 https://meet.example.com
📅 2025-06-15 10:00 內訓教練培訓 / 台中
咕嘟～這幾場都記好哦
---SPLIT---
噗咕…我剛從夜市回來，你們要去哪場記得提早報名

閒聊範例：
咕嚕咕嚕～
我在這裡啦
今天曬太陽曬太舒服了
咕～
---SPLIT---
欸…你今天吃飯了嗎

用繁體中文，短句，有節奏感，像一隻真實的角色在說話。"""

            # ── 讀取此用戶在此群組的對話記憶 ──
            memory = get_chat_memory(user_id, gid, limit=4)
            memory_text = ""
            if memory:
                lines = ["[這位用戶最近的對話記憶，可參考但不強制延續]"]
                for m in memory:
                    role_label = "用戶" if m["role"] == "user" else "羊駝"
                    lines.append(f"{role_label}：{m['content'][:120]}")  # 限制單則長度
                memory_text = "\n" + "\n".join(lines) + "\n"

            raw = gemini_call(
                f"{IPAPA_PERSONA}{db_context}{memory_text}\n現在：{today} {now.strftime('%H:%M')}（台灣時間）\n使用者問：{clean_text}",
                max_tokens=max_tok,
                temperature=temperature_val,
            )

            # 切分兩段訊息
            parts = raw.split("---SPLIT---", 1)
            msg1 = parts[0].strip()
            msg2 = parts[1].strip() if len(parts) > 1 else ""

            # ── 儲存這輪對話到記憶 & 完整紀錄 ──
            assistant_record = msg1 + ("\n" + msg2 if msg2 else "")
            save_chat_memory(user_id, gid, clean_text, assistant_record)
            # 寫入完整對話紀錄（不自動刪除）
            try:
                with db_conn() as _lconn:
                    _lcur = _lconn.cursor()
                    _lcur.execute("""
                        INSERT INTO chat_logs (group_id, group_name, user_id, question, answer, created_at)
                        VALUES (%s, (SELECT group_name FROM groups WHERE group_id=%s), %s, %s, %s, %s)
                    """, (gid, gid, user_id, clean_text, assistant_record, isonow()))
                    _lconn.commit()
            except Exception as le:
                logger.warning(f"[ChatLog] write failed: {le}")

        except Exception as e:
            msg1 = "咕嚕…\n我剛才在想事情想太遠了\n稍後再問我一次\n咕…"
            msg2 = ""

        # 一次 reply 送出最多兩則訊息（完全免費，不消耗 push 額度）
        headers = get_bot_headers(bot_key) if bot_key else HEADERS
        messages = [{"type": "text", "text": msg1}]
        if msg2:
            messages.append({"type": "text", "text": msg2})
        requests.post(
            "https://api.line.me/v2/bot/message/reply",
            headers=headers,
            json={"replyToken": reply_token, "messages": messages},
            timeout=10
        )
        return  # mention 處理完畢，不再走 Admin 指令邏輯


def _handle_admin_cmd(
    user_id: str, text: str, reply_token: str, bot_key: str
) -> None:
    """管理員文字指令（/公告、/新增課程、/課程清單 等）。"""
    if user_id not in ADMIN_USER_IDS:
        return

    if text.startswith("/公告 "):
        ok, total, _errors = push_text(f"📢 {text[4:].strip()}", bot_key=bot_key)
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
            with db_conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO courses
                      (title,course_date,course_time,location,description,image_url,
                       remind_value,remind_unit,remind_interval_value,remind_interval_unit,created_at)
                    VALUES (%s,%s,%s,%s,%s,'',30,'days',7,'days',%s) RETURNING id
                """, (c["title"],c["course_date"],c.get("course_time","09:00"),
                      c.get("location",""),c.get("description",""),isonow()))
                cid = cur.fetchone()["id"]
                conn.commit()
            dates = generate_reminders(cid, c["course_date"], 30, "days", 7, "days")
            reply_message(reply_token,
                f"✅ 課程已新增！\n📌 {c['title']}\n📅 {c['course_date']} {c.get('course_time','09:00')}\n"
                f"📍 {c.get('location','未指定')}\n🔔 {len(dates)} 個提醒",
                bot_key=bot_key)
        except Exception as e:
            reply_message(reply_token, f"❌ AI 解析失敗\n{str(e)[:80]}", bot_key=bot_key)

    elif text == "/課程清單":
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT title, course_date::text FROM courses ORDER BY course_date ASC LIMIT 10")
            rows = cur.fetchall()
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



def handle_text(event, bot_key: str = ""):
    user_id     = event["source"].get("userId","")
    reply_token = event["replyToken"]
    msg_obj     = event["message"]
    text        = msg_obj["text"].strip()

    # ── 群組：確保 DB 有該群組記錄，並更新名稱 ──
    if event["source"]["type"] == "group":
        gid = event["source"]["groupId"]
        _handle_group_upsert(gid, bot_key)

        # ── 偵測觸發條件：@mention 或 quote reply ──
        mentionees    = msg_obj.get("mention", {}).get("mentionees", [])
        is_mentioned  = any(m.get("type") == "user" and m.get("isSelf") for m in mentionees)
        is_quote_reply = bool(msg_obj.get("quotedMessageId"))

        if is_mentioned or is_quote_reply:
            _handle_mention_ai(event, msg_obj, user_id, reply_token, text, gid, bot_key)
            return

    # ── 管理員文字指令 ──
    _handle_admin_cmd(user_id, text, reply_token, bot_key)


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
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO groups (group_id, joined_at, group_name, bot_key)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (group_id) DO UPDATE SET group_name = EXCLUDED.group_name, bot_key = EXCLUDED.bot_key
        """, (gid, isonow(), group_name, bot_key))
    conn.commit()
    logger.info(f"Chat joined: {gid} name={group_name!r} bot={bot_key!r}")

    # ── 自我介紹 ──
    intro = (
        "咕嚕咕嚕～大家好 🦙\n"
        "我是麥可\n"
        "王宥忻養的那隻羊駝、坐過特斯拉的那隻\n"
        "媒體說我比主人還紅\n"
        "咕…我也不知道怎麼回事\n\n"
        "現在跑來這裡幫大家管行程\n"
        "@我 或 reply 我就會回你\n"
        "想查課程、會議、招商、內訓都可以問\n\n"
        "咕嘟～有事叫我 🦙"
    )
    try:
        # 第一則：自我介紹；第二則：Postback Quick Reply（按下不在群組顯示文字，體驗更乾淨）
        headers = get_bot_headers(bot_key) if bot_key else HEADERS
        requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers=headers,
            json={"to": gid, "messages": [
                {"type": "text", "text": intro},
                {
                    "type": "text",
                    "text": "咕嚕～先試試看？想查什麼都可以問我 👇",
                    "quickReply": {"items": _make_postback_qr_items()}
                }
            ]},
            timeout=10
        )
    except Exception as e:
        logger.warning(f"handle_join intro push failed: {e}")

def handle_leave(event):
    src = event["source"]
    src_type = src.get("type","")
    if src_type == "group":
        gid = src["groupId"]
    elif src_type == "room":
        gid = src["roomId"]
    else:
        return
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE groups SET active=FALSE WHERE group_id=%s", (gid,))  # 軟刪除，保留歷史記錄
    conn.commit()
    logger.info(f"Chat left: {gid} → active=FALSE")

# ── 關鍵字管理 API ──

@app.route("/admin/keywords", methods=["GET"])
def get_keywords_api():
    if not check_admin(request): return jsonify({"error": "unauthorized"}), 401
    result = {}
    for key, default in _KW_DEFAULTS.items():
        result[key] = get_setting(key, default)
    return jsonify(result)

@app.route("/admin/keywords", methods=["POST"])
def save_keywords_api():
    if not check_admin(request): return jsonify({"error": "unauthorized"}), 401
    data = request.get_json() or {}
    saved = []
    for key in _KW_DEFAULTS:
        if key in data:
            val = ",".join(kw.strip() for kw in str(data[key]).split(",") if kw.strip())
            set_setting(key, val)
            saved.append(key)
    # 清快取，讓下一次請求立即重讀
    global _KW_CACHE_TS
    _KW_CACHE_TS = 0.0
    logger.info(f"[Keywords] updated: {saved}")
    return jsonify({"ok": True, "updated": saved})

# ── 人設管理 API ──

@app.route("/admin/persona", methods=["GET"])
def get_persona_api():
    if not check_admin(request): return jsonify({"error": "unauthorized"}), 401
    bot_key = request.args.get("bot_key", "main")
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM bot_persona WHERE bot_key=%s LIMIT 1", (bot_key,))
        row = cur.fetchone()
    if row:
        return jsonify(dict(row))
    # 回傳預設空白人設供前端顯示
    return jsonify({
        "bot_key": bot_key, "name": "", "background": "",
        "personality": "", "restrictions": "", "greeting_words": "",
        "extra_notes": "", "active": True
    })

@app.route("/admin/persona", methods=["POST"])
def save_persona_api():
    if not check_admin(request): return jsonify({"error": "unauthorized"}), 401
    data = request.get_json() or {}
    bot_key      = data.get("bot_key", "main")
    name         = data.get("name", "").strip()
    background   = data.get("background", "").strip()
    personality  = data.get("personality", "").strip()
    restrictions = data.get("restrictions", "").strip()
    greeting_words = data.get("greeting_words", "").strip()
    extra_notes  = data.get("extra_notes", "").strip()
    active       = bool(data.get("active", True))
    if not name:
        return jsonify({"error": "名稱不可為空"}), 400
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO bot_persona
                (bot_key, name, background, personality, restrictions, greeting_words, extra_notes, active, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (bot_key) DO UPDATE SET
                name=EXCLUDED.name, background=EXCLUDED.background,
                personality=EXCLUDED.personality, restrictions=EXCLUDED.restrictions,
                greeting_words=EXCLUDED.greeting_words, extra_notes=EXCLUDED.extra_notes,
                active=EXCLUDED.active, updated_at=EXCLUDED.updated_at
        """, (bot_key, name, background, personality, restrictions, greeting_words, extra_notes, active, isonow()))
    conn.commit()
    invalidate_persona_cache(bot_key)   # 立即清快取
    logger.info(f"[Persona] bot_key={bot_key} updated")
    return jsonify({"ok": True})

# ── Quick Reply 按鈕管理 API ──

@app.route("/admin/quick-reply-buttons", methods=["GET"])
def get_quick_reply_buttons():
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, label, btn_type, tags, sort_order, active, created_at
            FROM quick_reply_buttons
            ORDER BY sort_order ASC, id ASC
        """)
        rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        if r.get("created_at"): r["created_at"] = str(r["created_at"])
    return jsonify({"buttons": rows})

@app.route("/admin/quick-reply-buttons", methods=["POST"])
def add_quick_reply_button():
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    d = request.json or {}
    label    = d.get("label","").strip()
    btn_type = d.get("btn_type","tag").strip()
    tags     = d.get("tags","").strip()
    sort_order = int(d.get("sort_order", 0))
    active   = bool(d.get("active", True))
    if not label:
        return jsonify({"ok":False,"error":"請填寫按鈕名稱"})
    if btn_type not in ("tag","all_courses","announcement"):
        return jsonify({"ok":False,"error":"btn_type 無效"})
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO quick_reply_buttons (label,btn_type,tags,sort_order,active,created_at)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING id
        """, (label, btn_type, tags, sort_order, active, isonow()))
        new_id = cur.fetchone()["id"]
    conn.commit()
    return jsonify({"ok":True,"id":new_id})

@app.route("/admin/quick-reply-buttons/<int:bid>", methods=["PUT"])
def update_quick_reply_button(bid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    d = request.json or {}
    label    = d.get("label","").strip()
    btn_type = d.get("btn_type","tag").strip()
    tags     = d.get("tags","").strip()
    sort_order = int(d.get("sort_order", 0))
    active   = bool(d.get("active", True))
    if not label:
        return jsonify({"ok":False,"error":"請填寫按鈕名稱"})
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE quick_reply_buttons
            SET label=%s, btn_type=%s, tags=%s, sort_order=%s, active=%s
            WHERE id=%s
        """, (label, btn_type, tags, sort_order, active, bid))
    conn.commit()
    return jsonify({"ok":True})

@app.route("/admin/quick-reply-buttons/<int:bid>", methods=["DELETE"])
def delete_quick_reply_button(bid):
    if not check_admin(request): return jsonify({"error":"unauthorized"}), 401
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM quick_reply_buttons WHERE id=%s", (bid,))
    conn.commit()
    return jsonify({"ok":True})

def _process_events(events: list, bot_key: str):
    """在背景執行緒中處理所有 LINE 事件，避免 Webhook 5 秒逾時。"""
    for event in events:
        t = event.get("type")
        try:
            if t == "message" and event["message"]["type"] == "text":
                handle_text(event, bot_key=bot_key)
            elif t == "postback":
                handle_postback(event, bot_key=bot_key)
            elif t == "join":
                handle_join(event, bot_key=bot_key)
            elif t == "leave":
                handle_leave(event)
        except Exception as e:
            logger.error(f"Event handler error (type={t}): {e}")

# 每個 IP 每分鐘最多記 10 次簽名失敗（避免 log 爆炸，但還是記下來）
_sig_fail_counter: dict[str, list] = {}
_sig_fail_lock = threading.Lock()

def _log_signature_failure(ip: str) -> None:
    """記錄簽名驗證失敗，並限制同一 IP 的 log 頻率（每分鐘最多 10 筆）。"""
    now_ts = time.time()
    with _sig_fail_lock:
        times = _sig_fail_counter.setdefault(ip, [])
        # 清掉 60 秒前的舊紀錄
        _sig_fail_counter[ip] = [t for t in times if now_ts - t < 60]
        if len(_sig_fail_counter[ip]) < 10:
            _sig_fail_counter[ip].append(now_ts)
            logger.warning(
                f"[Webhook] Signature verification FAILED | "
                f"ip={ip} | fail_count_60s={len(_sig_fail_counter[ip])}"
            )
        # 超過 10 次就靜默（已有前幾筆 log 足夠告警）

@app.route("/webhook", methods=["POST"])
def webhook():
    sig = request.headers.get("X-Line-Signature","")
    body = request.get_data()
    bot_key = find_bot_by_signature(body, sig)
    if not bot_key:
        _log_signature_failure(request.remote_addr or "unknown")
        abort(400)

    events = json.loads(body).get("events", [])
    if events:
        # 立刻在背景處理；LINE 只要求在 5 秒內收到 200，不需要等處理完
        t = threading.Thread(target=_process_events, args=(events, bot_key), daemon=True)
        t.start()

    return "OK"

# ── Postback 事件處理（Quick Reply 按鈕觸發）──
def handle_postback(event, bot_key: str = ""):
    user_id     = event["source"].get("userId", "")
    reply_token = event.get("replyToken", "")
    data        = event.get("postback", {}).get("data", "")
    gid         = event["source"].get("groupId", "")

    # 只處理動態按鈕（格式 qrb_{id}）
    if not data.startswith("qrb_"):
        logger.info(f"[Postback] 未知 data={data!r}，略過")
        return

    try:
        btn_id = int(data[4:])
    except ValueError:
        logger.info(f"[Postback] data 格式錯誤: {data!r}")
        return

    # 從 DB 取按鈕設定
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, label, btn_type, tags FROM quick_reply_buttons WHERE id=%s AND active=TRUE", (btn_id,))
            btn = cur.fetchone()
    except Exception as e:
        logger.error(f"[Postback] DB fetch button error: {e}")
        return

    if not btn:
        logger.info(f"[Postback] 找不到按鈕 id={btn_id}")
        return

    label    = btn["label"]
    btn_type = btn["btn_type"]
    tags     = [t.strip() for t in btn["tags"].split(",") if t.strip()] if btn["tags"] else []

    logger.info(f"[Postback] user={user_id} btn={label!r} type={btn_type} tags={tags}")

    if check_cooldown(user_id):
        logger.info(f"[Postback/Cooldown] user={user_id} 冷卻中，略過")
        return

    if is_tired_mode():
        reply_message(reply_token, get_tired_message(), bot_key=bot_key)
        return

    try:
        now   = now_tw()
        today = now.date().isoformat()
        with db_conn() as conn:
            cur = conn.cursor()
            reply_lines = []

            if btn_type == "announcement":
                # ── 公告查詢 ──
                cur.execute("""
                    SELECT title, content, next_run FROM scheduled_broadcasts
                    WHERE active = TRUE ORDER BY next_run ASC LIMIT 5
                """)
                rows = cur.fetchall()
                if rows:
                    reply_lines.append("嗚咕～\n最新公告在這\n")
                    for r in rows:
                        next_run = str(r["next_run"])[:16] if r.get("next_run") else ""
                        reply_lines.append(f"📢 {r['title']}")
                        reply_lines.append(f"   {str(r['content'])[:60]}")
                        if next_run:
                            reply_lines.append(f"   🕐 {next_run}")
                    reply_lines.append("\n咕嘟～")
                else:
                    reply_lines.append("咕嚕～\n目前沒有公告\n咕…")

            elif btn_type == "all_courses":
                # ── 所有近期課程 ──
                cur.execute("""
                    SELECT c.title, c.course_date::text, c.course_time,
                           c.location, c.description, cat.name AS category
                    FROM courses c
                    LEFT JOIN categories cat ON c.category_id = cat.id
                    WHERE c.course_date >= %s
                    ORDER BY c.course_date ASC LIMIT 8
                """, (today,))
                rows = cur.fetchall()
                if rows:
                    reply_lines.append("咕嚕咕嚕～\n近期的行程我整理一下\n")
                    for r in rows:
                        cat  = f"[{r['category']}] " if r.get("category") else ""
                        loc  = f"\n   📍 {r['location']}" if r.get("location") else ""
                        desc = r.get("description", "")
                        links = re.findall(r'https?://\S+', desc)
                        link_line = ("\n   🔗 " + "\n   🔗 ".join(links[:2])) if links else ""
                        reply_lines.append(f"📅 {r['course_date']} {r['course_time']} {cat}{r['title']}{loc}{link_line}")
                    reply_lines.append("\n咕嘟～這幾場都記好哦")
                else:
                    reply_lines.append("咕嚕～\n近期好像沒有排課\n咕…")

            elif btn_type == "tag" and tags:
                # ── 標籤查詢 ──
                like_clauses = " OR ".join(["c.title ILIKE %s OR c.description ILIKE %s"] * len(tags))
                like_params  = [p for t in tags for p in (f"%{t}%", f"%{t}%")]
                cur.execute(f"""
                    SELECT c.title, c.course_date::text, c.course_time,
                           c.location, c.description, cat.name AS category
                    FROM courses c
                    LEFT JOIN categories cat ON c.category_id = cat.id
                    WHERE c.course_date >= %s AND ({like_clauses})
                    ORDER BY c.course_date ASC LIMIT 8
                """, (today, *like_params))
                rows = cur.fetchall()
                if rows:
                    reply_lines.append(f"咕嚕咕嚕～\n我查了一下「{label}」相關的\n")
                    for r in rows:
                        cat  = f"[{r['category']}] " if r.get("category") else ""
                        loc  = f"\n   📍 {r['location']}" if r.get("location") else ""
                        desc = r.get("description", "")
                        links = re.findall(r'https?://\S+', desc)
                        link_line = ("\n   🔗 " + "\n   🔗 ".join(links[:2])) if links else (f"\n   💬 {desc.strip()[:80]}" if desc.strip() else "")
                        reply_lines.append(f"📅 {r['course_date']} {r['course_time']} {cat}{r['title']}{loc}{link_line}")
                    reply_lines.append("\n咕嘟～這幾場記好哦")
                else:
                    reply_lines.append(f"咕嚕～\n「{label}」近期好像沒有排\n咕…")
            else:
                reply_lines.append("咕嚕～\n查了一下沒找到相關資料\n咕…")

        msg = "\n".join(reply_lines)
        reply_with_postback_menu(reply_token, msg, bot_key=bot_key)

        # 寫入 chat_logs
        try:
            with db_conn() as lconn:
                lcur = lconn.cursor()
                lcur.execute("""
                    INSERT INTO chat_logs (group_id, group_name, user_id, question, answer, created_at)
                    VALUES (%s, (SELECT group_name FROM groups WHERE group_id=%s), %s, %s, %s, %s)
                """, (gid, gid, user_id, f"[qr] {label}", msg, isonow()))
                lconn.commit()
        except Exception as le:
            logger.warning(f"[Postback/ChatLog] write failed: {le}")

    except Exception as e:
        logger.error(f"[Postback] handler error: {e}")
        reply_message(reply_token, "咕嚕…\n我剛才在想事情\n查詢出了點狀況\n稍後再試試\n咕…", bot_key=bot_key)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
