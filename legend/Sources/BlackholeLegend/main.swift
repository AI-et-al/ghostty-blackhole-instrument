// BlackholeLegend — a hover legend for the black hole instrument.
//
// Ghostty does not give shaders the mouse position, so the legend lives
// outside the terminal: this background app watches the pointer, works out
// where the hole is in the Ghostty window under it, and while the pointer is
// on the hole shows a small panel with what each read-out means and its live
// value. It only reads: the pointer position, window bounds, Ghostty's
// scripting interface (which tab is showing which tty) and the state files
// blackhole_instrument.py writes. It never writes to a terminal. A click on the
// panel asks the bridge to open Tokenograph's live panel for that session.
//
//   BlackholeLegend [--shader <path.glsl>]     run the helper
//   BlackholeLegend --center <fill> <aspect> <time> [--shader <path>]
//                                            print the hole position (for tests)
//   BlackholeLegend --probe [--shader <path>]  print what the helper sees, once
//   BlackholeLegend --open-panel <session>     do what a click on the legend does
//   BlackholeLegend --render <file.png>        draw the legend to an image
//
// Optional tuning, ~/.cache/blackhole-instrument/legend-config.json:
//   {"time_offset": 0.0, "top_inset": 32, "tab_bar": 28}
// Debug ring showing where the helper thinks the hole is:
//   touch ~/.cache/blackhole-instrument/legend-debug     (rm it to turn off)

import AppKit
import SwiftUI

// ---------------------------------------------------------------- inputs --
func argument(after flag: String) -> String? {
    let a = CommandLine.arguments
    guard let i = a.firstIndex(of: flag), i + 1 < a.count else { return nil }
    return a[i + 1]
}

func shaderPath() -> String? {
    if let p = argument(after: "--shader") { return p }
    let config = (NSHomeDirectory() as NSString).appendingPathComponent(".config/ghostty/config")
    guard let text = try? String(contentsOfFile: config, encoding: .utf8) else { return nil }
    for line in text.split(separator: "\n") {
        let l = line.trimmingCharacters(in: .whitespaces)
        if l.hasPrefix("custom-shader ") || l.hasPrefix("custom-shader=") {
            return l.split(separator: "=", maxSplits: 1).last.map { $0.trimmingCharacters(in: .whitespaces) }
        }
    }
    return nil
}

final class TunableStore {
    private let path: String?
    private var stamp: Date?
    private(set) var current = Tunables()

    init(path: String?) { self.path = path; refresh() }

    func refresh() {
        guard let path, let attrs = try? FileManager.default.attributesOfItem(atPath: path),
              let modified = attrs[.modificationDate] as? Date, modified != stamp,
              let text = try? String(contentsOfFile: path, encoding: .utf8) else { return }
        stamp = modified
        current = Tunables.parse(text)
    }
}

struct LegendConfig {
    var timeOffset = 0.0
    var topInset: Double?
    var tabBar = 28.0
    var debug = false

    static func load() -> LegendConfig {
        var c = LegendConfig()
        let dir = stateDirectory as NSString
        c.debug = FileManager.default.fileExists(atPath: dir.appendingPathComponent("legend-debug"))
        if let data = FileManager.default.contents(atPath: dir.appendingPathComponent("legend-config.json")),
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            c.timeOffset = obj["time_offset"] as? Double ?? 0
            c.topInset = obj["top_inset"] as? Double
            c.tabBar = obj["tab_bar"] as? Double ?? 28
        }
        return c
    }
}

// --------------------------------------------------------------- windows --
struct ScreenWindow {
    var owner: String
    var pid: pid_t
    var bounds: CGRect          // CG coordinates: origin top-left of the primary screen
}

