"""Web-based GUI настройки ZoomHub Agent — открывается в браузере."""

import asyncio
import json
import platform
import threading
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs

import httpx

CONFIG_DIR = Path.home() / ".zoomhub"
CONFIG_FILE = CONFIG_DIR / "config.json"

import os as _os
DEFAULT_API_ID = int(_os.environ.get("TG_API_ID", "20610877"))
DEFAULT_API_HASH = _os.environ.get("TG_API_HASH", "06a021c0c0046cd67085dd7452deaaf8")
DEFAULT_BOT = _os.environ.get("TG_BOT", "bykvitsa")
DEFAULT_SERVER = "https://zoomhub-app.fly.dev"

_tg_client = None
_setup_complete = False
_bg_loop = None


def _start_bg_loop():
    """Фоновый event loop в отдельном потоке — для Telethon."""
    global _bg_loop
    import threading
    _bg_loop = asyncio.new_event_loop()
    t = threading.Thread(target=_bg_loop.run_forever, daemon=True)
    t.start()


def _run_async(coro):
    """Запускает корутину в фоновом event loop и ждёт результат."""
    future = asyncio.run_coroutine_threadsafe(coro, _bg_loop)
    return future.result(timeout=120)


def default_zoom_folder() -> str:
    home = Path.home()
    if platform.system() in ("Darwin", "Windows"):
        return str(home / "Documents" / "Zoom")
    return str(home / "Zoom")


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass
    return {}


def save_config(cfg: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))


def needs_setup() -> bool:
    return not load_config().get("token")


