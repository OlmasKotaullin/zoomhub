#!/usr/bin/env python3
"""ZoomHub Agent v2 — полная автоматизация: Zoom → Буквица → Саммари.

Мониторит папку Zoom, транскрибирует через Буквицу ЛОКАЛЬНО (Telegram-сессия на компе),
загружает транскрипт на сервер для генерации саммари.
"""

import argparse
import asyncio
import json
import platform
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

EXTENSIONS = {".mp4", ".m4a", ".mp3", ".wav", ".webm", ".ogg"}
POLL_INTERVAL = 30
STABLE_WAIT = 10
CONFIG_DIR = Path.home() / ".zoomhub"
CONFIG_FILE = CONFIG_DIR / "config.json"
STATE_FILE = ".zoomhub-agent-state.json"

# Telegram credentials for Bukvitsa (shared, non-secret — это ID приложения, не токен)
import os as _os
DEFAULT_API_ID = int(_os.environ.get("TG_API_ID", "20610877"))
DEFAULT_API_HASH = _os.environ.get("TG_API_HASH", "06a021c0c0046cd67085dd7452deaaf8")
DEFAULT_BOT = _os.environ.get("TG_BOT", "bykvitsa")
DEFAULT_SERVER = "https://zoomhub-app.fly.dev"


def default_zoom_folder() -> str:
    home = Path.home()
    if platform.system() in ("Darwin", "Windows"):
        return str(home / "Documents" / "Zoom")
    return str(home / "Zoom")


def file_hash(path: Path) -> str:
    stat = path.stat()
    return f"{path.name}:{stat.st_size}:{int(stat.st_mtime)}"


def load_state(state_path: Path) -> set:
    if state_path.exists():
        try:
            return set(json.loads(state_path.read_text()))
        except Exception:
            pass
    return set()


def save_state(state_path: Path, hashes: set):
    state_path.write_text(json.dumps(list(hashes)))


def _extract_zoom_id(filename: str) -> str | None:
    """Extract Zoom meeting ID from filename like 'audio1282806098.m4a' or 'video1282806098.mp4'."""
    import re
    m = re.match(r'^(?:audio|video)(\d{5,})', filename)
    return m.group(1) if m else None


def find_new_files(folder: Path, processed: set, since_ts: float = 0) -> list[Path]:
    files = []
    if not folder.exists():
        return files
    for sub in [folder] + [d for d in folder.iterdir() if d.is_dir()]:
        try:
            for f in sub.iterdir():
                if f.is_file() and f.suffix.lower() in EXTENSIONS:
                    if f.stat().st_mtime < since_ts:
                        continue
                    if file_hash(f) not in processed:
                        files.append(f)
        except PermissionError:
            continue

    # Dedup: if both audio123 and video123 exist, keep only audio (smaller file)
    seen_zoom_ids: dict[str, Path] = {}
    deduped = []
    for f in files:
        zoom_id = _extract_zoom_id(f.name)
        if zoom_id:
            if zoom_id in seen_zoom_ids:
                # Prefer audio over video (smaller, faster to process)
                existing = seen_zoom_ids[zoom_id]
                if f.name.startswith("audio") and existing.name.startswith("video"):
                    seen_zoom_ids[zoom_id] = f
                # else keep existing
            else:
                seen_zoom_ids[zoom_id] = f
        else:
            deduped.append(f)

    deduped.extend(seen_zoom_ids.values())
    return sorted(deduped, key=lambda f: f.stat().st_mtime)


def is_stable(path: Path) -> bool:
    try:
        size1 = path.stat().st_size
        time.sleep(STABLE_WAIT)
        size2 = path.stat().st_size
        return size1 == size2 and size2 > 0
    except OSError:
        return False


def load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def save_config(cfg: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))


def upload_transcript(server: str, token: str, title: str,
                      transcript: dict, duration: int = 0) -> bool:
    """Загружает готовый транскрипт на сервер (без аудио)."""
    url = f"{server.rstrip('/')}/api/agent/upload-transcript"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = {
        "title": title,
        "transcript_text": transcript["full_text"],
        "segments": transcript.get("segments", []),
        "duration_seconds": duration,
    }

    try:
        resp = httpx.post(url, json=body, headers=headers, timeout=60)
        if resp.status_code == 200:
            data = resp.json()
            print(f"  ✅ Загружено: {data.get('title', '')} (ID: {data.get('id', '')})", flush=True)
            return True
        else:
            print(f"  ❌ Ошибка сервера {resp.status_code}: {resp.text[:200]}", flush=True)
            return False
    except Exception as e:
        print(f"  ❌ Ошибка соединения: {e}", flush=True)
        return False