/// Normal windows, front to back.
func onScreenWindows() -> [ScreenWindow] {
    guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
            as? [[String: Any]] else { return [] }
    var out: [ScreenWindow] = []
    for w in list {
        guard (w[kCGWindowLayer as String] as? Int) == 0,
              (w[kCGWindowAlpha as String] as? Double ?? 1) > 0.01,
              let b = w[kCGWindowBounds as String] as? [String: Any],
              let rect = CGRect(dictionaryRepresentation: b as CFDictionary),
              rect.width > 120, rect.height > 80 else { continue }
        out.append(ScreenWindow(owner: w[kCGWindowOwnerName as String] as? String ?? "",
                                pid: pid_t(w[kCGWindowOwnerPID as String] as? Int ?? 0), bounds: rect))
    }
    return out
}

/// Height of Ghostty's title bar. AppKit reports 28 pt for a plain titled
/// window, but Ghostty's measures 32 pt on macOS 26 and later (read off a
/// screenshot); `top_inset` in legend-config.json overrides this.
let titleBarHeight: Double = {
    let plain = Double(NSWindow.frameRect(forContentRect: .zero, styleMask: [.titled]).height)
    return ProcessInfo.processInfo.operatingSystemVersion.majorVersion >= 26 ? max(plain, 32) : plain
}()

// ----------------------------------------------------------------- hover --
struct Sighting {
    var surface: CGRect         // the terminal surface, CG coordinates
    var center: CGPoint         // hole centre, CG coordinates
    var radius: Double          // shadow radius, points
    var hoverRadius: Double
    var box: CGRect             // roam box, CG coordinates
    var tracked: Bool           // false: the shader clock is unknown, the roam box is the target
    var session: SessionState
}

final class Tracker {
    let tunables: TunableStore
    var config = LegendConfig.load()
    private var sessions: [SessionState] = []
    private var sessionsAt = Date.distantPast
    private var tabs: [GhosttyTab]?
    private var tabsAt = Date.distantPast
    private var tabsBusy = false
    private var starts: [String: (value: Double?, at: Date)] = [:]
    private let work = DispatchQueue(label: "blackhole.legend.lookup")

    init(shader: String?) { tunables = TunableStore(path: shader) }

    private func refresh(now: Date) {
        if now.timeIntervalSince(sessionsAt) > 0.5 {
            sessions = loadSessions()
            sessionsAt = now
            config = LegendConfig.load()
            tunables.refresh()
        }
        if now.timeIntervalSince(tabsAt) > 2, !tabsBusy {        // Apple events are slow: off the main thread
            tabsBusy = true
            work.async {
                let result = queryGhostty()
                DispatchQueue.main.async { self.tabs = result; self.tabsAt = Date(); self.tabsBusy = false }
            }
        }
    }

    private func start(for tty: String, now: Date) -> Double? {
        if let cached = starts[tty], now.timeIntervalSince(cached.at) < 30 { return cached.value }
        starts[tty] = (starts[tty]?.value, now)                    // hold the old value while refreshing
        work.async {
            let value = surfaceStart(tty: tty)
            DispatchQueue.main.async { self.starts[tty] = (value, Date()) }
        }
        return starts[tty]?.value
    }

    /// The latest state for a session already on show.
    func session(key: String) -> SessionState? {
        refresh(now: Date())
        return sessions.first { $0.key == key }
    }