SETUP_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ZoomHub Agent — Настройка</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f7f9fc; min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px; }
.card { background: white; border-radius: 20px; box-shadow: 0 4px 24px rgba(0,0,0,0.08); padding: 40px; max-width: 480px; width: 100%; }
h1 { font-size: 24px; font-weight: 800; color: #131820; margin-bottom: 4px; }
.sub { color: #8f9bb3; font-size: 14px; margin-bottom: 28px; }
label { display: block; font-size: 14px; font-weight: 600; color: #374151; margin-bottom: 6px; }
.hint { font-size: 12px; color: #8f9bb3; margin-bottom: 8px; }
input[type=text] { width: 100%; padding: 12px 14px; border: 1.5px solid #dde3ed; border-radius: 12px; font-size: 14px; outline: none; transition: border 0.15s; margin-bottom: 18px; }
input:focus { border-color: #0b5cff; box-shadow: 0 0 0 3px rgba(11,92,255,0.1); }
.btn { width: 100%; padding: 14px; border: none; border-radius: 12px; font-size: 15px; font-weight: 700; cursor: pointer; transition: all 0.15s; }
.btn-primary { background: #0b5cff; color: white; }
.btn-primary:hover { background: #0047d4; }
.btn-green { background: #16a34a; color: white; }
.btn-green:hover { background: #15803d; }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.status { text-align: center; padding: 10px; margin-top: 12px; border-radius: 10px; font-size: 13px; font-weight: 600; }
.ok { background: #f0fdf4; color: #16a34a; }
.err { background: #fef2f2; color: #dc2626; }
.info { background: #eff6ff; color: #0b5cff; }
.step { display: none; }
.step.active { display: block; }
.done-list { margin: 20px 0; }
.done-list p { padding: 6px 0; font-size: 14px; color: #374151; }
.security { background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 12px; padding: 14px; margin: 18px 0; font-size: 13px; color: #166534; }
</style>
</head>
<body>
<div class="card">

  <!-- Step 1: Token -->
  <div class="step active" id="step1">
    <h1>🎯 ZoomHub Agent</h1>
    <p class="sub">Настройка за 2 минуты</p>

    <label>API-токен</label>
    <p class="hint">Скопируйте из ZoomHub → Настройки → Локальный агент</p>
    <input type="text" id="token" placeholder="Вставьте токен сюда">

    <label>Папка Zoom</label>
    <input type="text" id="folder" value="ZOOM_FOLDER">

    <button class="btn btn-primary" onclick="saveToken()">Далее →</button>
    <div id="status1"></div>
  </div>

  <!-- Step 2: Telegram -->
  <div class="step" id="step2">
    <h1>📱 Telegram</h1>
    <p class="sub">Подключение к Буквице</p>

    <div class="security">🔒 Telegram-сессия хранится ТОЛЬКО на вашем компьютере. Никуда не передаётся.</div>

    <label>Номер телефона</label>
    <p class="hint">С кодом страны, например: +79001234567</p>
    <input type="text" id="phone" placeholder="+7...">

    <button class="btn btn-primary" id="sendCodeBtn" onclick="sendCode()">Отправить код</button>
    <div id="status2"></div>

    <div id="codeBlock" style="display:none; margin-top: 20px;">
      <label>Код из Telegram</label>
      <p class="hint">Придёт в приложение Telegram на телефоне</p>
      <input type="text" id="code" placeholder="12345" style="font-size:20px; text-align:center; letter-spacing:8px;">
      <div id="passwordBlock" style="display:none; margin-top:12px;">
        <label>Облачный пароль Telegram</label>
        <p class="hint">У вас включена двухфакторная аутентификация</p>
        <input type="password" id="tg_password" placeholder="Пароль">
      </div>
      <button class="btn btn-green" onclick="confirmCode()">Подтвердить</button>
      <div id="status3"></div>
    </div>
  </div>

  <!-- Step 3: Done -->
  <div class="step" id="step3">
    <div style="text-align:center; padding: 20px 0;">
      <div style="font-size:64px; margin-bottom:10px;">🎉</div>
      <h1>Всё готово!</h1>
      <p class="sub">ZoomHub Agent настроен и работает</p>
    </div>
    <div class="done-list">
      <p>✅ Сервер подключён</p>
      <p>✅ Telegram авторизован</p>
      <p>✅ Буквица готова к работе</p>
      <p>✅ Папка Zoom настроена</p>
    </div>
    <p style="text-align:center; font-size:14px; color:#6b7a94; margin:16px 0;">
      Закройте эту вкладку. Агент начнёт мониторинг автоматически.
    </p>
    <button class="btn btn-primary" onclick="finish()">Готово</button>
  </div>

</div>
<script>
function show(id) { document.querySelectorAll('.step').forEach(s => s.classList.remove('active')); document.getElementById(id).classList.add('active'); }
function status(id, msg, cls) { const el = document.getElementById(id); el.className = 'status ' + cls; el.textContent = msg; }

function saveToken() {
  const token = document.getElementById('token').value.trim();
  const folder = document.getElementById('folder').value.trim();
  if (!token) { status('status1', 'Введите API-токен', 'err'); return; }
  status('status1', 'Проверяю...', 'info');
  fetch('/api/save-token', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({token, folder}) })
    .then(r => r.json()).then(d => {
      if (d.ok) { show('step2'); } else { status('status1', d.error || 'Ошибка', 'err'); }
    }).catch(() => status('status1', 'Ошибка соединения', 'err'));
}

function sendCode() {
  const phone = document.getElementById('phone').value.trim();
  if (!phone) { status('status2', 'Введите номер', 'err'); return; }
  document.getElementById('sendCodeBtn').disabled = true;
  status('status2', 'Отправляю код...', 'info');
  fetch('/api/send-code', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({phone}) })
    .then(r => r.json()).then(d => {
      if (d.ok) { status('status2', '✅ Код отправлен! Проверьте Telegram.', 'ok'); document.getElementById('codeBlock').style.display='block'; }
      else { status('status2', d.error || 'Ошибка', 'err'); document.getElementById('sendCodeBtn').disabled = false; }
    }).catch(() => { status('status2', 'Ошибка', 'err'); document.getElementById('sendCodeBtn').disabled = false; });
}

function confirmCode() {
  const code = document.getElementById('code').value.trim();
  const phone = document.getElementById('phone').value.trim();
  if (!code) { status('status3', 'Введите код', 'err'); return; }

  // Check if password field is visible — means we need to send password
  const pwBlock = document.getElementById('passwordBlock');
  if (pwBlock.style.display !== 'none') {
    const password = document.getElementById('tg_password').value.trim();
    if (!password) { status('status3', 'Введите облачный пароль', 'err'); return; }
    status('status3', 'Проверяю пароль...', 'info');
    fetch('/api/confirm-password', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({password}) })
      .then(r => r.json()).then(d => {
        if (d.ok) { show('step3'); } else { status('status3', d.error || 'Неверный пароль', 'err'); }
      }).catch(() => status('status3', 'Ошибка', 'err'));
    return;
  }

  status('status3', 'Проверяю код...', 'info');
  fetch('/api/confirm-code', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({code, phone}) })
    .then(r => r.json()).then(d => {
      if (d.ok) { show('step3'); }
      else if (d.need_password) { pwBlock.style.display='block'; status('status3', 'Введите облачный пароль Telegram', 'info'); }
      else { status('status3', d.error || 'Неверный код', 'err'); }
    }).catch(() => status('status3', 'Ошибка', 'err'));
}

function finish() {
  fetch('/api/finish', { method: 'POST' }).then(() => { window.close(); });
}
</script>
</body>
</html>"""


class SetupHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Suppress logs

    def do_GET(self):
        html = SETUP_HTML.replace("ZOOM_FOLDER", default_zoom_folder())
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())

    def do_POST(self):
        global _tg_client, _tg_sent, _tg_loop, _setup_complete

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        if self.path == "/api/save-token":
            token = body.get("token", "")
            folder = body.get("folder", default_zoom_folder())
            try:
                cfg = load_config()
                cfg["token"] = token
                cfg["server"] = DEFAULT_SERVER
                cfg["folder"] = folder
                cfg["mode"] = "full"
                cfg["api_id"] = DEFAULT_API_ID
                cfg["api_hash"] = DEFAULT_API_HASH
                cfg["bot_username"] = DEFAULT_BOT

                resp = httpx.get(f"{DEFAULT_SERVER}/health", timeout=10)
                if resp.status_code != 200:
                    self._json({"ok": False, "error": "Сервер недоступен"})
                    return

                save_config(cfg)
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)[:100]})

        elif self.path == "/api/send-code":
            phone = body.get("phone", "").strip()
            if phone and not phone.startswith("+"):
                phone = "+" + phone
            try:
                async def _do():
                    global _tg_client
                    from telethon import TelegramClient
                    if _tg_client:
                        try: await _tg_client.disconnect()
                        except: pass
                    session = str(CONFIG_DIR / "zoomhub")
                    _tg_client = TelegramClient(session, DEFAULT_API_ID, DEFAULT_API_HASH)
                    await _tg_client.connect()
                    await _tg_client.send_code_request(phone)
                _run_async(_do())
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)[:100]})

        elif self.path == "/api/confirm-code":
            code = body.get("code", "")
            phone = body.get("phone", "").strip()
            if phone and not phone.startswith("+"):
                phone = "+" + phone
            try:
                async def _do():
                    await _tg_client.sign_in(phone, code)
                _run_async(_do())
                async def _done():
                    await _tg_client.disconnect()
                _run_async(_done())
                self._json({"ok": True})
            except Exception as e:
                err = str(e).lower()
                if "password" in err or "two-step" in err or "2fa" in err:
                    self._json({"ok": False, "need_password": True})
                else:
                    self._json({"ok": False, "error": str(e)[:100]})

        elif self.path == "/api/confirm-password":
            password = body.get("password", "")
            try:
                async def _do():
                    await _tg_client.sign_in(password=password)
                    await _tg_client.disconnect()
                _run_async(_do())
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)[:100]})

        elif self.path == "/api/finish":
            _setup_complete = True
            self._json({"ok": True})

        else:
            self._json({"error": "Not found"}, 404)

    def _json(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())


def run_gui_setup():
    global _setup_complete

    _start_bg_loop()

    server = HTTPServer(("127.0.0.1", 0), SetupHandler)
    port = server.server_address[1]

    print(f"🔧 Откройте браузер: http://127.0.0.1:{port}", flush=True)
    webbrowser.open(f"http://127.0.0.1:{port}")

    while not _setup_complete:
        server.handle_request()

    server.server_close()
    print("✅ Настройка завершена!", flush=True)
