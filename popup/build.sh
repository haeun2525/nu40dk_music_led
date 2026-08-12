#!/bin/bash
# 팝업 위젯을 .app 번들로 빌드한다.
#
#   ./build.sh              빌드
#   ./build.sh run          빌드 후 실행 (기존 인스턴스는 종료)
#   ./build.sh autostart    로그인할 때 자동 실행 등록
#   ./build.sh autostop     자동 실행 해제
#
# Xcode는 필요 없다. 커맨드라인 도구의 swiftc만 쓴다.

set -euo pipefail
cd "$(dirname "$0")"

APP="build/NU40DK Popup.app"
NAME="NU40DK Popup"
LABEL="com.nucode.nu40dk.popup"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

# ---------------------------------------------------------------------------
# 로그인 항목 등록/해제. 빌드와 무관하므로 먼저 처리하고 빠진다.
# ---------------------------------------------------------------------------
if [ "${1:-}" = "autostop" ]; then
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "자동 실행을 해제했습니다. (팝업 자체는 그대로 떠 있습니다)"
    exit 0
fi

if [ "${1:-}" = "autostart" ]; then
    BIN="$(pwd)/$APP/Contents/MacOS/NU40DKPopup"
    if [ ! -x "$BIN" ]; then
        echo "먼저 ./build.sh 로 빌드해 주세요." >&2
        exit 1
    fi

    mkdir -p "$(dirname "$PLIST")"
    # KeepAlive를 그냥 true로 두면 우클릭 > 팝업 종료가 무의미해진다.
    # 곧바로 다시 뜨기 때문이다. SuccessfulExit=false 면 정상 종료는 존중하고
    # 비정상 종료(크래시)에만 다시 띄운다.
    cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>            <string>${LABEL}</string>
    <key>ProgramArguments</key> <array><string>${BIN}</string></array>
    <key>RunAtLoad</key>        <true/>
    <key>KeepAlive</key>        <dict><key>SuccessfulExit</key><false/></dict>
    <key>ProcessType</key>      <string>Interactive</string>
    <key>LimitLoadToSessionType</key><string>Aqua</string>
</dict>
</plist>
PLIST_EOF

    # 이미 떠 있는 인스턴스가 있으면 두 개가 된다
    pkill -f "NU40DKPopup" 2>/dev/null || true
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "등록했습니다. 이제 로그인하면 팝업이 자동으로 뜹니다."
    echo "해제: ./build.sh autostop"
    exit 0
fi

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