    /// The hole in the Ghostty window under `point` (CG coordinates), if any.
    func sight(at point: CGPoint, now: Date = Date()) -> Sighting? {
        let windows = onScreenWindows()
        guard let top = windows.firstIndex(where: { $0.bounds.contains(point) }),
              windows[top].owner == "Ghostty" else { return nil }
        refresh(now: now)
        let ghostty = windows.enumerated().filter { $0.element.owner == "Ghostty" && $0.element.pid == windows[top].pid }
        let order = ghostty.firstIndex { $0.offset == top } ?? 0

        // which session is this window showing?
        var tab: GhosttyTab?
        if let tabs, !tabs.isEmpty {
            if tabs.count == ghostty.count { tab = tabs[order] } else if order == 0 { tab = tabs[0] }
        }
        let session: SessionState?
        if let tab {
            session = sessions.first { $0.tty == tab.tty }
        } else if tabs == nil {                                    // Ghostty could not be asked: best guess
            session = sessions.max { $0.updated < $1.updated }
        } else {
            session = nil
        }
        guard let session else { return nil }

        // the terminal surface inside the window
        let win = windows[top].bounds
        let fullScreen = NSScreen.screens.contains { abs($0.frame.width - win.width) < 1 && abs($0.frame.height - win.height) < 1 }
        var inset = fullScreen ? 0 : (config.topInset ?? titleBarHeight)
        if !fullScreen, let tab, tab.tabCount > 1 { inset += config.tabBar }
        let surface = CGRect(x: win.minX, y: win.minY + inset, width: win.width, height: win.height - inset)
        guard surface.height > 40 else { return nil }

        let began = start(for: session.tty, now: now)
        let time = began.map { uptimeNow() - $0 + config.timeOffset }
        let g = holeGeometry(fill: session.fill, aspect: surface.width / surface.height,
                             time: time ?? 0, tunables.current)
        let radius = g.radius * surface.height
        let toScreen = { (u: Double, v: Double) in CGPoint(x: surface.minX + u * surface.width, y: surface.minY + v * surface.height) }
        let lo = toScreen(g.boxLo.x, g.boxLo.y), hi = toScreen(g.boxHi.x, g.boxHi.y)
        return Sighting(surface: surface, center: toScreen(g.center.x, g.center.y), radius: radius,
                        hoverRadius: max(radius * 1.9, 30),
                        box: CGRect(x: lo.x, y: lo.y, width: hi.x - lo.x, height: hi.y - lo.y),
                        tracked: time != nil, session: session)
    }
}

func isHovering(_ s: Sighting, _ p: CGPoint) -> Bool {
    if s.tracked { return hypot(p.x - s.center.x, p.y - s.center.y) <= s.hoverRadius }
    return s.box.insetBy(dx: -s.radius * 1.5, dy: -s.radius * 1.5).contains(p)
}

// --------------------------------------------------------------- content --
func ago(_ seconds: Double) -> String {
    if seconds < 90 { return "\(Int(max(seconds, 0)))s ago" }
    if seconds < 5400 { return "\(Int(seconds / 60))m ago" }
    return "\(Int(seconds / 3600))h ago"
}

func legendContent(for s: SessionState, now: Date = Date(), opening: Bool = false) -> LegendContent {
    let action = !s.hasTranscript ? "" : opening ? "Opening Tokenograph…" : "Click to open in Tokenograph"
    let speed: String
    if s.working == false { speed = "idle" }
    else if let t = s.tokensPerSecond { speed = "\(Int(t.rounded())) tok/s" }
    else { speed = "\(Int((s.burn * 100).rounded()))%" }

    // the same white-to-amber swing the disk makes
    let warm = max(0, min(1, (1 - s.heat) * 1.6))
    let heatTint = Color(red: 1, green: 1 - 0.35 * warm, blue: 1 - 0.75 * warm)

    var flare = "none yet"
    if let kind = s.lastFlareKind, let at = s.lastFlareAt {
        flare = (kind == 2 ? "compaction" : "cache rebuild") + " · " + ago(now.timeIntervalSince1970 - at)
    }
    var footer = ""
    if let r = s.rebuilds, let c = s.compactions {
        footer = "This session: \(r) cache rebuild\(r == 1 ? "" : "s") · \(c) compaction\(c == 1 ? "" : "s")"
    }
    return LegendContent(rows: [
        LegendRow(symbol: "circle.circle", measure: "Context window", cue: "Size of the hole",
                  value: "\(Int((s.fill * 100).rounded()))%"),
        LegendRow(symbol: "sun.max", measure: "Output speed", cue: "Brightness and spin of the disk", value: speed),
        LegendRow(symbol: "thermometer.medium", measure: "Cache health", cue: "Disk colour: white healthy, amber lost",
                  value: "\(Int((s.heat * 100).rounded()))%", tint: heatTint),
        LegendRow(symbol: "sparkle", measure: "Flares", cue: "Ring through the disk", value: flare),
    ], footer: footer, action: action)
}

