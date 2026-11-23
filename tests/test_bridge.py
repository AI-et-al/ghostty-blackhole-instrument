"""Tests for the ghostty-blackhole-instrument bridge. Run: python3 -m unittest discover -s tests"""

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import blackhole_instrument as bt  # noqa: E402


def shader_cursor_fill(rgb):
    """Mirror of tokenFromBytes() in the shader."""
    hi = [v >> 4 for v in rgb]
    lo = [v & 0xF for v in rgb]
    if hi[0] != 0xF or hi[1] != 0xB or hi[2] > 1 or lo[0] != (lo[1] ^ lo[2] ^ 0x5):
        return None
    fill = (lo[1] << 4) | lo[2]
    return None if fill > 250 else fill


def nibbles_of(colors):
    return [ch & 0xF for _slot, rgb in colors for ch in rgb]


def model_call(t0, t1, computed, cached, out, stop="tool_use", sub=0):
    return {"k": "m", "t0": t0, "t1": t1, "p": [t0 + 1.0, t0 + 1.5],
            "tok": [computed, cached, out, 0], "stop": stop, "sub": sub}


def panel(calls, events=(), compactions=0, laps=None, window=200000):
    return {"calls": list(calls), "laps": laps or [{"t0": 0.0}],
            "ledger": {"events": list(events), "window": window},
            "stats": {"context": {"window": window}, "counts": {"compactions": compactions}}}


class WireFormat(unittest.TestCase):
    def test_instrument_round_trip(self):
        for args in [(0, 0, 0, 0, 0, 0, 0), (255, 255, 255, 255, 3, 3, 15),
                     (217, 31, 12, 255, 1, 2, 7), (128, 0, 255, 13, 2, 3, 1)]:
            got = bt.unpack_instrument(bt.pack_instrument(*args))
            self.assertEqual((got["burn_to"], got["burn_from"], got["heat_to"],
                              got["heat_from"], got["kind"], got["mag"], got["age_half"]), args)

    def test_corruption_is_rejected(self):
        nib = bt.pack_instrument(217, 31, 12, 255, 1, 2, 7)
        for i in range(12):
            bad = list(nib)
            bad[i] ^= 0x4
            self.assertIsNone(bt.unpack_instrument(bad), "flipped nibble %d" % i)

    def test_untouched_palette_is_not_a_signal(self):
        defaults = [(s, bt.xterm_rgb(s)) for s in bt.PALETTE_SLOTS]
        self.assertIsNone(bt.unpack_instrument(nibbles_of(defaults)))

    def test_xterm_defaults(self):
        self.assertEqual(bt.xterm_rgb(60), (0x5F, 0x5F, 0x87))
        self.assertEqual(bt.xterm_rgb(58), (0x5F, 0x5F, 0x00))
        self.assertEqual(bt.xterm_rgb(244), (0x80, 0x80, 0x80))

    def test_palette_colours_stay_close_to_default(self):
        nib = bt.pack_instrument(255, 0, 170, 85, 3, 3, 15)
        for slot, rgb in bt.palette_colors(nib):
            for got, base in zip(rgb, bt.xterm_rgb(slot)):
                self.assertEqual(got & 0xF0, base & 0xF0)
                self.assertLessEqual(abs(got - base), 15)
        self.assertEqual(bt.unpack_instrument(nibbles_of(bt.palette_colors(nib)))["heat_to"], 170)

    def test_cursor_tick_zero_matches_upstream(self):
        for fill in (0, 1, 125, 250):
            hi, lo = fill >> 4, fill & 0xF
            upstream = (0xF0 | (hi ^ lo ^ 0x5), 0xB0 | hi, 0x00 | lo)
            self.assertEqual(bt.cursor_rgb(fill, 0), upstream)

    def test_cursor_decodes_with_either_tick(self):
        for fill in range(251):
            for tick in (0, 1):
                self.assertEqual(shader_cursor_fill(bt.cursor_rgb(fill, tick)), fill)
        self.assertNotEqual(bt.cursor_rgb(40, 0), bt.cursor_rgb(40, 1))
        self.assertIsNone(shader_cursor_fill((0xD0, 0xD0, 0xD0)))   # the theme's own cursor

    def test_frame_bytes_shape(self):
        _s, frame = bt.step({}, 100.0, fill=0.4)
        seq = frame_text = bt.frame_bytes(frame).decode("latin-1")
        self.assertEqual(len(re.findall(r"\x1b\]4;\d+;rgb:[0-9a-f/]{8}\x07", frame_text)), 4)
        self.assertTrue(seq.rstrip("\x07").split("\x1b]")[-1].startswith("12;#f"))   # cursor last

    def test_shader_agrees_on_slots_and_salt(self):
        with open(os.path.join(ROOT, "shader", "blackhole-instrument.glsl")) as f:
            src = f.read()
        slots = tuple(int(re.search(r"#define SIG_P%d (\d+)" % i, src).group(1)) for i in range(4))
        self.assertEqual(slots, bt.PALETTE_SLOTS)
        self.assertIn("^ 0x%X)" % bt.CHECK_SALT, src)


