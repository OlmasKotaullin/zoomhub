#!/bin/bash
# ZoomHub Agent — установка автозапуска на Mac
# Запусти один раз: ./install-mac.sh

set -e

AGENT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$HOME/.zoomhub/venv"
PLIST_NAME="com.zoomhub.agent"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"

echo "🎯 Установка ZoomHub Agent"
echo ""

# 1. Создаём виртуальное окружение
echo "📦 Устанавливаю зависимости..."
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install -q httpx telethon 2>/dev/null
echo "   ✅ Готово"

# 2. Проверяем конфиг
if [ ! -f "$HOME/.zoomhub/config.json" ]; then
    echo ""
    echo "⚠️  Конфиг не найден. Запускаю первичную настройку..."
    "$VENV_DIR/bin/python3" "$AGENT_DIR/zoomhub_agent_v2.py" --setup
    exit 0
fi

# 3. Создаём LaunchAgent (автозапуск при входе в систему)
echo "⚙️  Настраиваю автозапуск..."

mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST_PATH" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_NAME}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${VENV_DIR}/bin/python3</string>
        <string>${AGENT_DIR}/zoomhub_agent_v2.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${HOME}/.zoomhub/agent.log</string>
    <key>StandardErrorPath</key>
    <string>${HOME}/.zoomhub/agent.log</string>
    <key>WorkingDirectory</key>
    <string>${AGENT_DIR}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
</dict>
</plist>
PLIST

# 4. Запускаем
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"

echo "   ✅ Автозапуск настроен"
echo ""
echo "🎉 Готово! ZoomHub Agent работает в фоне."
echo ""
echo "   📊 Логи:      tail -f ~/.zoomhub/agent.log"
echo "   🔄 Перезапуск: launchctl kickstart -k gui/\$(id -u)/${PLIST_NAME}"
echo "   ⏹  Остановить: launchctl unload ${PLIST_PATH}"
echo "   ▶️  Запустить:  launchctl load ${PLIST_PATH}"
echo ""
