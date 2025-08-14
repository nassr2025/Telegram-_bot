# -*- coding: utf-8 -*-
"""
main tel.py — Telegram watcher for Render

— يعمل بعدّة جلسات (StringSession) من ملف sessions.json
— يرسل التنبيهات عبر Bot API لمجموعة محددة من IDs (قائمة ثابتة + allowed_ids.json + ALLOWED_IDS من البيئة)
— فلترة ذكية: طول الرسالة، عدد الأسطر، تجاهل المشرفين، مطابقة كلمات مفتاحية
— منع التكرار (نفس الرسالة، نفس النص لنفس المستخدم، وتكرار عالمي للإعلانات)
— أرشفة الرسائل (نص/وسائط) إلى قناة خاصة حتى لو حُذفت الرسالة الأصلية
— آمن للخيوط: اتصال SQLite مستقل لكل ثريد لتفادي "Recursive use of cursors not allowed"
— يدعم Render:
    * اضبط متغيّر البيئة VALIDATE_SESSIONS=0 لتخطي فحص الجلسات المسبق (تشغيل أسرع)
    * ARCHIVE_CHANNEL_ID (رقم القناة بالسالب) أو ARCHIVE_INVITE (رابط دعوة)
    * API_ID, API_HASH, BOT_TOKEN, ALLOWED_IDS (IDs مفصولة بفواصل)

ملاحظات:
- ملف sessions.json يجب أن يحتوي قائمة من StringSession (سلاسل) لحسابات تيليجرام الوسيطة.
- يُفضّل رفع allowed_ids.json (اختياري) ليضم IDs إضافية بدون تعديل الكود.
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
import tempfile
import shutil

from telethon.sync import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import AuthKeyUnregisteredError, FloodWaitError
from telethon.tl.functions.channels import GetParticipantRequest, JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.types import ChannelParticipantAdmin, ChannelParticipantCreator

# ====== إعدادات / بيئة ======
# يمكن ترك القيم هنا كـ default، ويفضل ضبطها من متغيرات البيئة على Render
API_ID = int(os.environ.get("API_ID", "27365071"))
API_HASH = os.environ.get("API_HASH", "4ab2f70c153a54c1738ba2e81e9ea822")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "7991348516:AAG2-wBullJmGz4h1Ob2ii5djb8bQFLjm4w")

# فحص الجلسات قبل البدء؟ 1=يفحص (أبطأ)، 0=يتخطى (أسرع ولا يحذف جلسات)
VALIDATE_SESSIONS = int(os.environ.get("VALIDATE_SESSIONS", "1"))

# قناة الأرشفة:
# 1) مفضّل: ARCHIVE_CHANNEL_ID (int بالسالب مثل -1001234567890)
# 2) بديل: ARCHIVE_INVITE (رابط دعوة مثل: https://t.me/+XXXXXXXXXXXXXXX)
ARCHIVE_CHANNEL_ID = int(os.environ.get("ARCHIVE_CHANNEL_ID", "0"))
ARCHIVE_INVITE = os.environ.get("ARCHIVE_INVITE", "").strip()

# ملف اختياري لتوسيع المصرح لهم بدون تعديل الكود
ALLOWED_IDS_FILE = "allowed_ids.json"

# القائمة الافتراضية (لا تغيّرها إن كنت تستخدم الملفات/البيئة)
DEFAULT_ALLOWED_RECIPIENTS = [698815776, 7052552394]

# ملفات التخزين
SESS_FILE = "sessions.json"
DB_PATH = "seen.db"

# ====== فلاتر المحتوى ======
MAX_AD_LENGTH = 300   # تجاهل أي رسالة أطول من هذا (نعتبرها إعلان/سبام)
MAX_LINES = 2         # تجاهل أي رسالة تحتوي أكثر من سطرين
DUP_WINDOW_SECONDS = 6 * 60 * 60  # 6 ساعات نافذة منع التكرار

# (اختياري) قصر المراقبة على Users/Groups بعينها (usernames بدون @)
ALLOWED_CHAT_USERNAMES: List[str] = []  # مثال: ["somegroup", "another_channel"]

# الكلمات المفتاحية (OR)
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
    "بحث بسيط بمقابل رمزي", "مينسوي", "فاهم", "ابي تسريبات", "احد شاطر", "دكتور شاطر",
    "شاطر", "يحل كويس", "يحل اختبار", "يحل كويل", "كويل", "يحل lab", "يحل برمجه",
    "فاهم برمجه", "يحل كود", "يحل جافا", "يحل منطقي", "logic", "يحل مشاريع",
    "يسوي project", "يسوي ملخص", "يلخص كتاب", "واجب اكسل"
]

# طلبات أكاديمية مهمة (تُطبع دائمًا)
ACADEMIC_ALWAYS_PRINT = [
    "حل واجب", "حل واجبات", "ابي حل", "ابغى حل", "مساعدة في الواجب", "مساعدة واجب",
    "تقرير", "ابي تقرير", "تقرير ميداني", "عرض تقديمي", "برزنتيشن", "باوربوينت",
    "كويز", "اختبار", "امتحان", "بحث", "اسايمنت", "assignment", "يفهم", "شرح",
    "سيرة ذاتية", "cv", "سي في"
]

# مؤشرات الإعلانات
AD_HINT_PATTERNS = [
    r"(?:\+?\d[\d\-\s]{7,}\d)",                  # أرقام هاتف
    r"(?:https?://\S+|www\.\S+)",                # روابط
    r"(?:t\.me/[A-Za-z0-9_]+)",                  # روابط تيليجرام
    r"(?:@[\w\d_]{4,})",                         # منشن/يوزر
]
AD_HINT_KEYWORDS = [
    "تواصل", "راسلني", "خاص", "الخاص", "واتساب", "واتس", "whatsapp",
    "رقمي", "رقم", "راسل", "ادخل", "انضم", "قناتي", "قناة", "رابط", "link", "contact"
]

# ====== أدوات مساعدة ======
def _ensure_loop():
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

def _dedup_preserve_order(seq):
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            out.append(x); seen.add(x)
    return out

def _load_allowed_ids_from_file(path: str):
    try:
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            out = []
            for item in data:
                try:
                    out.append(int(item))
                except Exception:
                    pass
            return _dedup_preserve_order(out)
    except Exception:
        pass
    return []

def _load_allowed_ids_from_env():
    val = os.environ.get("ALLOWED_IDS", "")
    out = []
    for tok in val.split(","):
        tok = tok.strip()
        if tok.isdigit():
            out.append(int(tok))
    return _dedup_preserve_order(out)

# القائمة النهائية للمستلمين المسموح لهم
ALLOWED_RECIPIENTS = _dedup_preserve_order(
    DEFAULT_ALLOWED_RECIPIENTS + _load_allowed_ids_from_file(ALLOWED_IDS_FILE) + _load_allowed_ids_from_env()
)

# حذف قاعدة البيانات القديمة (> 24 ساعة)
def _maybe_delete_old_db(db_path: str, max_age_seconds: int = 86400):
    if os.path.exists(db_path):
        try:
            age = time.time() - os.path.getmtime(db_path)
            if age > max_age_seconds:
                os.remove(db_path)
                print("🗑️ [DB] تم حذف قاعدة البيانات لأنها أقدم من يوم.")
        except Exception as e:
            print(f"⚠️ [DB] فشل حذف قاعدة البيانات: {e}")

_maybe_delete_old_db(DB_PATH)

# ====== تحميل الجلسات ======
def load_sessions() -> List[str]:
    if not os.path.exists(SESS_FILE):
        return []
    try:
        with open(SESS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            uniq = _dedup_preserve_order([s for s in data if isinstance(s, str) and s.strip()])
            return uniq
    except Exception as e:
        print(f"⚠️ [Sessions] خطأ قراءة {SESS_FILE}: {e}")
    return []

def save_sessions_list(sessions: List[str]):
    try:
        with open(SESS_FILE, "w", encoding="utf-8") as f:
            json.dump(_dedup_preserve_order(sessions), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ [Sessions] خطأ حفظ {SESS_FILE}: {e}")

def validate_sessions(api_id, api_hash, sessions: List[str]) -> List[str]:
    _ensure_loop()
    valid, invalid = [], []
    print("🔎 [Sessions] التحقق من صلاحية الجلسات...")
    for i, s in enumerate(sessions, 1):
        try:
            c = TelegramClient(StringSession(s), api_id, api_hash)
            c.connect()
            c.get_me()
            c.disconnect()
            valid.append(s)
        except AuthKeyUnregisteredError:
            print(f"  ⛔ جلسة #{i} غير مسجلة (AuthKeyUnregisteredError).")
            invalid.append(s)
        except Exception as e:
            print(f"  ⛔ جلسة #{i} خطأ: {e}")
            invalid.append(s)
    if invalid:
        print(f"⚠️ [Sessions] جلسات غير صالحة: {len(invalid)} — سيتم حذفها من {SESS_FILE}")
        new_list = [s for s in sessions if s in valid]
        save_sessions_list(new_list)
    return valid

# ====== قاعدة البيانات ======
def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS seen_messages(
        chat_id INTEGER NOT NULL,
        msg_id  INTEGER NOT NULL,
        PRIMARY KEY(chat_id, msg_id)
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS seen_text_user(
        sender_id INTEGER NOT NULL,
        text_hash TEXT NOT NULL,
        last_ts   INTEGER NOT NULL,
        PRIMARY KEY(sender_id, text_hash)
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS seen_text_global(
        text_hash TEXT NOT NULL,
        last_ts   INTEGER NOT NULL,
        PRIMARY KEY(text_hash)
    )""")
    conn.commit()
    conn.close()
    print("🗄️ [DB] قاعدة البيانات جاهزة.")

