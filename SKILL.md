---
name: jrtt
description: Use the `jrtt` CLI to stream, snapshot, and control SEGGER J-Link RTT output from an embedded target. jrtt replaces JLinkRTTViewer with a daemon + pipe-friendly agent interface. Invoke when the user is debugging firmware, watching live RTT logs, capturing recent lines from a target, or asking about the state of a connected J-Link probe on Windows. Covers the full workflow — start the daemon, tail/dump/status/ping, stop it, and recover from a dead pipe.
---

# jrtt — J-Link RTT, agent-first

`jrtt` is a Windows CLI + daemon for **SEGGER J-Link RTT** (Real-Time Transfer).
It exposes a single binary with two roles:

- `jrtt -d` → daemon (background, owns the J-Link probe and the ring buffer)
- `jrtt <cmd>` → client (connects to the daemon over a Named Pipe)

Designed for AI agents: pipe-friendly, structured output, non-blocking, no GUI.

## When to use this skill

Use it whenever the user wants to:

- watch live `printf` / `SEGGER_RTT_printf` output from a running target
- replay the last N RTT lines
- check whether the J-Link probe is connected and what speed/chip is in use
- capture RTT output into a file or JSON stream for later analysis

Do **not** use it for: writing to the target (down-buffers are not supported in v1),
non-Windows hosts (Named Pipes are Windows-only), or anything that needs a GUI
(use `JLinkRTTViewer.exe` for that).

## Prerequisites

- **Windows** (uses `\\.\pipe\jrtt` Named Pipe)
- **SEGGER J-Link driver** installed (`JLink_x64.dll` auto-detected in standard locations)
- **Python ≥ 3.10**
- `pylink-square >= 1.0, < 3` (Apache 2.0)
- A known target chip name (e.g. `N32G430C8`, `STM32F407VG`, `nRF52840_xxAA`)

## Install

```bash
pip install pylink-square>=1.0
pip install -e .          # from this repo
# or, from a clone:
# pip install git+https://github.com/yu1745/jrtt
```

This installs the `jrtt` console script.

## Core workflow

### 1. Start the daemon (once per machine)

```bash
jrtt -d --chip N32G430C8
```

Flags worth knowing: `--pipe NAME` (default `\\.\pipe\jrtt`), `--dll PATH` (override
auto-detect), `--tif {swd,jtag}` (default `swd`), `--speed 4000` (kHz, 0 = adaptive).

The daemon runs detached — exit your shell, the daemon keeps running.

Any `jrtt` client command will **auto-spawn the daemon** if it isn't already up,
so step 1 is optional for one-off use.

### 2. Tail live output

```bash
jrtt tail                       # stream live
jrtt tail -n 100                # replay last 100 lines, then live
jrtt tail --regex '\[ERR\]'     # filter by Python regex
jrtt tail --channel 1           # different RTT up-buffer
jrtt tail --since 30s           # skip lines older than 30s
jrtt tail --max-lines 10        # exit after 10 lines
jrtt tail --json                # NDJSON output (one JSON object per line)
```

`tail` exits cleanly on **Ctrl+C** (sends `SIGINT`, drains pipe, returns 0).

### 3. Snapshot the ring buffer

```bash
jrtt dump                       # full ring buffer (up to 4096 lines)
jrtt dump --last 50             # last 50 lines
jrtt dump --since 5m            # last 5 minutes
jrtt dump --channel 1 --json    # channel 1, NDJSON
```

### 4. Inspect state

```bash
jrtt status          # human-readable: daemon/JLink/RTT/subscribers/ring
jrtt status --json   # machine-readable
jrtt ping            # roundtrip latency probe → "pong roundtrip=1.2ms version=0.0.1"
```

### 5. Shut down

```bash
jrtt stop
```

## Output formats

**Human (default):** `22:37:27.701 foc[171818999] cnt=171819007 us=37 mode=0 ...`

**NDJSON (`--json`):** one JSON object per line on stdout

```json
{"ts": 1718190047.701, "channel": 0, "data": "foc[171818999] cnt=171819007 us=37 mode=0 ...\n"}
```

NDJSON is the right choice for agents — it's line-delimited, parseable, and
won't break if a line contains special characters.

## Reading status output

`jrtt status` (human):

```
daemon:    up (pid 55440, uptime 14s)
jlink:     connected (SN 123456, SWD, 4000 kHz)
device:    N32G430C8
rtt:       active (ch0=1KB, ch1=1KB)
subscribers: 1
ring buffer: 59/4096 lines
```

`daemon.up` + `jlink.connected` + `rtt.running` is the green path. If any of
those is false, the corresponding line will explain why (`last_error`, `reason`).

## Common failure modes

| symptom                                    | likely cause                              | fix                                          |
|--------------------------------------------|-------------------------------------------|----------------------------------------------|
| `daemon:    up` but `jlink: disconnected`  | probe unplugged, wrong chip, bad SWD wire | reseat, check `--chip`, try `--tif jtag`     |
| `rtt:       inactive`                      | target not running RTT control block      | confirm firmware calls `SEGGER_RTT_Init()`   |
| `ping` hangs then `ping failed: ...`       | no daemon, pipe doesn't exist             | run `jrtt -d --chip ...` first, or just use any other `jrtt` cmd (auto-spawn) |
| `tail` emits nothing for seconds           | target idle, or RTT block in zero-init   | trigger an RTT write from the target         |
| `dump not ok: E_JLINK ...`                 | JLink DLL error                           | check `JLink_x64.dll` path / 32/64 mismatch  |

## Architecture (so you understand the IPC)

```
jrtt tail / dump / ping / stop
          ↕ Named Pipe (NDJSON length-prefixed frames)
jrtt -d ─── JLinkSession (pylink-square)
           ─── RttDaemonReader (poll 5ms)
           ─── RingBuffer (4096 lines, in-memory)
           ─── Broadcaster (per-subscriber fan-out)
```

- **One daemon, many clients.** Multiple `jrtt tail` / `jrtt dump` processes
  can connect to the same daemon simultaneously.
- **Auto-spawn.** First client after a reboot transparently starts the daemon.
- **Ring buffer = last 4096 lines.** `dump` returns whatever is still in it;
  older lines are gone.

## Agent playbook

When the user says "watch the logs" / "tail the output" / "what's the firmware printing":

1. Confirm the target chip and probe are connected (ask if unsure).
2. Start the daemon if not running: `jrtt -d --chip <NAME>` (or rely on auto-spawn).
3. Stream with `jrtt tail --json` so the output is parseable.
4. To stop streaming, send `Ctrl+C` / `SIGINT` — `tail` returns 0 cleanly.

When the user says "show me the last 50 lines" / "what just happened":

1. `jrtt dump --last 50` (or `--since 30s` for a time window).
2. If the output is empty, the ring buffer has been overwritten — try a
   `jrtt tail -n 50` after restart instead.

When the user says "is the probe still connected?" / "is the daemon alive?":

1. `jrtt ping` — fastest, single roundtrip.
2. `jrtt status --json` — full structured state for diagnostics.

When done debugging:

1. `jrtt stop` — clean shutdown, releases the probe.

## Known limitations (v1)

- **Read-only.** Down-buffer writes not supported yet.
- **Windows-only.** Named Pipes; no Linux/macOS.
- **No config file.** CLI args only.
- **Single channel by default** (`--channel N` works).
