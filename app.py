import os
import json
import hashlib
import hmac
import base64
import sqlite3
import logging
from datetime import datetime
from flask import Flask, request, abort, g
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
DATABASE = os.environ.get("DATABASE_PATH", "bot.db")

logger.info(f"Admin IDs loaded: {ADMIN_USER_IDS}")

HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
}


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
    """)
    conn.commit()
    conn.close()


def verify_signature(body: bytes, signature: str) -> bool:
    hash_val = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256
    ).digest()
    expected = base64.b64encode(hash_val).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def send_message(target_id: str, text: str):
    url = "https://api.line.me/v2/bot/message/push"
    payload = {
        "to": target_id,
        "messages": [format_announcement(text)],
    }
    resp = requests.post(url, headers=HEADERS, json=payload)
    logger.info(f"Push to {target_id[:10]}...: {resp.status_code} {resp.text}")
    return resp.status_code == 200


def reply_message(reply_token: str, text: str):
    url = "https://api.line.me/v2/bot/message/reply"
    payload = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text}],
    }
    resp = requests.post(url, headers=HEADERS, json=payload)
    logger.info(f"Reply: {resp.status_code} {resp.text}")


def format_announcement(text: str) -> dict:
    now = datetime.now().strftime("%Y/%m/%d %H:%M")
    return {
        "type": "flex",
        "altText": f"[公告] {text[:50]}",
        "contents": {
            "type": "bubble",
            "header": {
                "type": "box",
                "layout": "vertical",
                "contents": [{"type": "text", "text": "📢 公告", "weight": "bold", "size": "lg", "color": "#ffffff"}],
                "backgroundColor": "#06C755",
                "paddingAll": "16px",
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": [{"type": "text", "text": text, "wrap": True, "size": "md", "color": "#333333"}],
                "paddingAll": "16px",
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "contents": [{"type": "text", "text": now, "size": "xs", "color": "#aaaaaa", "align": "end"}],
                "paddingAll": "10px",
            },
        },
    }


def broadcast_to_all_groups(text: str):
    conn = get_db()
    groups = conn.execute("SELECT group_id FROM groups").fetchall()
    logger.info(f"Broadcasting to {len(groups)} groups")
    success = 0
    for row in groups:
        if send_message(row["group_id"], text):
            success += 1
    conn.execute(
        "INSERT INTO announcements (content, sent_at, group_count) VALUES (?, ?, ?)",
        (text, datetime.now().isoformat(), success),
    )
    conn.commit()
    conn.close()
    return success, len(groups)


def handle_text_message(event: dict):
    user_id = event["source"].get("userId", "")
    reply_token = event["replyToken"]
    text = event["message"]["text"].strip()
    source_type = event["source"]["type"]

    logger.info(f"Message from user_id={user_id}, source_type={source_type}, text={text}")
    logger.info(f"Is admin: {user_id in ADMIN_USER_IDS}, Admin list: {ADMIN_USER_IDS}")

    # 記錄群組 ID
    if source_type == "group":
        group_id = event["source"]["groupId"]
        conn = get_db()
        conn.execute(
            "INSERT OR IGNORE INTO groups (group_id, joined_at) VALUES (?, ?)",
            (group_id, datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()
        logger.info(f"Registered group: {group_id}")

    if user_id not in ADMIN_USER_IDS:
        logger.info(f"Non-admin user {user_id}, ignoring")
        return

    # 管理員指令
    if text.startswith("/公告 "):
        content = text[4:].strip()
        if not content:
            reply_message(reply_token, "用法：/公告 [內容]")
            return
        ok, total = broadcast_to_all_groups(content)
        reply_message(reply_token, f"✅ 已發送公告到 {ok}/{total} 個群組")

    elif text == "/公告":
        reply_message(reply_token, "用法：/公告 [內容]\n例：/公告 今天下午開會請準時")

    elif text.startswith("/排程 "):
        parts = text[4:].strip().split(" ", 1)
        if len(parts) < 2:
            reply_message(reply_token, "用法：/排程 HH:MM 公告內容\n例：/排程 09:00 早安提醒")
            return
        time_str, content = parts
        try:
            hour, minute = map(int, time_str.split(":"))
        except ValueError:
            reply_message(reply_token, "時間格式錯誤，請用 HH:MM，例如 09:30")
            return
        job_id = f"sched_{user_id}_{time_str.replace(':','')}"
        scheduler.add_job(
            broadcast_to_all_groups, "cron",
            hour=hour, minute=minute, args=[content],
            id=job_id, replace_existing=True,
        )
        reply_message(reply_token, f"✅ 已排程：每天 {time_str} 自動發送\n內容：{content}")

    elif text == "/取消排程":
        jobs = scheduler.get_jobs()
        user_jobs = [j for j in jobs if j.id.startswith(f"sched_{user_id}_")]
        for j in user_jobs:
            j.remove()
        reply_message(reply_token, f"✅ 已取消 {len(user_jobs)} 個排程")

    elif text == "/排程清單":
        jobs = scheduler.get_jobs()
        if not jobs:
            reply_message(reply_token, "目前沒有排程")
        else:
            lines = [f"• {j.id}: {j.trigger}" for j in jobs]
            reply_message(reply_token, "排程清單：\n" + "\n".join(lines))

    elif text == "/群組清單":
        conn = get_db()
        groups = conn.execute("SELECT group_id, joined_at FROM groups").fetchall()
        conn.close()
        if not groups:
            reply_message(reply_token, "尚未加入任何群組\n請先把機器人邀請進群組")
        else:
            lines = [f"• {r['group_id'][:15]}... ({r['joined_at'][:10]})" for r in groups]
            reply_message(reply_token, f"已加入 {len(groups)} 個群組：\n" + "\n".join(lines))

    elif text in ("/說明", "/help"):
        help_text = (
            "📋 公告機器人指令\n\n"
            "/公告 [內容]\n立即發公告到所有群組\n\n"
            "/排程 HH:MM [內容]\n設定每天定時公告\n\n"
            "/取消排程\n取消所有排程\n\n"
            "/排程清單\n查看排程\n\n"
            "/群組清單\n查看已加入群組"
        )
        reply_message(reply_token, help_text)

    else:
        logger.info(f"Admin sent unrecognized command: {text}")


def handle_join(event: dict):
    if event["source"]["type"] == "group":
        group_id = event["source"]["groupId"]
        conn = get_db()
        conn.execute(
            "INSERT OR IGNORE INTO groups (group_id, joined_at) VALUES (?, ?)",
            (group_id, datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()
        logger.info(f"Bot joined group: {group_id}")


def handle_leave(event: dict):
    if event["source"]["type"] == "group":
        group_id = event["source"]["groupId"]
        conn = get_db()
        conn.execute("DELETE FROM groups WHERE group_id = ?", (group_id,))
        conn.commit()
        conn.close()
        logger.info(f"Bot left group: {group_id}")


@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data()

    if not verify_signature(body, signature):
        logger.warning("Invalid signature")
        abort(400)

    data = json.loads(body)
    logger.info(f"Received events: {json.dumps(data)}")

    for event in data.get("events", []):
        event_type = event.get("type")
        if event_type == "message" and event["message"]["type"] == "text":
            handle_text_message(event)
        elif event_type == "join":
            handle_join(event)
        elif event_type == "leave":
            handle_leave(event)

    return "OK"


@app.route("/", methods=["GET"])
def index():
    return "LINE 公告機器人運行中 ✅"


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