init_db()

def get_db_connection():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def is_seen(conn, chat_id: int, msg_id: int) -> bool:
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO seen_messages(chat_id, msg_id) VALUES (?, ?)", (chat_id, msg_id))
    conn.commit()
    cur.execute("SELECT changes()")
    return cur.fetchone()[0] == 0

def _hash_text(text_norm: str) -> str:
    return hashlib.sha1(text_norm.encode("utf-8")).hexdigest()

def is_duplicate_for_user(conn, sender_id: int, text_norm: str, now_ts: int) -> bool:
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

# ====== التطبيع / الأنماط ======
_ARABIC_NORM_MAP = str.maketrans({"أ":"ا","إ":"ا","آ":"ا","ى":"ي","ئ":"ي","ؤ":"و","ة":"ه","ـ":""})
_DIACRITICS = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")
def normalize_ar(text: str) -> str:
    text = text.strip().lower()
    text = _DIACRITICS.sub("", text)
    text = text.translate(_ARABIC_NORM_MAP)
    text = re.sub(r"\s+", " ", text)
    return text

def build_pattern(words: List[str]) -> re.Pattern:
    escaped = [re.escape(normalize_ar(w)) for w in words if w.strip()]
    if not escaped:
        return re.compile(r"^\b\B$")
    pattern = r"(?:(?<=^)|(?<=\s)|(?<=[\.\,\!\?\:\;\-\_\/\(\)\[\]\{\}]))(" + "|".join(escaped) + r")(?:(?=$)|(?=\s)|(?=[\.\,\!\?\:\;\-\_\/\(\)\[\]\{\}]))"
    return re.compile(pattern, flags=re.IGNORECASE)