class Metrics(unittest.TestCase):
    def test_burn_uses_decode_time_of_recent_calls(self):
        # 300 tokens over 2 s of decode (t1 - prefill end), 100 tokens over 1 s
        calls = [model_call(0, 3, 10, 990, 300), model_call(10, 12, 10, 990, 100)]
        m = bt.derive_metrics(panel(calls))
        self.assertAlmostEqual(m["tok_s"], 400 / 3.0)

    def test_old_calls_fall_out_of_the_burn_window(self):
        calls = [model_call(0, 3, 10, 990, 9000), model_call(500, 503, 10, 990, 200)]
        self.assertAlmostEqual(bt.derive_metrics(panel(calls))["tok_s"], 100.0)

    def test_heat_drops_on_a_miss_and_recovers(self):
        warm = [model_call(i * 10, i * 10 + 3, 100, 99900, 50) for i in range(4)]
        self.assertGreater(bt.derive_metrics(panel(warm))["heat"], 0.99)
        miss = warm + [model_call(40, 43, 100000, 0, 50)]
        self.assertAlmostEqual(bt.derive_metrics(panel(miss))["heat"], 0.0)
        later = miss + [model_call(50, 53, 100, 99900, 50), model_call(60, 63, 100, 99900, 50)]
        self.assertAlmostEqual(bt.derive_metrics(panel(later))["heat"], 1.0 - 0.7 ** 2, places=3)

    def test_subagent_cold_starts_do_not_cool_the_disk(self):
        calls = [model_call(i * 10, i * 10 + 3, 100, 99900, 50) for i in range(3)]
        calls.append(model_call(30, 33, 20000, 0, 50, sub=1))
        self.assertGreater(bt.derive_metrics(panel(calls))["heat"], 0.99)

    def test_working_follows_the_latest_mark(self):
        tool = [model_call(1, 4, 10, 990, 50, stop="tool_use")]
        self.assertTrue(bt.derive_metrics(panel(tool))["working"])
        done = [model_call(1, 4, 10, 990, 50, stop="end_turn")]
        self.assertFalse(bt.derive_metrics(panel(done))["working"])
        self.assertTrue(bt.derive_metrics(panel(done, laps=[{"t0": 0}, {"t0": 9}]))["working"])
        cut = tool + [{"k": "x", "t0": 6.0, "t1": 6.0}]
        self.assertFalse(bt.derive_metrics(panel(cut))["working"])

    def test_event_counts(self):
        ev = [{"t": 5.0, "recomputed": 150000}]
        m = bt.derive_metrics(panel([model_call(1, 4, 10, 990, 50)], events=ev, compactions=2))
        self.assertEqual((m["rebuilds"], m["compactions"]), (1, 2))
        self.assertAlmostEqual(m["rebuild_frac"], 0.75)

    def test_empty_panel(self):
        m = bt.derive_metrics({})
        self.assertIsNone(m["tok_s"])
        self.assertEqual((m["heat"], m["working"], m["rebuilds"]), (1.0, False, 0))


