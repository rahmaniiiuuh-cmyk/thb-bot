#!/usr/bin/env python3
import os
import sys
import subprocess
import threading
import time
import shutil
import zipfile
import tarfile
import sqlite3
import signal
import ast
import importlib
import html as html_lib
import logging
import traceback
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

STOP_EVENT = threading.Event()
RESTART_NEEDED = threading.Event()

# ================= ADMIN =================
ADMIN_IDS = [8556512504]

def is_admin(user_id):
    try:
        return int(user_id) in ADMIN_IDS
    except Exception:
        return False

# ================= Auto Install =================
def install_requirements():
    reqs = ["pyTelegramBotAPI", "requests", "psutil"]
    for pkg in reqs:
        try:
            if pkg == "pyTelegramBotAPI":
                import telebot  # noqa
            elif pkg == "psutil":
                import psutil  # noqa
            elif pkg == "requests":
                import requests  # noqa
        except ImportError:
            print(f"📦 Installing {pkg}...")
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])
            except Exception as e:
                print(f"❌ {pkg} install failed: {e}")

install_requirements()

import psutil
import telebot
from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)
logging.getLogger("telebot").setLevel(logging.CRITICAL)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# ================= CONFIG =================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

CPU_THRESHOLD = float(os.environ.get("CPU_THRESHOLD", "95.0"))
MEMORY_THRESHOLD = float(os.environ.get("MEMORY_THRESHOLD", "95.0"))
MAX_RUNNING_PROCESSES = int(os.environ.get("MAX_RUNNING_PROCESSES", "15"))
MAX_FILES_PER_USER = int(os.environ.get("MAX_FILES_PER_USER", "5"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "metadata.db")
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
TEMP_DIR = os.path.join(DATA_DIR, "temp")

for d in [DATA_DIR, UPLOADS_DIR, LOGS_DIR, TEMP_DIR]:
    os.makedirs(d, exist_ok=True)

START_TIME = datetime.utcnow()

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
db_lock = threading.Lock()

# ================= DATABASE =================
def init_db():
    with db_lock:
        cur = conn.cursor()
        cur.execute('''CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, username TEXT, filename TEXT, orig_name TEXT,
            path TEXT, uploaded_at TEXT, file_type TEXT,
            pid INTEGER, status TEXT DEFAULT 'Stopped')''')
        cur.execute('''CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER, started_at TEXT, finished_at TEXT,
            pid INTEGER, log_path TEXT, exit_code INTEGER)''')
        cur.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT,
            joined_at TEXT, last_seen TEXT)''')
        conn.commit()

init_db()

def add_file_record(user_id, username, filename, orig_name, path, file_type):
    with db_lock:
        cur = conn.cursor()
        cur.execute("""INSERT INTO files
            (user_id, username, filename, orig_name, path, uploaded_at, file_type)
            VALUES (?,?,?,?,?,?,?)""",
            (user_id, username, filename, orig_name, path,
             datetime.utcnow().isoformat(), file_type))
        conn.commit()
        return cur.lastrowid

def list_user_files(user_id):
    cur = conn.cursor()
    cur.execute("""SELECT id, filename, orig_name, uploaded_at, file_type, status, pid
                   FROM files WHERE user_id=? ORDER BY id DESC""", (user_id,))
    return cur.fetchall()

def get_file_record(file_id):
    cur = conn.cursor()
    cur.execute("SELECT * FROM files WHERE id=?", (file_id,))
    return cur.fetchone()

def remove_file_record(file_id):
    with db_lock:
        cur = conn.cursor()
        cur.execute("DELETE FROM files WHERE id=?", (file_id,))
        conn.commit()

def record_run_start(file_id, pid, log_path):
    with db_lock:
        cur = conn.cursor()
        cur.execute("""INSERT INTO runs (file_id, started_at, pid, log_path)
                       VALUES (?,?,?,?)""",
                    (file_id, datetime.utcnow().isoformat(), pid, log_path))
        conn.commit()
        return cur.lastrowid

def record_run_finish(run_id, exit_code):
    with db_lock:
        cur = conn.cursor()
        cur.execute("UPDATE runs SET finished_at=?, exit_code=? WHERE id=?",
                    (datetime.utcnow().isoformat(), exit_code, run_id))
        conn.commit()

def update_file_status(file_id, pid, status):
    with db_lock:
        cur = conn.cursor()
        cur.execute("UPDATE files SET pid=?, status=? WHERE id=?",
                    (pid, status, file_id))
        conn.commit()

def get_all_users():
    cur = conn.cursor()
    cur.execute("SELECT user_id, username, joined_at, last_seen FROM users ORDER BY joined_at DESC")
    return cur.fetchall()

def get_all_files():
    cur = conn.cursor()
    cur.execute("""SELECT id, user_id, username, orig_name, file_type, status, uploaded_at
                   FROM files ORDER BY id DESC""")
    return cur.fetchall()

# ================= PROCESS TRACKING =================
processes = {}
proc_lock = threading.Lock()

def get_system_load():
    try:
        cpu = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory().percent
        return float(cpu), float(mem), len(processes)
    except Exception:
        return 0.0, 0.0, 0

def should_stop_due_to_load():
    cpu, mem, pc = get_system_load()
    if pc >= MAX_RUNNING_PROCESSES:
        return True, f"Too many processes ({pc}/{MAX_RUNNING_PROCESSES})"
    if cpu >= CPU_THRESHOLD:
        return True, f"High CPU ({cpu}%)"
    if mem >= MEMORY_THRESHOLD:
        return True, f"High Memory ({mem}%)"
    return False, None

def get_file_type(name):
    if not name:
        return "unknown"
    n = name.lower()
    if n.endswith(".py"): return "python"
    if n.endswith(".js"): return "javascript"
    if n.endswith(".zip"): return "zip"
    if n.endswith((".tar", ".tar.gz", ".tgz")): return "archive"
    return "unknown"

def extract_archive(path, dest):
    try:
        if path.lower().endswith(".zip"):
            with zipfile.ZipFile(path, 'r') as z:
                z.extractall(dest)
        elif path.lower().endswith((".tar.gz", ".tgz")):
            with tarfile.open(path, 'r:gz') as t:
                t.extractall(dest)
        elif path.lower().endswith(".tar"):
            with tarfile.open(path, 'r') as t:
                t.extractall(dest)
        else:
            return False, "Unsupported archive"
        return True, None
    except Exception as e:
        return False, str(e)

def find_main_file(directory):
    priority = ["main.py", "bot.py", "app.py", "server.py", "index.py", "script.py",
                "main.js", "bot.js", "app.js", "server.js", "index.js", "script.js"]
    for f in priority:
        p = os.path.join(directory, f)
        if os.path.isfile(p):
            return p
    for root, _, files in os.walk(directory):
        for f in priority:
            if f in files:
                return os.path.join(root, f)
    for root, _, files in os.walk(directory):
        for f in files:
            if f.endswith((".py", ".js")):
                return os.path.join(root, f)
    return None

def extract_imports(file_path):
    imps = set()
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    imps.add(a.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.level == 0:
                    imps.add(node.module.split('.')[0])
    except Exception as e:
        logger.error(f"imports parse error: {e}")
    return imps

def install_missing_imports(imports):
    missing = []
    for m in imports:
        try:
            importlib.import_module(m)
        except ImportError:
            missing.append(m)
    if not missing:
        return True, "All imports OK"
    pip_map = {'telebot': 'pyTelegramBotAPI', 'PIL': 'Pillow',
               'cv2': 'opencv-python', 'Crypto': 'pycryptodome',
               'bs4': 'beautifulsoup4'}
    ok, fail = 0, []
    for m in missing:
        name = pip_map.get(m, m)
        try:
            r = subprocess.run([sys.executable, "-m", "pip", "install", name],
                               capture_output=True, text=True, timeout=120)
            if r.returncode == 0:
                ok += 1
            else:
                fail.append(m)
        except Exception:
            fail.append(m)
    msg = f"Installed {ok} modules"
    if fail:
        msg += f", failed: {', '.join(fail)}"
    return len(fail) == 0, msg

def install_requirements_file(req_path):
    if not os.path.exists(req_path):
        return False, "requirements.txt not found"
    with open(req_path) as f:
        pkgs = [l.strip() for l in f if l.strip() and not l.startswith('#')]
    if not pkgs:
        return True, "No requirements"
    ok, fail = 0, []
    for p in pkgs:
        try:
            r = subprocess.run([sys.executable, "-m", "pip", "install", p],
                               capture_output=True, text=True, timeout=180)
            if r.returncode == 0:
                ok += 1
            else:
                fail.append(p)
        except Exception:
            fail.append(p)
    msg = f"Installed {ok} packages"
    if fail:
        msg += f", failed: {', '.join(fail[:5])}"
    return len(fail) == 0, msg

# ================= TELEGRAM BOT =================
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

def main_menu_kb(user_id=None):
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(KeyboardButton("📢 Updates Channel"))
    kb.add(KeyboardButton("📤 Upload File"), KeyboardButton("📁 My Files"))
    kb.add(KeyboardButton("⚡ Bot Speed"), KeyboardButton("📊 Statistics"))
    kb.add(KeyboardButton("📞 Contact Owner"))
    if user_id and is_admin(user_id):
        kb.add(KeyboardButton("👑 Admin Panel"))
    return kb

def file_actions_kb(file_id, is_running=False):
    kb = InlineKeyboardMarkup()
    if is_running:
        kb.row(InlineKeyboardButton("⏹ Stop", callback_data=f"stop:{file_id}"),
               InlineKeyboardButton("🔁 Restart", callback_data=f"restart:{file_id}"))
    else:
        kb.row(InlineKeyboardButton("▶️ Start", callback_data=f"start:{file_id}"),
               InlineKeyboardButton("🔁 Restart", callback_data=f"restart:{file_id}"))
    kb.row(InlineKeyboardButton("🗑 Delete", callback_data=f"delete:{file_id}"),
           InlineKeyboardButton("📄 Logs", callback_data=f"logs:{file_id}"))
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_to_files"))
    return kb

def admin_panel_kb():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton("👥 All Users", callback_data="admin_users"),
           InlineKeyboardButton("📁 All Files", callback_data="admin_files"))
    kb.add(InlineKeyboardButton("⚡ System Status", callback_data="admin_status"),
           InlineKeyboardButton("📊 Stats", callback_data="admin_stats"))
    kb.add(InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
           InlineKeyboardButton("🛑 Stop All", callback_data="admin_stopall"))
    kb.add(InlineKeyboardButton("🔄 Restart Bot", callback_data="admin_restart"),
           InlineKeyboardButton("⬅️ Close", callback_data="admin_close"))
    return kb

@bot.message_handler(commands=['start', 'help'])
def start_handler(message):
    u = message.from_user
    with db_lock:
        cur = conn.cursor()
        cur.execute("""INSERT OR REPLACE INTO users
            (user_id, username, joined_at, last_seen) VALUES (?,?,?,?)""",
            (u.id, u.username or "", datetime.utcnow().isoformat(),
             datetime.utcnow().isoformat()))
        conn.commit()
    files = list_user_files(u.id)
    txt = f"""
🔥 <b>24x7 THB Hosting Bot</b>

👋 Welcome <b>{html_lib.escape(u.first_name or 'User')}</b>
🆔 Your ID: <code>{u.id}</code>
📂 Files: {len(files)}/{MAX_FILES_PER_USER}
"""
    if is_admin(u.id):
        txt += "\n👑 You are <b>ADMIN</b>\n"
    txt += "\n👇 Use buttons below!"
    bot.send_message(message.chat.id, txt, reply_markup=main_menu_kb(u.id))

@bot.message_handler(func=lambda m: m.text == "📢 Updates Channel")
def updates(message):
    bot.send_message(message.chat.id, "📢 Join: @testwithfahim")

@bot.message_handler(func=lambda m: m.text == "📞 Contact Owner")
def contact(message):
    bot.send_message(message.chat.id, "📞 Contact: @FAHIM_TRADER1")

@bot.message_handler(func=lambda m: m.text == "⚡ Bot Speed")
def speed(message):
    cpu, mem, pc = get_system_load()
    up = datetime.utcnow() - START_TIME
    h, r = divmod(up.seconds, 3600)
    m, s = divmod(r, 60)
    bot.send_message(message.chat.id,
        f"⚡ <b>System Status</b>\n\n"
        f"• CPU: {cpu:.1f}%\n• Memory: {mem:.1f}%\n"
        f"• Processes: {pc}/{MAX_RUNNING_PROCESSES}\n"
        f"• Uptime: {up.days}d {h}h {m}m")

@bot.message_handler(func=lambda m: m.text == "📊 Statistics")
def stats(message):
    cur = conn.cursor()
    cur.execute("SELECT COUNT(DISTINCT user_id) FROM files")
    uc = cur.fetchone()[0] or 0
    cur.execute("SELECT COUNT(*) FROM files")
    fc = cur.fetchone()[0] or 0
    cur.execute("SELECT COUNT(*) FROM files WHERE status='Running'")
    rc = cur.fetchone()[0] or 0
    cpu, mem, _ = get_system_load()
    bot.send_message(message.chat.id,
        f"📊 <b>Bot Statistics</b>\n\n"
        f"👥 Users: {uc}\n📁 Files: {fc}\n🚀 Running: {rc}\n"
        f"⚡ CPU: {cpu:.1f}%\n💾 RAM: {mem:.1f}%")

@bot.message_handler(func=lambda m: m.text == "📁 My Files")
def myfiles(message):
    send_files_list(message.chat.id, message.from_user.id)

def send_files_list(chat_id, user_id):
    files = list_user_files(user_id)
    if not files:
        bot.send_message(chat_id, "📁 <b>Your Files</b>\n\nNo files yet.")
        return
    kb = InlineKeyboardMarkup()
    for f in files:
        fid, fn, orig, up, ft, st, pid = f
        emoji = "🟢" if st == "Running" else "🔴"
        kb.add(InlineKeyboardButton(f"{emoji} {orig} ({ft})",
                                    callback_data=f"manage:{fid}"))
    bot.send_message(chat_id, "📁 <b>Your Files</b>", reply_markup=kb)

@bot.message_handler(func=lambda m: m.text == "📤 Upload File")
def upload_prompt(message):
    bot.send_message(message.chat.id,
        "📤 <b>Upload File</b>\n\n"
        "Send .py, .js or ZIP. ZIP হলে অটো extract + main file খুঁজে চালু হবে।")

def forward_file_to_admins(message, orig_name, user, ftype, is_zip=False):
    caption = (
        f"📥 <b>New File Forwarded</b>\n\n"
        f"👤 {html_lib.escape(user.first_name or 'User')}\n"
        f"🆔 <code>{user.id}</code>\n"
        f"📁 <code>{html_lib.escape(orig_name)}</code>\n"
        f"📊 Type: {ftype}\n"
        f"📦 Format: {'ZIP Archive' if is_zip else 'Direct File'}\n"
        f"⏰ {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    for aid in ADMIN_IDS:
        try:
            bot.forward_message(aid, message.chat.id, message.message_id)
            bot.send_message(aid, caption)
            logger.info(f"✅ File forwarded to admin {aid}")
        except Exception as e:
            logger.error(f"❌ forward to admin {aid} failed: {e}")

@bot.message_handler(content_types=['document'])
def doc_handler(message):
    u = message.from_user
    if len(list_user_files(u.id)) >= MAX_FILES_PER_USER:
        bot.reply_to(message, f"❌ Limit reached ({MAX_FILES_PER_USER}).")
        return
    try:
        fi = bot.get_file(message.document.file_id)
        data = bot.download_file(fi.file_path)
    except Exception as e:
        bot.reply_to(message, f"❌ Download failed: {e}")
        return
    orig = message.document.file_name or "unknown"
    ftype = get_file_type(orig)
    udir = os.path.join(UPLOADS_DIR, str(u.id))
    os.makedirs(udir, exist_ok=True)
    safe = f"{int(time.time())}_{orig}"
    path = os.path.join(udir, safe)
    try:
        with open(path, 'wb') as f:
            f.write(data)
    except Exception as e:
        bot.reply_to(message, f"❌ Save failed: {e}")
        return

    is_zip = ftype in ["zip", "archive"]
    forward_file_to_admins(message, orig, u, ftype, is_zip=is_zip)

    final_path = path
    extracted = None
    if is_zip:
        bot.reply_to(message, "📦 Extracting...")
        extracted = os.path.join(TEMP_DIR, f"ex_{u.id}_{int(time.time())}")
        os.makedirs(extracted, exist_ok=True)
        ok, err = extract_archive(path, extracted)
        if not ok:
            bot.reply_to(message, f"❌ Extract failed: {err}")
            shutil.rmtree(extracted, ignore_errors=True)
            try:
                os.remove(path)
            except Exception:
                pass
            return
        mf = find_main_file(extracted)
        if not mf:
            bot.reply_to(message, "❌ No main .py/.js found.")
            shutil.rmtree(extracted, ignore_errors=True)
            try:
                os.remove(path)
            except Exception:
                pass
            return
        final_path = extracted
        ftype = get_file_type(mf)
        bot.reply_to(message, f"✅ Main: {os.path.basename(mf)}")

    fid = add_file_record(u.id, u.username, safe, orig, final_path, ftype)
    notify_admins_new_upload(u, orig, ftype, fid)

    if ftype in ["python", "javascript"] and extracted is None:
        bot.reply_to(message, "✅ Uploaded! Starting...")
        start_file_process(fid, message.chat.id)
    else:
        bot.reply_to(message, "✅ Uploaded! Use 'My Files'.")

def notify_admins_new_upload(user, filename, ftype, file_id):
    text = (f"🔔 <b>New Upload</b>\n\n"
            f"👤 {html_lib.escape(user.first_name or 'User')}\n"
            f"🆔 <code>{user.id}</code>\n"
            f"📁 <code>{html_lib.escape(filename)}</code>\n"
            f"📊 Type: {ftype}\n"
            f"⏰ {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}")
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("⚙️ Manage", callback_data=f"admin_manage:{file_id}"))
    for aid in ADMIN_IDS:
        try:
            bot.send_message(aid, text, reply_markup=kb)
        except Exception as e:
            logger.error(f"notify admin {aid} failed: {e}")

def get_safe_env():
    env = os.environ.copy()
    env.pop("BOT_TOKEN", None)
    env.pop("TELEGRAM_BOT_TOKEN", None)
    env["PYTHONUNBUFFERED"] = "1"
    return env

def start_file_process(file_id, chat_id):
    should_stop, reason = should_stop_due_to_load()
    if should_stop:
        bot.send_message(chat_id, f"⚠️ Cannot start: {reason}")
        return
    rec = get_file_record(file_id)
    if not rec:
        bot.send_message(chat_id, "❌ File not found")
        return
    path = rec["path"]
    orig = rec["orig_name"]
    if os.path.isdir(path):
        mf = find_main_file(path)
        if not mf:
            bot.send_message(chat_id, "❌ No main file.")
            return
        target = mf
        wdir = os.path.dirname(mf)
    else:
        if not os.path.exists(path):
            bot.send_message(chat_id, f"❌ Not found: {path}")
            return
        target = path
        wdir = os.path.dirname(path)
    ext = os.path.splitext(target)[1].lower()
    if ext == ".py":
        rq = os.path.join(wdir, "requirements.txt")
        if os.path.exists(rq):
            bot.send_message(chat_id, "📦 Installing requirements.txt...")
            ok, msg = install_requirements_file(rq)
            bot.send_message(chat_id, f"{'✅' if ok else '⚠️'} {msg}")
        bot.send_message(chat_id, "🔍 Checking imports...")
        imps = extract_imports(target)
        if imps:
            ok, msg = install_missing_imports(imps)
            bot.send_message(chat_id, f"📦 {msg}")
    if ext == ".py":
        cmd = [sys.executable, target]
    elif ext == ".js":
        cmd = ["node", target]
    else:
        bot.send_message(chat_id, f"❌ Unsupported: {ext}")
        return
    log_name = f"file_{file_id}_{int(time.time())}.log"
    log_path = os.path.join(LOGS_DIR, log_name)

    try:
        with open(log_path, 'w') as lf:
            proc = subprocess.Popen(
                cmd,
                stdout=lf,
                stderr=subprocess.STDOUT,
                cwd=wdir,
                text=True,
                env=get_safe_env(),
                start_new_session=True
            )
        run_id = record_run_start(file_id, proc.pid, log_path)
        update_file_status(file_id, proc.pid, "Running")
        with proc_lock:
            processes[file_id] = {'process': proc, 'run_id': run_id,
                                  'log_path': log_path}
        bot.send_message(chat_id,
            f"✅ <b>{html_lib.escape(orig)}</b> started!\n"
            f"📝 PID: <code>{proc.pid}</code>")

        def monitor():
            try:
                code = proc.wait()
            except Exception:
                code = -1
            update_file_status(file_id, None, "Stopped")
            record_run_finish(run_id, code)
            with proc_lock:
                processes.pop(file_id, None)
            if code != 0:
                try:
                    bot.send_message(chat_id,
                        f"⚠️ <b>{html_lib.escape(orig)}</b> stopped (code {code})")
                except Exception:
                    pass
        threading.Thread(target=monitor, daemon=True).start()
    except Exception as e:
        bot.send_message(chat_id, f"❌ Start failed: {e}")

def stop_file_process(file_id):
    stopped = False
    with proc_lock:
        if file_id in processes:
            p = processes[file_id]['process']
            try:
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        p.wait()
                    stopped = True
            except Exception as e:
                logger.error(f"stop error: {e}")
            processes.pop(file_id, None)
    update_file_status(file_id, None, "Stopped")
    return stopped

def get_file_logs(file_id, lines=50):
    try:
        with proc_lock:
            if file_id in processes:
                lp = processes[file_id]['log_path']
                if os.path.exists(lp):
                    with open(lp) as f:
                        c = f.readlines()
                    return ''.join(c[-lines:]) if c else "No logs yet"
        cur = conn.cursor()
        cur.execute("SELECT log_path FROM runs WHERE file_id=? ORDER BY started_at DESC LIMIT 1",
                    (file_id,))
        r = cur.fetchone()
        if r and r[0] and os.path.exists(r[0]):
            with open(r[0]) as f:
                c = f.readlines()
            return ''.join(c[-lines:]) if c else "No log"
        return "No log found"
    except Exception as e:
        return f"Error: {e}"

def show_file_management(chat_id, file_id, user_id, message_id=None):
    rec = get_file_record(file_id)
    if not rec:
        bot.send_message(chat_id, "❌ Not found")
        return
    if rec["user_id"] != user_id and not is_admin(user_id):
        bot.send_message(chat_id, "❌ Access denied")
        return
    with proc_lock:
        running = file_id in processes
    st = "🟢 Running" if running else "🔴 Stopped"
    pid = f"\nPID: {rec['pid']}" if rec['pid'] else ""
    text = (f"⚙️ <b>File Management</b>\n\n"
            f"📁 {html_lib.escape(rec['orig_name'])}\n"
            f"📊 Type: {rec['file_type']}\n"
            f"📈 Status: {st}{pid}\n"
            f"⏰ {rec['uploaded_at'][:16]}")
    kb = file_actions_kb(file_id, running)
    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
        except Exception:
            bot.send_message(chat_id, text, reply_markup=kb)
    else:
        bot.send_message(chat_id, text, reply_markup=kb)

@bot.message_handler(func=lambda m: m.text == "👑 Admin Panel" and is_admin(m.from_user.id))
def admin_panel(message):
    bot.send_message(message.chat.id, "👑 <b>Admin Panel</b>", reply_markup=admin_panel_kb())

def send_admin_users(chat_id):
    users = get_all_users()
    if not users:
        bot.send_message(chat_id, "No users yet.")
        return
    lines = [f"👥 <b>All Users ({len(users)})</b>\n"]
    for u in users[:50]:
        lines.append(f"• <code>{u['user_id']}</code> — {html_lib.escape(u['username'] or 'No username')}")
    bot.send_message(chat_id, "\n".join(lines))

def send_admin_files(chat_id):
    files = get_all_files()
    if not files:
        bot.send_message(chat_id, "No files yet.")
        return
    kb = InlineKeyboardMarkup()
    lines = [f"📁 <b>All Files ({len(files)})</b>\n"]
    for f in files[:40]:
        emoji = "🟢" if f['status'] == 'Running' else "🔴"
        lines.append(f"{emoji} <code>{f['id']}</code> | {html_lib.escape(f['orig_name'])} | by <code>{f['user_id']}</code>")
        kb.add(InlineKeyboardButton(f"{emoji} {f['orig_name'][:30]} | {f['user_id']}",
                                    callback_data=f"admin_manage:{f['id']}"))
    bot.send_message(chat_id, "\n".join(lines), reply_markup=kb)

def send_admin_status(chat_id):
    cpu, mem, pc = get_system_load()
    up = datetime.utcnow() - START_TIME
    h, r = divmod(up.seconds, 3600)
    m, s = divmod(r, 60)
    bot.send_message(chat_id,
        f"⚡ <b>System Status</b>\n\n"
        f"• CPU: {cpu:.1f}%\n• Memory: {mem:.1f}%\n"
        f"• Processes: {pc}/{MAX_RUNNING_PROCESSES}\n"
        f"• Uptime: {up.days}d {h}h {m}m")

def send_admin_stats(chat_id):
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    tu = cur.fetchone()[0] or 0
    cur.execute("SELECT COUNT(DISTINCT user_id) FROM files")
    hu = cur.fetchone()[0] or 0
    cur.execute("SELECT COUNT(*) FROM files")
    tf = cur.fetchone()[0] or 0
    cur.execute("SELECT COUNT(*) FROM files WHERE status='Running'")
    rf = cur.fetchone()[0] or 0
    bot.send_message(chat_id,
        f"📊 <b>Admin Stats</b>\n\n"
        f"👥 Total Users: {tu}\n👤 Uploaders: {hu}\n"
        f"📁 Total Files: {tf}\n🚀 Running: {rf}")

def do_broadcast(message):
    if not is_admin(message.from_user.id):
        return
    text = message.text
    users = get_all_users()
    ok = 0
    for u in users:
        try:
            bot.send_message(u['user_id'], f"📢 <b>Broadcast</b>\n\n{text}")
            ok += 1
            time.sleep(0.05)
        except Exception:
            pass
    bot.send_message(message.chat.id, f"✅ Sent to {ok}/{len(users)} users.")

@bot.callback_query_handler(func=lambda c: True)
def cb_handler(call):
    data = call.data
    chat_id = call.message.chat.id
    uid = call.from_user.id

    if data == "back_to_files":
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception:
            pass
        send_files_list(chat_id, uid)
        return

    if data.startswith("admin_") and not is_admin(uid):
        bot.answer_callback_query(call.id, "❌ Admin only")
        return

    if data == "admin_users":
        bot.answer_callback_query(call.id)
        send_admin_users(chat_id)
        return
    if data == "admin_files":
        bot.answer_callback_query(call.id)
        send_admin_files(chat_id)
        return
    if data == "admin_status":
        bot.answer_callback_query(call.id)
        send_admin_status(chat_id)
        return
    if data == "admin_stats":
        bot.answer_callback_query(call.id)
        send_admin_stats(chat_id)
        return
    if data == "admin_close":
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception:
            pass
        return
    if data == "admin_stopall":
        bot.answer_callback_query(call.id, "Stopping all...")
        with proc_lock:
            ids = list(processes.keys())
        for fid in ids:
            stop_file_process(fid)
        bot.send_message(chat_id, f"🛑 Stopped {len(ids)} processes.")
        return
    if data == "admin_restart":
        bot.answer_callback_query(call.id, "Restarting...")
        bot.send_message(chat_id, "🔄 Restarting bot now...")
        RESTART_NEEDED.set()
        os.kill(os.getpid(), signal.SIGTERM)
        return
    if data == "admin_broadcast":
        bot.answer_callback_query(call.id)
        msg = bot.send_message(chat_id, "📢 Send broadcast message:")
        bot.register_next_step_handler(msg, do_broadcast)
        return
    if data.startswith("admin_manage:"):
        fid = int(data.split(":")[1])
        bot.answer_callback_query(call.id)
        show_file_management(chat_id, fid, uid, call.message.message_id)
        return

    try:
        if data.startswith("manage:"):
            fid = int(data.split(":")[1])
            show_file_management(chat_id, fid, uid, call.message.message_id)
        elif data.startswith("start:"):
            fid = int(data.split(":")[1])
            bot.answer_callback_query(call.id, "Starting...")
            start_file_process(fid, chat_id)
            time.sleep(1)
            show_file_management(chat_id, fid, uid, call.message.message_id)
        elif data.startswith("stop:"):
            fid = int(data.split(":")[1])
            bot.answer_callback_query(call.id, "Stopping...")
            stop_file_process(fid)
            bot.send_message(chat_id, "⏹ Stopped")
            time.sleep(1)
            show_file_management(chat_id, fid, uid, call.message.message_id)
        elif data.startswith("restart:"):
            fid = int(data.split(":")[1])
            bot.answer_callback_query(call.id, "Restarting...")
            stop_file_process(fid)
            time.sleep(2)
            start_file_process(fid, chat_id)
            time.sleep(1)
            show_file_management(chat_id, fid, uid, call.message.message_id)
        elif data.startswith("delete:"):
            fid = int(data.split(":")[1])
            bot.answer_callback_query(call.id, "Deleting...")
            rec = get_file_record(fid)
            if rec:
                stop_file_process(fid)
                try:
                    if os.path.isdir(rec["path"]):
                        shutil.rmtree(rec["path"], ignore_errors=True)
                    elif os.path.exists(rec["path"]):
                        os.remove(rec["path"])
                except Exception as e:
                    logger.error(f"delete err: {e}")
                remove_file_record(fid)
            bot.send_message(chat_id, "🗑 Deleted")
            send_files_list(chat_id, uid)
        elif data.startswith("logs:"):
            fid = int(data.split(":")[1])
            bot.answer_callback_query(call.id, "Getting logs...")
            logs = get_file_logs(fid)
            rec = get_file_record(fid)
            name = rec["orig_name"] if rec else "Unknown"
            if len(logs) > 3800:
                logs = "...(truncated)...\n" + logs[-3800:]
            bot.send_message(chat_id,
                f"📄 <b>Logs: {html_lib.escape(name)}</b>\n\n<pre>{html_lib.escape(logs)}</pre>")
    except Exception as e:
        bot.answer_callback_query(call.id, "Error")
        logger.error(f"callback err: {e}")

def _shutdown(signum=None, frame=None):
    STOP_EVENT.set()
    try:
        bot.stop_polling()
    except Exception:
        pass
    logger.info("🛑 Bot stopped.")

signal.signal(signal.SIGINT, _shutdown)
signal.signal(signal.SIGTERM, _shutdown)

def start_bot_once():
    logger.info("Starting Telegram bot...")
    bot.infinity_polling(timeout=60, long_polling_timeout=50)

def run_bot_forever():
    attempts = 0
    while not STOP_EVENT.is_set():
        try:
            start_bot_once()
            if RESTART_NEEDED.is_set():
                RESTART_NEEDED.clear()
                logger.info("♻️ Restart requested by admin. Restarting...")
                attempts = 0
                time.sleep(1)
                continue
            break
        except KeyboardInterrupt:
            logger.info("🛑 Ctrl+C pressed, stopping.")
            STOP_EVENT.set()
            break
        except SystemExit:
            STOP_EVENT.set()
            break
        except Exception as e:
            attempts += 1
            logger.error(f"❌ Bot crashed: {e}\n{traceback.format_exc()}")
            if attempts > 50:
                logger.error("Too many crashes, stopping.")
                break
            wait = min(5 * attempts, 30)
            logger.info(f"♻️ Auto-restart in {wait}s (attempt {attempts})...")
            time.sleep(wait)
            continue

# ================= DUMMY WEB SERVER FOR RAILWAY =================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'OK')
    def log_message(self, format, *args):
        pass

def start_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f"🌐 Web server on port {port}")
    server.serve_forever()

threading.Thread(target=start_web_server, daemon=True).start()

# ================= MAIN =================
if __name__ == "__main__":
    logger.info("🚀 24x7 THB Hosting Bot starting...")
    run_bot_forever()
    logger.info("👋 Bot finished.")
