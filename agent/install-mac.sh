#!/bin/bash
# ZoomHub Agent — установка на Mac
# Запуск: curl -sL https://raw.githubusercontent.com/OlmasKotaullin/zoomhub/main/agent/install-mac.sh | bash

set -e

INSTALL_DIR="$HOME/.zoomhub/agent"
VENV_DIR="$HOME/.zoomhub/venv"
PLIST_NAME="com.zoomhub.agent"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"
REPO_BASE="https://raw.githubusercontent.com/OlmasKotaullin/zoomhub/main/agent"

echo ""
echo "🎯 ZoomHub Agent — установка"
echo "   Мониторит папку Zoom и автоматически"
echo "   транскрибирует и делает саммари."
echo ""

# 1. Проверяем Python 3
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 не найден. Установите: https://python.org/downloads"
    exit 1
fi
echo "✅ Python 3: $(python3 --version)"

# 2. Скачиваем файлы агента
echo "📥 Скачиваю агент..."
mkdir -p "$INSTALL_DIR"
curl -sL "$REPO_BASE/zoomhub_agent_v2.py" -o "$INSTALL_DIR/zoomhub_agent_v2.py"
curl -sL "$REPO_BASE/bukvitsa_local.py" -o "$INSTALL_DIR/bukvitsa_local.py"
curl -sL "$REPO_BASE/requirements.txt" -o "$INSTALL_DIR/requirements.txt"
echo "   ✅ Файлы скачаны в $INSTALL_DIR"

# 3. Создаём виртуальное окружение и устанавливаем зависимости
echo "📦 Устанавливаю зависимости..."
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt" 2>/dev/null
echo "   ✅ Зависимости установлены"

# 4. Запускаем настройку
echo ""
echo "🔧 Запускаю первичную настройку..."
echo "   Вам понадобится API-токен из https://zoomhub.ru/settings"
echo ""
"$VENV_DIR/bin/python3" "$INSTALL_DIR/zoomhub_agent_v2.py" --setup

# 5. Проверяем что конфиг создан
if [ ! -f "$HOME/.zoomhub/config.json" ]; then
    echo "⚠️  Настройка не завершена. Запустите заново:"
    echo "   $VENV_DIR/bin/python3 $INSTALL_DIR/zoomhub_agent_v2.py --setup"
    exit 1
fi

# 6. Спрашиваем про автозапуск
echo ""
read -p "Настроить автозапуск при входе в систему? [Y/n]: " autostart
if [ "$autostart" != "n" ] && [ "$autostart" != "N" ]; then
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
        <string>${INSTALL_DIR}/zoomhub_agent_v2.py</string>
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
    <string>${INSTALL_DIR}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
</dict>
</plist>
PLIST

    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    launchctl load "$PLIST_PATH"
    echo "   ✅ Автозапуск настроен"
fi

echo ""
echo "🎉 ZoomHub Agent установлен!"
echo ""
echo "   📊 Логи:       tail -f ~/.zoomhub/agent.log"
echo "   🔄 Перезапуск:  launchctl kickstart -k gui/$(id -u)/${PLIST_NAME}"
echo "   ⏹  Остановить:  launchctl unload ${PLIST_PATH}"
echo "   ⚙️  Настроить:  $VENV_DIR/bin/python3 $INSTALL_DIR/zoomhub_agent_v2.py --setup"
echo ""
