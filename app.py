# app.py
# يشغّل بوتك الموجود في "main tel.py" داخل خيط خلفي + يعرّف سيرفر Flask للصحة/الإيقاظ

import threading
import time
from flask import Flask, jsonify
from importlib.machinery import SourceFileLoader

# نحمل ملفك الرئيسي حتى لو اسمه فيه مسافة
main_mod = SourceFileLoader("main_tel", "main tel.py").load_module()

app = Flask(__name__)
_started = False
_lock = threading.Lock()

def _start_bot_once():
    global _started
    with _lock:
        if not _started:
            t = threading.Thread(target=main_mod.main, name="tg-bot", daemon=True)
            t.start()
            _started = True

# نشغّل البوت عند استيراد التطبيق
_start_bot_once()

@app.get("/")
def root():
    # مسار صحة/إيقاظ
    return jsonify({"status": "ok", "bot_running": True}), 200

@app.get("/health")
def health():
    return "OK", 200

# للتشغيل المحلي فقط: python app.py
if __name__ == "__main__":
    # تشغيل Flask محليًا (Render سيشغّل عبر gunicorn)
    port = 8000
    print(f"Starting Flask dev server on http://127.0.0.1:{port}")
    _start_bot_once()
    app.run(host="0.0.0.0", port=port)
