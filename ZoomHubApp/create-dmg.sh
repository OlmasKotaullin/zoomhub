#!/bin/bash
# Создание DMG-пакета для ZoomHub
set -e

cd "$(dirname "$0")"

APP_NAME="ZoomHub"
DMG_NAME="ZoomHub-1.1.0"
DMG_DIR="/tmp/${APP_NAME}_dmg"
DMG_PATH="${DMG_NAME}.dmg"

# Сначала собираем приложение
echo "🔨 Сборка приложения..."
bash build.sh

# Очистка
rm -rf "$DMG_DIR" "$DMG_PATH"
mkdir -p "$DMG_DIR"

# Копируем .app
cp -R "${APP_NAME}.app" "$DMG_DIR/"

# Симлинк на Applications
ln -s /Applications "$DMG_DIR/Applications"

# Создаём DMG
echo "📦 Создание DMG..."
hdiutil create -volname "$APP_NAME" \
    -srcfolder "$DMG_DIR" \
    -ov -format UDZO \
    "$DMG_PATH"

# Очистка
rm -rf "$DMG_DIR"

echo "✅ Готово: $DMG_PATH"
echo "   Размер: $(du -h "$DMG_PATH" | cut -f1)"
