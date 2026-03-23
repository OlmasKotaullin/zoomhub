#!/bin/bash
# ZoomHub Auto-Setup Script
# Outputs structured progress: STEP:N:message, PROGRESS:N:detail, DONE:N:message, ERROR:N:message

set -uo pipefail

SUPPORT_DIR="$HOME/Library/Application Support/ZoomHub"
VENV_DIR="$SUPPORT_DIR/venv"
DATA_DIR="$SUPPORT_DIR/data"
ENV_FILE="$SUPPORT_DIR/.env"

mkdir -p "$SUPPORT_DIR" "$DATA_DIR/recordings" "$DATA_DIR/logs"

# --- Step 1: Check / Install Ollama ---
echo "STEP:1:Проверяю Ollama..."

OLLAMA_BIN=""
for p in /usr/local/bin/ollama /opt/homebrew/bin/ollama; do
    if [ -x "$p" ]; then
        OLLAMA_BIN="$p"
        break
    fi
done

if [ -z "$OLLAMA_BIN" ] && [ -d "/Applications/Ollama.app" ]; then
    OLLAMA_BIN="/Applications/Ollama.app/Contents/Resources/ollama"
fi

if [ -n "$OLLAMA_BIN" ]; then
    echo "DONE:1:Ollama уже установлена"
else
    echo "PROGRESS:1:Скачиваю Ollama..."
    TMPDIR_OL=$(mktemp -d)
    if curl -fsSL -o "$TMPDIR_OL/Ollama-darwin.zip" "https://ollama.com/download/Ollama-darwin.zip" 2>/dev/null; then
        echo "PROGRESS:1:Устанавливаю Ollama..."
        unzip -q "$TMPDIR_OL/Ollama-darwin.zip" -d "$TMPDIR_OL/" 2>/dev/null
        if [ -d "$TMPDIR_OL/Ollama.app" ]; then
            cp -R "$TMPDIR_OL/Ollama.app" "/Applications/Ollama.app" 2>/dev/null || {
                mkdir -p "$HOME/Applications"
                cp -R "$TMPDIR_OL/Ollama.app" "$HOME/Applications/Ollama.app"
            }
            OLLAMA_BIN="/Applications/Ollama.app/Contents/Resources/ollama"
            [ -x "$OLLAMA_BIN" ] || OLLAMA_BIN="$HOME/Applications/Ollama.app/Contents/Resources/ollama"
            echo "DONE:1:Ollama установлена"
        else
            echo "ERROR:1:Не удалось распаковать Ollama"
        fi
        rm -rf "$TMPDIR_OL"
    else
        rm -rf "$TMPDIR_OL"
        echo "ERROR:1:Не удалось скачать Ollama. Проверьте интернет-соединение"
    fi
fi

# --- Step 2: Start Ollama ---
echo "STEP:2:Запускаю Ollama..."

if curl -sf http://localhost:11434/api/tags &>/dev/null; then
    echo "DONE:2:Ollama уже запущена"
else
    # Try to start Ollama
    if [ -d "/Applications/Ollama.app" ]; then
        open "/Applications/Ollama.app" 2>/dev/null
    elif [ -d "$HOME/Applications/Ollama.app" ]; then
        open "$HOME/Applications/Ollama.app" 2>/dev/null
    elif [ -n "$OLLAMA_BIN" ]; then
        "$OLLAMA_BIN" serve &>/dev/null &
    fi

    echo "PROGRESS:2:Ожидаю запуск Ollama..."
    STARTED=0
    for i in $(seq 1 30); do
        if curl -sf http://localhost:11434/api/tags &>/dev/null; then
            STARTED=1
            break
        fi
        sleep 1
    done

    if [ "$STARTED" -eq 1 ]; then
        echo "DONE:2:Ollama запущена"
    else
        echo "ERROR:2:Не удалось запустить Ollama (таймаут 30 сек)"
    fi
fi

# --- Step 3: Find Python and create venv ---
echo "STEP:3:Настраиваю Python..."

PYTHON=""
for p in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if [ -x "$p" ]; then
        PYTHON="$p"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "ERROR:3:Python 3 не найден. Установите Xcode Command Line Tools: xcode-select --install"
    exit 1
fi

