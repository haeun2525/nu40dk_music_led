// NU40DK Popup — 지금 재생 중인 곡을 데스크톱 위에 띄우는 위젯.
//
// 창은 테두리가 없고 배경이 투명하다. 안에 WKWebView를 얹고
// server.py의 /popup 페이지를 띄운다. 디자인은 전부 그 HTML에 있고,
// 이 파일은 "어디에, 얼마나 크게, 무엇보다 위에" 만 담당한다.
//
// 웹뷰는 마우스를 먹으므로 그 위에 투명한 DragView를 덮는다.
// 재생 조작은 보드가 하니까 페이지에 누를 것이 없다. 덕분에 이 구조가 가능하다.

import AppKit
import WebKit

// MARK: - 설정

let pageURL = URL(string: "http://127.0.0.1:8765/popup")!

// 창은 보이는 카드보다 사방 20pt씩 크다. 그 여백에 CSS 그림자가 번진다.
// 창이 카드에 딱 맞으면 그림자가 잘려 사각 테두리로 보인다.
let designSize = NSSize(width: 400, height: 168)
let aspect = designSize.width / designSize.height
let minWidth: CGFloat = 260
let maxWidth: CGFloat = 900
let screenMargin: CGFloat = 16                     // 우측 상단에서 띄울 간격
let frameKey = "popupFrame"

// 크기 조정을 잡는 폭. 카드의 보이는 테두리에서 안쪽으로 이만큼.
// 창 가장자리가 아니라 카드 테두리가 기준이다. 둘은 그림자 여백만큼 떨어져 있고,
// 사용자가 겨냥하는 것은 눈에 보이는 카드 쪽이다.
let edgeGrab: CGFloat = 10
let cornerGrab: CGFloat = 26

// 카드는 창 안쪽으로 이만큼 들어가 있다. popup.html의 body padding과 같은 값이어야 한다.
//   padding 1.25rem, 1rem = 창너비/25  →  창너비/20
func cardInset(forWidth w: CGFloat) -> CGFloat { w / 20 }

// MARK: - 창
//
// NSPanel + .nonactivatingPanel 이라야 위젯을 만져도 쓰던 앱의 포커스를
// 빼앗지 않는다. 일반 NSWindow로 하면 드래그할 때마다 앞 앱이 바뀐다.

final class PopupPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

// MARK: - 마우스 처리 (이동 / 크기 조정)

final class DragView: NSView {

    /// 어디를 잡았는가. move 를 뺀 나머지는 전부 크기 조정이다.
    private enum Grip {
        case move
        case left, right, top, bottom
        case topLeft, topRight, bottomLeft, bottomRight

        var isResize: Bool { if case .move = self { return false }; return true }
    }

    private var grip: Grip = .move             // 드래그 중인 동작
    private var dragging = false
    private var startMouse = NSPoint.zero      // 화면 좌표
    private var startFrame = NSRect.zero
    private var shownGrip: Grip?               // 지금 커서에 반영된 것. 매번 set() 하지 않기 위해
    private var tracking: NSTrackingArea?

