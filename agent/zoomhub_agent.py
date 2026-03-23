#!/usr/bin/env python3
"""ZoomHub Local Agent — сканирует папку Zoom и загружает записи на сервер."""

import argparse
import hashlib
import json
import platform
import sys
import time
from pathlib import Path

import httpx

EXTENSIONS = {".mp4", ".m4a", ".mp3", ".wav", ".webm", ".ogg"}
POLL_INTERVAL = 30  # секунд
STABLE_WAIT = 10  # секунд ожидания стабилизации файла
STATE_FILE = ".zoomhub-agent-state.json"


def default_zoom_folder() -> str:
    home = Path.home()
    if platform.system() == "Darwin":
        return str(home / "Documents" / "Zoom")
    elif platform.system() == "Windows":
        return str(home / "Documents" / "Zoom")
    return str(home / "Zoom")


def file_hash(path: Path) -> str:
    """Fast fingerprint: name + size + mtime (no content hashing)."""
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


def find_new_files(folder: Path, processed: set, since_ts: float = 0) -> list[Path]:
    files = []
    if not folder.exists():
        return files
    # Scan max 2 levels deep (folder/subfolder/file)
    for sub in [folder] + [d for d in folder.iterdir() if d.is_dir()]:
        try:
            for f in sub.iterdir():
                if f.is_file() and f.suffix.lower() in EXTENSIONS:
                    if f.stat().st_mtime < since_ts:
                        continue
                    fh = file_hash(f)
                    if fh not in processed:
                        files.append(f)
        except PermissionError:
            continue
    return sorted(files, key=lambda f: f.stat().st_mtime)


def is_stable(path: Path) -> bool:
    try:
        size1 = path.stat().st_size
        time.sleep(STABLE_WAIT)
        size2 = path.stat().st_size
        return size1 == size2 and size2 > 0
    except OSError:
        return False


def upload_file(server: str, token: str, filepath: Path) -> bool:
    url = f"{server.rstrip('/')}/api/agent/upload"
    headers = {"Authorization": f"Bearer {token}"}

    print(f"  Загрузка {filepath.name} ({filepath.stat().st_size / 1024 / 1024:.1f} МБ)...")

    try:
        with open(filepath, "rb") as f:
            resp = httpx.post(
                url,
                headers=headers,
                files={"file": (filepath.name, f, "application/octet-stream")},
                data={"title": filepath.stem},
                timeout=600,
            )

        if resp.status_code == 200:
            data = resp.json()
            print(f"  ✅ Загружено: {data.get('title', '')} (ID: {data.get('id', '')})")
            return True
        else:
            print(f"  ❌ Ошибка {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        print(f"  ❌ Ошибка соединения: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="ZoomHub Local Agent")
    parser.add_argument("--token", required=True, help="API token (получить в настройках ZoomHub)")
    parser.add_argument("--server", default="https://zoomhub-app.fly.dev", help="URL сервера ZoomHub")
    parser.add_argument("--folder", default=default_zoom_folder(), help="Путь к папке Zoom")
    parser.add_argument("--since", default="today", help="Обрабатывать файлы с даты (YYYY-MM-DD или 'today' или 'all')")
    parser.add_argument("--once", action="store_true", help="Однократная проверка (без цикла)")
    args = parser.parse_args()

    folder = Path(args.folder)
    state_path = folder / STATE_FILE

    print(f"🎯 ZoomHub Agent", flush=True)
    print(f"   Сервер: {args.server}")
    print(f"   Папка:  {folder}")
    print(f"   Режим:  {'однократно' if args.once else f'каждые {POLL_INTERVAL} сек'}")
    print()

    if not folder.exists():
        print(f"⚠️  Папка {folder} не существует. Создаю...", flush=True)
        folder.mkdir(parents=True, exist_ok=True)

    # Parse --since
    from datetime import datetime
    if args.since == "today":
        since_ts = datetime.now().replace(hour=0, minute=0, second=0).timestamp()
    elif args.since == "all":
        since_ts = 0
    else:
        since_ts = datetime.strptime(args.since, "%Y-%m-%d").timestamp()

    processed = load_state(state_path)
    print(f"📂 Уже обработано: {len(processed)} файлов", flush=True)
    print(f"📅 Файлы с: {datetime.fromtimestamp(since_ts).strftime('%Y-%m-%d') if since_ts else 'все'}", flush=True)

    while True:
        new_files = find_new_files(folder, processed, since_ts)

        if new_files:
            print(f"\n🔍 Найдено {len(new_files)} новых файлов:")
            for filepath in new_files:
                print(f"\n  📄 {filepath.name}")

                if not is_stable(filepath):
                    print(f"  ⏳ Файл ещё записывается, пропускаю...")
                    continue

                fh = file_hash(filepath)
                if upload_file(args.server, args.token, filepath):
                    processed.add(fh)
                    save_state(state_path, processed)

        if args.once:
            break

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
