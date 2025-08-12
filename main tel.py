# -*- coding: utf-8 -*-
"""
telegram_watcher.py

مراقب رسائل تيليجرام متعدد الجلسات باستخدام Telethon.
- يدعم تشغيل حتى 10 جلسات (أو أكثر) بالتوازي.
- يعتمد قاعدة بيانات SQLite واحدة مشتركة بين كل الجلسات.
- كل ثريد (جلسة) يستخدم اتصال مستقل للقاعدة لتفادي مشكلة "Recursive use of cursors not allowed".
- يحذف قاعدة البيانات تلقائيًا إذا تجاوز عمرها 24 ساعة (يُعاد إنشاؤها آليًا).
- فلترة ذكية: طول الرسالة، عدد الأسطر، تجاهل المشرفين، كلمات مفتاحية، تكرار عالمي/لكل مستخدم مع استثناء الرسائل الأكاديمية المهمة.
- تنسيق الإرسال بـ HTML مع روابط قابلة للنقر للمستخدم والرسالة.

قبل التشغيل:
    pip install telethon requests
"""

import os
import re
import json
import time
import sqlite3
import threading
import asyncio
import hashlib
from typing import List
import requests
import html

from telethon.sync import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import AuthKeyUnregisteredError
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.types import ChannelParticipantAdmin, ChannelParticipantCreator

# ===== إعداداتك =====
API_ID = 27365071
API_HASH = '4ab2f70c153a54c1738ba2e81e9ea822'
BOT_TOKEN = "7991348516:AAG2-wBullJmGz4h1Ob2ii5djb8bQFLjm4w"

# إرسال التنبيهات فقط لهؤلاء المستخدمين (ضع IDs المسموح لهم هنا)
ALLOWED_RECIPIENTS = [698815776, 7052552394]  # مثال: [698815776, 123456789]

SESS_FILE = "sessions.json"
DB_PATH = "seen.db"

# ===== حذف قاعدة البيانات إذا مرّ عليها يوم كامل =====
def _maybe_delete_old_db(db_path: str, max_age_seconds: int = 86400):
    if os.path.exists(db_path):
        try:
            age = time.time() - os.path.getmtime(db_path)
            if age > max_age_seconds:
                os.remove(db_path)
                print("🗑 تم حذف قاعدة البيانات لأنها أقدم من يوم.")
        except Exception as e:
            print(f"⚠️ فشل حذف قاعدة البيانات: {e}")

_maybe_delete_old_db(DB_PATH)

# ===== فلاتر المحتوى العامة =====
MAX_AD_LENGTH = 300   # أي رسالة أطول من هذا تعتبر إعلان وتُتجاهل
MAX_LINES = 2         # تجاهل أي رسالة عدد أسطرها > 2

# نافذة تجاهل التكرار (ثواني)
DUP_WINDOW_SECONDS = 6 * 60 * 60  # 6 ساعات

# (اختياري) حصر المراقبة على جروبات/قنوات معينة (usernames بدون @). فارغة = كل شيء
ALLOWED_CHAT_USERNAMES: List[str] = []  # مثال: ["somegroup", "another_channel"]

# ===== الكلمات المفتاحية (OR) =====
KEYWORDS = [
    "يحل", "من يحل واجب", "من يكتب بحث", "يسوي لي بحث", "تكفون حل",
    "احتاج احد يسوي لي", "اريد مختص يحل", "من يساعدني", "تقرير", "سكليف",
    "سكاليف", "اعذار", "اعذار طبية", "يسوي بحث", "حل واجب", "تلخيص", "شرح",
    "يحل لي", "واجب", "مساعدة", "واجبات", "ابغى حل", "يكتب", "ابغى بحث", "عذر",
    "يزين لي بحث", "يزين", "يسوي لي سكليف", "يجمل", "يسوي سكليف ينزل في صحتي",
    "يحل بحث", "يحل مشاريع", "من يعرف مختص مجروح", "من يطلع سكليف عذر طبي",
    "احد يعرف", "أحد يعرف", "احد يسوي", "نسوي", "تقرير ميداني", "ابغى عذر طبي",
    "سكليف عذر", "تقرير تدريب", "سيفي", "cv", "سي في", "سيرة ذاتية", "سيره",
    "ملف انجاز", "ملفات انجاز", "ابغى احد يساعدني", "ابي واجب", "ابي بحث",
    "احتاج تقرير", "مين يعرف احد", "احد فاهم", "من فاهم", "تسوي لي واجب",
    "تسوي لي بحث", "مين يسويلي واجب", "مين يسويلي بحث", "حل واجب جامعي",
    "كتابة سيرة ذاتية", "مساعدة في الواجب", "برزنتيشن", "باوربوينت", "عرض تقديمي",
    "بحث بسيط بمقابل رمزي", "مينسوي", "فاهم"
]

