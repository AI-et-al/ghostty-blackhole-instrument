# Porting brief: other coding agents

For an agent picking up the job of making the black hole instrument work with a harness
other than Claude Code. Two ports are wanted: **Codex CLI** and **Antigravity (`agy`)**.
Each is meant to be done by that harness's own model, in its own session.

Read `README.md` first, in particular "How it works". This brief only covers what a port
has to change and what was found out about each harness on 2026-11-16.

## The job

Make the hole in a Ghostty tab reflect a session of your harness the way it reflects a
Claude Code session today: size from context fill, disk brightness from output speed, disk
colour from cache health, flares on cache rebuilds and compactions.

Done means, for your harness:

1. Starting a session shows the seed hole; ending it hides the hole and resets the palette.
2. During a turn the read-outs update at least once per model call or tool call.
3. The disk settles to idle when the turn ends and wakes when a prompt is submitted.
4. `scripts/switch.py` can install and remove your wiring, with backups, without touching
   Claude Code's wiring. Both can be installed at once.
5. The existing tests pass, and the installer changes have tests in the style of
   `tests/test_switch.py` (fake home folders, byte-for-byte restore on `off`).

## What is already harness-neutral

Leave these alone unless you find a bug:

| Piece | Why it does not care which agent runs |
|---|---|
| `shader/blackhole-instrument.glsl` | Reads only the cursor colour and four palette entries |
| Wire format (`pack_instrument`, `cursor_rgb`, `frame_bytes`) | Bytes on a tty |
| `step()` in `blackhole_instrument.py` | Takes fill, working, metrics; knows nothing about where they came from |
| `derive_metrics()` | Works on Tokenograph's panel JSON, which is normalised across agents |
| `legend/` | Matches a Ghostty tab to a session by the `tty` in the bridge's state file |
| `preview/` | Drives the shader through the bridge's manual path |

The wire format is shared with the shader and pinned by `tests/test_bridge.py`. If you think
it needs to change, change the shader, the bridge and the tests together, and say why.

## What is Claude-specific

All of it is in two places: `hook_or_statusline()` in `blackhole_instrument.py`, and the
settings half of `scripts/switch.py`.

| The bridge needs | Claude Code supplies it as | Used for |
|---|---|---|
| A session key | `session_id` in the JSON on stdin | Name of the state file in `~/.cache/blackhole-instrument/` |
| The transcript path | `transcript_path` in the same JSON | Input to `tokenograph json`; also the legend's click target |
| Context fill, 0..1 | `context_window.used_percentage` (status line only) | The hole's size |
| Lifecycle events | `hook_event_name`: `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `Stop`, `Notification` | Seed hole, reset, wake, settle |
| A frequent tick | The status line command, run after each message | Refreshing every read-out |
| The tab's tty | Found by walking up the process tree (`session_tty()`) | Where the escape sequences are written |

Two things follow from that table:

- **Fill does not have to come from the harness.** Tokenograph's JSON has
  `stats.context.pct`. If your harness has no status line, take fill from there.
- **The tty lookup assumes the bridge is a descendant of the agent process.** If your
  harness runs hooks detached, the walk finds nothing and nothing is drawn. Check this
  early: `./blackhole_instrument.py --dry-run` prints sequences without needing a tty, but
  only a real run in a Ghostty tab proves the tty is found.

## Suggested shape

Do not make your harness imitate Claude Code's JSON. Add a neutral entry point and let each
harness call it with plain arguments, for example:

```
blackhole_instrument.py tick --session <key> --transcript <path>
                             [--fill 0.42] [--event start|prompt|stop|end]