/// The bridge sits beside the shader's folder: <root>/blackhole_instrument.py
func bridgePath() -> String? {
    guard let shader = shaderPath() else { return nil }
    let root = ((shader as NSString).deletingLastPathComponent as NSString).deletingLastPathComponent
    let path = root + "/blackhole_instrument.py"
    return FileManager.default.isExecutableFile(atPath: path) ? path : nil
}

/// Open Tokenograph's live panel for a session, through the bridge (which
/// starts one local server per session and reuses it). Returns the URL.
@discardableResult
func openTokenographNow(session key: String) -> String? {
    guard let bridge = bridgePath() else { return nil }
    return runTool("/usr/bin/python3", [bridge, "panel", key], timeout: 12)?
        .trimmingCharacters(in: .whitespacesAndNewlines)
}

func openTokenograph(session key: String) {
    DispatchQueue.global(qos: .userInitiated).async { openTokenographNow(session: key) }
}

// -------------------------------------------------------------------- run --
let shader = shaderPath()

if let i = CommandLine.arguments.firstIndex(of: "--center"), i + 3 < CommandLine.arguments.count {
    let a = CommandLine.arguments
    guard let fill = Double(a[i + 1]), let aspect = Double(a[i + 2]), let time = Double(a[i + 3]) else { exit(2) }
    let g = holeGeometry(fill: fill, aspect: aspect, time: time, TunableStore(path: shader).current)
    print(String(format: "%.5f %.5f %.5f", g.center.x, g.center.y, g.radius))
    exit(0)
}

func cgPoint(fromCocoa p: NSPoint) -> CGPoint {
    CGPoint(x: p.x, y: (NSScreen.screens.first?.frame.height ?? 0) - p.y)
}

func cocoaRect(fromCG r: CGRect) -> NSRect {
    NSRect(x: r.minX, y: (NSScreen.screens.first?.frame.height ?? 0) - r.maxY, width: r.width, height: r.height)
}

// --open-panel <session>: what a click on the legend does, run once
if let key = argument(after: "--open-panel") {
    print(openTokenographNow(session: key) ?? "could not open a panel for \(key)")
    exit(0)
}

if CommandLine.arguments.contains("--probe") {
    print("shader:", shader ?? "none")
    print("title bar height:", titleBarHeight)
    let tabs = queryGhostty()
    print("ghostty windows:", tabs.map { $0.map { "\($0.tty) tabs=\($0.tabCount) splits=\($0.splitCount)" }.joined(separator: "; ") } ?? "could not query")
    let k = TunableStore(path: shader).current
    for w in onScreenWindows() where w.owner == "Ghostty" { print("ghostty window bounds:", w.bounds) }
    for s in loadSessions() {
        let began = surfaceStart(tty: s.tty)
        let time = began.map { uptimeNow() - $0 }
        print("session \(s.tty): fill \(Int(s.fill * 100))% burn \(Int(s.burn * 100))% heat \(Int(s.heat * 100))%",
              "tok/s \(s.tokensPerSecond.map { String(Int($0)) } ?? "-") working \(s.working.map(String.init) ?? "-")",
              "surface age \(time.map { String(format: "%.1fs", $0) } ?? "unknown")")
        if let time {
            let g = holeGeometry(fill: s.fill, aspect: 1.5, time: time, k)
            print(String(format: "   hole at uv (%.3f, %.3f), radius %.3f of height (for a 3:2 surface)", g.center.x, g.center.y, g.radius))
        }
    }
    exit(0)
}

