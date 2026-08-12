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
let cornerGrab: CGFloat = 18                       // 모서리 사선 드래그 감지 폭
let frameKey = "popupFrame"

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

    private enum Corner { case bottomLeft, bottomRight, topLeft, topRight }
    private enum Mode { case idle, move, resize(Corner) }

    private var mode: Mode = .idle
    private var startMouse = NSPoint.zero      // 화면 좌표
    private var startFrame = NSRect.zero
    private var tracking: NSTrackingArea?

    // 앱이 비활성 상태여도 첫 클릭이 바로 먹혀야 한다.
    // 없으면 위젯을 옮기려면 두 번 클릭해야 한다.
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let t = tracking { removeTrackingArea(t) }
        let t = NSTrackingArea(
            rect: .zero,
            options: [.activeAlways, .mouseMoved, .mouseEnteredAndExited, .inVisibleRect],
            owner: self
        )
        addTrackingArea(t)
        tracking = t
    }

    /// 좌표가 어느 모서리에 있는지. 아니면 nil(= 이동)
    private func corner(at p: NSPoint) -> Corner? {
        let left = p.x <= cornerGrab
        let right = p.x >= bounds.width - cornerGrab
        let bottom = p.y <= cornerGrab
        let top = p.y >= bounds.height - cornerGrab
        switch (left, right, bottom, top) {
        case (true, _, true, _):  return .bottomLeft
        case (_, true, true, _):  return .bottomRight
        case (true, _, _, true):  return .topLeft
        case (_, true, _, true):  return .topRight
        default:                  return nil
        }
    }

    private func cursor(for c: Corner?) -> NSCursor {
        guard let c else { return .openHand }
        if #available(macOS 15.0, *) {
            let position: NSCursor.FrameResizePosition
            switch c {
            case .bottomLeft:  position = .bottomLeft
            case .bottomRight: position = .bottomRight
            case .topLeft:     position = .topLeft
            case .topRight:    position = .topRight
            }
            return NSCursor.frameResize(position: position, directions: .all)
        }
        return .crosshair
    }

    override func mouseMoved(with event: NSEvent) {
        cursor(for: corner(at: convert(event.locationInWindow, from: nil))).set()
    }

    override func mouseExited(with event: NSEvent) {
        NSCursor.arrow.set()
    }

    override func mouseDown(with event: NSEvent) {
        guard let window else { return }
        let p = convert(event.locationInWindow, from: nil)
        startMouse = NSEvent.mouseLocation
        startFrame = window.frame
        if let c = corner(at: p) {
            mode = .resize(c)
        } else {
            mode = .move
            NSCursor.closedHand.set()
        }
    }

    override func mouseDragged(with event: NSEvent) {
        guard let window else { return }
        let now = NSEvent.mouseLocation
        let dx = now.x - startMouse.x
        let dy = now.y - startMouse.y

        switch mode {
        case .idle:
            return

        case .move:
            window.setFrameOrigin(NSPoint(x: startFrame.origin.x + dx,
                                          y: startFrame.origin.y + dy))

        case .resize(let c):
            // 종횡비를 고정한다. 자유 변형을 허용하면 어떤 비율에서든
            // 레이아웃이 깨지는데, 위젯에서 그건 이득이 없다.
            let widened: CGFloat
            switch c {
            case .bottomRight, .topRight: widened = startFrame.width + dx
            case .bottomLeft, .topLeft:   widened = startFrame.width - dx
            }
            let w = min(maxWidth, max(minWidth, widened))
            let h = w / aspect

            // 잡지 않은 반대편 모서리를 제자리에 고정한다
            var origin = startFrame.origin
            switch c {
            case .bottomRight:
                origin = NSPoint(x: startFrame.minX, y: startFrame.maxY - h)
            case .topRight:
                origin = NSPoint(x: startFrame.minX, y: startFrame.minY)
            case .bottomLeft:
                origin = NSPoint(x: startFrame.maxX - w, y: startFrame.maxY - h)
            case .topLeft:
                origin = NSPoint(x: startFrame.maxX - w, y: startFrame.minY)
            }
            window.setFrame(NSRect(origin: origin, size: NSSize(width: w, height: h)),
                            display: true)
        }
    }

    override func mouseUp(with event: NSEvent) {
        mode = .idle
        cursor(for: corner(at: convert(event.locationInWindow, from: nil))).set()
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
