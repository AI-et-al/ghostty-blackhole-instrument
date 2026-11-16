import Foundation

/// The shader's tunables, read from the .glsl file so tuner edits carry over.
struct Tunables {
    private var values: [String: Double] = [:]

    private static let defaults: [String: Double] = [
        "HOLE_RADIUS": 0.0376, "DRIFT_SPEED": 1.0, "WORK_AREA": 0.33,
        "TOKEN_AREA_MIN": 0.01, "TOKEN_AREA_MAX": 0.5,
        "TOKEN_HOME_X": 0.96, "TOKEN_HOME_Y": 0.04,
        "TOKEN_EASE": 1.0, "TOKEN_REACH": 1.0, "TOKEN_CALM": 0.04, "TOKEN_RUSH": 1.1,
    ]

    subscript(name: String) -> Double { values[name] ?? Tunables.defaults[name] ?? 0 }

    static func parse(_ source: String) -> Tunables {
        var t = Tunables()
        for line in source.split(separator: "\n") {
            let parts = line.split(separator: " ", omittingEmptySubsequences: true)
            // const float NAME = 1.2345; // comment
            guard parts.count >= 5, parts[0] == "const", parts[1] == "float", parts[3] == "=" else { continue }
            let number = parts[4].trimmingCharacters(in: CharacterSet(charactersIn: ";"))
            if let v = Double(number) { t.values[String(parts[2])] = v }
        }
        return t
    }
}

/// Where the hole is, in the surface's uv space (x right, y down, both 0..1).
struct HoleGeometry {
    var center: (x: Double, y: Double)
    var radius: Double                          // shadow radius, in units of surface height
    var boxLo: (x: Double, y: Double)           // the roam box the hole wanders in
    var boxHi: (x: Double, y: Double)
}

private func mix(_ a: Double, _ b: Double, _ t: Double) -> Double { a + (b - a) * t }
private func clamp(_ x: Double, _ lo: Double, _ hi: Double) -> Double { min(max(x, lo), hi) }

private func lissa(_ t: Double) -> (x: Double, y: Double) {
    (0.75 * sin(t * 0.37) + 0.25 * sin(t * 0.83 + 1.0),
     0.70 * sin(t * 0.54 + 2.1) + 0.30 * sin(t * 1.07))
}

/// Port of the token-mode position maths in blackhole-instrument.glsl
/// (mainImage, "token mode" branch). Keep the two in step.
func holeGeometry(fill: Double, aspect: Double, time: Double, _ k: Tunables) -> HoleGeometry {
    let t = time * k["DRIFT_SPEED"]
    let g = pow(clamp(fill, 0, 1), k["TOKEN_EASE"])
    let rhMin = (k["TOKEN_AREA_MIN"] * aspect / Double.pi).squareRoot()
    let rhMax = (k["TOKEN_AREA_MAX"] * aspect / Double.pi).squareRoot()
    let rh = mix(rhMin, rhMax, g) * (k["HOLE_RADIUS"] / 0.08)

    let work = k["WORK_AREA"]
    let marg = min(rh * mix(1.45, 0.90, g), 0.5 * (1.0 - work - 0.03))
    let xPad = marg / aspect
    let fullLo = (x: min(xPad, 0.5), y: marg)
    let fullHi = (x: max(0.5, 1.0 - xPad), y: max(marg, 1.0 - (work + 0.03 + marg)))
    let corner = (x: clamp(k["TOKEN_HOME_X"], fullLo.x, fullHi.x),
                  y: clamp(k["TOKEN_HOME_Y"], fullLo.y, fullHi.y))
    let reach = mix(0.06, max(k["TOKEN_REACH"], 0.06), g)
    let lo = (x: mix(corner.x, fullLo.x, reach), y: fullLo.y)
    let hi = (x: fullHi.x, y: mix(corner.y, fullHi.y, reach))

    let room = (x: max((hi.x - lo.x) * 0.5, 0), y: max((hi.y - lo.y) * 0.5, 0))
    let wobBase = 0.010 + 0.030 * g
    let wob = (x: min(wobBase, max(room.x * 0.35, 0.006)), y: min(wobBase, max(room.y * 0.35, 0.006)))
    let amp = (x: max(room.x - wob.x, 0), y: max(room.y - wob.y, 0))
    let calm = lissa(t * k["TOKEN_CALM"]), rush = lissa(t * k["TOKEN_RUSH"])
    let wander = (x: mix(calm.x, rush.x, g), y: mix(calm.y, rush.y, g))
    let center = (x: (lo.x + hi.x) * 0.5 + wander.x * amp.x + wob.x * cos(t * 0.8),
                  y: (lo.y + hi.y) * 0.5 + wander.y * amp.y + wob.y * sin(t * 1.0))
    return HoleGeometry(center: center, radius: rh, boxLo: lo, boxHi: hi)
}
