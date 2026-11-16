import AppKit
import SwiftUI

struct LegendRow: Equatable, Identifiable {
    var id: String { symbol }
    var symbol: String          // SF Symbol
    var measure: String         // what is measured
    var cue: String             // how the hole shows it
    var value: String
    var tint: Color = .white
}

struct LegendContent: Equatable {
    var rows: [LegendRow] = []
    var footer: String = ""
    var action: String = ""      // the click target's label; empty = nothing to click
}

final class LegendModel: ObservableObject {
    @Published var content = LegendContent()
}

struct LegendView: View {
    @ObservedObject var model: LegendModel

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("BLACK HOLE")
                .font(.system(size: 10, weight: .semibold))
                .kerning(1.2)
                .foregroundStyle(.secondary)
            Grid(alignment: .leading, horizontalSpacing: 10, verticalSpacing: 9) {
                ForEach(model.content.rows) { row in
                    GridRow {
                        Image(systemName: row.symbol)
                            .font(.system(size: 13))
                            .foregroundStyle(.secondary)
                            .frame(width: 18)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(row.measure).font(.system(size: 12.5, weight: .medium))
                            Text(row.cue).font(.system(size: 10.5)).foregroundStyle(.secondary)
                        }
                        Text(row.value)
                            .font(.system(size: 12.5, weight: .semibold).monospacedDigit())
                            .foregroundStyle(row.tint)
                            .gridColumnAlignment(.trailing)
                    }
                }
            }
            if !model.content.footer.isEmpty {
                Text(model.content.footer)
                    .font(.system(size: 10.5))
                    .foregroundStyle(.secondary)
            }
            if !model.content.action.isEmpty {
                Divider()
                HStack(spacing: 6) {
                    Image(systemName: "chart.bar.xaxis").font(.system(size: 11))
                    Text(model.content.action).font(.system(size: 12, weight: .medium))
                    Spacer(minLength: 12)
                    Image(systemName: "arrow.up.right").font(.system(size: 10, weight: .semibold))
                }
                .foregroundStyle(Color(red: 0.96, green: 0.69, blue: 0.0))    // accretion amber
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .fixedSize()
    }
}

/// Takes every click on the panel, including the first one on an inactive
/// window, and reports it; the panel never becomes key or activates the app,
/// so the terminal keeps the keyboard.
private final class ClickCatcher: NSView {
    var onClick: (() -> Void)?
    override func hitTest(_ point: NSPoint) -> NSView? { frame.contains(point) ? self : nil }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override func mouseDown(with event: NSEvent) {}
    override func mouseUp(with event: NSEvent) {
        if bounds.contains(convert(event.locationInWindow, from: nil)) { onClick?() }
    }
}

/// A small HUD that appears beside the pointer and then stays put, so the
/// pointer can travel onto it and click.
final class LegendPanel {
    let model = LegendModel()
    var onClick: (() -> Void)?
    private let panel: NSPanel
    private let host: NSHostingView<LegendView>

    init() {
        panel = NSPanel(contentRect: .zero, styleMask: [.borderless, .nonactivatingPanel],
                        backing: .buffered, defer: true)
        panel.level = .floating
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.ignoresMouseEvents = false
        panel.becomesKeyOnlyIfNeeded = true
        panel.hidesOnDeactivate = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .transient, .ignoresCycle]
        panel.appearance = NSAppearance(named: .darkAqua)