def metrics(tok_s=100.0, heat=1.0, working=True, rebuilds=0, compactions=0, frac=0.0):
    return {"tok_s": tok_s, "heat": heat, "working": working, "rebuilds": rebuilds,
            "compactions": compactions, "rebuild_frac": frac}


class Step(unittest.TestCase):
    def test_tick_flips_only_on_change(self):
        s, f1 = bt.step({}, 100.0, fill=0.40)
        s, f2 = bt.step(s, 100.3, fill=0.40)
        self.assertEqual(f1, f2)
        s, f3 = bt.step(s, 100.6, fill=0.41)
        self.assertEqual(f3["tick"], f1["tick"] ^ 1)
        self.assertEqual(f3["fill"], round(0.41 * 250))

    def test_glide_starts_from_the_last_target(self):
        s, _ = bt.step({}, 100.0, fill=0.3, metrics=metrics(tok_s=20.0, heat=1.0))
        s, f = bt.step(s, 101.0, metrics=metrics(tok_s=200.0, heat=0.2))
        self.assertEqual(f["burn"], 255)
        self.assertEqual(f["burn_from"], bt.to_byte(bt.burn_from_speed(20.0)))
        self.assertEqual((f["heat"], f["heat_from"]), (bt.to_byte(0.2), 255))

    def test_idle_is_an_ember_and_hooks_override(self):
        s, f = bt.step({}, 100.0, fill=0.3, metrics=metrics(working=True))
        self.assertGreater(f["burn"], 0)
        s, f = bt.step(s, 101.0, working=False)            # Stop hook
        self.assertEqual(f["burn"], 0)
        s, f = bt.step(s, 102.0, working=True)             # UserPromptSubmit hook
        self.assertEqual(f["burn"], bt.to_byte(bt.burn_from_speed(100.0)))

    def test_first_look_never_flares(self):
        s, f = bt.step({}, 100.0, metrics=metrics(rebuilds=4, compactions=1))
        self.assertEqual(f["kind"], bt.FLARE_NONE)

    def test_rebuild_flares_ages_and_ends(self):
        s, _ = bt.step({}, 100.0, metrics=metrics(rebuilds=1))
        s, f = bt.step(s, 110.0, metrics=metrics(rebuilds=2, frac=0.8, heat=0.0))
        self.assertEqual((f["kind"], f["mag"], f["age_half"]), (bt.FLARE_REBUILD, 3, 0))
        s, f = bt.step(s, 112.2)
        self.assertEqual((f["kind"], f["age_half"]), (bt.FLARE_REBUILD, 4))
        s, f = bt.step(s, 110.0 + bt.FLARE_WINDOW + 0.1)
        self.assertEqual(f["kind"], bt.FLARE_NONE)
        s, f = bt.step(s, 130.0, metrics=metrics(rebuilds=2))     # same count: no new flare
        self.assertEqual(f["kind"], bt.FLARE_NONE)

    def test_small_rebuild_is_a_small_flare(self):
        s, _ = bt.step({}, 100.0, metrics=metrics())
        s, f = bt.step(s, 110.0, metrics=metrics(rebuilds=1, frac=0.1))
        self.assertEqual((f["kind"], f["mag"]), (bt.FLARE_REBUILD, 1))

    def test_compaction_outranks_rebuild(self):
        s, _ = bt.step({}, 100.0, metrics=metrics())
        s, f = bt.step(s, 110.0, metrics=metrics(rebuilds=1, compactions=1, frac=0.9))
        self.assertEqual(f["kind"], bt.FLARE_COMPACTION)

    def test_no_numbers_is_neutral(self):
        _s, f = bt.step({}, 100.0, fill=0.5)
        self.assertEqual((f["burn"], f["heat"]), (bt.to_byte(bt.BURN_NEUTRAL), 255))

    def test_state_survives_json(self):
        import json
        s, _ = bt.step({}, 100.0, fill=0.3, metrics=metrics(rebuilds=1))
        s = json.loads(json.dumps(s))
        s, f = bt.step(s, 101.0, fill=0.3, metrics=metrics(rebuilds=2, frac=0.5))
        self.assertEqual(f["kind"], bt.FLARE_REBUILD)
        s2 = json.loads(json.dumps(s))
        _s, f2 = bt.step(s2, 101.1, fill=0.3)
        self.assertEqual(f, f2)


