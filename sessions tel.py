# -*- coding: utf-8 -*-
import os
import json
import getpass
import sys
import time  # --- إضافة: تم استيراد مكتبة الوقت للتحكم في الانتظار ---
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneNumberInvalidError

# --- إعدادات أساسية ---
API_ID = 27365071
API_HASH = '4ab2f70c153a54c1738ba2e81e9ea822'
SESS_FILE = "sessions.json"
ALLOWED_IDS_FILE = "allowed_ids.json"
WIDTH = 60  # عرض الواجهة النصية

# --- إعدادات الألوان والواجهة ---
class C:
    R = "\033[31m"; G = "\033[32m"; Y = "\033[33m"; B = "\033[34m"; M = "\033[35m"; C = "\033[36m"
    BR = "\033[91m"; BG = "\033[92m"; BY = "\033[93m"; BB = "\033[94m"; BM = "\033[95m"; BC = "\033[96m"
    W = "\033[97m"; BOLD = "\033[1m"; RESET = "\033[0m"
    CLS = 'clear' if os.name != 'nt' else 'cls'

# --- دوال مساعدة للواجهة الرسومية (UI Helpers) ---
def clear_screen():
    os.system(C.CLS)

def print_header(title):
    clear_screen()
    print(C.BB + "╔" + "═" * (WIDTH - 2) + "╗" + C.RESET)
    clean_title = f" {title} "
    print(C.BB + "║" + C.BOLD + C.W + clean_title.center(WIDTH - 2) + C.BB + "║" + C.RESET)
    print(C.BB + "╚" + "═" * (WIDTH - 2) + "╝" + C.RESET)
    print()

def print_success(message):
    print(f"{C.G}✅ {message}{C.RESET}")

def print_error(message):
    print(f"{C.R}❌ {message}{C.RESET}")

def print_info(message):
    print(f"{C.C}ℹ️  {message}{C.RESET}")

def get_input(prompt):
    return input(f"{C.Y}❯ {prompt}:{C.RESET} ")

def wait_for_enter():
    getpass.getpass(prompt=f'\n{C.M}[ اضغط Enter للعودة... ]{C.RESET}')

# --- دوال مساعدة عامة (ملفات) ---
def _dedup_list(items):
    seen, out = set(), []
    for x in items:
        try:
            val = type(items[0])(x) if items else x
            if val and val not in seen:
                out.append(val)
                seen.add(val)
        except (ValueError, TypeError): continue
    return out

def _load_json_list(path, data_type=str):
    if not os.path.exists(path): return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            items = [data_type(item) for item in data if isinstance(data, list)]
            return _dedup_list(items)
    except Exception: return []

def _save_json_list(path, items, label):
    items = _dedup_list(items)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    print_success(f"تم حفظ {len(items)} {label} في ملف {path}")

# --- دوال إدارة الجلسات (مُعدّلة لترجع حالة النجاح/الفشل) ---
def load_sessions(): return _load_json_list(SESS_FILE, str)
def save_sessions(sessions): _save_json_list(SESS_FILE, sessions, "جلسة")

def add_session():
    """إضافة جلسة جديدة. ترجع True عند النجاح و False عند الفشل."""
    print_header("إضافة جلسة تيليجرام جديدة")
    print_info("مثال لأرقام الهواتف: +9665xxxxxxxx أو +9677xxxxxxx")
    phone = get_input("أدخل رقم الهاتف مع رمز الدولة").strip()
    if not phone:
        print_error("لم يتم إدخال رقم."); return False

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    try:
        client.connect()
        if not client.is_user_authorized():
            try:
                client.send_code_request(phone)
            except PhoneNumberInvalidError:
                print_error("رقم الهاتف الذي أدخلته غير صحيح."); return False

            code = get_input("أدخل كود التحقق (OTP) الذي وصلك").strip()
            try:
                client.sign_in(phone=phone, code=code)
            except PhoneCodeInvalidError:
                print_error("كود التحقق غير صحيح."); return False
            except SessionPasswordNeededError:
                pwd = getpass.getpass("🔒 الحساب محمي، أدخل كلمة مرور التحقق بخطوتين: ")
                client.sign_in(password=pwd)

        session_str = client.session.save()
        sessions = load_sessions()
        sessions.append(session_str)
        save_sessions(sessions)
        return True
    except Exception as e:
        print_error(f"فشل إضافة الجلسة: {e}")
        return False
    finally:
        if client.is_connected():
            client.disconnect()

def list_sessions():
    """عرض الجلسات. تنجح دائمًا."""
    print_header("قائمة الجلسات المحفوظة")
    sessions = load_sessions()
    if not sessions:
        print_info("لا توجد أي جلسات محفوظة حاليًا."); return True
    
    for i, s in enumerate(sessions, 1):
        print(f"{C.B}{i:>2}. {C.RESET}{s[:10]}...{s[-10:]}")
    print(f"\n{C.M}--- الإجمالي: {len(sessions)} جلسة ---{C.RESET}")
    return True