    // 앱이 비활성 상태여도 첫 클릭이 바로 먹혀야 한다.
    // 없으면 위젯을 옮기려면 두 번 클릭해야 한다.
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let t = tracking { removeTrackingArea(t) }
        let t = NSTrackingArea(
            rect: .zero,
            // activeAlways: 이 앱은 거의 항상 비활성 상태다. 그래도 커서는 바뀌어야 한다.
            options: [.activeAlways, .mouseMoved, .mouseEnteredAndExited,
                      .cursorUpdate, .inVisibleRect],
            owner: self
        )
        addTrackingArea(t)
        tracking = t
    }

    /// 좌표가 카드의 어느 가장자리에 있는지.
    ///
    /// 기준은 창이 아니라 **눈에 보이는 카드의 테두리**다. 창은 그림자가 번질
    /// 자리만큼 카드보다 크다. 창 가장자리로 재면 감지 영역이 통째로 투명한
    /// 여백에 들어앉아서, 카드 테두리를 겨냥한 마우스에는 아무 반응이 없다.
    private func grip(at p: NSPoint) -> Grip {
        let inset = cardInset(forWidth: bounds.width)

        // 바깥 여백(투명 영역)도 그 변에 속한 것으로 친다. 카드 밖이라고
        // 이동으로 처리하면 테두리 바로 옆에서 동작이 뒤바뀐다.
        func near(_ v: CGFloat, _ limit: CGFloat, _ grab: CGFloat) -> Bool {
            v <= inset + grab || v >= limit - inset - grab
        }
        let w = bounds.width, h = bounds.height
        let cornerX = p.x <= inset + cornerGrab || p.x >= w - inset - cornerGrab
        let cornerY = p.y <= inset + cornerGrab || p.y >= h - inset - cornerGrab
        let isLeft = p.x < w / 2, isBottom = p.y < h / 2

        if cornerX && cornerY {
            switch (isLeft, isBottom) {
            case (true, true):   return .bottomLeft
            case (false, true):  return .bottomRight
            case (true, false):  return .topLeft
            case (false, false): return .topRight
            }
        }
        if near(p.x, w, edgeGrab) { return isLeft ? .left : .right }
        if near(p.y, h, edgeGrab) { return isBottom ? .bottom : .top }
        return .move
    }

    private func cursor(for g: Grip) -> NSCursor {
        if case .move = g { return .openHand }
        if #available(macOS 15.0, *) {
            let position: NSCursor.FrameResizePosition
            switch g {
            case .left:        position = .left
            case .right:       position = .right
            case .top:         position = .top
            case .bottom:      position = .bottom
            case .topLeft:     position = .topLeft
            case .topRight:    position = .topRight
            case .bottomLeft:  position = .bottomLeft
            case .bottomRight: position = .bottomRight
            case .move:        return .openHand
            }
            return NSCursor.frameResize(position: position, directions: .all)
        }
        return .crosshair
    }

    /// 지금 위치에 맞는 커서를 세운다. 바뀔 때만 부른다(매 이동마다 set 하면 깜빡인다).
    private func refreshCursor(at p: NSPoint, force: Bool = false) {
        let g = grip(at: p)
        guard force || g != shownGrip else { return }
        shownGrip = g
        cursor(for: g).set()
    }

    override func mouseMoved(with event: NSEvent) {
        guard !dragging else { return }
        refreshCursor(at: convert(event.locationInWindow, from: nil))
    }

    // 커서는 창 밖으로 나갔다 오거나 다른 앱이 건드리면 초기화된다.
    // 영역에 들어올 때마다 다시 세운다.
    override func cursorUpdate(with event: NSEvent) {
        refreshCursor(at: convert(event.locationInWindow, from: nil), force: true)
    }

    override func mouseEntered(with event: NSEvent) {
        refreshCursor(at: convert(event.locationInWindow, from: nil), force: true)
    }

    override func mouseExited(with event: NSEvent) {
        shownGrip = nil
        NSCursor.arrow.set()
    }

    override func mouseDown(with event: NSEvent) {
        guard let window else { return }
        startMouse = NSEvent.mouseLocation
        startFrame = window.frame
        grip = grip(at: convert(event.locationInWindow, from: nil))
        dragging = true
        if !grip.isResize { NSCursor.closedHand.set() }
    }

    override func mouseDragged(with event: NSEvent) {
        guard let window else { return }
        let now = NSEvent.mouseLocation
        let dx = now.x - startMouse.x
        let dy = now.y - startMouse.y

        guard dragging else { return }

        if case .move = grip {
            window.setFrameOrigin(NSPoint(x: startFrame.origin.x + dx,
                                          y: startFrame.origin.y + dy))
            return
        }

        // 종횡비를 고정한다. 자유 변형을 허용하면 어떤 비율에서든 레이아웃이
        // 깨지는데, 위젯에서 그건 이득이 없다. 위아래 변은 높이로, 나머지는
        // 너비로 크기를 정하고 반대쪽을 따라오게 한다.
        let wanted: CGFloat
        switch grip {
        case .right, .topRight, .bottomRight: wanted = startFrame.width + dx
        case .left, .topLeft, .bottomLeft:    wanted = startFrame.width - dx
        case .top:                            wanted = (startFrame.height + dy) * aspect
        case .bottom:                         wanted = (startFrame.height - dy) * aspect
        case .move:                           return
        }
        let w = min(maxWidth, max(minWidth, wanted))
        let h = w / aspect

        // 잡지 않은 쪽을 제자리에 고정한다
        let origin: NSPoint
        switch grip {
        case .right, .bottom, .bottomRight:
            origin = NSPoint(x: startFrame.minX, y: startFrame.maxY - h)
        case .top, .topRight:
            origin = NSPoint(x: startFrame.minX, y: startFrame.minY)
        case .left, .bottomLeft:
            origin = NSPoint(x: startFrame.maxX - w, y: startFrame.maxY - h)
        case .topLeft:
            origin = NSPoint(x: startFrame.maxX - w, y: startFrame.minY)
        case .move:
            return
        }
        window.setFrame(NSRect(origin: origin, size: NSSize(width: w, height: h)),
                        display: true)
    }

    override func mouseUp(with event: NSEvent) {
        dragging = false
        grip = .move
        refreshCursor(at: convert(event.locationInWindow, from: nil), force: true)
        if let window {
            UserDefaults.standard.set(NSStringFromRect(window.frame), forKey: frameKey)
        }
    }
}

