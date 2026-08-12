#!/bin/bash
# 팝업 위젯을 .app 번들로 빌드한다.
#
#   ./build.sh          빌드
#   ./build.sh run      빌드 후 실행 (기존 인스턴스는 종료)
#
# Xcode는 필요 없다. 커맨드라인 도구의 swiftc만 쓴다.

set -euo pipefail
cd "$(dirname "$0")"

APP="build/NU40DK Popup.app"
NAME="NU40DK Popup"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# ATS가 http 로컬 접속을 막는다. 127.0.0.1 예외를 넣어야 웹뷰가 페이지를 연다.
# LSUIElement: Dock 아이콘과 메뉴 막대 없이 창만 띄운다.
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>              <string>${NAME}</string>
    <key>CFBundleDisplayName</key>       <string>${NAME}</string>
    <key>CFBundleIdentifier</key>        <string>com.nucode.nu40dk.popup</string>
    <key>CFBundleExecutable</key>        <string>NU40DKPopup</string>
    <key>CFBundlePackageType</key>       <string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>LSMinimumSystemVersion</key>    <string>13.0</string>
    <key>LSUIElement</key>               <true/>
    <key>NSAppTransportSecurity</key>
    <dict>
        <key>NSAllowsLocalNetworking</key><true/>
        <key>NSExceptionDomains</key>
        <dict>
            <key>127.0.0.1</key>
            <dict>
                <key>NSExceptionAllowsInsecureHTTPLoads</key><true/>
            </dict>
        </dict>
    </dict>
</dict>
</plist>
PLIST

swiftc -O main.swift -o "$APP/Contents/MacOS/NU40DKPopup"

# 서명이 없으면 웹뷰가 켜질 때 권한 문제가 날 수 있다. 임시 서명이면 충분하다.
codesign --force --sign - "$APP" 2>/dev/null || true

echo "빌드 완료: $(cd "$(dirname "$APP")" && pwd)/$(basename "$APP")"

if [ "${1:-}" = "run" ]; then
    pkill -f "NU40DKPopup" 2>/dev/null || true
    sleep 0.3
    open "$APP"
    echo "실행했습니다. 종료는 팝업에서 마우스 오른쪽 클릭 > 팝업 종료."
fi
