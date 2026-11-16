import Darwin
import Foundation

/// What the bridge last wrote for one Claude Code session (or a manual `set`).
struct SessionState {
    var key: String                  // the state file's name: the session id
    var tty: String
    var hasTranscript: Bool          // the bridge knows the transcript, so Tokenograph can open it
    var fill: Double                 // 0..1
    var burn: Double                 // 0..1, as sent to the shader
    var heat: Double                 // 0..1, as sent to the shader
    var tokensPerSecond: Double?
    var working: Bool?
    var rebuilds: Int?
    var compactions: Int?
    var lastFlareKind: Int?          // 1 rebuild, 2 compaction
    var lastFlareAt: Double?         // epoch seconds
    var updated: Date
}

let stateDirectory = (NSHomeDirectory() as NSString).appendingPathComponent(".cache/blackhole-instrument")

func loadSessions() -> [SessionState] {
    let fm = FileManager.default
    guard let names = try? fm.contentsOfDirectory(atPath: stateDirectory) else { return [] }
    var out: [SessionState] = []
    for name in names where name.hasSuffix(".json") && !name.hasPrefix("legend") {
        let path = (stateDirectory as NSString).appendingPathComponent(name)
        guard let data = fm.contents(atPath: path),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let frame = obj["frame"] as? [String: Any],
              let fill = frame["fill"] as? Double else { continue }
        // `blackhole_instrument.py set` keys its state by tty: manual-ttys004.json
        var tty = obj["tty"] as? String
        if tty == nil, name.hasPrefix("manual-") {
            tty = "/dev/" + name.dropFirst("manual-".count).dropLast(".json".count)
        }
        guard let device = tty else { continue }
        let seen = obj["seen"] as? [Int]
        let last = obj["last_flare"] as? [String: Any]
        let attrs = try? fm.attributesOfItem(atPath: path)
        out.append(SessionState(
            key: String(name.dropLast(".json".count)),
            tty: device,
            hasTranscript: obj["transcript"] is String,
            fill: fill / 250.0,
            burn: (frame["burn"] as? Double ?? 128) / 255.0,
            heat: (frame["heat"] as? Double ?? 255) / 255.0,
            tokensPerSecond: obj["tok_s"] as? Double,
            working: obj["working"] as? Bool,
            rebuilds: seen?.first,
            compactions: seen?.last,
            lastFlareKind: last?["kind"] as? Int,
            lastFlareAt: last?["t0"] as? Double,
            updated: (attrs?[.modificationDate] as? Date) ?? .distantPast))
    }
    return out
}

/// One Ghostty window as its scripting interface reports it, front to back.
struct GhosttyTab {
    var tty: String
    var tabCount: Int
    var splitCount: Int
}

private let ghosttyScript = """
tell application "Ghostty"
  set out to ""
  repeat with w in windows
    set t to focused terminal of selected tab of w
    set out to out & (tty of t) & "|" & (count of tabs of w) & "|" & (count of terminals of selected tab of w) & linefeed
  end repeat
  return out
end tell
"""

func runTool(_ path: String, _ args: [String], timeout: TimeInterval = 3) -> String? {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: path)
    p.arguments = args
    let pipe = Pipe()
    p.standardOutput = pipe
    p.standardError = FileHandle.nullDevice
    do { try p.run() } catch { return nil }
    let deadline = Date().addingTimeInterval(timeout)
    while p.isRunning && Date() < deadline { usleep(20_000) }
    if p.isRunning { p.terminate(); return nil }
    guard p.terminationStatus == 0 else { return nil }
    return String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)
}

/// Ask Ghostty which terminal each window is showing. nil when Ghostty
/// can't be queried (not running, or Automation permission not granted).
func queryGhostty() -> [GhosttyTab]? {
    guard let text = runTool("/usr/bin/osascript", ["-e", ghosttyScript]) else { return nil }
    return text.split(separator: "\n").compactMap { line in
        let f = line.split(separator: "|")
        guard f.count == 3, let tabs = Int(f[1]), let splits = Int(f[2]) else { return nil }
        return GhosttyTab(tty: String(f[0]), tabCount: tabs, splitCount: splits)
    }
}

// ---- the shader's clock ----
// iTime counts from when the terminal surface started, on the uptime clock
// (it does not advance while the Mac sleeps). The kernel records each
// process's start on that same clock, so the oldest process on the tab's tty
// gives the surface's start to within a fraction of a second.

private let timebase: Double = {
    var tb = mach_timebase_info_data_t()
    mach_timebase_info(&tb)
    return Double(tb.numer) / Double(tb.denom) / 1e9
}()

func uptimeNow() -> Double { Double(mach_absolute_time()) * timebase }

private func processStart(_ pid: pid_t) -> Double? {
    var info = rusage_info_v2()
    let rc = withUnsafeMutablePointer(to: &info) { p in
        p.withMemoryRebound(to: rusage_info_t?.self, capacity: 1) { proc_pid_rusage(pid, RUSAGE_INFO_V2, $0) }
    }
    return rc == 0 && info.ri_proc_start_abstime > 0 ? Double(info.ri_proc_start_abstime) * timebase : nil
}

/// Uptime-clock second at which the surface on this tty started, or nil.
func surfaceStart(tty: String) -> Double? {
    let name = tty.hasPrefix("/dev/") ? String(tty.dropFirst(5)) : tty
    guard let text = runTool("/bin/ps", ["-t", name, "-o", "pid="], timeout: 2) else { return nil }
    let starts = text.split(separator: "\n")
        .compactMap { pid_t($0.trimmingCharacters(in: .whitespaces)) }
        .compactMap(processStart)
    return starts.min()
}