def upload_audio_fallback(server: str, token: str, filepath: Path) -> bool:
    """Fallback: загружает аудио на сервер (транскрипция на сервере)."""
    url = f"{server.rstrip('/')}/api/agent/upload"
    headers = {"Authorization": f"Bearer {token}"}
    print(f"  📤 Fallback: загрузка аудио на сервер...", flush=True)

    try:
        with open(filepath, "rb") as f:
            resp = httpx.post(
                url, headers=headers,
                files={"file": (filepath.name, f, "application/octet-stream")},
                data={"title": filepath.stem},
                timeout=600,
            )
        if resp.status_code == 200:
            data = resp.json()
            print(f"  ✅ Аудио загружено: {data.get('title', '')} (ID: {data.get('id', '')})", flush=True)
            return True
        else:
            print(f"  ❌ Ошибка {resp.status_code}", flush=True)
            return False
    except Exception as e:
        print(f"  ❌ Ошибка: {e}", flush=True)
        return False


async def process_file(filepath: Path, cfg: dict) -> bool:
    """Обработка одного файла: Буквица → сервер."""
    mode = cfg.get("mode", "full")
    server = cfg.get("server", DEFAULT_SERVER)
    token = cfg.get("token", "")

    if mode == "upload-only":
        return upload_audio_fallback(server, token, filepath)

    # Полный режим: локальная Буквица → транскрипт → сервер
    try:
        from bukvitsa_local import transcribe

        api_id = cfg.get("api_id", DEFAULT_API_ID)
        api_hash = cfg.get("api_hash", DEFAULT_API_HASH)
        bot = cfg.get("bot_username", DEFAULT_BOT)

        print(f"  🎙 Транскрибация через Буквицу...", flush=True)
        result = await transcribe(str(filepath), api_id, api_hash, bot)

        if not result.get("full_text"):
            print(f"  ⚠️  Пустой транскрипт, загружаю аудио на сервер...", flush=True)
            return upload_audio_fallback(server, token, filepath)

        word_count = len(result["full_text"].split())
        print(f"  📝 Транскрипт: {len(result['full_text'])} символов, ~{word_count} слов", flush=True)

        # Загрузка транскрипта на сервер
        duration = int(word_count / 150 * 60)  # ~150 слов/мин
        return upload_transcript(server, token, filepath.stem, result, duration)

    except Exception as e:
        print(f"  ⚠️  Ошибка Буквицы: {e}", flush=True)
        print(f"  📤 Fallback: загрузка аудио на сервер...", flush=True)
        return upload_audio_fallback(server, token, filepath)


async def run_setup():
    """Интерактивная настройка агента."""
    print("\n🔧 Настройка ZoomHub Agent\n", flush=True)

    cfg = load_config()

    # Сервер
    server = input(f"Сервер ZoomHub [{cfg.get('server', DEFAULT_SERVER)}]: ").strip()
    cfg["server"] = server or cfg.get("server", DEFAULT_SERVER)

    # Токен
    token = input("API-токен (скопируйте из Настроек → Локальный агент): ").strip()
    if token:
        cfg["token"] = token

    if not cfg.get("token"):
        print("❌ Токен обязателен. Получите его в настройках ZoomHub.", flush=True)
        return

    # Проверка подключения к серверу
    try:
        resp = httpx.get(f"{cfg['server']}/health", timeout=10)
        if resp.status_code == 200:
            print(f"✅ Сервер {cfg['server']} доступен", flush=True)
        else:
            print(f"⚠️  Сервер ответил {resp.status_code}", flush=True)
    except Exception as e:
        print(f"⚠️  Не удалось подключиться: {e}", flush=True)

    # Режим
    print("\nРежим работы:")
    print("  1. Полный — Буквица локально + саммари на сервере (рекомендуется)")
    print("  2. Только загрузка — аудио отправляется на сервер (транскрипция на сервере)")
    mode_choice = input("Выберите [1]: ").strip()
    cfg["mode"] = "upload-only" if mode_choice == "2" else "full"

    if cfg["mode"] == "full":
        # Настройка Telegram
        print("\n📱 Настройка Telegram для Буквицы", flush=True)
        print("   Telegram-сессия хранится ТОЛЬКО на вашем компьютере.", flush=True)
        print("   Никуда не отправляется.\n", flush=True)

        cfg["api_id"] = cfg.get("api_id", DEFAULT_API_ID)
        cfg["api_hash"] = cfg.get("api_hash", DEFAULT_API_HASH)
        cfg["bot_username"] = cfg.get("bot_username", DEFAULT_BOT)

        from bukvitsa_local import setup_session
        await setup_session(cfg["api_id"], cfg["api_hash"], cfg["bot_username"])

    # Папка Zoom
    default_folder = default_zoom_folder()
    folder = input(f"\nПапка Zoom [{default_folder}]: ").strip()
    cfg["folder"] = folder or default_folder

    save_config(cfg)
    print(f"\n✅ Настройка сохранена в {CONFIG_FILE}", flush=True)
    print("   Запустите агент без --setup для начала мониторинга.", flush=True)