KEYWORDS_RE = build_pattern(KEYWORDS)
ACADEMIC_ALWAYS_PRINT_NORM = [normalize_ar(w) for w in ACADEMIC_ALWAYS_PRINT]
AD_HINT_KEYWORDS_NORM = [normalize_ar(w) for w in AD_HINT_KEYWORDS]
AD_HINT_REGEXES = [re.compile(p, flags=re.IGNORECASE) for p in AD_HINT_PATTERNS]

def is_academic_request(text_norm: str) -> bool:
    return any(kw in text_norm for kw in ACADEMIC_ALWAYS_PRINT_NORM)

def is_ad_like(text_raw: str, text_norm: str) -> bool:
    if any(rx.search(text_raw) for rx in AD_HINT_REGEXES):
        return True
    if any(kw in text_norm for kw in AD_HINT_KEYWORDS_NORM):
        return True
    return False

# ====== إرسال التنبيه عبر Bot API ======
def send_alert_http(text: str):
    if not ALLOWED_RECIPIENTS:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    CHUNK = 3900
    parts = [text[i:i+CHUNK] for i in range(0, len(text), CHUNK)] or [text]
    for rid in ALLOWED_RECIPIENTS:
        for p in parts:
            try:
                requests.post(url, data={"chat_id": rid, "text": p, "parse_mode": "HTML"}, timeout=10)
            except requests.exceptions.RequestException as e:
                print(f"⚠️ فشل إرسال التنبيه لـ {rid}: {e}")
        time.sleep(0.3)

