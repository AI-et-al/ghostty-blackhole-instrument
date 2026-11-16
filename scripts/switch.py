#!/usr/bin/env python3
"""Switch the black hole instrument on or off.

  scripts/switch.py status          show what is wired where
  scripts/switch.py on [--dry-run]  point Ghostty and Claude Code at the instrument
  scripts/switch.py off [--dry-run] undo exactly what "on" did
  scripts/switch.py restart-legend  run the newest build of the hover legend

"on" works out what you already have and changes as little as it can:

  Ghostty config     If a custom-shader line points at upstream blackhole.glsl it
                     is replaced; otherwise the instrument shader is added as one
                     more custom-shader. custom-shader-animation is turned on if
                     it is not set.
  Claude Code hooks  SessionStart, SessionEnd, UserPromptSubmit, Stop and
                     Notification hooks for the bridge. Upstream claude-token.py
                     hooks are replaced, not doubled.
  Status line        None yet: the bridge becomes the status line.
                     Upstream claude-token.py (directly, or inside the script the
                     status line runs): it is swapped for the bridge.
                     Anything else: a small wrapper feeds the bridge and then runs
                     your own status line unchanged.
  Tuner              If ~/ghostty-blackhole/blackhole.glsl is a symlink (the
                     upstream tuner's shader), it is pointed at the instrument.
  Hover legend       If legend/build.sh has been run (macOS), the helper is
                     installed as a login item and started.

Every file is backed up before its first change (a timestamped copy beside it)
and what was done is written to ~/.config/blackhole-instrument/install.json, which is
what "off" reads to put things back.

After either direction, reload Ghostty's config (Cmd+Shift+, on macOS). The
instrument shader understands the upstream bridge, but the upstream shader does
not understand this bridge and blinks the hole off until Ghostty reloads.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK_EVENTS = ("SessionStart", "SessionEnd", "UserPromptSubmit", "Stop", "Notification")
UPSTREAM_BRIDGE = "claude-token.py"
UPSTREAM_SHADER = "blackhole.glsl"
LEGEND_LABEL = "com.blackholeinstrument.legend"
# a possibly quoted shell word ending in claude-token.py
UPSTREAM_WORD = re.compile(r'"[^"\n]*claude-token\.py"|\'[^\'\n]*claude-token\.py\'|[^\s"\']*claude-token\.py')
SHADER_LINE = re.compile(r"^\s*custom-shader\s*=\s*(.+?)\s*$")


class Paths:
    def __init__(self, home=None, root=ROOT):
        self.home = home or os.path.expanduser("~")
        self.root = root
        self.shader = os.path.join(root, "shader", "blackhole-instrument.glsl")
        self.bridge = os.path.join(root, "blackhole_instrument.py")
        self.settings = os.path.join(self.home, ".claude", "settings.json")
        self.wrapper = os.path.join(self.home, ".claude", "blackhole-statusline.sh")
        self.manifest = os.path.join(self.home, ".config", "blackhole-instrument", "install.json")
        self.tuner_link = os.path.join(self.home, "ghostty-blackhole", "blackhole.glsl")
        self.legend_bin = os.path.join(root, "legend", "BlackholeLegend.app", "Contents", "MacOS", "BlackholeLegend")
        self.legend_plist = os.path.join(self.home, "Library", "LaunchAgents", LEGEND_LABEL + ".plist")

    def ghostty_config(self):
        candidates = [os.path.join(self.home, ".config", "ghostty", "config"),
                      os.path.join(self.home, "Library", "Application Support", "com.mitchellh.ghostty", "config")]
        return next((c for c in candidates if os.path.isfile(c)), candidates[0])

    def legend_plist_text(self):
        return """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>%s</string>
  <key>ProgramArguments</key>
  <array>
    <string>%s</string>
    <string>--shader</string>
    <string>%s</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>ProcessType</key><string>Interactive</string>