class TickAndAgyHook(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.old_state_dir = bt.STATE_DIR
        self.old_no_watcher = os.environ.get("BLACKHOLE_NO_WATCHER")
        bt.STATE_DIR = os.path.join(self.tmp.name, "state")
        os.environ["BLACKHOLE_NO_WATCHER"] = "1"
        self.tty = os.path.join(self.tmp.name, "fake-tty")
        self.tx = os.path.join(self.tmp.name, "transcript.jsonl")

    def tearDown(self):
        bt.STATE_DIR = self.old_state_dir
        if self.old_no_watcher is None:
            os.environ.pop("BLACKHOLE_NO_WATCHER", None)
        else:
            os.environ["BLACKHOLE_NO_WATCHER"] = self.old_no_watcher
        self.tmp.cleanup()

    def write_panel(self, obj, text="step\n"):
        import json
        with open(self.tx, "w") as f:
            f.write(text)
        self.panel_obj = obj

    def test_neutral_tick_lifecycle_and_tokenograph_fill(self):
        import unittest.mock as mock
        # 1. Start -> seed hole (fill 0, idle burn 0)
        self.assertEqual(bt.main(["tick", "--session", "s1", "--event", "start", "--tty", self.tty]), 0)
        st = bt.load_state(bt.state_path("s1"))
        self.assertEqual((st["frame"]["fill"], st["frame"]["burn"]), (0, 0))
        with open(self.tty, "rb") as f:
            self.assertEqual(f.read(), bt.frame_bytes(st["frame"]))

        # 2. Mid-turn tick without --fill pulls fill from Tokenograph stats.context.pct
        tg_data = panel([model_call(1, 3, 200, 99800, 320, stop="tool_use")], window=1_000_000)
        tg_data["stats"]["context"] = {"used": 320_000, "window": 1_000_000, "pct": 0.32}
        self.write_panel(tg_data, "line1\n")
        with mock.patch.object(bt, "run_tokenograph", return_value=tg_data):
            self.assertEqual(bt.main(["tick", "--session", "s1", "--transcript", self.tx, "--tty", self.tty]), 0)
        st = bt.load_state(bt.state_path("s1"))
        self.assertEqual(st["frame"]["fill"], round(0.32 * 250))
        self.assertGreater(st["frame"]["burn"], 0)
        self.assertTrue(st["working"])

        # 3. Stop settles disk to idle while preserving fill
        self.assertEqual(bt.main(["tick", "--session", "s1", "--event", "stop", "--tty", self.tty]), 0)
        st = bt.load_state(bt.state_path("s1"))
        self.assertEqual((st["frame"]["fill"], st["frame"]["burn"]), (round(0.32 * 250), 0))

        # 4. End emits reset_bytes and deletes session state
        self.assertEqual(bt.main(["tick", "--session", "s1", "--event", "end"]), 0)
        with open(self.tty, "rb") as f:
            self.assertEqual(f.read(), bt.reset_bytes())
        self.assertFalse(os.path.exists(bt.state_path("s1")))

    def test_agy_hook_contract_and_lifecycle(self):
        import io
        import json
        import unittest.mock as mock

        def invoke(payload, tty_pair=(None, None)):
            stdin = io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))
            stdout = io.StringIO()
            with mock.patch.object(bt.sys, "stdin", stdin), \
                    mock.patch.object(bt.sys, "stdout", stdout), \
                    mock.patch.object(bt, "agy_tty_and_pid", return_value=tty_pair):
                rc = bt.main(["agy-hook"])
            return rc, stdout.getvalue()

        sid = "conv-123"
        # SessionStart -> {} on stdout, seed hole on tty
        rc, out = invoke({"hookEventName": "SessionStart", "conversationId": sid,
                          "transcriptPath": self.tx}, (self.tty, 4242))
        self.assertEqual((rc, json.loads(out)), (0, {}))
        st = bt.load_state(bt.state_path(sid))
        self.assertEqual((st["frame"]["fill"], st["frame"]["burn"], st["tty"]), (0, 0, self.tty))

        # PreInvocation -> {} on stdout, disk wakes up
        tg1 = panel([model_call(1, 3, 500, 24500, 200, stop="tool_use")], window=1_000_000)
        tg1["stats"]["context"] = {"used": 25_000, "window": 1_000_000, "pct": 0.025}
        self.write_panel(tg1, "step1\n")
        with mock.patch.object(bt, "run_tokenograph", return_value=tg1):
            rc, out = invoke({"hookEventName": "PreInvocation", "conversationId": sid,
                              "transcriptPath": self.tx}, (self.tty, 4242))
        self.assertEqual((rc, json.loads(out)), (0, {}))
        st = bt.load_state(bt.state_path(sid))
        self.assertGreater(st["frame"]["burn"], 0)

        # PostToolUse after a cache rebuild -> flare fires
        tg2 = panel([model_call(1, 3, 500, 24500, 200), model_call(4, 7, 400_000, 0, 250)],
                    events=[{"t": 4.0, "recomputed": 400_000}], window=1_000_000)
        tg2["stats"]["context"] = {"used": 400_000, "window": 1_000_000, "pct": 0.40}
        self.write_panel(tg2, "step1\nstep2\n")
        with mock.patch.object(bt, "run_tokenograph", return_value=tg2):
            rc, out = invoke({"hookEventName": "PostToolUse", "conversationId": sid,
                              "transcriptPath": self.tx}, (self.tty, 4242))
        self.assertEqual((rc, json.loads(out)), (0, {}))
        st = bt.load_state(bt.state_path(sid))
        self.assertEqual((st["frame"]["fill"], st["frame"]["kind"]), (100, bt.FLARE_REBUILD))

        # Stop -> {"decision": "allow"} on stdout, burn settles to 0
        rc, out = invoke({"hookEventName": "Stop", "conversationId": sid,
                          "transcriptPath": self.tx}, (self.tty, 4242))
        self.assertEqual((rc, json.loads(out)), (0, {"decision": "allow"}))
        self.assertEqual(bt.load_state(bt.state_path(sid))["frame"]["burn"], 0)

        # GUI session (no tty) and malformed stdin never raise and always emit valid JSON
        tty2 = os.path.join(self.tmp.name, "unused-tty")
        rc, out = invoke({"hookEventName": "Stop", "conversationId": "gui"}, (None, None))
        self.assertEqual((rc, json.loads(out)), (0, {"decision": "allow"}))
        self.assertFalse(os.path.exists(tty2))
        rc, out = invoke("{broken json", (self.tty, 4242))
        self.assertEqual((rc, json.loads(out)), (0, {}))

    def test_exit_watcher_resets_when_parent_dies_and_respects_newer_session(self):
        import subprocess
        import time
        bt.main(["tick", "--session", "s-exit", "--event", "start", "--tty", self.tty])
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.1)"])
        try:
            self.assertEqual(bt.main(["watch-exit", "--session", "s-exit",
                                      "--pid", str(proc.pid), "--tty", self.tty]), 0)
        finally:
            proc.wait()
        with open(self.tty, "rb") as f:
            self.assertEqual(f.read(), bt.reset_bytes())
        self.assertFalse(os.path.exists(bt.state_path("s-exit")))

        # If a newer session took over the same tty before the old one ends, do not clobber it
        bt.main(["tick", "--session", "s-old", "--event", "start", "--tty", self.tty])
        time.sleep(0.02)
        bt.main(["tick", "--session", "s-new", "--fill", "0.5", "--tty", self.tty])
        with open(self.tty, "rb") as f:
            newer_frame = f.read()
        bt.tick_session("s-old", event="end", tty=self.tty, use_dev_tty=False)
        with open(self.tty, "rb") as f:
            self.assertEqual(f.read(), newer_frame)
        self.assertFalse(os.path.exists(bt.state_path("s-old")))
        self.assertTrue(os.path.exists(bt.state_path("s-new")))


if __name__ == "__main__":
    unittest.main()