# ====== أدوات قناة الأرشفة ======
async def ensure_join_archive(client: TelegramClient):
    """يحاول الانضمام للقناة عبر id أو رابط دعوة (مرة واحدة)."""
    if ARCHIVE_CHANNEL_ID:
        try:
            # مجرد محاولة جلب الكيان للتأكد من الانضمام/الصلاحية
            await client.get_entity(ARCHIVE_CHANNEL_ID)
            print("📥 بالفعل عضو في قناة الأرشفة (ID).")
            return True
        except Exception as e:
            print(f"ℹ️ فشل التحقق عبر ID: {e} — سنحاول بالـ INVITE إن توفر.")
    if ARCHIVE_INVITE:
        try:
            invite_part = ARCHIVE_INVITE.split("+", 1)[-1]
            await client(ImportChatInviteRequest(invite_part))
            print("✅ انضمّ بنجاح إلى قناة الأرشفة عبر الدعوة.")
            return True
        except Exception as e:
            # ربما نحن بالفعل أعضاء
            try:
                ent = await client.get_entity(ARCHIVE_INVITE)
                if ent:
                    print("📥 بالفعل عضو في قناة الأرشفة (INVITE).")
                    return True
            except Exception:
                pass
            print(f"⚠️ فشل الانضمام عبر الدعوة: {e}")
    return False

async def archive_message_copy(client: TelegramClient, event):
    """ينسخ نص/وسائط الرسالة إلى قناة الأرشفة حتى لو حُذفت لاحقًا."""
    if not (ARCHIVE_CHANNEL_ID or ARCHIVE_INVITE):
        return

    # تأكد من العضوية
    await ensure_join_archive(client)

    # حضّر نص ملخص + روابط مفيدة
    sender = await event.get_sender()
    sender_id = getattr(sender, "id", 0)
    sender_username = getattr(sender, "username", None)
    if sender_username:
        sender_link = f"https://t.me/{sender_username}"
        sender_label = f"@{sender_username}"
    else:
        sender_link = f"tg://user?id={sender_id}"
        sender_label = f"ID:{sender_id}"

    chat = await event.get_chat()
    chat_username = getattr(chat, "username", None)
    msg_link = f"https://t.me/{chat_username}/{event.id}" if chat_username else "غير متاح"

    header = (f"📥 <b>أرشفة رسالة</b>\n"
              f"• <b>المرسل:</b> <a href=\"{sender_link}\">{html.escape(sender_label)}</a>\n"
              f"• <b>رابط الرسالة:</b> {msg_link}\n")

    # أرسل النص + الوسائط (إن وجدت)
    text_raw = event.raw_text or ""
    safe_text = html.escape(text_raw) if text_raw else ""

    try:
        # إذا فيها وسائط: نزّل ثم أرسل كملف (كي تبقى محفوظة حتى لو حُذفت الأصلية)
        if event.message and (event.message.media or event.photo or event.video or event.document):
            with tempfile.TemporaryDirectory() as tmpd:
                path = await event.download_media(file=tmpd)
                if path:
                    # أرسل هيدر + نص منفصلين لمرونة أكبر
                    if ARCHIVE_CHANNEL_ID:
                        await client.send_message(ARCHIVE_CHANNEL_ID, header, parse_mode="HTML")
                        if safe_text:
                            await client.send_message(ARCHIVE_CHANNEL_ID, f"<code>{safe_text}</code>", parse_mode="HTML")
                        await client.send_file(ARCHIVE_CHANNEL_ID, path)
                    else:
                        # لو ما عُرف ID بنجاح، جرّب عبر invite entity
                        ent = await client.get_entity(ARCHIVE_INVITE)
                        await client.send_message(ent, header, parse_mode="HTML")
                        if safe_text:
                            await client.send_message(ent, f"<code>{safe_text}</code>", parse_mode="HTML")
                        await client.send_file(ent, path)
                    return
        # بدون وسائط: أرسل نصًا فقط
        payload = header + (f"\n<code>{safe_text}</code>" if safe_text else "")
        if ARCHIVE_CHANNEL_ID:
            await client.send_message(ARCHIVE_CHANNEL_ID, payload, parse_mode="HTML")
        else:
            ent = await client.get_entity(ARCHIVE_INVITE)
            await client.send_message(ent, payload, parse_mode="HTML")
    except FloodWaitError as e:
        print(f"⏳ FloodWait أثناء الأرشفة — الانتظار {e.seconds}s")
        time.sleep(e.seconds)
    except Exception as e:
        print(f"⚠️ فشل أرشفة الرسالة: {e}")