</dict>
</plist>
""" % (LEGEND_LABEL, self.legend_bin, self.shader)


def read(path):
    with open(path) as f:
        return f.read()


class Run:
    """One on/off pass: writes with a backup, or only reports when dry."""

    def __init__(self, dry=False, launchd=True):
        self.dry, self.launchd = dry, launchd
        self.stamp = time.strftime("%Y%m%d-%H%M%S")
        self.changed, self.notes, self.backed_up = [], [], set()

    def write(self, path, text, what, mode=None):
        self.changed.append(what)
        if self.dry:
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.isfile(path) and path not in self.backed_up:
            shutil.copy2(path, "%s.bak-blackhole-%s" % (path, self.stamp))
            self.backed_up.add(path)
        with open(path, "w") as f:
            f.write(text)
        if mode is not None:
            os.chmod(path, mode)

    def note(self, text):
        self.notes.append(text)


# ---------------------------------------------------------------- Ghostty --
def ghostty_on(p, run, record):
    path = p.ghostty_config()
    lines = read(path).split("\n") if os.path.isfile(path) else []
    ours = "custom-shader = " + p.shader
    shaders = [(i, SHADER_LINE.match(l).group(1)) for i, l in enumerate(lines) if SHADER_LINE.match(l)]
    if any(os.path.expanduser(value) == p.shader for _i, value in shaders):
        run.note("Ghostty shader (already done)")
        return record
    record = {"path": path, "original_line": None, "added_animation": False}
    upstream = [i for i, value in shaders if os.path.basename(value) == UPSTREAM_SHADER]
    if upstream:
        record["original_line"] = lines[upstream[0]]
        lines[upstream[0]] = ours
    else:
        while lines and not lines[-1].strip():
            lines.pop()
        lines.append(ours)
    if not any(re.match(r"^\s*custom-shader-animation\s*=", l) for l in lines):
        lines.append("custom-shader-animation = true")
        record["added_animation"] = True
    run.write(path, "\n".join(lines).rstrip("\n") + "\n", "Ghostty shader")
    return record


def ghostty_off(p, run, record):
    if not record or not os.path.isfile(record["path"]):
        return
    lines = read(record["path"]).split("\n")
    ours = "custom-shader = " + p.shader
    if ours not in lines:
        run.note("Ghostty shader (our line is gone; left alone)")
        return
    i = lines.index(ours)
    if record.get("original_line"):
        lines[i] = record["original_line"]
    else:
        del lines[i]
    if record.get("added_animation") and "custom-shader-animation = true" in lines:
        lines.remove("custom-shader-animation = true")
    run.write(record["path"], "\n".join(lines).rstrip("\n") + "\n", "Ghostty shader")


# ------------------------------------------------------- Claude settings --
def hook_dicts(settings, event):
    for entry in (settings.get("hooks") or {}).get(event) or []:
        for h in entry.get("hooks") or []:
            yield entry, h


def dump_settings(settings, original):
    indent = 2
    for line in original.splitlines()[1:2]:
        indent = len(line) - len(line.lstrip(" ")) or 2
    return json.dumps(settings, indent=indent, ensure_ascii=False) + "\n"


def script_of(command, home):
    """The file a status-line command runs, when it is just a path to a script."""
    words = command.split()
    if len(words) != 1:
        return None
    word = words[0].strip("\"'")
    path = os.path.join(home, word[2:]) if word.startswith("~/") else word
    if os.path.isfile(path) and os.path.getsize(path) < 65536:
        return path
    return None


def swap_upstream(text, bridge):
    """Replace each upstream bridge word outside comments. Returns (text, originals)."""
    originals, out = [], []

    def swap(match):
        originals.append(match.group(0))
        return '"%s"' % bridge

    for line in text.split("\n"):
        out.append(line if line.lstrip().startswith("#") else UPSTREAM_WORD.sub(swap, line))
    return "\n".join(out), originals


def unswap_upstream(text, bridge, originals):
    """Undo swap_upstream: put the original words back, in order."""
    parts = text.split('"%s"' % bridge)
    if not originals or len(parts) == 1:
        return text
    words = originals if len(originals) == len(parts) - 1 else [originals[0]] * (len(parts) - 1)
    return "".join(a + b for a, b in zip(parts, words + [""]))


def settings_on(p, run, record):
    original = read(p.settings) if os.path.isfile(p.settings) else "{}\n"
    settings = json.loads(original)
    record = record or {"replaced_hooks": {}, "added_hooks": [], "statusline": None}
    touched = False

    hooks = settings.setdefault("hooks", {})
    for event in HOOK_EVENTS:
        for _entry, h in hook_dicts(settings, event):
            command = h.get("command") or ""
            if UPSTREAM_BRIDGE in command and p.bridge not in command:
                record["replaced_hooks"][event] = command
                h["command"], touched = p.bridge, True
        if not any(h.get("command") == p.bridge for _e, h in hook_dicts(settings, event)):
            hooks.setdefault(event, []).append({"hooks": [{"type": "command", "command": p.bridge}]})
            record["added_hooks"].append(event)
            touched = True

    line = settings.get("statusLine") if isinstance(settings.get("statusLine"), dict) else None
    command = (line or {}).get("command") or ""
    script = script_of(command, p.home) if command else None
    if record.get("statusline"):
        pass                                             # already wired on an earlier run
    elif not command:
        record["statusline"] = {"mode": "set", "original": settings.get("statusLine")}
        settings["statusLine"] = {"type": "command", "command": p.bridge}
        touched = True
    elif p.bridge in command or command == p.wrapper:
        pass
    elif UPSTREAM_BRIDGE in command:
        record["statusline"] = {"mode": "command", "original": command}
        line["command"] = swap_upstream(command, p.bridge)[0]
        touched = True
    elif script and p.bridge in read(script):
        pass
    elif script and UPSTREAM_BRIDGE in read(script):
        swapped, originals = swap_upstream(read(script), p.bridge)
        record["statusline"] = {"mode": "script", "script": script, "originals": originals}
        run.write(script, swapped, "status-line script")
    else:
        record["statusline"] = {"mode": "wrap", "original": command}
        wrapper = ("#!/bin/bash\n"
                   "# Written by ghostty-blackhole-instrument (scripts/switch.py). Feeds the black hole bridge,\n"
                   "# then runs the status line that was configured before.\n"
                   "input=$(cat)\n"
                   "printf '%%s' \"$input\" | \"%s\" >/dev/null 2>&1\n"
                   "printf '%%s' \"$input\" | %s\n") % (p.bridge, command)
        run.write(p.wrapper, wrapper, "status-line wrapper", mode=0o755)
        line["command"] = p.wrapper
        touched = True

    if touched:
        run.write(p.settings, dump_settings(settings, original), "Claude Code hooks and status line")
    else:
        run.note("Claude Code hooks and status line (already done)")
    return record


def settings_off(p, run, record):
    if not record or not os.path.isfile(p.settings):
        return
    original = read(p.settings)
    settings = json.loads(original)
    hooks = settings.get("hooks") or {}
    touched = False
    for event in HOOK_EVENTS:
        entries = hooks.get(event) or []
        if event in record.get("replaced_hooks", {}):
            for _entry, h in hook_dicts(settings, event):
                if h.get("command") == p.bridge:
                    h["command"], touched = record["replaced_hooks"][event], True
                    break
        if event in record.get("added_hooks", []):
            for entry in entries:
                kept = [h for h in entry.get("hooks") or [] if h.get("command") != p.bridge]
                if len(kept) != len(entry.get("hooks") or []):
                    entry["hooks"], touched = kept, True
            hooks[event] = [e for e in entries if e.get("hooks")]
            if not hooks[event]:
                del hooks[event]
    if "hooks" in settings and not settings["hooks"]:
        del settings["hooks"]

    sl = record.get("statusline") or {}
    line = settings.get("statusLine") if isinstance(settings.get("statusLine"), dict) else None
    if sl.get("mode") == "set" and line and line.get("command") == p.bridge:
        if sl.get("original") is None:
            del settings["statusLine"]
        else:
            settings["statusLine"] = sl["original"]
        touched = True
    elif sl.get("mode") == "command" and line and p.bridge in (line.get("command") or ""):
        line["command"], touched = sl["original"], True
    elif sl.get("mode") == "wrap" and line and line.get("command") == p.wrapper:
        line["command"], touched = sl["original"], True
        if os.path.isfile(p.wrapper) and not run.dry:
            os.remove(p.wrapper)
    elif sl.get("mode") == "script" and os.path.isfile(sl.get("script", "")):
        text = read(sl["script"])
        restored = unswap_upstream(text, p.bridge, sl.get("originals") or [])
        if restored != text:
            run.write(sl["script"], restored, "status-line script")
    if touched:
        run.write(p.settings, dump_settings(settings, original), "Claude Code hooks and status line")


# ------------------------------------------------------- tuner and legend --
def tuner_on(p, run, record):
    if not os.path.islink(p.tuner_link):
        return record
    current = os.readlink(p.tuner_link)
    if current == p.shader:
        run.note("tuner symlink (already done)")
        return record
    run.changed.append("tuner symlink")
    if not run.dry:
        os.remove(p.tuner_link)
        os.symlink(p.shader, p.tuner_link)
    return {"original": current}


def tuner_off(p, run, record):
    if not record or not os.path.islink(p.tuner_link) or os.readlink(p.tuner_link) != p.shader:
        return
    run.changed.append("tuner symlink")
    if not run.dry:
        os.remove(p.tuner_link)
        os.symlink(record["original"], p.tuner_link)


def launchctl(*args):
    return subprocess.run(("launchctl",) + args, capture_output=True, text=True).returncode


def legend_running():
    return sys.platform == "darwin" and launchctl("print", "gui/%d/%s" % (os.getuid(), LEGEND_LABEL)) == 0


def legend_on(p, run, record):
    if sys.platform != "darwin":
        return record
    if not os.path.isfile(p.legend_bin):
        run.note("hover legend (not built; run legend/build.sh, then switch.py on again)")
        return record
    text = p.legend_plist_text()
    if os.path.isfile(p.legend_plist) and read(p.legend_plist) == text and (not run.launchd or legend_running()):
        run.note("hover legend (already done)")
        return True
    run.write(p.legend_plist, text, "hover legend (starts at login)")
    if not run.dry and run.launchd:
        domain = "gui/%d" % os.getuid()
        launchctl("bootout", domain + "/" + LEGEND_LABEL)
        launchctl("bootstrap", domain, p.legend_plist)
    return True


def legend_off(p, run, record):
    if not os.path.isfile(p.legend_plist):
        return
    run.changed.append("hover legend")
    if run.dry:
        return
    if run.launchd:
        launchctl("bootout", "gui/%d/%s" % (os.getuid(), LEGEND_LABEL))
        subprocess.run([sys.executable, p.bridge, "panel", "--close-all"], capture_output=True)
    os.remove(p.legend_plist)


# -------------------------------------------------------------------- run --
def load_manifest(p):
    try:
        return json.loads(read(p.manifest))
    except (OSError, ValueError):
        return {}


def switch_on(p, run):
    m = load_manifest(p)
    m["ghostty"] = ghostty_on(p, run, m.get("ghostty"))
    m["settings"] = settings_on(p, run, m.get("settings"))
    m["tuner"] = tuner_on(p, run, m.get("tuner"))
    m["legend"] = legend_on(p, run, m.get("legend"))
    if not run.dry:
        os.makedirs(os.path.dirname(p.manifest), exist_ok=True)
        with open(p.manifest, "w") as f:
            json.dump(m, f, indent=2)
    return m


def switch_off(p, run):
    m = load_manifest(p)
    if not m:
        run.note("nothing recorded as installed (no %s)" % p.manifest)
    ghostty_off(p, run, m.get("ghostty"))
    settings_off(p, run, m.get("settings"))
    tuner_off(p, run, m.get("tuner"))
    legend_off(p, run, m.get("legend"))
    if not run.dry and os.path.isfile(p.manifest):
        os.remove(p.manifest)


def status(p):
    config = p.ghostty_config()
    lines = read(config).split("\n") if os.path.isfile(config) else []
    print("Ghostty shader      ", "instrument" if "custom-shader = " + p.shader in lines else "not wired",
          "(%s)" % config)
    try:
        settings = json.loads(read(p.settings))
    except (OSError, ValueError):
        settings = {}
    wired = [e for e in HOOK_EVENTS if any(h.get("command") == p.bridge for _x, h in hook_dicts(settings, e))]
    print("Claude Code hooks   ", "%d of %d" % (len(wired), len(HOOK_EVENTS)))
    line = settings.get("statusLine") if isinstance(settings.get("statusLine"), dict) else {}
    command = line.get("command") or ""
    script = script_of(command, p.home) if command else None
    feeds = p.bridge in command or command == p.wrapper or bool(script and p.bridge in read(script))
    print("status line         ", "feeds the bridge" if feeds else "not wired")
    link = os.readlink(p.tuner_link) if os.path.islink(p.tuner_link) else None
    print("tuner edits         ", "instrument" if link == p.shader else "upstream" if link else "no tuner found")
    print("hover legend        ", "running" if legend_running() else
          "installed, not running" if os.path.isfile(p.legend_plist) else "off")
    print("install record      ", p.manifest if os.path.isfile(p.manifest) else "none")


def main(argv):
    if not argv or argv[0] not in ("on", "off", "status", "restart-legend"):
        print(__doc__.strip())
        return 2
    p = Paths()
    if argv[0] == "status":
        status(p)
        return 0
    if argv[0] == "restart-legend":
        if not legend_running():
            print("The hover legend is not installed; run: scripts/switch.py on")
            return 1
        rc = launchctl("kickstart", "-k", "gui/%d/%s" % (os.getuid(), LEGEND_LABEL))
        print("hover legend restarted" if rc == 0 else "could not restart the hover legend")
        return rc
    run = Run(dry="--dry-run" in argv[1:])
    print("Switching the instrument %s%s:" % (argv[0], " (dry run, nothing written)" if run.dry else ""))
    (switch_on if argv[0] == "on" else switch_off)(p, run)
    for what in run.changed:
        print("  %s  %s" % ("would change" if run.dry else "changed", what))
    for what in run.notes:
        print("  ok     %s" % what)
    if run.backed_up:
        print("\nBackups end in .bak-blackhole-%s." % run.stamp)
    if any(c.startswith("Ghostty") for c in run.changed) and not run.dry:
        print("Now reload Ghostty's config (Cmd+Shift+, on macOS) so the shader matches the bridge.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