def remove_session():
    """حذف جلسة. ترجع True عند النجاح و False عند الفشل."""
    print_header("حذف جلسة")
    sessions = load_sessions()
    if not sessions:
        print_info("لا توجد جلسات لحذفها."); return True
    
    list_sessions()
    try:
        idx_str = get_input("\nأدخل رقم الجلسة التي تريد حذفها")
        idx = int(idx_str)
        if 1 <= idx <= len(sessions):
            sessions.pop(idx - 1)
            save_sessions(sessions)
            return True
        else:
            print_error("الرقم الذي أدخلته خارج النطاق الصحيح.")
            return False
    except (ValueError, TypeError):
        print_error("إدخال غير صالح. الرجاء إدخال رقم فقط.")
        return False

# --- دوال إدارة المستخدمين المسموح لهم (مُعدّلة لترجع حالة النجاح/الفشل) ---
def load_allowed_ids(): return _load_json_list(ALLOWED_IDS_FILE, int)
def save_allowed_ids(ids): _save_json_list(ALLOWED_IDS_FILE, ids, "ID مسموح به")

def add_allowed_id():
    """إضافة ID. ترجع True عند النجاح و False عند الفشل."""
    print_header("إضافة ID جديد")
    print_info("سيتمكن هذا المستخدم من استقبال تنبيهات البوت.")
    try:
        uid_str = get_input("أدخل الـ ID الرقمي للمستخدم")
        uid = int(uid_str)
        ids = load_allowed_ids()
        if uid in ids:
            print_info(f"هذا الـ ID ({uid}) موجود بالفعل في القائمة.")
        else:
            ids.append(uid)
            save_allowed_ids(ids)
        return True
    except (ValueError, TypeError):
        print_error("إدخال غير صالح. يجب أن يكون الـ ID رقمًا صحيحًا.")
        return False

def list_allowed_ids():
    """عرض IDs. تنجح دائمًا."""
    print_header("قائمة IDs المستخدمين المسموح لهم")
    ids = load_allowed_ids()
    if not ids:
        print_info("لا توجد أي IDs محفوظة حاليًا."); return True

    for i, uid in enumerate(ids, 1):
        print(f"{C.G}{i:>2}. {C.RESET}{uid}")
    print(f"\n{C.M}--- الإجمالي: {len(ids)} مستخدم ---{C.RESET}")
    return True

def remove_allowed_id():
    """حذف ID. يرجع True عند النجاح و False عند الفشل."""
    print_header("حذف ID مسموح به")
    ids = load_allowed_ids()
    if not ids:
        print_info("لا توجد IDs لحذفها."); return True

    list_allowed_ids()
    try:
        val_str = get_input("\nأدخل رقم القائمة أو الـID نفسه للحذف")
        val = int(val_str)
        
        removed_id = None
        if 1 <= val <= len(ids):
            removed_id = ids.pop(val - 1)
        elif val in ids:
            ids.remove(val)
            removed_id = val
        
        if removed_id:
            save_allowed_ids(ids)
            return True
        else:
            print_error("لم يتم العثور على القيمة المدخلة في القائمة.")
            return False
    except (ValueError, TypeError):
        print_error("إدخال غير صالح. الرجاء إدخال رقم فقط.")
        return False

# --- القائمة الرئيسية والتنفيذ ---
def main_menu():
    """
    عرض القائمة الرئيسية وتوجيه المستخدم.
    --- تحسين: تتعامل مع حالات النجاح والفشل لتقرر هل تنتظر المستخدم أم لا.
    """
    menu_options = {
        "1": {"text": "إضافة جلسة تيليجرام جديدة", "func": add_session},
        "2": {"text": "عرض كل الجلسات المحفوظة", "func": list_sessions},
        "3": {"text": "حذف جلسة من القائمة", "func": remove_session},
        "4": {"text": "إضافة ID مستخدم مسموح له", "func": add_allowed_id},
        "5": {"text": "عرض كل IDs المستخدمين", "func": list_allowed_ids},
        "6": {"text": "حذف ID مستخدم من القائمة", "func": remove_allowed_id},
        "7": {"text": "الخروج من البرنامج", "func": lambda: sys.exit(0)}
    }
    
    while True:
        print_header("🛠️ مدير الجلسات والمستخدمين 🛠️")
        for key, value in menu_options.items():
            print(f"  {C.BY}{key}) {C.W}{value['text']}{C.RESET}")
        
        print("\n" + C.BB + "─" * WIDTH + C.RESET)
        choice = get_input("الرجاء اختيار رقم من القائمة")

        action = menu_options.get(choice)
        if action:
            if choice == "7": # التعامل مع الخروج مباشرة
                action["func"]()

            was_successful = action["func"]()
            
            if was_successful:
                # إذا نجحت العملية، انتظر ضغطة Enter كالمعتاد
                wait_for_enter()
            else:
                # إذا فشلت العملية، انتظر 3 ثوانٍ فقط ثم عد للقائمة
                time.sleep(3)
        else:
            print_error("خيار غير صحيح. الرجاء المحاولة مرة أخرى.")
            time.sleep(2) # انتظر ثانيتين عند الخطأ في الاختيار

if __name__ == "__main__":
    try:
        main_menu()
    except (KeyboardInterrupt, SystemExit):
        # رسالة وداع عند الخروج
        print(f"\n{C.Y}👋 مع السلامة!{C.RESET}")