# ===== كلمات الطلب الأكاديمي (تُطبع دائمًا حتى لو مكررة) =====
ACADEMIC_ALWAYS_PRINT = [
    "حل واجب", "حل واجبات", "ابي حل", "ابغى حل", "مساعدة في الواجب", "مساعدة واجب",
    "تقرير", "ابي تقرير", "تقرير ميداني", "عرض تقديمي", "برزنتيشن", "باوربوينت",
    "كويز", "اختبار", "امتحان", "بحث", "اسايمنت", "assignment", "يفهم", "شرح",
    "سيرة ذاتية", "cv", "سي في"
]

# ===== مؤشرات الرسائل الإعلانية (تخضع لتجاهل التكرار عالميًا) =====
AD_HINT_PATTERNS = [
    r"(?:\+?\d[\d\-\s]{7,}\d)",                  # رقم هاتف
    r"(?:https?://\S+|www\.\S+)",                # روابط http/https/www
    r"(?:t\.me/[A-Za-z0-9_]+)",                  # روابط تيليجرام عامة
    r"(?:@[\w\d_]{4,})",                         # منشن/يوزر
]
AD_HINT_KEYWORDS = [
    "تواصل", "راسلني", "خاص", "الخاص", "واتساب", "واتس", "whatsapp",
    "رقمي", "رقم", "راسل", "ادخل", "انضم", "قناتي", "قناة", "رابط", "link", "contact"
]

