#!/usr/bin/env python3
"""ghostty-blackhole-instrument bridge: Tokenograph numbers -> the Ghostty black hole.

The upstream bridge (ghostty-blackhole's claude-token.py) sends one number, the
context-window fill, to blackhole.glsl through the cursor colour. This bridge
keeps that channel and adds a second one, so the hole becomes an instrument:

  fill   context window in use          -> the hole's size          (cursor colour, OSC 12)
  burn   output tokens per second       -> disk brightness and spin (palette, OSC 4)
  heat   prompt-cache health            -> disk colour              (palette, OSC 4)
  flare  cache rebuild / compaction     -> a shock ring             (palette, OSC 4)

burn, heat and the flare events come from Tokenograph (`tokenograph json`) run
against the session's transcript. Nothing here calls a model or the network.

Wired three ways, like the upstream script; the role comes from the JSON
Claude Code pipes on stdin:

  statusLine (no hook_event_name)  refresh every read-out
  SessionStart                     fresh seed hole, idle disk
  SessionEnd                       reset cursor colour and palette -> hole hidden
  UserPromptSubmit                 disk wakes up
  Stop / Notification              disk settles to an idle ember

Manual use, from a Ghostty tab running the instrument shader:

  blackhole_instrument.py set --fill 0.4 --burn 0.9 --heat 0.2
  blackhole_instrument.py set --flare rebuild
  blackhole_instrument.py demo
  blackhole_instrument.py off
  blackhole_instrument.py show <transcript.jsonl>      # print the derived numbers

It must never raise: a crashing statusLine blanks Claude's status bar. Every
step is best-effort and the worst case is "no update this tick".

Wire format (keep in sync with shader/blackhole-instrument.glsl):

  cursor colour  R = F0 | check   G = B0 | fill_hi   B = tick << 4 | fill_lo
                 fill is 0..250; check = fill_hi ^ fill_lo ^ 5; tick flips on
                 every changed update so Ghostty restamps iTimeCursorChange.
  palette        entries 58, 94, 100, 142. Only the low nibble of each channel
                 carries data (12 nibbles); the high nibbles keep the xterm
                 default so the colours themselves barely move.
                   n0 n1    burn target      n2 n3   burn previous
                   n4 n5    heat target      n6 n7   heat previous
                   n8       flare kind << 2 | magnitude
                   n9       flare age in half seconds
                   n10 n11  xor of the five bytes above, xor B5
"""

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time

# ----------------------------------------------------------------- format --
CURSOR_BASE = (0xF0, 0xB0, 0x00)       # accretion-disk amber, as upstream
PALETTE_SLOTS = (58, 94, 100, 142)     # olive/mustard cube entries, rarely used
CHECK_SALT = 0xB5

FLARE_NONE, FLARE_REBUILD, FLARE_COMPACTION = 0, 1, 2
FLARE_KINDS = {"rebuild": FLARE_REBUILD, "compaction": FLARE_COMPACTION}
FLARE_WINDOW = 7.5                     # seconds the bridge keeps a flare alive (age nibble max)

# ---------------------------------------------------------------- mapping --
BURN_FULL_TOK_S = 200.0                # output speed that reads as burn 1.0
BURN_CURVE = 0.7                       # <1 lifts slow speeds so they stay visible
BURN_FLOOR = 0.12                      # working but between calls / in tools
BURN_NEUTRAL = 0.5                     # no Tokenograph numbers yet
RECENT_WINDOW = 120.0                  # seconds of session time averaged for burn
HEAT_DECAY = 0.7                       # how fast a cache miss fades, per later call
HEAT_LOOKBACK = 12                     # model calls considered for heat

STATE_DIR = os.path.expanduser("~/.cache/blackhole-instrument")
STATE_MAX_AGE = 7 * 86400


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def to_byte(x):
    return int(round(clamp(x) * 255.0))


