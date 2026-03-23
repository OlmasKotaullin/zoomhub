#!/bin/bash
# Сборка нативного macOS-приложения ZoomHub (self-contained)
set -e

cd "$(dirname "$0")"
PROJECT_ROOT="$(cd .. && pwd)"

echo "🔨 Компиляция Swift..."
swiftc -parse-as-library -framework SwiftUI -framework AppKit -framework UniformTypeIdentifiers -framework UserNotifications \
  Sources/ZoomHubApp/Models/Meeting.swift \
  Sources/ZoomHubApp/Services/APIClient.swift \
  Sources/ZoomHubApp/Services/BackendManager.swift \
  Sources/ZoomHubApp/Services/SetupManager.swift \
  Sources/ZoomHubApp/Services/UpdateChecker.swift \
  Sources/ZoomHubApp/Services/ExportService.swift \
  Sources/ZoomHubApp/Services/NotificationManager.swift \
  Sources/ZoomHubApp/ZoomHubApp.swift \
  Sources/ZoomHubApp/Views/ContentView.swift \
  Sources/ZoomHubApp/Views/DashboardView.swift \
  Sources/ZoomHubApp/Views/MeetingDetailView.swift \
  Sources/ZoomHubApp/Views/ChatTabView.swift \
  Sources/ZoomHubApp/Views/SettingsView.swift \
  Sources/ZoomHubApp/Views/UploadView.swift \
  Sources/ZoomHubApp/Views/MenuBarView.swift \
  Sources/ZoomHubApp/Views/OnboardingView.swift \
  -o ZoomHub \
  -target arm64-apple-macosx14.0 \
  -swift-version 5 \
  -O

echo "📦 Создание .app бандла..."
rm -rf ZoomHub.app
mkdir -p ZoomHub.app/Contents/MacOS ZoomHub.app/Contents/Resources

cp ZoomHub ZoomHub.app/Contents/MacOS/

# Info.plist
cat > ZoomHub.app/Contents/Info.plist << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleDevelopmentRegion</key>
	<string>ru</string>
	<key>CFBundleExecutable</key>
	<string>ZoomHub</string>
	<key>CFBundleIdentifier</key>
	<string>com.almazgataullin.ZoomHubApp</string>
	<key>CFBundleInfoDictionaryVersion</key>
	<string>6.0</string>
	<key>CFBundleIconFile</key>
	<string>AppIcon</string>
	<key>CFBundleName</key>
	<string>ZoomHub</string>
	<key>CFBundlePackageType</key>
	<string>APPL</string>
	<key>CFBundleShortVersionString</key>
	<string>1.1.0</string>
	<key>CFBundleVersion</key>
	<string>2</string>
	<key>LSMinimumSystemVersion</key>
	<string>14.0</string>
	<key>NSPrincipalClass</key>
	<string>NSApplication</string>
</dict>
</plist>
PLIST

# Иконка
if [ -f Sources/ZoomHubApp/Resources/AppIcon.icns ]; then
  cp Sources/ZoomHubApp/Resources/AppIcon.icns ZoomHub.app/Contents/Resources/AppIcon.icns
fi

# Setup script
if [ -f Resources/setup.sh ]; then
  cp Resources/setup.sh ZoomHub.app/Contents/Resources/setup.sh
  chmod +x ZoomHub.app/Contents/Resources/setup.sh
fi

# Embed backend code
echo "📦 Встраиваю backend..."
BACKEND_DEST="ZoomHub.app/Contents/Resources/backend"
mkdir -p "$BACKEND_DEST"

# Copy Python backend
cp -R "$PROJECT_ROOT/app" "$BACKEND_DEST/app"
cp "$PROJECT_ROOT/requirements.txt" "$BACKEND_DEST/requirements.txt"

# Copy templates and static if they exist
if [ -d "$PROJECT_ROOT/app/templates" ]; then
  # Already copied with app/
  true
fi

# Remove __pycache__ to keep bundle clean
find "$BACKEND_DEST" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "$BACKEND_DEST" -name "*.pyc" -delete 2>/dev/null || true

echo "APPL????" > ZoomHub.app/Contents/PkgInfo

# Cleanup temp binary
rm -f ZoomHub

echo "✅ Готово: ZoomHub.app (self-contained)"
echo "   Backend встроен в Resources/backend/"
echo "   Setup script в Resources/setup.sh"
echo "   Запуск: open ZoomHub.app"