async def main_async():
    parser = argparse.ArgumentParser(description="ZoomHub Agent v2")
    parser.add_argument("--setup", action="store_true", help="Интерактивная настройка")
    parser.add_argument("--token", help="API-токен (или из конфига)")
    parser.add_argument("--server", help="URL сервера")
    parser.add_argument("--folder", help="Папка Zoom")
    parser.add_argument("--mode", choices=["full", "upload-only"], help="Режим: full или upload-only")
    parser.add_argument("--since", default="today", help="С какой даты (YYYY-MM-DD, today, all)")
    parser.add_argument("--once", action="store_true", help="Однократная проверка")
    args = parser.parse_args()

    if args.setup:
        await run_setup()
        return

    # Загрузка конфига
    cfg = load_config()
    if args.token:
        cfg["token"] = args.token
    if args.server:
        cfg["server"] = args.server
    if args.folder:
        cfg["folder"] = args.folder
    if args.mode:
        cfg["mode"] = args.mode

    if not cfg.get("token"):
        print("❌ Нет API-токена. Запустите: ZoomHubAgent --setup", flush=True)
        sys.exit(1)

    folder = Path(cfg.get("folder", default_zoom_folder()))
    server = cfg.get("server", DEFAULT_SERVER)
    mode = cfg.get("mode", "full")
    state_path = folder / STATE_FILE

    print(f"🎯 ZoomHub Agent v2", flush=True)
    print(f"   Сервер: {server}")
    print(f"   Папка:  {folder}")
    print(f"   Режим:  {'Буквица + Саммари' if mode == 'full' else 'Только загрузка'}")
    print(f"   Цикл:   {'однократно' if args.once else f'каждые {POLL_INTERVAL} сек'}")
    print()

    if not folder.exists():
        print(f"⚠️  Папка {folder} не существует. Создаю...", flush=True)
        folder.mkdir(parents=True, exist_ok=True)

    # Parse --since
    if args.since == "today":
        since_ts = datetime.now().replace(hour=0, minute=0, second=0).timestamp()
    elif args.since == "all":
        since_ts = 0
    else:
        since_ts = datetime.strptime(args.since, "%Y-%m-%d").timestamp()

    processed = load_state(state_path)
    print(f"📂 Обработано: {len(processed)} файлов", flush=True)
    print(f"📅 С: {datetime.fromtimestamp(since_ts).strftime('%Y-%m-%d') if since_ts else 'все'}\n", flush=True)

    while True:
        new_files = find_new_files(folder, processed, since_ts)

        if new_files:
            print(f"🔍 Найдено {len(new_files)} новых файлов:", flush=True)
            for filepath in new_files:
                print(f"\n  📄 {filepath.name}", flush=True)

                if not is_stable(filepath):
                    print(f"  ⏳ Файл записывается, пропускаю...", flush=True)
                    continue

                fh = file_hash(filepath)
                success = await process_file(filepath, cfg)
                if success:
                    processed.add(fh)
                    save_state(state_path, processed)

        if args.once:
            break

        time.sleep(POLL_INTERVAL)


def main():
    # If no config and no CLI args → open GUI setup
    cfg = load_config()
    if not cfg.get("token") and len(sys.argv) == 1:
        from web_setup import run_gui_setup
        run_gui_setup()
        return

    asyncio.run(main_async())


if __name__ == "__main__":
    main()
