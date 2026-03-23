#!/bin/bash
# Скрипт для запуска бэкенда ZoomHub (используется Tauri beforeDevCommand)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Активируем venv
if [ -f "venv/bin/python" ]; then
    PYTHON="$SCRIPT_DIR/venv/bin/python"
else
    PYTHON="python3"
fi

# Убиваем старый процесс на порту 8002 если есть
lsof -ti:8002 | xargs kill 2>/dev/null
sleep 1

echo "[ZoomHub] Starting backend with $PYTHON"
exec "$PYTHON" -m uvicorn app.main:app --port 8002 --host 127.0.0.1