if [ ! -f "$VENV_DIR/bin/python3" ]; then
    echo "PROGRESS:3:Создаю виртуальное окружение..."
    "$PYTHON" -m venv "$VENV_DIR" 2>&1 || {
        echo "ERROR:3:Не удалось создать venv"
        exit 1
    }
fi

echo "DONE:3:Python настроен ($PYTHON)"

# --- Step 4: Install dependencies ---
echo "STEP:4:Устанавливаю зависимости..."

# Find requirements.txt — in BACKEND_PATH (set by app) or relative to script
REQ_FILE=""
if [ -n "${BACKEND_PATH:-}" ] && [ -f "$BACKEND_PATH/requirements.txt" ]; then
    REQ_FILE="$BACKEND_PATH/requirements.txt"
else
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    for candidate in "$SCRIPT_DIR/../Resources/backend/requirements.txt" \
                     "$SCRIPT_DIR/../../requirements.txt" \
                     "$SCRIPT_DIR/../backend/requirements.txt"; do
        if [ -f "$candidate" ]; then
            REQ_FILE="$candidate"
            break
        fi
    done
fi

if [ -z "$REQ_FILE" ]; then
    echo "ERROR:4:requirements.txt не найден"
    exit 1
fi

echo "PROGRESS:4:Обновляю pip..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip 2>&1

echo "PROGRESS:4:Устанавливаю пакеты..."
"$VENV_DIR/bin/pip" install --quiet -r "$REQ_FILE" 2>&1 || {
    echo "ERROR:4:Ошибка установки зависимостей"
    exit 1
}

echo "DONE:4:Зависимости установлены"

# --- Step 5: Pull Ollama model ---
echo "STEP:5:Скачиваю AI-модель..."

# Find ollama binary
if [ -z "${OLLAMA_BIN:-}" ]; then
    for p in /usr/local/bin/ollama /opt/homebrew/bin/ollama; do
        if [ -x "$p" ]; then
            OLLAMA_BIN="$p"
            break
        fi
    done
fi

MODEL_READY=0
if [ -n "$OLLAMA_BIN" ]; then
    if "$OLLAMA_BIN" list 2>/dev/null | grep -q "qwen2.5"; then
        echo "DONE:5:Модель уже скачана"
        MODEL_READY=1
    else
        echo "PROGRESS:5:Загрузка qwen2.5:7b (~4.7 ГБ)..."
        if "$OLLAMA_BIN" pull qwen2.5:7b 2>&1; then
            echo "DONE:5:Модель загружена"
            MODEL_READY=1
        else
            echo "PROGRESS:5:Пробую меньшую модель qwen2.5:3b..."
            if "$OLLAMA_BIN" pull qwen2.5:3b 2>&1; then
                echo "DONE:5:Модель qwen2.5:3b загружена"
                MODEL_READY=1
            else
                echo "ERROR:5:Не удалось скачать модель"
            fi
        fi
    fi
elif curl -sf http://localhost:11434/api/tags &>/dev/null; then
    # Ollama running but binary not in PATH — try via API
    if curl -sf http://localhost:11434/api/tags | grep -q "qwen2.5"; then
        echo "DONE:5:Модель уже скачана"
        MODEL_READY=1
    else
        echo "PROGRESS:5:Загрузка qwen2.5:7b через API..."
        curl -sf http://localhost:11434/api/pull -d '{"name":"qwen2.5:7b"}' | while read -r line; do
            STATUS=$(echo "$line" | grep -o '"status":"[^"]*"' | head -1 | cut -d'"' -f4)
            if [ -n "$STATUS" ]; then
                echo "PROGRESS:5:$STATUS"
            fi
        done
        echo "DONE:5:Модель загружена"
        MODEL_READY=1
    fi
else
    echo "ERROR:5:Ollama не найдена для скачивания модели"
fi

# --- Step 6: Finalize ---
echo "STEP:6:Финализация..."

if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" << 'ENVEOF'
# ZoomHub Configuration

# Claude API (для длинных транскриптов, опционально)
ANTHROPIC_API_KEY=

# LLM провайдер: auto | claude | ollama
LLM_PROVIDER=auto

# Транскрипция: whisper | bukvitsa
TRANSCRIPTION_PROVIDER=whisper
ENVEOF
fi

echo "DONE:6:Готово"
echo "SETUP_COMPLETE"