// --render <file.png>: draw the legend for the newest session (or sample
// numbers) to an image, to check its layout without a screen
if let out = argument(after: "--render") {
    let sample = SessionState(key: "sample", tty: "/dev/ttys000", hasTranscript: true, fill: 0.42, burn: 0.7, heat: 0.31, tokensPerSecond: 115,
                              working: true, rebuilds: 2, compactions: 1, lastFlareKind: 1,
                              lastFlareAt: Date().timeIntervalSince1970 - 134, updated: Date())
    let session = CommandLine.arguments.contains("--sample") ? sample
        : (loadSessions().max { $0.updated < $1.updated } ?? sample)
    let model = LegendModel()
    model.content = legendContent(for: session)
    let host = NSHostingView(rootView: LegendView(model: model).background(Color(white: 0.11)).environment(\.colorScheme, .dark))
    host.frame = NSRect(origin: .zero, size: host.fittingSize)
    host.layoutSubtreeIfNeeded()
    guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { exit(1) }
    host.cacheDisplay(in: host.bounds, to: rep)
    guard let png = rep.representation(using: .png, properties: [:]) else { exit(1) }
    try? png.write(to: URL(fileURLWithPath: out))
    print("rendered \(Int(host.bounds.width))x\(Int(host.bounds.height)) pt to \(out)")
    exit(0)
}

final class AppController: NSObject, NSApplicationDelegate {
    let tracker = Tracker(shader: shader)
    let panel = LegendPanel()
    let overlay = DebugOverlay()
    var hoverSince: Date?
    var lastHover = Date.distantPast
    var shown: SessionState?            // the session the visible panel is about
    var openingUntil = Date.distantPast
    var timer: Timer?

    func applicationDidFinishLaunching(_ note: Notification) {
        panel.onClick = { [weak self] in self?.clicked() }
        timer = Timer.scheduledTimer(withTimeInterval: 1.0 / 15.0, repeats: true) { [weak self] _ in self?.tick() }
        timer?.tolerance = 0.01
    }

    func clicked() {
        guard let s = shown, s.hasTranscript else { return }
        openingUntil = Date().addingTimeInterval(2.5)
        openTokenograph(session: s.key)
        tick()
    }

    func present(_ s: SessionState, at mouse: NSPoint, now: Date) {
        shown = s
        panel.show(at: mouse, content: legendContent(for: s, now: now, opening: now < openingUntil))
    }

    func tick() {
        let now = Date()
        let mouse = NSEvent.mouseLocation
        let point = cgPoint(fromCocoa: mouse)

        // The pointer is on the legend itself: hold it open so it can be clicked.
        if panel.isVisible, panel.frame.insetBy(dx: -8, dy: -8).contains(mouse), let s = shown {
            lastHover = now
            let fresh = tracker.session(key: s.key) ?? s
            present(fresh, at: mouse, now: now)
            return
        }
        guard let s = tracker.sight(at: point, now: now) else {
            hoverSince = nil
            overlay.hide()
            if now.timeIntervalSince(lastHover) > 0.6 { panel.hide(); shown = nil }
            return
        }
        if tracker.config.debug {
            overlay.show(surface: cocoaRect(fromCG: s.surface),
                         center: NSPoint(x: s.center.x, y: (NSScreen.screens.first?.frame.height ?? 0) - s.center.y),
                         radius: s.radius, hover: s.hoverRadius, box: cocoaRect(fromCG: s.box))
        } else {
            overlay.hide()
        }
        if isHovering(s, point) {
            lastHover = now
            if hoverSince == nil { hoverSince = now }
            if panel.isVisible || now.timeIntervalSince(hoverSince!) >= 0.25 {   // a tooltip's short dwell
                present(s.session, at: mouse, now: now)
            }
        } else {
            hoverSince = nil
            // a moment's grace, long enough to move from the hole onto the legend
            if now.timeIntervalSince(lastHover) > 0.6 { panel.hide(); shown = nil }
        }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let controller = AppController()
app.delegate = controller
app.run()