// MARK: - 앱

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate {

    var panel: PopupPanel!
    var webView: WKWebView!
    private var retryTimer: Timer?

    func applicationDidFinishLaunching(_ note: Notification) {
        NSApp.setActivationPolicy(.accessory)   // Dock 아이콘 없이 뜬다

        panel = PopupPanel(
            contentRect: NSRect(origin: .zero, size: designSize),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.isFloatingPanel = true
        panel.level = .floating                 // 일반 창들보다 위
        panel.collectionBehavior = [.canJoinAllSpaces,   // 어느 데스크톱으로 가도 따라온다
                                    .stationary,
                                    .fullScreenAuxiliary] // 전체화면 앱 위에도 뜬다
        panel.isOpaque = false
        panel.backgroundColor = .clear
        // 네이티브 그림자는 창의 사각형을 따라 그려져서 둥근 카드와 어긋난다.
        // 그림자는 CSS로 그리고 여기서는 끈다.
        panel.hasShadow = false
        panel.isMovableByWindowBackground = false   // 이동은 DragView가 직접 한다
        panel.hidesOnDeactivate = false
        panel.aspectRatio = designSize
        panel.acceptsMouseMovedEvents = true        // 없으면 커서가 바뀌지 않는다
        // AppKit은 마우스가 움직일 때마다 커서 사각형을 보고 커서를 되돌린다.
        // 그러면 우리가 세운 크기 조정 커서가 곧바로 화살표로 덮인다.
        panel.disableCursorRects()

        // 웹뷰
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()
        webView = WKWebView(frame: NSRect(origin: .zero, size: designSize), configuration: config)
        webView.autoresizingMask = [.width, .height]
        webView.navigationDelegate = self
        webView.setValue(false, forKey: "drawsBackground")   // 흰 배경 제거
        webView.underPageBackgroundColor = .clear
        webView.wantsLayer = true
        webView.layer?.backgroundColor = .clear

        let container = NSView(frame: NSRect(origin: .zero, size: designSize))
        container.addSubview(webView)

        let drag = DragView(frame: container.bounds)
        drag.autoresizingMask = [.width, .height]
        drag.menu = buildMenu()
        container.addSubview(drag)

        panel.contentView = container

        // 저장해둔 위치가 있으면 그 자리에, 없으면 우측 상단
        if let saved = UserDefaults.standard.string(forKey: frameKey) {
            panel.setFrame(sanitize(NSRectFromString(saved)), display: false)
        } else {
            panel.setFrame(defaultFrame(), display: false)
        }

        panel.orderFrontRegardless()
        load()
    }

    // MARK: 위치

    private func defaultFrame() -> NSRect {
        let visible = (NSScreen.main ?? NSScreen.screens[0]).visibleFrame
        return NSRect(x: visible.maxX - designSize.width - screenMargin,
                      y: visible.maxY - designSize.height - screenMargin,
                      width: designSize.width,
                      height: designSize.height)
    }

    /// 저장된 위치를 되살리되 지금 화면과 지금 종횡비에 맞춘다.
    /// 모니터 구성이 바뀌면 화면 밖 좌표가, 디자인이 바뀌면 옛 비율이 남는다.
    private func sanitize(_ frame: NSRect) -> NSRect {
        guard frame.width >= minWidth else { return defaultFrame() }
        let w = min(maxWidth, max(minWidth, frame.width))
        // 위쪽 가장자리를 기준으로 다시 잡는다. 우측 상단에 두는 위젯이라
        // 아래로 자라는 편이 자연스럽다.
        let fixed = NSRect(x: frame.minX, y: frame.maxY - w / aspect,
                           width: w, height: w / aspect)
        let onScreen = NSScreen.screens.contains { $0.visibleFrame.intersects(fixed) }
        return onScreen ? fixed : defaultFrame()
    }

    // MARK: 페이지

    private func load() {
        webView.load(URLRequest(url: pageURL, cachePolicy: .reloadIgnoringLocalCacheData))
    }

    /// 서버가 아직 안 떠 있을 수 있다. 안내를 띄우고 계속 두드린다.
    func webView(_ webView: WKWebView, didFailProvisionalNavigation nav: WKNavigation!,
                 withError error: Error) {
        showWaiting()
        scheduleRetry()
    }

    func webView(_ webView: WKWebView, didFail nav: WKNavigation!, withError error: Error) {
        showWaiting()
        scheduleRetry()
    }

    func webView(_ webView: WKWebView, didFinish nav: WKNavigation!) {
        retryTimer?.invalidate()
        retryTimer = nil
    }

    private func scheduleRetry() {
        guard retryTimer == nil else { return }
        retryTimer = Timer.scheduledTimer(withTimeInterval: 3.0, repeats: true) { [weak self] _ in
            self?.load()
        }
    }

    private func showWaiting() {
        let html = """
        <html><head><meta charset="utf-8"><style>
        html,body{margin:0;height:100%;background:transparent;overflow:hidden;
          font-family:-apple-system,"Apple SD Gothic Neo",sans-serif;-webkit-user-select:none}
        .c{margin:20px;height:calc(100% - 40px);border-radius:18px;
          background:rgba(17,18,22,.88);border:1px solid rgba(255,255,255,.10);
          box-shadow:0 2px 4px rgba(0,0,0,.30),0 4px 12px rgba(0,0,0,.28);
          display:flex;flex-direction:column;align-items:center;justify-content:center;gap:6px}
        .t{color:rgba(255,255,255,.9);font-size:13px;font-weight:600}
        .s{color:rgba(255,255,255,.42);font-size:11px}
        </style></head><body><div class="c">
        <div class="t">서버를 기다리는 중</div>
        <div class="s">host/server.py 를 실행해 주세요</div>
        </div></body></html>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }

    // MARK: 메뉴
    //
    // Dock 아이콘도 창 버튼도 없다. 종료할 방법이 여기밖에 없으므로 반드시 필요하다.

    private func buildMenu() -> NSMenu {
        let menu = NSMenu()
        menu.addItem(withTitle: "위치·크기 초기화", action: #selector(resetFrame), keyEquivalent: "")
        menu.addItem(withTitle: "새로고침", action: #selector(reload), keyEquivalent: "")
        menu.addItem(.separator())
        menu.addItem(withTitle: "팝업 종료", action: #selector(quit), keyEquivalent: "")
        menu.items.forEach { $0.target = self }
        return menu
    }

    @objc private func resetFrame() {
        UserDefaults.standard.removeObject(forKey: frameKey)
        panel.setFrame(defaultFrame(), display: true, animate: true)
    }

    @objc private func reload() { load() }

    @objc private func quit() { NSApp.terminate(nil) }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