# ====== فحص المشرف ======
async def is_sender_admin(event) -> bool:
    try:
        chat = await event.get_chat()
        # قنوات/سوبرجروبات
        if getattr(chat, "megagroup", None) or getattr(chat, "broadcast", None) \
           or getattr(chat, "gigagroup", None) or getattr(chat, "username", None):
            res = await event.client(GetParticipantRequest(chat, event.sender_id))
            part = getattr(res, "participant", None)
            return isinstance(part, (ChannelParticipantAdmin, ChannelParticipantCreator))
        # مجموعات قديمة
        perms = await event.client.get_permissions(chat, event.sender_id)
        return bool(
            getattr(perms, "is_admin", False) or
            getattr(perms, "is_creator", False) or
            getattr(perms, "admin_rights", None)
        )
    except Exception:
        return False

# ====== مُولّد المعالج المرتبط باتصال DB ======
def get_message_handler(db_conn, client: TelegramClient):
    @events.register(events.NewMessage())
    async def handle_message(event):
        try:
            if not event.message or (event.raw_text is None and not event.message.media):
                return

            # (اختياري) حصر على قوائم محددة
            if ALLOWED_CHAT_USERNAMES:
                ch = await event.get_chat()
                uname = getattr(ch, "username", None)
                if not uname or uname.lower() not in [u.lower() for u in ALLOWED_CHAT_USERNAMES]:
                    return

            # فلتر الطول والأسطر
            text_raw = event.raw_text or ""
            if len(text_raw) > MAX_AD_LENGTH:
                return
            if text_raw.count("\n") + 1 > MAX_LINES:
                return

            text_norm = normalize_ar(text_raw)

            # الكلمات المفتاحية
            if not KEYWORDS_RE.search(text_norm):
                return

            # تجاهل رسائل المشرفين
            if await is_sender_admin(event):
                return

            chat_id = event.chat_id
            msg_id = event.message.id

            # منع تكرار نفس الرسالة (id)
            if is_seen(db_conn, chat_id, msg_id):
                return

            # تصنيف إعلاني/أكاديمي
            academic = is_academic_request(text_norm)
            ad_like = is_ad_like(text_raw, text_norm)

            now_ts = int(event.message.date.timestamp())
            sender = await event.get_sender()
            sender_id = getattr(sender, 'id', 0)

            # منع التكرار
            if not academic:
                if is_duplicate_for_user(db_conn, sender_id, text_norm, now_ts):
                    return
                if ad_like and is_duplicate_global(db_conn, text_norm, now_ts):
                    return

            # روابط
            if getattr(sender, "username", None):
                sender_link = f"https://t.me/{sender.username}"
                sender_label = f"@{sender.username}"
            else:
                sender_link = f"tg://user?id={sender_id}"
                sender_label = f"ID:{sender_id}"

            chat = await event.get_chat()
            msg_link = "غير متاح"
            if getattr(chat, "username", None):
                msg_link = f"https://t.me/{chat.username}/{msg_id}"

            safe_text = html.escape(text_raw) if text_raw else ""
            alert = (
                f"<b>ID المرسل :</b> {sender_id}\n"
                f"<b>نص الرساله :</b>\n<code>{safe_text}</code>\n"
                f"<b>رابط الرساله :</b> {msg_link}\n"
                f"<b>رابط يوزر المرسل :</b> <a href=\"{sender_link}\">{html.escape(sender_label)}</a>"
            )

            # أرسل التنبيه
            send_alert_http(alert)

            # أرشِف نسخة مستقلة (نص/وسائط)
            await archive_message_copy(client, event)

            print(f"🚨 تنبيه (ID:{sender_id})")

        except Exception as e:
            print(f"⚠️ خطأ في معالجة رسالة: {e}")

    return handle_message