# ===== تحميل / حفظ الجلسات =====
def load_sessions() -> List[str]:
    if not os.path.exists(SESS_FILE):
        return []
    try:
        with open(SESS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                seen, uniq = set(), []
                for s in data:
                    if s and s not in seen:
                        uniq.append(s); seen.add(s)
                return uniq
    except Exception as e:
        print(f"⚠️ خطأ قراءة {SESS_FILE}: {e}")
    return []

def save_sessions_list(sessions):
    with open(SESS_FILE, "w", encoding="utf-8") as f:
        json.dump(sessions, f, ensure_ascii=False, indent=2)

def validate_sessions(api_id, api_hash, sessions):
    """استبعاد الجلسات التالفة مسبقًا لضمان عدم الانهيار."""
    valid, invalid = [], []
    for s in sessions:
        try:
            c = TelegramClient(StringSession(s), api_id, api_hash)
            c.connect()
            c.get_me()
            c.disconnect()
            valid.append(s)
        except AuthKeyUnregisteredError:
            invalid.append(s)
        except Exception:
            invalid.append(s)
    if invalid:
        print(f"⚠️ جلسات غير صالحة: {len(invalid)} — سيتم حذفها من {SESS_FILE}")
        new_list = [s for s in sessions if s in valid]
        save_sessions_list(new_list)
    return valid

# ===== قاعدة بيانات (إنشاء الجداول) =====
def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS seen_messages(
        chat_id INTEGER NOT NULL,
        msg_id  INTEGER NOT NULL,
        PRIMARY KEY(chat_id, msg_id)
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS seen_text_user(
        sender_id INTEGER NOT NULL,
        text_hash TEXT NOT NULL,
        last_ts   INTEGER NOT NULL,
        PRIMARY KEY(sender_id, text_hash)
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS seen_text_global(
        text_hash TEXT NOT NULL,
        last_ts   INTEGER NOT NULL,
        PRIMARY KEY(text_hash)
    )
    """)
    conn.commit()
    conn.close()

# استدعِه مرة عند بدء البرنامج لضمان وجود الجداول
init_db()

# كل ثريد يحصل على اتصال مستقل (لكن إلى نفس الملف DB_PATH)
def get_db_connection():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

# ===== دوال/مساعدات قاعدة البيانات =====
def is_seen(conn, chat_id: int, msg_id: int) -> bool:
    """يمنع تكرار نفس الرسالة (chat_id,msg_id) حتى بعد إعادة التشغيل."""
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO seen_messages(chat_id, msg_id) VALUES (?, ?)", (chat_id, msg_id))
    conn.commit()
    cur.execute("SELECT changes()")
    return cur.fetchone()[0] == 0

def _hash_text(text_norm: str) -> str:
    return hashlib.sha1(text_norm.encode("utf-8")).hexdigest()

def is_duplicate_for_user(conn, sender_id: int, text_norm: str, now_ts: int) -> bool:
    """يتجاهل تكرار نفس النص من نفس المستخدم خلال النافذة الزمنية."""
    h = _hash_text(text_norm)
    cur = conn.cursor()
    cur.execute("SELECT last_ts FROM seen_text_user WHERE sender_id=? AND text_hash=?", (sender_id, h))
    row = cur.fetchone()
    if row and (now_ts - row[0]) < DUP_WINDOW_SECONDS:
        return True
    cur.execute("""
        INSERT INTO seen_text_user(sender_id, text_hash, last_ts)
        VALUES(?, ?, ?)
        ON CONFLICT(sender_id, text_hash) DO UPDATE SET last_ts=excluded.last_ts
    """, (sender_id, h, now_ts))
    conn.commit()
    return False

def is_duplicate_global(conn, text_norm: str, now_ts: int) -> bool:
    """يتجاهل تكرار نفس النص عالميًا (حتى من مستخدمين مختلفين) خلال النافذة الزمنية."""
    h = _hash_text(text_norm)
    cur = conn.cursor()
    cur.execute("SELECT last_ts FROM seen_text_global WHERE text_hash=?", (h,))
    row = cur.fetchone()
    if row and (now_ts - row[0]) < DUP_WINDOW_SECONDS:
        return True
    cur.execute("""
        INSERT INTO seen_text_global(text_hash, last_ts)
        VALUES(?, ?)
        ON CONFLICT(text_hash) DO UPDATE SET last_ts=excluded.last_ts
    """, (h, now_ts))
    conn.commit()
    return False

# ===== التطبيع العربي =====
_ARABIC_NORM_MAP = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا",
    "ى": "ي", "ئ": "ي", "ؤ": "و",
    "ة": "ه", "ـ": ""
})
_DIACRITICS = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")

def normalize_ar(text: str) -> str:
    text = text.strip().lower()
    text = _DIACRITICS.sub("", text)
    text = text.translate(_ARABIC_NORM_MAP)
    text = re.sub(r"\s+", " ", text)  # توحيد المسافات
    return text

def build_pattern(words: List[str]) -> re.Pattern:
    escaped = [re.escape(normalize_ar(w)) for w in words if w.strip()]
    if not escaped:
        return re.compile(r"^\b\B$")  # نمط لا يطابق شيء لو القائمة فاضية
    pattern = r"(?:(?<=^)|(?<=\s)|(?<=[\.\,\!\?\:\;\-\_\/\(\)\[\]\{\}]))(" + "|".join(escaped) + r")(?:(?=$)|(?=\s)|(?=[\.\,\!\?\:\;\-\_\/\(\)\[\]\{\}]))"
    return re.compile(pattern, flags=re.IGNORECASE)

# تهيئة الأنماط مسبقًا للسرعة
KEYWORDS_RE = build_pattern(KEYWORDS)
ACADEMIC_ALWAYS_PRINT_NORM = [normalize_ar(w) for w in ACADEMIC_ALWAYS_PRINT]
AD_HINT_KEYWORDS_NORM = [normalize_ar(w) for w in AD_HINT_KEYWORDS]
AD_HINT_REGEXES = [re.compile(p, flags=re.IGNORECASE) for p in AD_HINT_PATTERNS]

# ===== تصنيف النوع =====
def is_academic_request(text_norm: str) -> bool:
    """يرجع True إذا كانت الرسالة طلبًا أكاديميًا مهمًا (تُطبع دائمًا)."""
    return any(kw in text_norm for kw in ACADEMIC_ALWAYS_PRINT_NORM)

def is_ad_like(text_raw: str, text_norm: str) -> bool:
    """يرجع True إذا كانت الرسالة إعلانية (أرقام/روابط/تواصل...)."""
    if any(rx.search(text_raw) for rx in AD_HINT_REGEXES):
        return True
    if any(kw in text_norm for kw in AD_HINT_KEYWORDS_NORM):
        return True
    return False

# ===== إرسال التنبيه عبر Bot API (HTTP) — مع قائمة السماح =====
def send_alert_http(text: str):
    if not ALLOWED_RECIPIENTS:
        return
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        CHUNK = 3900
        parts = [text[i:i+CHUNK] for i in range(0, len(text), CHUNK)] or [text]
        for rid in ALLOWED_RECIPIENTS:
            for p in parts:
                requests.post(url, data={
                    "chat_id": rid,
                    "text": p,
                    "parse_mode": "HTML"  # تفعيل HTML
                })
    except Exception as e:
        print(f"⚠️ فشل إرسال التنبيه: {e}")

# ===== فحص المشرف (async دقيق لكل أنواع الدردشات) =====
async def is_sender_admin(event) -> bool:
    try:
        chat = event.chat
        # سوبرجروبات/قنوات: استخدم GetParticipantRequest
        if getattr(chat, "megagroup", None) or getattr(chat, "broadcast", None) \
           or getattr(chat, "gigagroup", None) or getattr(chat, "username", None):
            res = await event.client(GetParticipantRequest(chat, event.sender_id))
            part = getattr(res, "participant", None)
            return isinstance(part, (ChannelParticipantAdmin, ChannelParticipantCreator))
        # مجموعات قديمة (chats):
        perms = await event.client.get_permissions(chat, event.sender_id)
        return bool(
            getattr(perms, "is_admin", False) or
            getattr(perms, "is_creator", False) or
            getattr(perms, "admin_rights", None)
        )
    except Exception:
        # لو فشل الاستعلام، لا نعتبره مشرف لتفادي إسكات كل شيء بالخطأ
        return False

# ===== مولّد معالج الرسائل (يرتبط بكل اتصال DB خاص بالثريد) =====
def get_message_handler(db_conn):
    @events.register(events.NewMessage())
    async def handle_message(event):
        try:
            if not event.message or not event.message.message:
                return

            # (اختياري) حصر على قوائم محددة
            if ALLOWED_CHAT_USERNAMES:
                ch = event.chat
                uname = getattr(ch, "username", None)
                if not uname or uname.lower() not in [u.lower() for u in ALLOWED_CHAT_USERNAMES]:
                    return

            text_raw = event.message.message

            # فلتر الطول والأسطر مبكرًا لتحسين الأداء
            if len(text_raw) > MAX_AD_LENGTH:
                return
            if len(text_raw.splitlines()) > MAX_LINES:
                return

            # طبّع مرة واحدة واستخدمه في كل الفحوص
            text_norm = normalize_ar(text_raw)

            # فحص الكلمات المفتاحية (OR) — لو ما في تطابق، نخرج بسرعة
            if not KEYWORDS_RE.search(text_norm):
                return

            # تجاهل رسائل المشرفين
            if await is_sender_admin(event):
                return

            chat_id = event.chat_id
            msg_id = event.message.id

            # منع تكرار الرسالة نفسها (chat_id,msg_id)
            if is_seen(db_conn, chat_id, msg_id):
                return

            # تصنيف نوع الرسالة
            academic = is_academic_request(text_norm)
            ad_like = is_ad_like(text_raw, text_norm)

            now_ts = int(event.message.date.timestamp())
            sender = event.sender
            sender_id = getattr(sender, 'id', 0)

            # منطق التكرار:
            # 1) الأكاديمي المهم → لا نطبق أي تكرار (نطبع دائمًا)
            # 2) غير الأكاديمي:
            #    - نمنع إعادة نفس النص من نفس المرسل خلال النافذة
            #    - إذا كان إعلاني ad_like → نطبق تكرار عالمي (حتى من مستخدمين مختلفين)
            if not academic:
                if is_duplicate_for_user(db_conn, sender_id, text_norm, now_ts):
                    return
                if ad_like and is_duplicate_global(db_conn, text_norm, now_ts):
                    return

            # تجهيز الروابط والمخرجات — مع HTML
            if getattr(sender, "username", None):
                sender_link = f"https://t.me/{sender.username}"
                sender_label = f'@{sender.username}'
            else:
                sender_link = f"tg://user?id={sender_id}"
                sender_label = f'ID:{sender_id}'

            chat = event.chat
            msg_link = "غير متاح"
            if getattr(chat, "username", None):
                msg_link = f"https://t.me/{chat.username}/{msg_id}"

            # نهرب النص لتفادي مشاكل HTML
            safe_text = html.escape(text_raw)

            message_text = (
                f"<b>ID المرسل :</b> {sender_id}\n"
                f"<b>نص الرساله :</b>\n<code>{safe_text}</code>\n"
                f"<b>رابط الرساله :</b> {msg_link}\n"
                f"<b>رابط يوزر المرسل :</b> <a href=\"{sender_link}\">{sender_label}</a>"
            )

            send_alert_http(message_text)
            print(f"🚨 تنبيه (ID:{sender_id})")

        except Exception as e:
            print(f"⚠️ خطأ: {e}")
    return handle_message

# ===== مُشغّل كل عميل داخل Thread مع event loop خاص =====
def client_runner(session_str: str, idx: int):
    name = f"client-{idx}"
    backoff = 2
    # اتصال DB خاص بهذا الثريد
    db_conn = get_db_connection()
    while True:
        try:
            # إنشاء loop لهذا الخيط (ضروري لتليثون داخل الخيوط)
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
            client.start()
            # إضافة المعالج المرتبط باتصال هذا الثريد
            client.add_event_handler(get_message_handler(db_conn), events.NewMessage())
            print(f"✅ [{name}] جاهز — يبدأ الاستماع")
            send_alert_http(f"<b>✅ [{name}] متصل — بدأ الاستماع للرسائل.</b>")
            client.run_until_disconnected()
            print(f"ℹ️ [{name}] خرج — إعادة محاولة خلال {backoff}s")
        except (OSError, ConnectionError) as e:
            print(f"⛔ [{name}] انقطاع: {e} — إعادة محاولة خلال {backoff}s")
        except AuthKeyUnregisteredError:
            print(f"⛔ [{name}] جلسة غير صالحة — سيتم تجاهلها.")
            break
        except Exception as e:
            print(f"⛔ [{name}] خطأ: {e} — إعادة محاولة خلال {backoff}s")
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)

# ===== التشغيل =====
def main():
    sessions = load_sessions()
    if not sessions:
        print("❌ لا توجد جلسات — شغّل session_manager.py أولًا.")
        return
    sessions = validate_sessions(API_ID, API_HASH, sessions)
    if not sessions:
        print("❌ كل الجلسات غير صالحة — أنشئ جلسات جديدة.")
        return

    send_alert_http(
        "<b>✅ البوت يعمل — فلاتر:</b> طول ≤ "
        f"{MAX_AD_LENGTH} حرف & ≤ {MAX_LINES} سطر — تجاهل المشرفين — "
        "تكرار ذكي (الإعلانات تُحجب عند التكرار، الطلبات الأكاديمية تُطبع دائمًا) — كلمة مفتاحية واحدة تكفي."
    )

    threads = []
    for i, s in enumerate(sessions, 1):
        t = threading.Thread(target=client_runner, args=(s, i), daemon=True)
        t.start()
        threads.append(t)
        print(f"✅ تشغيل جلسة #{i}")

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("👋 تم الإيقاف.")

if __name__ == "__main__":
    main()