def xterm_rgb(index):
    """Default colour of a 256-colour palette entry (xterm's table)."""
    if 16 <= index <= 231:
        i = index - 16
        levels = [0 if v == 0 else 55 + 40 * v for v in (i // 36, (i // 6) % 6, i % 6)]
        return tuple(levels)
    if 232 <= index <= 255:
        g = 8 + 10 * (index - 232)
        return (g, g, g)
    raise ValueError("palette slots must be in 16..255")


def pack_instrument(burn_to, burn_from, heat_to, heat_from, kind, mag, age_half):
    """The 12 data nibbles for the four palette entries."""
    flare = ((kind & 3) << 6) | ((mag & 3) << 4) | (age_half & 0xF)
    check = burn_to ^ burn_from ^ heat_to ^ heat_from ^ flare ^ CHECK_SALT
    nibbles = []
    for byte in (burn_to, burn_from, heat_to, heat_from, flare, check):
        nibbles += [(byte >> 4) & 0xF, byte & 0xF]
    return nibbles


def unpack_instrument(nibbles):
    """Mirror of readInstrument() in the shader. None when the check fails."""
    b = [(nibbles[i] << 4) | nibbles[i + 1] for i in range(0, 12, 2)]
    burn_to, burn_from, heat_to, heat_from, flare, check = b
    if check != burn_to ^ burn_from ^ heat_to ^ heat_from ^ flare ^ CHECK_SALT:
        return None
    return {"burn_to": burn_to, "burn_from": burn_from, "heat_to": heat_to,
            "heat_from": heat_from, "kind": flare >> 6, "mag": (flare >> 4) & 3,
            "age_half": flare & 0xF}


def palette_colors(nibbles):
    """[(slot, (r, g, b))] with the data in the low nibbles."""
    out = []
    for n, slot in enumerate(PALETTE_SLOTS):
        base = xterm_rgb(slot)
        out.append((slot, tuple((base[c] & 0xF0) | nibbles[3 * n + c] for c in range(3))))
    return out


def cursor_rgb(fill, tick):
    hi, lo = fill >> 4, fill & 0xF
    return (CURSOR_BASE[0] | (hi ^ lo ^ 0x5), CURSOR_BASE[1] | hi, ((tick & 1) << 4) | lo)


def frame_bytes(frame):
    """Escape sequences for one frame: palette first, cursor last, so the
    cursor change (the shader's clock) lands with the palette already set."""
    nibbles = pack_instrument(frame["burn"], frame["burn_from"], frame["heat"],
                              frame["heat_from"], frame["kind"], frame["mag"],
                              frame["age_half"])
    out = b""
    for slot, rgb in palette_colors(nibbles):
        out += b"\033]4;%d;rgb:%02x/%02x/%02x\007" % ((slot,) + rgb)
    out += b"\033]12;#%02x%02x%02x\007" % cursor_rgb(frame["fill"], frame["tick"])
    return out


def reset_bytes():
    """Hand the cursor colour and the four palette entries back to the theme."""
    out = b"\033]112\007"
    for slot in PALETTE_SLOTS:
        out += b"\033]104;%d\007" % slot
    return out


# ---------------------------------------------------------------- metrics --
def derive_metrics(tg):
    """Reduce Tokenograph's panel JSON to what the instrument shows.

    Returns {"tok_s", "heat", "working", "rebuilds", "compactions",
    "rebuild_frac"}; tok_s is None when there is no recent output to measure.
    """
    calls = tg.get("calls") or []
    models = [c for c in calls if c.get("k") == "m"]
    main = [c for c in models if not c.get("sub")]

    # burn: output tokens over decode time, across the recent model calls
    tok_s = None
    if models:
        horizon = max(c.get("t1", 0.0) for c in models) - RECENT_WINDOW
        out = dur = 0.0
        for c in models:
            if c.get("t1", 0.0) < horizon:
                continue
            tok = c.get("tok") or [0, 0, 0, 0]
            start = (c.get("p") or [c.get("t0", 0.0)])[0]
            span = c.get("t1", 0.0) - start
            if span > 0 and tok[2] > 0:
                out += tok[2]
                dur += span
        if dur >= 0.5:
            tok_s = out / dur

    # heat: 1 = the prefix came from cache; a miss fades over later calls
    worst = 0.0
    for i, c in enumerate(reversed(main[-HEAT_LOOKBACK:])):
        tok = c.get("tok") or [0, 0, 0, 0]
        seen = tok[0] + tok[1]
        if seen > 0:
            worst = max(worst, (tok[0] / seen) * HEAT_DECAY ** i)
    heat = 1.0 - worst

    # working: whichever happened last — a model call (still working unless it
    # ended the turn), an interrupt (stopped), or a new prompt (working)
    marks = []
    if main:
        last = main[-1]
        marks.append((last.get("t1", 0.0), last.get("stop") == "tool_use"))
    stops = [c for c in calls if c.get("k") == "x"]
    if stops:
        marks.append((stops[-1].get("t0", 0.0), False))
    laps = tg.get("laps") or []
    if laps:
        marks.append((laps[-1].get("t0", 0.0), True))
    working = max(marks)[1] if marks else False

    ledger = tg.get("ledger") or {}
    events = ledger.get("events") or []
    stats = tg.get("stats") or {}
    window = (stats.get("context") or {}).get("window") or ledger.get("window") or 0
    frac = 0.0
    if events and window:
        frac = clamp((events[-1].get("recomputed") or 0) / float(window))
    return {"tok_s": tok_s, "heat": clamp(heat), "working": bool(working),
            "rebuilds": len(events),
            "compactions": int((stats.get("counts") or {}).get("compactions") or 0),
            "rebuild_frac": frac}


def find_tokenograph():
    override = os.environ.get("BLACKHOLE_TOKENOGRAPH")
    if override:
        return override
    found = shutil.which("tokenograph")
    if found:
        return found
    fallback = os.path.expanduser("~/.local/bin/tokenograph")   # statusLine PATH can be bare
    return fallback if os.access(fallback, os.X_OK) else None


def run_tokenograph(transcript):
    """Panel JSON for a transcript, or None. Fast (~0.1 s) and local; the
    timeout keeps a slow run from holding up the status line."""
    exe = find_tokenograph()
    if not exe or not transcript or not os.path.isfile(transcript):
        return None
    try:
        r = subprocess.run([exe, "json", transcript], capture_output=True,
                           text=True, timeout=1.5)
        return json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def burn_from_speed(tok_s):
    if tok_s is None:
        return BURN_NEUTRAL
    return max(BURN_FLOOR, clamp(tok_s / BURN_FULL_TOK_S) ** BURN_CURVE)


# ------------------------------------------------------------------ state --
def step(state, now, fill=None, working=None, metrics=None, flare=None):
    """Advance the instrument one tick. Pure: returns (state, frame).

    fill      0..1, or None to keep the last one
    working   True/False from a hook, or None to keep / take it from metrics
    metrics   derive_metrics() output when the transcript changed, else None
    flare     (kind, mag) to start a flare by hand, else None
    """
    s = dict(state or {})
    if fill is not None:
        s["fill"] = int(round(round(clamp(fill) * 100.0) / 100.0 * 250.0))   # 1% steps, as upstream
    s.setdefault("fill", 0)

    if metrics is not None:
        s["tok_s"] = metrics["tok_s"] if metrics["tok_s"] is not None else s.get("tok_s")
        s["heat_now"] = metrics["heat"]
        s["working"] = metrics["working"]
        seen = s.get("seen")
        if seen is not None:                      # first look at a session never flares
            if metrics["compactions"] > seen[1]:
                flare = (FLARE_COMPACTION, 3)
            elif metrics["rebuilds"] > seen[0]:
                frac = metrics["rebuild_frac"]
                flare = (FLARE_REBUILD, 1 if frac < 0.25 else 2 if frac < 0.6 else 3)
        s["seen"] = [metrics["rebuilds"], metrics["compactions"]]
    if working is not None:
        s["working"] = working
    if flare is not None:
        s["flare"] = {"kind": flare[0], "mag": flare[1], "t0": now}
        s["last_flare"] = {"kind": flare[0], "t0": now}      # kept for the hover legend

    if "burn_manual" in s:
        burn = to_byte(s["burn_manual"])
    elif s.get("working") is False:
        burn = 0
    else:
        burn = to_byte(burn_from_speed(s.get("tok_s")))
    heat = to_byte(s.get("heat_manual", s.get("heat_now", 1.0)))

    kind = mag = age_half = 0
    fl = s.get("flare")
    if fl:
        age = now - fl["t0"]
        if 0 <= age < FLARE_WINDOW:
            kind, mag, age_half = fl["kind"], fl["mag"], min(15, int(round(age * 2.0)))
        else:
            s.pop("flare")

    payload = [s["fill"], burn, heat, kind, mag, age_half]
    prev = s.get("frame")
    if prev and s.get("payload") == payload:
        return s, prev                           # nothing changed: re-emit as is
    frame = {"fill": s["fill"], "burn": burn, "heat": heat,
             "burn_from": prev["burn"] if prev else burn,
             "heat_from": prev["heat"] if prev else heat,
             "kind": kind, "mag": mag, "age_half": age_half,
             "tick": (prev["tick"] ^ 1) if prev else 0}
    s["payload"], s["frame"] = payload, frame
    return s, frame


def safe_key(key):
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(key))[:80] or "session"


def state_path(key):
    return os.path.join(STATE_DIR, safe_key(key) + ".json")


def load_state(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except OSError:
        pass


def prune_states():
    try:
        cutoff = time.time() - STATE_MAX_AGE
        for name in os.listdir(STATE_DIR):
            p = os.path.join(STATE_DIR, name)
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
    except OSError:
        pass


class Lock:
    """One bridge at a time per session, so ticks can't interleave."""

    def __init__(self, path, wait):
        self.path, self.wait, self.fd = path + ".lock", wait, None

    def __enter__(self):
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:
            return True                          # can't lock: run anyway
        deadline = time.time() + self.wait
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                if time.time() >= deadline:
                    return False
                time.sleep(0.03)

    def __exit__(self, *exc):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass


# ------------------------------------------------------- Tokenograph panel --
# One click in the hover legend opens Tokenograph's full live panel for that
# tab's session: `tokenograph serve <transcript>` on a free local port, one
# server per session, reused while it is alive and stopped at SessionEnd.
PANEL_PORTS = range(8787, 8838)


def panel_path(key):
    return os.path.join(STATE_DIR, "panel-" + safe_key(key) + ".json")


def port_listening(port):
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def port_free(port):
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def is_tokenograph(pid):
    """True when pid is alive and is a Tokenograph process (never kill anything else)."""
    try:
        r = subprocess.run(["ps", "-o", "command=", "-p", str(int(pid))],
                           capture_output=True, text=True, timeout=2)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and "tokenograph" in r.stdout


def open_panel(key, launch=True):
    """Start (or reuse) the live panel for a session. Returns (url, error)."""
    transcript = load_state(state_path(key)).get("transcript")
    if not transcript or not os.path.isfile(transcript):
        return None, "no transcript recorded for this session yet"
    info = load_state(panel_path(key))
    alive = (info.get("transcript") == transcript and is_tokenograph(info.get("pid"))
             and port_listening(info.get("port", 0)))
    if not alive:
        exe = find_tokenograph()
        if not exe:
            return None, "tokenograph is not installed"
        port = next((p for p in PANEL_PORTS if port_free(p)), None)
        if port is None:
            return None, "no free port between %d and %d" % (PANEL_PORTS[0], PANEL_PORTS[-1])
        try:
            proc = subprocess.Popen([exe, "serve", transcript, "--port", str(port)],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            return None, "could not start tokenograph: %s" % e
        deadline = time.time() + 5.0
        while time.time() < deadline and not port_listening(port):
            if proc.poll() is not None:
                return None, "tokenograph exited straight away"
            time.sleep(0.05)
        if not port_listening(port):
            proc.terminate()
            return None, "tokenograph did not start listening"
        info = {"pid": proc.pid, "port": port, "transcript": transcript}
        save_state(panel_path(key), info)
    url = "http://127.0.0.1:%d/" % info["port"]
    if launch:
        try:
            subprocess.run(["open", url], timeout=5)
        except (OSError, subprocess.SubprocessError):
            return url, "panel is running but the browser could not be opened"
    return url, None


def close_panel(key):
    """Stop a session's panel server, if it is ours and still running."""
    path = panel_path(key)
    info = load_state(path)
    if is_tokenograph(info.get("pid")):
        try:
            os.kill(int(info["pid"]), 15)
        except OSError:
            pass
    try:
        os.remove(path)
    except OSError:
        pass


def close_all_panels():
    try:
        names = os.listdir(STATE_DIR)
    except OSError:
        return 0
    keys = [n[len("panel-"):-len(".json")] for n in names if n.startswith("panel-") and n.endswith(".json")]
    for key in keys:
        close_panel(key)
    return len(keys)


# -------------------------------------------------------------------- tty --
def session_tty():
    """The terminal this session runs in. Claude Code spawns statusLine/hook
    commands without a controlling terminal, but `claude` itself sits on the
    Ghostty pty: walk up the ancestors and take the first real tty."""
    pid = os.getppid()
    for _ in range(10):
        try:
            out = subprocess.run(["ps", "-o", "ppid=,tty=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=1).stdout.split()
        except (OSError, subprocess.SubprocessError):
            return None
        if len(out) < 2:
            return None
        if out[1] not in ("??", "?"):            # macOS prints ??, Linux ?
            return "/dev/" + out[1]
        if not out[0].isdigit() or int(out[0]) <= 1:
            return None
        pid = int(out[0])
    return None


def emit(seq, dry_run=False, tty=None):
    """Write escape sequences straight to the session's terminal. `tty` is
    the session's device when already known; otherwise it is looked up."""
    if dry_run:
        print(seq.decode("latin-1").replace("\033", "ESC").replace("\007", " BEL\n"), end="")
        return
    for path in ("/dev/tty", tty, None):
        try:
            target = path or session_tty()
            if not target:
                return
            with open(target, "wb") as out:
                out.write(seq)
            return
        except OSError:
            continue


# ------------------------------------------------------------------ roles --
def context_fill(data):
    cw = data.get("context_window") or {}
    pct = cw.get("used_percentage")
    if pct is None:
        used = cw.get("total_input_tokens") or 0
        size = cw.get("context_window_size") or 0
        pct = (100.0 * used / size) if size else 0.0
    return clamp(pct / 100.0)


def readout(state, frame):
    parts = ["⚫ %d%%" % round(frame["fill"] / 2.5)]
    if state.get("working") is False:
        parts.append("idle")
    elif state.get("tok_s") is not None:
        parts.append("%d tok/s" % round(state["tok_s"]))
    parts.append("cache %d%%" % round(frame["heat"] / 2.55))
    if frame["kind"]:
        parts.append("rebuild" if frame["kind"] == FLARE_REBUILD else "compaction")
    return "\033[2m" + "  ·  ".join(parts) + "\033[0m"


def hook_or_statusline(data, dry_run):
    event = data.get("hook_event_name")
    key = data.get("session_id") or "session"
    path = state_path(key)

    if event == "SessionEnd":
        emit(reset_bytes(), dry_run)
        if not dry_run:
            close_panel(key)
        for p in (path, path + ".lock"):
            try:
                os.remove(p)
            except OSError:
                pass
        prune_states()
        return None

    with Lock(path, wait=1.5 if event else 0.0) as got:
        if not got:
            return None                          # another tick is mid-flight
        now = time.time()
        state = load_state(path)
        fill = working = metrics = None
        if event == "SessionStart":
            state, fill, working = {}, 0.0, False
        elif event == "UserPromptSubmit":
            working = True
        elif event in ("Stop", "Notification"):
            working = False
        elif event is None:                      # statusLine
            fill = context_fill(data)
            transcript = data.get("transcript_path")
            try:
                st = os.stat(transcript)
                stamp = [st.st_size, st.st_mtime]
            except (OSError, TypeError):
                stamp = None
            if stamp and stamp != state.get("stamp"):
                tg = run_tokenograph(transcript)
                if tg is not None:
                    metrics = derive_metrics(tg)
                    state["stamp"] = stamp
        else:
            return None                          # a hook we don't act on
        if not state.get("tty") and not dry_run:
            state["tty"] = session_tty()         # also tells the hover legend which tab this is
        if data.get("transcript_path"):
            state["transcript"] = data["transcript_path"]   # for the legend's Tokenograph button
        state, frame = step(state, now, fill=fill, working=working, metrics=metrics)
        emit(frame_bytes(frame), dry_run, state.get("tty"))
        save_state(path, state)
        return readout(state, frame)


def manual(args):
    key = "manual-" + os.path.basename(os.ttyname(0) if os.isatty(0) else "tty")
    path = state_path(key)
    if args.cmd == "off":
        emit(reset_bytes(), args.dry_run)
        try:
            os.remove(path)
        except OSError:
            pass
        return
    state = load_state(path)
    if args.burn is not None:
        state["burn_manual"] = clamp(args.burn)
    if args.heat is not None:
        state["heat_manual"] = clamp(args.heat)
    state.setdefault("burn_manual", BURN_NEUTRAL)
    flare = (FLARE_KINDS[args.flare], args.mag) if args.flare else None
    state, frame = step(state, time.time(), fill=args.fill, flare=flare)
    emit(frame_bytes(frame), args.dry_run)
    save_state(path, state)
    if args.dry_run:
        print(json.dumps(frame))


def demo(dry_run):
    """A 50-second tour of every read-out, on the current terminal."""
    key = state_path("demo-%d" % os.getpid())
    state = {}
    script = [
        (0.0, dict(fill=0.25), dict(burn_manual=0.0, heat_manual=1.0), None, "idle: a dim, slow ember"),
        (6.0, dict(), dict(burn_manual=0.9), None, "working: 180 tok/s, bright and spinning"),
        (8.0, dict(fill=0.45), dict(), None, "context grows"),
        (6.0, dict(), dict(heat_manual=0.05), (FLARE_REBUILD, 3), "cache rebuild: flare, disk goes red"),
        (8.0, dict(), dict(heat_manual=0.6), None, "cache warming back up"),
        (5.0, dict(fill=0.7), dict(heat_manual=1.0), None, "healthy again, context at 70%"),
        (6.0, dict(fill=0.2), dict(heat_manual=0.1), (FLARE_COMPACTION, 3), "compaction: the hole sheds mass"),
        (8.0, dict(), dict(burn_manual=0.0, heat_manual=1.0), None, "turn over: back to an ember"),
    ]
    try:
        for wait, kw, manual_vals, flare, label in script:
            time.sleep(wait)
            state.update(manual_vals)
            state, frame = step(state, time.time(), flare=flare, **kw)
            emit(frame_bytes(frame), dry_run)
            print("  " + label, flush=True)
        time.sleep(6.0)
    finally:
        emit(reset_bytes(), dry_run)
        try:
            os.remove(key)
        except OSError:
            pass


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv == ["--dry-run"]:
        try:
            data = json.load(sys.stdin)
        except (ValueError, OSError):
            data = {}
        try:
            line = hook_or_statusline(data if isinstance(data, dict) else {}, bool(argv))
        except Exception:                        # never blank the status bar
            line = None
        if line and not data.get("hook_event_name"):
            print(line)
        return 0

    ap = argparse.ArgumentParser(prog="blackhole_instrument.py",
                                 description="Drive the Ghostty black hole instrument.")
    ap.add_argument("--dry-run", action="store_true", help="print the escape sequences instead of sending them")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("set", help="set read-outs by hand")
    p.add_argument("--fill", type=float, help="context fill 0..1 (hole size)")
    p.add_argument("--burn", type=float, help="0 idle .. 1 fastest output")
    p.add_argument("--heat", type=float, help="0 cache lost .. 1 cache healthy")
    p.add_argument("--flare", choices=sorted(FLARE_KINDS), help="start a flare now")
    p.add_argument("--mag", type=int, choices=(1, 2, 3), default=3, help="flare strength")
    sub.add_parser("off", help="reset cursor colour and palette; the hole hides")
    sub.add_parser("demo", help="a 50-second tour of every read-out")
    p = sub.add_parser("show", help="print what Tokenograph says about a transcript")
    p.add_argument("transcript")
    p = sub.add_parser("panel", help="open Tokenograph's live panel for a session")
    p.add_argument("session", nargs="?", help="session id (the state file's name)")
    p.add_argument("--no-open", action="store_true", help="start the panel but do not open the browser")
    p.add_argument("--close", action="store_true", help="stop that session's panel")
    p.add_argument("--close-all", action="store_true", help="stop every panel this bridge started")
    args = ap.parse_args(argv)

    if args.cmd == "panel":
        if args.close_all:
            print("stopped %d panel(s)" % close_all_panels())
            return 0
        if not args.session:
            print("panel needs a session id", file=sys.stderr)
            return 2
        if args.close:
            close_panel(args.session)
            return 0
        quiet = args.no_open or bool(os.environ.get("BLACKHOLE_PANEL_NO_OPEN"))   # for tests
        url, error = open_panel(args.session, launch=not quiet)
        if url:
            print(url)
        if error:
            print(error, file=sys.stderr)
        return 0 if url else 1

    if args.cmd == "show":
        tg = run_tokenograph(args.transcript)
        if tg is None:
            print("tokenograph could not read that transcript", file=sys.stderr)
            return 1
        m = derive_metrics(tg)
        m["burn"] = round(burn_from_speed(m["tok_s"]), 3)
        print(json.dumps(m, indent=2))
        return 0
    if args.cmd == "demo":
        demo(args.dry_run)
        return 0
    manual(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
