"""Tests for scripts/switch.py, run against throwaway home folders.
Run: python3 -m unittest discover -s tests"""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import switch  # noqa: E402


_TEMP_DIRS = []


def tearDownModule():
    for tmp in _TEMP_DIRS:
        tmp.cleanup()


class Home:
    """A fake home folder plus a fake checkout, so nothing real is touched."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        _TEMP_DIRS.append(self.tmp)
        self.home = os.path.join(self.tmp.name, "home")
        self.root = os.path.join(self.tmp.name, "ghostty-blackhole-instrument")
        os.makedirs(self.home)
        os.makedirs(self.root)
        self.p = switch.Paths(home=self.home, root=self.root)
        self.config = os.path.join(self.home, ".config", "ghostty", "config")

    def put(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def settings(self, obj):
        self.put(self.p.settings, json.dumps(obj, indent=2) + "\n")

    def get(self, path):
        with open(path) as f:
            return f.read()

    def on(self, dry=False, harness="claude"):
        run = switch.Run(dry=dry, launchd=False)
        switch.switch_on(self.p, run, harness=harness)
        return run

    def off(self, harness=None):
        run = switch.Run(launchd=False)
        switch.switch_off(self.p, run, harness=harness)
        return run

    def hook_commands(self, event):
        s = json.loads(self.get(self.p.settings))
        return [h.get("command") for _e, h in switch.hook_dicts(s, event)]


class Fresh(unittest.TestCase):
    def test_on_from_nothing_then_off(self):
        h = Home()
        h.on()
        lines = h.get(h.config).split("\n")
        self.assertIn("custom-shader = " + h.p.shader, lines)
        self.assertIn("custom-shader-animation = true", lines)
        s = json.loads(h.get(h.p.settings))
        self.assertEqual(s["statusLine"]["command"], h.p.bridge)
        for event in switch.HOOK_EVENTS:
            self.assertEqual(h.hook_commands(event), [h.p.bridge])
        self.assertTrue(os.path.isfile(h.p.manifest))

        h.off()
        self.assertEqual(h.get(h.config).strip(), "")
        self.assertEqual(json.loads(h.get(h.p.settings)), {})
        self.assertFalse(os.path.exists(h.p.manifest))

    def test_dry_run_writes_nothing(self):
        h = Home()
        run = h.on(dry=True)
        self.assertTrue(run.changed)
        self.assertEqual(os.listdir(h.home), [])

    def test_on_twice_changes_nothing_the_second_time(self):
        h = Home()
        h.on()
        before = (h.get(h.config), h.get(h.p.settings), h.get(h.p.manifest))
        run = h.on()
        self.assertEqual(run.changed, [])
        self.assertEqual(before, (h.get(h.config), h.get(h.p.settings), h.get(h.p.manifest)))
        h.off()
        self.assertEqual(json.loads(h.get(h.p.settings)), {})

    def test_backups_are_made_for_existing_files_only(self):
        h = Home()
        h.put(h.config, "font-size = 13\n")
        run = h.on()
        backups = [n for n in os.listdir(os.path.dirname(h.config)) if ".bak-blackhole-" in n]
        self.assertEqual(len(backups), 1)
        self.assertEqual(run.backed_up, {h.config})


class Upstream(unittest.TestCase):
    """Someone who followed the upstream ghostty-blackhole README."""

    def setUp(self):
        self.h = h = Home()
        self.config = ("theme = dark\ncustom-shader = ~/ghostty-shaders/blackhole.glsl\n"
                       "custom-shader-animation = true\nfont-size = 13\n")
        h.put(h.config, self.config)
        token = "~/ghostty-shaders/claude-token.py"
        h.settings({"model": "x", "statusLine": {"type": "command", "command": token},
                    "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": token}]}],
                              "SessionEnd": [{"hooks": [{"type": "command", "command": token}]}],
                              "Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]}})
        self.settings = h.get(h.p.settings)

    def test_upstream_is_replaced_not_doubled(self):
        h = self.h
        h.on()
        lines = h.get(h.config).split("\n")
        self.assertEqual(lines[1], "custom-shader = " + h.p.shader)
        self.assertEqual(len([l for l in lines if l.startswith("custom-shader =")]), 1)
        self.assertEqual(lines.count("custom-shader-animation = true"), 1)
        self.assertEqual(h.hook_commands("SessionStart"), [h.p.bridge])
        self.assertEqual(h.hook_commands("Stop"), ["say done", h.p.bridge])
        s = json.loads(h.get(h.p.settings))
        self.assertEqual(s["statusLine"]["command"], '"%s"' % h.p.bridge)

    def test_off_restores_every_byte(self):
        h = self.h
        h.on()
        h.off()
        self.assertEqual(h.get(h.config), self.config)
        self.assertEqual(h.get(h.p.settings), self.settings)

    def test_off_leaves_later_edits_alone(self):
        h = self.h
        h.on()
        h.put(h.config, h.get(h.config) + "window-padding-x = 8\n")
        s = json.loads(h.get(h.p.settings))
        s["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": "afplay ding.aiff"}]})
        s["theme"] = "dark"
        h.settings(s)
        h.off()
        self.assertEqual(h.get(h.config), self.config + "window-padding-x = 8\n")
        after = json.loads(h.get(h.p.settings))
        self.assertEqual(after["theme"], "dark")
        self.assertEqual(h.hook_commands("Stop"), ["say done", "afplay ding.aiff"])
        self.assertEqual(h.hook_commands("SessionEnd"), ["~/ghostty-shaders/claude-token.py"])
        self.assertNotIn("UserPromptSubmit", after["hooks"])


class OwnStatusLine(unittest.TestCase):
    def test_other_status_line_is_wrapped_and_unwrapped(self):
        h = Home()
        command = "bash ~/.claude/my-status.sh --compact"
        h.settings({"statusLine": {"type": "command", "command": command, "padding": 1}})
        before = h.get(h.p.settings)
        h.on()
        s = json.loads(h.get(h.p.settings))
        self.assertEqual(s["statusLine"], {"type": "command", "command": h.p.wrapper, "padding": 1})
        wrapper = h.get(h.p.wrapper)
        self.assertIn('"%s" >/dev/null' % h.p.bridge, wrapper)
        self.assertTrue(wrapper.rstrip().endswith("| " + command))
        self.assertTrue(os.access(h.p.wrapper, os.X_OK))
        h.off()
        self.assertEqual(h.get(h.p.settings), before)
        self.assertFalse(os.path.exists(h.p.wrapper))

    def test_script_that_calls_upstream_is_edited_in_place(self):
        h = Home()
        script = os.path.join(h.home, ".claude", "statusline-with-blackhole.sh")
        text = ("#!/bin/bash\n# claude-token.py sizes the hole; its own output is discarded\n"
                "input=$(cat)\n"
                "printf '%s' \"$input\" | \"$HOME/ghostty-shaders/claude-token.py\" >/dev/null 2>&1\n"
                "printf '%s' \"$input\" | bash \"$HOME/.claude/statusline/statusline.sh\"\n")
        h.put(script, text)
        h.settings({"statusLine": {"type": "command", "command": "~/.claude/statusline-with-blackhole.sh"}})
        before = h.get(h.p.settings)
        h.on()
        edited = h.get(script).split("\n")
        self.assertEqual(edited[1], "# claude-token.py sizes the hole; its own output is discarded")
        self.assertEqual(edited[3], "printf '%%s' \"$input\" | \"%s\" >/dev/null 2>&1" % h.p.bridge)
        self.assertEqual(json.loads(h.get(h.p.settings))["statusLine"]["command"],
                         "~/.claude/statusline-with-blackhole.sh")
        self.assertFalse(os.path.exists(h.p.wrapper))
        h.off()
        self.assertEqual(h.get(script), text)
        after = json.loads(h.get(h.p.settings))
        self.assertEqual(after, json.loads(before))


class Extras(unittest.TestCase):
    def test_extra_shader_is_added_beside_others(self):
        h = Home()
        config = "custom-shader = ~/shaders/crt.glsl\ncustom-shader-animation = always\n"
        h.put(h.config, config)
        h.on()
        lines = h.get(h.config).split("\n")
        self.assertIn("custom-shader = ~/shaders/crt.glsl", lines)
        self.assertIn("custom-shader = " + h.p.shader, lines)
        self.assertNotIn("custom-shader-animation = true", lines)
        h.off()
        self.assertEqual(h.get(h.config), config)

    def test_macos_config_location_is_used_when_it_is_the_one_that_exists(self):
        h = Home()
        mac = os.path.join(h.home, "Library", "Application Support", "com.mitchellh.ghostty", "config")
        h.put(mac, "font-size = 12\n")
        h.on()
        self.assertIn("custom-shader = " + h.p.shader, h.get(mac))
        self.assertFalse(os.path.exists(h.config))
        h.off()
        self.assertEqual(h.get(mac), "font-size = 12\n")

    def test_tuner_symlink_follows_and_returns(self):
        h = Home()
        upstream = os.path.join(h.home, "ghostty-shaders", "blackhole.glsl")
        h.put(upstream, "// upstream\n")
        os.makedirs(os.path.dirname(h.p.tuner_link))
        os.symlink(upstream, h.p.tuner_link)
        h.on()
        self.assertEqual(os.readlink(h.p.tuner_link), h.p.shader)
        h.off()
        self.assertEqual(os.readlink(h.p.tuner_link), upstream)

    @unittest.skipUnless(sys.platform == "darwin", "the hover legend is macOS only")
    def test_legend_login_item_is_written_only_when_built(self):
        h = Home()
        h.on()
        self.assertFalse(os.path.exists(h.p.legend_plist))
        h.put(h.p.legend_bin, "")
        h.on()
        plist = h.get(h.p.legend_plist)
        self.assertIn(h.p.legend_bin, plist)
        self.assertIn(switch.LEGEND_LABEL, plist)
        h.off()
        self.assertFalse(os.path.exists(h.p.legend_plist))


class Antigravity(unittest.TestCase):
    def test_agy_on_from_nothing_then_off_leaves_claude_untouched(self):
        h = Home()
        h.on(harness="agy")
        self.assertIn("custom-shader = " + h.p.shader, h.get(h.config).split("\n"))
        self.assertEqual(switch.agy_wired_count(h.p), len(switch.AGY_HOOK_EVENTS))
        self.assertFalse(os.path.exists(h.p.settings))   # Claude settings untouched
        hooks = json.loads(h.get(h.p.agy_hooks))
        block = hooks[switch.AGY_HOOK_NAME]
        self.assertEqual(block["PostToolUse"][0]["matcher"], "*")
        self.assertEqual(block["Stop"][0]["command"], h.p.agy_command())

        # Second run is idempotent
        self.assertEqual(h.on(harness="agy").changed, [])

        h.off(harness="agy")
        self.assertFalse(os.path.exists(h.p.agy_hooks))
        self.assertEqual(h.get(h.config).strip(), "")
        self.assertFalse(os.path.exists(h.p.manifest))
        self.assertFalse(os.path.exists(h.p.settings))

    def test_existing_hooks_json_without_trailing_newline_restores_every_byte(self):
        h = Home()
        existing = json.dumps({
            "herdr": {"PreInvocation": [{"command": "herdr.sh", "timeout": 10, "type": "command"}]},
            "supacode-hooks": {"Stop": [{"command": "supacode.sh", "timeout": 10, "type": "command"}]},
            "elevenlabs-narrator": {"Stop": [{"command": "narrate.sh", "timeout": 10, "type": "command"}]},
        }, indent=2)  # deliberately no trailing newline
        h.put(h.p.agy_hooks, existing)
        run = h.on(harness="agy")
        self.assertIn(h.p.agy_hooks, run.backed_up)
        installed = json.loads(h.get(h.p.agy_hooks))
        self.assertEqual(list(installed)[:3], ["herdr", "supacode-hooks", "elevenlabs-narrator"])
        self.assertIn(switch.AGY_HOOK_NAME, installed)

        h.off(harness="agy")
        self.assertEqual(h.get(h.p.agy_hooks), existing)

    def test_off_leaves_later_edits_to_hooks_json_alone(self):
        h = Home()
        h.put(h.p.agy_hooks, json.dumps({"herdr": {"PreInvocation": []}}, indent=2) + "\n")
        h.on(harness="agy")
        edited = json.loads(h.get(h.p.agy_hooks))
        edited["custom-linter"] = {"PostToolUse": [{"matcher": "edit_file", "hooks": [{"command": "lint"}]}]}
        h.put(h.p.agy_hooks, json.dumps(edited, indent=2) + "\n")

        h.off(harness="agy")
        after = json.loads(h.get(h.p.agy_hooks))
        self.assertNotIn(switch.AGY_HOOK_NAME, after)
        self.assertIn("herdr", after)
        self.assertIn("custom-linter", after)

    def test_both_claude_and_agy_coexist_and_uninstall_independently(self):
        h = Home()
        config = "font-size = 14\n"
        claude_before = json.dumps({"model": "opus", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say ok"}]}]}}, indent=2) + "\n"
        agy_before = json.dumps({"herdr": {"PreInvocation": [{"type": "command", "command": "herdr"}]}}, indent=2)
        h.put(h.config, config)
        h.put(h.p.settings, claude_before)
        h.put(h.p.agy_hooks, agy_before)

        # Install Claude, then install Antigravity
        h.on(harness="claude")
        claude_wired = h.get(h.p.settings)
        h.on(harness="agy")
        self.assertEqual(h.get(h.p.settings), claude_wired)   # agy install did not touch Claude
        self.assertEqual(switch.agy_wired_count(h.p), len(switch.AGY_HOOK_EVENTS))

        # Remove Antigravity only: Claude + Ghostty stay active
        h.off(harness="agy")
        self.assertEqual(h.get(h.p.agy_hooks), agy_before)
        self.assertEqual(h.get(h.p.settings), claude_wired)
        self.assertIn("custom-shader = " + h.p.shader, h.get(h.config))
        self.assertTrue(os.path.isfile(h.p.manifest))

        # Remove Claude: Ghostty and manifest clean up too
        h.off(harness="claude")
        self.assertEqual(h.get(h.p.settings), claude_before)
        self.assertEqual(h.get(h.config), config)
        self.assertFalse(os.path.exists(h.p.manifest))

        # Reverse: install both at once via --harness all, remove Claude first, then agy
        h.on(harness="all")
        self.assertEqual(switch.agy_wired_count(h.p), len(switch.AGY_HOOK_EVENTS))
        self.assertEqual(len(h.hook_commands("SessionStart")), 1)
        h.off(harness="claude")
        self.assertEqual(h.get(h.p.settings), claude_before)
        self.assertEqual(switch.agy_wired_count(h.p), len(switch.AGY_HOOK_EVENTS))
        self.assertIn("custom-shader = " + h.p.shader, h.get(h.config))
        h.off(harness="agy")
        self.assertEqual(h.get(h.p.agy_hooks), agy_before)
        self.assertEqual(h.get(h.config), config)
        self.assertFalse(os.path.exists(h.p.manifest))


if __name__ == "__main__":
    unittest.main()