# ====== مُشغّل كل عميل في Thread ======
def client_runner(session_str: str, idx: int):
    name = f"client-{idx}"
    backoff = 2
    max_backoff = 60
    db_conn = get_db_connection()
    while True:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
            client.start()

            # انضم لقناة الأرشفة (إن أمكن)
            loop.run_until_complete(ensure_join_archive(client))

            # اربط المعالج
            client.add_event_handler(get_message_handler(db_conn, client), events.NewMessage())
            print(f"✅ [{name}] جاهز — يبدأ الاستماع")
            send_alert_http(f"<b>✅ [{name}] متصل — بدأ الاستماع للرسائل.</b>")

            client.run_until_disconnected()
            print(f"ℹ️ [{name}] خرج — إعادة محاولة خلال {backoff}s")
        except (OSError, ConnectionError) as e:
            print(f"⛔ [{name}] انقطاع: {e} — إعادة محاولة خلال {backoff}s")
        except AuthKeyUnregisteredError:
            print(f"⛔ [{name}] جلسة غير صالحة — سيتم تجاهلها.")
            break
        except FloodWaitError as e:
            wait_time = getattr(e, "seconds", 10)
            print(f"⏳ [{name}] FloodWait — الانتظار {wait_time} ثانية")
            time.sleep(wait_time)
            continue
        except Exception as e:
            print(f"⛔ [{name}] خطأ: {e} — إعادة محاولة خلال {backoff}s")
        time.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)

# ====== التشغيل ======
def main():
    _ensure_loop()
    print("🚀 بدء تشغيل البوت...")
    print(f"📤 [Recipients] IDs المسموح لها: {ALLOWED_RECIPIENTS}")

    sessions = load_sessions()
    print(f"📦 [Sessions] تم تحميل {len(sessions)} جلسة.")
    if not sessions:
        print("❌ لا توجد جلسات — أضف StringSession إلى sessions.json ثم أعد التشغيل.")
        return

    if VALIDATE_SESSIONS:
        sessions = validate_sessions(API_ID, API_HASH, sessions)
        if not sessions:
            print("❌ كل الجلسات غير صالحة — أنشئ جلسات جديدة.")
            return
    else:
        print("⚠️ تم تخطي فحص الجلسات المسبق — تشغيل مباشر.")

    send_alert_http(
        "<b>✅ البوت يعمل — فلاتر:</b> طول ≤ "
        f"{MAX_AD_LENGTH} حرف & ≤ {MAX_LINES} سطر — تجاهل المشرفين — "
        "تكرار ذكي (الإعلانات تُحجب عند التكرار، الطلبات الأكاديمية تُطبع دائمًا) — أرشفة نص/وسائط."
    )

    threads = []
    for i, s in enumerate(sessions, 1):
        t = threading.Thread(target=client_runner, args=(s, i), daemon=True, name=f"tg-client-{i}")
        t.start()
        threads.append(t)
        print(f"✅ تشغيل جلسة #{i}")
        time.sleep(0.5)  # تهدئة بسيطة

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("👋 تم الإيقاف.")

if __name__ == "__main__":
    main()