        let blur = NSVisualEffectView()
        blur.material = .hudWindow
        blur.blendingMode = .behindWindow
        blur.state = .active
        blur.wantsLayer = true
        blur.layer?.cornerRadius = 12
        blur.layer?.masksToBounds = true
        host = NSHostingView(rootView: LegendView(model: model))
        host.translatesAutoresizingMaskIntoConstraints = false
        blur.addSubview(host)
        NSLayoutConstraint.activate([
            host.leadingAnchor.constraint(equalTo: blur.leadingAnchor),
            host.trailingAnchor.constraint(equalTo: blur.trailingAnchor),
            host.topAnchor.constraint(equalTo: blur.topAnchor),
            host.bottomAnchor.constraint(equalTo: blur.bottomAnchor),
        ])
        let catcher = ClickCatcher()
        catcher.onClick = { [weak self] in self?.onClick?() }
        blur.translatesAutoresizingMaskIntoConstraints = false
        catcher.addSubview(blur)
        NSLayoutConstraint.activate([
            blur.leadingAnchor.constraint(equalTo: catcher.leadingAnchor),
            blur.trailingAnchor.constraint(equalTo: catcher.trailingAnchor),
            blur.topAnchor.constraint(equalTo: catcher.topAnchor),
            blur.bottomAnchor.constraint(equalTo: catcher.bottomAnchor),
        ])
        panel.contentView = catcher
    }

    var isVisible: Bool { panel.isVisible }
    var frame: NSRect { panel.frame }

    /// Show the panel. It is placed beside the pointer (Cocoa coordinates)
    /// when it first appears and stays there until hidden; later calls only
    /// refresh the numbers, growing or shrinking from its top-left corner.
    func show(at mouse: NSPoint, content: LegendContent) {
        if model.content != content { model.content = content }
        let size = host.fittingSize
        if panel.isVisible {
            let old = panel.frame
            if old.size != size {
                panel.setFrame(NSRect(x: old.minX, y: old.maxY - size.height, width: size.width, height: size.height), display: true)
            }
            return
        }
        let screen = NSScreen.screens.first { NSMouseInRect(mouse, $0.frame, false) } ?? NSScreen.main
        let area = screen?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        var origin = NSPoint(x: mouse.x + 14, y: mouse.y - 14 - size.height)
        if origin.x + size.width > area.maxX - 8 { origin.x = mouse.x - 14 - size.width }
        if origin.y < area.minY + 8 { origin.y = mouse.y + 14 }
        origin.x = max(origin.x, area.minX + 8)
        panel.setFrame(NSRect(origin: origin, size: size), display: true)
        panel.orderFrontRegardless()
    }

    func hide() { if panel.isVisible { panel.orderOut(nil) } }
}

/// Debug aid: draws where the helper believes the hole and its roam box are.
final class DebugOverlay {
    private let window: NSWindow
    private let view = RingView()

    init() {
        window = NSWindow(contentRect: .zero, styleMask: [.borderless], backing: .buffered, defer: true)
        window.level = .floating
        window.isOpaque = false
        window.backgroundColor = .clear
        window.ignoresMouseEvents = true
        window.hasShadow = false
        window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .transient, .ignoresCycle]
        window.contentView = view
    }

    /// All rects in Cocoa screen coordinates.
    func show(surface: NSRect, center: NSPoint, radius: CGFloat, hover: CGFloat, box: NSRect) {
        window.setFrame(surface, display: false)
        view.center = NSPoint(x: center.x - surface.minX, y: center.y - surface.minY)
        view.radius = radius
        view.hover = hover
        view.box = box.offsetBy(dx: -surface.minX, dy: -surface.minY)
        view.needsDisplay = true
        if !window.isVisible { window.orderFrontRegardless() }
    }

    func hide() { if window.isVisible { window.orderOut(nil) } }

    private final class RingView: NSView {
        var center = NSPoint.zero, radius: CGFloat = 0, hover: CGFloat = 0, box = NSRect.zero

        override func draw(_ dirtyRect: NSRect) {
            NSColor.systemGreen.withAlphaComponent(0.35).setStroke()
            let frame = NSBezierPath(rect: bounds.insetBy(dx: 1, dy: 1)); frame.lineWidth = 2; frame.stroke()
            NSColor.systemYellow.withAlphaComponent(0.6).setStroke()
            let roam = NSBezierPath(rect: box); roam.lineWidth = 1; roam.stroke()
            for (r, color, width) in [(radius, NSColor.systemRed, CGFloat(2)), (hover, NSColor.systemOrange, CGFloat(1))] {
                color.setStroke()
                let ring = NSBezierPath(ovalIn: NSRect(x: center.x - r, y: center.y - r, width: 2 * r, height: 2 * r))
                ring.lineWidth = width
                ring.stroke()
            }
        }
    }
}