```

`hook_or_statusline()` would then become a thin Claude adapter over the same function. In
`scripts/switch.py`, the Ghostty, tuner and legend steps are already generic; the settings
step is the part to make per-harness (`switch.py on --harness codex`), recording what it did
in the same install manifest. This is a suggestion, not a requirement. If your harness
points to a better shape, use it and explain the choice.

## Codex CLI

Checked on one Mac with `codex-cli 0.166.0`:

- **Tokenograph reads Codex rollouts already.** `tokenograph json <rollout.jsonl>` detects
  the format. On a 118-call session it reported context fill, per-call token counts, and
  `stop` values of `tool_use` and `end_turn`, the same vocabulary `derive_metrics()` expects.
  `./blackhole_instrument.py show <rollout>` returned sensible burn, heat and working
  values with no code changes.
- **Rollouts live at** `~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<id>.jsonl`.
- **Hooks exist.** `~/.codex/hooks.json` on that machine has `SessionStart`,
  `UserPromptSubmit`, `Stop` and `PostToolUse` entries in the same shape as Claude Code's,
  behind `[features] hooks` in `~/.codex/config.toml`. There is also a `notify` program
  setting that fires on turn end.

Not known, and yours to find out:

- What JSON, if any, a Codex hook receives on stdin, and whether it names the session and
  the rollout path. If it does not, you need another way to find the current rollout.
- Whether there is a session-end hook. Without one the hole and the palette entries are
  left behind when a session exits; find the best available substitute.
- Whether Codex has a status-line command. If not, `PostToolUse` plus `Stop` is the
  likely tick.
- Some Codex calls are "unmetered" in Tokenograph (usage not confirmed; token counts are
  placeholders). Check they do not make heat or burn misread.
- An empty rollout made Tokenograph return no ledger. `derive_metrics()` tolerates that,
  but it reported `working: true` for a session with no calls. Decide if that is right at
  session start.

## Antigravity (`agy`) — landed 2026-11-22

Verified and implemented on macOS 27 inside a live `agy` Ghostty session:

- **Transcripts are plain JSONL, not SQLite.** Every session writes
  `~/.gemini/antigravity*/brain/<id>/.system_generated/logs/transcript.jsonl` alongside an
  untruncated `transcript_full.jsonl`. `PLANNER_RESPONSE` steps record `created_at`, `model`,
  uncached `input_tokens`, `cache_read_tokens`, `output_tokens`, `thinking` and `tool_calls`;
  `CHECKPOINT` steps record compactions.
- **Tokenograph adapter landed upstream** (`_scan_agy` in `AI-et-al/Tokenograph`), preferring
  `transcript_full.jsonl` for untruncated tool lengths, pairing tool steps FIFO via their
  `Created At` / `Completed At` headers, merging sibling `invoke_subagent` transcripts, and
  leaving legacy unmetered steps out of token/ledger totals. The hover legend's click-to-open
  Tokenograph panel works unchanged.
- **Hooks live in `~/.gemini/config/hooks.json`.** `switch.py on --harness agy` installs a
  top-level `"blackhole-instrument"` entry (`SessionStart`, `PreInvocation`, `PostToolUse`,
  `PostInvocation`, `Stop`) pointing at `blackhole_instrument.py agy-hook`, which obeys
  `agy`'s camelCase `stdin` / JSON `stdout` contract over the shared `tick_session()` engine
  (also exposed as `blackhole_instrument.py tick` for Codex CLI and other harnesses).
- **Session-end cleanup without `SessionEnd`.** `agy` has no exit hook, so `agy-hook` spawns
  one zero-CPU detached sentinel (`watch-exit`, using `kqueue` `EVFILT_PROC` `NOTE_EXIT` on
  macOS with a poll fallback) bound to the ancestor `agy` PID; when `agy` exits it calls
  `tick_session(..., event="end")` unless a newer session has already claimed the same `tty`.
- **Verified:** all 62 Tokenograph tests (+ 25 real `agy` transcripts checked against additive
  wall-clock and ledger-bound invariants), all 45 bridge and installer tests (fake home
  folders, byte-for-byte restore on `hooks.json` with and without trailing newlines, Claude +
  `agy` coexistence), live `agy_tty_and_pid()` resolution in Ghostty, and `--dry-run` ticks
  against the active `agy` session. Live hook attachment on the owner's `~/.gemini/config/hooks.json`
  was left uninstalled per the ground rules (`switch.py on --harness agy --dry-run` verified).

## Ground rules

- **Do not disturb the Claude Code wiring** on a machine where it is installed. Do not run
  `scripts/switch.py on` or `off` on the owner's machine without asking; test the installer
  against a throwaway home folder, as `tests/test_switch.py` does.
- **The bridge must never raise and never block.** It runs on the hot path of a status line
  or hook. Every step is best-effort; the worst case is "no update this tick".
- **Standard library only**, Python 3.8 or later.
- **Write to a terminal only through `emit()`**, and only the sequences `frame_bytes()` and
  `reset_bytes()` produce.
- **Say what you verified and what you did not.** Much of this project could only be
  checked in the browser preview; the README is explicit about that, and a port should be
  too.

## Testing without a live session

```sh
python3 -m unittest discover -s tests            # bridge and installer
./blackhole_instrument.py show <transcript>      # what the metrics come out as
echo '<json>' | ./blackhole_instrument.py --dry-run     # sequences for one tick, no tty needed
python3 preview/server.py                        # the shader in a browser, driven by the bridge
./blackhole_instrument.py demo                   # in a Ghostty tab running the shader
```

## Questions for the owner

Ask before deciding these:

- Should each port land in this repository, or as its own?
- If two agents run in two tabs at once, is anything expected beyond each tab showing its
  own session?
