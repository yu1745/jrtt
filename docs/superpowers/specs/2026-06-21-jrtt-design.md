# jrtt — Design Spec

**Date:** 2026-06-21
**Status:** Draft (post–preliminary validation)

## TL;DR

Replace SEGGER `JLinkRTTViewer.exe` with a CLI tool optimized for AI agents.
Single Python binary that can act as a daemon (`jrtt -d`) or as a one-shot
client (`jrtt tail`, `jrtt dump`, `jrtt status`). Clients connect to the
daemon over a Windows Named Pipe. Daemon owns the J-Link probe, broadcasts
RTT bytes to all subscribers.

---

## §0 — Pre-validated facts (verified on real hardware)

These are not assumptions — they were verified end-to-end against a J-Link
probe + N32G430C8 target during the brainstorming phase. They drive every
downstream decision in this spec.

| Fact | Evidence |
|---|---|
| `pylink-square` (PyPI: `pylink-square`, NOT `pylink`) wraps `JLink_x64.dll` correctly | `pip install git+https://github.com/square/pylink.git` → `pylink-square-2.0.1` |
| pip name `pylink` is a different package (Salem Harrache's GSM library) — do NOT install | `pip install pylink` → 0.3.3, unrelated |
| SEGGER's RTT high-level API is `JLINK_RTTERMINAL_*` (NOT `JLINKARM_RTT_*` — those don't exist) | pefile dump of `JLink_x64.dll` shows only 3 RTT symbols: `JLINK_RTTERMINAL_{Control,Read,Write}` |
| RTT control block auto-discovery: `rtt_start()` needs no address (SEGGER finds it) | Verified on N32G430C8: 11782 bytes read after `rtt_start()` |
| pylink 2.x requires explicit `chip_name` on `connect()` (e.g. `"N32G430C8"`) | Required argument; without it `TypeError` |
| pylink `Library` does NOT auto-discover DLL on this machine; must pass `dllpath=` explicitly | `Library()` → `dll=None`, `TypeError: Expected to be given a valid DLL` |
| Probe must be exclusively held — no other J-Link tool can run concurrently | `JLinkRTTViewer.exe` running → `JLINKARM_Open()` hangs indefinitely |
| pylink's `open()` internally: `JLINKARM_SelectUSB(0)` → `jlock.JLock(sn)` → `JLINKARM_OpenEx(log_h, err_h)` | Read source of `pylink/jlink.py:683–759` |
| TIF must be set BEFORE `set_speed` for SWD-only probes | Verified by successful run order: `set_tif(1)` → `set_speed(4000)` |
| Target emits FOC logs at ~1ms cadence on up-buffer 0 (observed) | ~1000 bytes/sec; lines like `foc[N] cnt=N us=37 mode=0 rpm=0 vref=0 pos=2147483647mdeg ...` |
| FOC lines are 136 bytes with `\r\n` terminator | Direct observation in integration tests |

---

## §1 — Goals & non-goals

**Goals (v1):**
- CLI / daemon hybrid for SEGGER J-Link RTT
- Agent-first: structured NDJSON output, streamable via stdout, daemon
  addressable over local IPC
- GNU `tail`-style command UX for the streaming case
- Single-JLink-probe, single-host (Windows-only in v1)
- Apache-2.0 dependency (`pylink-square`) — no copy-pasted ctypes

**Non-goals (v1):**
- Multi-probe concurrency
- Linux / macOS support
- RTT down-buffer writes (v1 read-only on up-buffer 0)
- Web / TUI front-ends
- Flash programming, SWO trace, JScope (separate tools)
- MCP server (deferred; pipe protocol is designed to be wrapping-friendly)

---

## §2 — Architecture

```
                ┌────────────────────────────────────┐
   jrtt -d      │           jrtt daemon              │
   (no cmd) ──► │  ┌────────────────────────────┐    │
                │  │ JLinkSession (pylink)      │    │
                │  │   open(N32G430C8, SWD)     │    │
                │  │   start_rtt()              │    │
                │  └────────────────────────────┘    │
                │  ┌────────────────────────────┐    │
                │  │ RttReader (poll loop)      │    │
                │  │   poll 5ms                 │    │
                │  │   split into lines         │    │
                │  └────────────────────────────┘    │
                │  ┌────────────────────────────┐    │
   cli ─────►   │  │ Ring buffer (4096 lines)   │◄──►│ Named Pipe
   tail         │  │ Sub broadcaster (NDJSON)   │    │ \\.\pipe\jrtt
   dump         │  └────────────────────────────┘    │
   status       │                                    │
                └────────────────────────────────────┘
```

**Process model:**
- Single entry point `jrtt` (PyInstaller-ready, but v1 ships as `python -m jrtt`)
- `jrtt -d` → daemon role, takes the Named Pipe and the J-Link probe
- `jrtt <cmd>` → CLI role; spawns daemon if not running, then connects via pipe
- Daemon lifetime: 30-minute idle timeout (no CLI connected → exits)
- Lock: `\\.\pipe\jrtt` creation itself acts as the singleton check —
  if `CreateNamedPipe` succeeds, no daemon is present; spawn one and retry

**Why a daemon at all?**
- Agent spawns many short-lived CLI invocations; without a daemon, each one
  reopens the J-Link (slow: ~300ms probe handshake + RTT start block scan)
- Daemon keeps RTT state warm; new CLIs attach in <10ms
- Multiple agents / shells can subscribe to the same stream

---

## §3 — Process roles & lifecycle

### Daemon

Created when:
1. User runs `jrtt -d` explicitly, OR
2. CLI runs and finds no daemon. Detection: try `CreateFile(
   \\.\pipe\jrtt, ..., OPEN_EXISTING)` with `ERROR_FILE_NOT_FOUND` → no daemon
   listening. CLI then `Popen([sys.executable, '-m', 'jrtt', '-d'])` and
   `os._exit(0)`. Belt-and-braces file lock at
   `%LOCALAPPDATA%\jrtt\daemon.lock` prevents the rare race where two CLIs
   spawn two daemons simultaneously.

Daemon lifecycle:
1. Acquire file lock at `%LOCALAPPDATA%\jrtt\daemon.lock` (extra belt-and-braces)
2. Create Named Pipe `\\.\pipe\jrtt` (PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED)
3. Open J-Link: `JLinkSession(dll_path=...).open(chip=..., tif=SWD, speed=...)`
4. Start RTT: `session.start_rtt()`
5. Spawn RTT reader thread (polls 5ms, fills ring buffer, broadcasts to subscribers)
6. Spawn pipe accept loop (one thread per subscriber)
7. On SIGTERM / `shutdown` op: stop RTT, close probe, exit 0

Idle timeout: 30 min no CLI connections → graceful exit. `--idle-keepalive 0` disables.

### CLI

Created for every user invocation. Auto-spawns daemon if missing.

```
jrtt -d                    # enter daemon role
jrtt tail [opts]           # subscribe + stream
jrtt dump [opts]           # snapshot ring buffer
jrtt status                # daemon + J-Link + RTT state
jrtt ping                  # health probe (agent-friendly)
jrtt stop                  # ask daemon to exit gracefully
```

---

## §4 — Named Pipe protocol

**Pipe name:** `\\.\pipe\jrtt`

**Wire format:** newline-delimited JSON (NDJSON), one frame per line, ≤ 64 KB per frame.

**Frame types:**
```jsonc
// request (CLI → daemon)
{"v":1, "t":"req", "id":"<uuid>", "op":"tail", "args":{...}}

// response (daemon → CLI, exactly one per req)
{"v":1, "t":"res", "id":"<uuid>", "ok":true,  "data":{...}}
{"v":1, "t":"res", "id":"<uuid>", "ok":false, "code":"E_NO_JLINK", "msg":"..."}

// event (daemon → CLI, unsolicited, scoped by source req's id)
{"v":1, "t":"evt", "id":"<uuid>", "name":"rtt.line", "data":{...}}
{"v":1, "t":"evt", "id":"<uuid>", "name":"jlink.disconnected", "data":{}}
{"v":1, "t":"evt", "id":"<uuid>", "name":"daemon.shutdown", "data":{}}
```

**Operations:**

| op | args | response data |
|---|---|---|
| `ping` | — | `{roundtrip_ms}` |
| `status` | — | `{daemon, jlink, rtt, ring_buffer, subscribers}` |
| `tail` | `{channel?, regex?, since?, max_lines?}` | starts a subscription; subsequent `rtt.line` events |
| `dump` | `{channel?, last?, since?}` | `{lines:[{ts,channel,data},...]}` |
| `subscribe` | (alias of tail) | — |
| `unsubscribe` | — | — |
| `shutdown` | — | — (then daemon exits) |

**`rtt.line` event data:**
```json
{"ts":1719000000.123, "channel":0, "data":"hello\n"}
```

`data` is UTF-8 if decodable; falls back to `{"hex": "..."}` on decode failure.

---

## §5 — CLI command spec

### `jrtt tail`

GNU `tail` style + jrtt extensions:
```
jrtt tail [-f] [-n N] [--regex PAT] [--channel N] [--since DUR]
          [--max-lines N] [--json] [--no-pager]
```

Behaviors:
- `-f / --follow` (default): block, stream until EOF/Ctrl-C / `max-lines`
- `-n N / --lines N`: how many historical lines to show before live streaming
- `--regex PAT`: filter lines; non-matching suppressed
- `--channel N`: which up-buffer (default 0)
- `--since DUR`: only show lines newer than `DUR` ago (e.g. `10s`, `2m`),
  where the reference point is **the moment the CLI connected** to the daemon,
  not wall-clock. Format: `<integer><s|m|h>`.
- `--max-lines N`: exit after N lines emitted
- `--json`: NDJSON output for agent consumption

Default: `-f -n 0 --channel 0` (live only, no replay, channel 0).

### `jrtt dump`
```
jrtt dump [--last N] [--since DUR] [--channel N] [--json]
```
One-shot dump from ring buffer; exits after writing.

### `jrtt status`
```
jrtt status [--json]
```
Human or JSON.

### `jrtt ping`
```
jrtt ping
```
Prints `pong roundtrip=Nms version=X.Y.Z`, exit 0. Non-zero if daemon absent.

### `jrtt stop`
```
jrtt stop
```
Asks daemon to shut down cleanly. Exits 0 after ack.

---

## §6 — Error model

| Exit code | Meaning |
|---|---|
| 0 | Success |
| 1 | User error (bad args, missing required option) |
| 2 | Daemon error (not running, pipe broken, protocol error) |
| 3 | J-Link error (probe not found, target connect failed, RTT start failed) |
| 4 | Timeout (no data, no daemon ack) |
| 5 | Conflicting process (other J-Link tool holding probe) |

---

## §7 — Data model

**Ring buffer (daemon-side):**
- Capacity: 4096 lines (configurable, `--ring-size`)
- Eviction: FIFO when full
- Each entry: `{ts: float, channel: int, data: bytes}`
- Used for `dump` and `--since` historical queries

**Subscriber state:**
- One per active CLI connection holding a `tail` op
- Holds: filter regex, channel filter, queue to writer thread
- Auto-removed on pipe disconnect

**Idle timeout:**
- Reset on every new pipe connection
- 30 min default; `--idle-keepalive 0` disables

---

## §8 — Configuration

v1: command-line only. v2: `~/.jrtt/config.toml` for defaults
(`chip`, `tif`, `speed`, `channel`, `idle_timeout`).

---

## §9 — Testing strategy

Three layers:

1. **Pure unit tests** (no hardware, runs in CI)
   - FakeJLinkSession for JLinkSession API contract
   - RttReader line splitting / partial buffer / CRLF
   - Ring buffer eviction
   - Protocol codec (NDJSON round-trip)
   - CLI parser / argv validation

2. **Integration tests** (`@pytest.mark.requires_hardware`)
   - Real J-Link probe + N32G430C8 target
   - Currently 3 tests, all pass on this hardware:
     - `test_session_connected`
     - `test_rtt_reads_real_bytes`
     - `test_rtt_reader_splits_lines_on_real_data`
   - Skipped in CI; ran manually on 2026-06-21.

3. **Daemon-CLI end-to-end** (manual, on real hardware)
   - Spawn daemon, connect two CLIs (one `tail`, one `dump`), verify both receive data
   - Verify daemon graceful shutdown via `jrtt stop`

CI signal: `pytest tests/unit -v` (must pass; currently 11/11).
Hardware signal: `pytest tests/ -v -m requires_hardware` (3/3 on this machine).

---

## §10 — Repository layout (target)

```
jrtt/
├── pyproject.toml                    # deps: pylink-square>=1.0,<3
├── README.md
├── docs/superpowers/specs/           # this file
├── src/jrtt/
│   ├── __init__.py
│   ├── __main__.py                   # entry point for `python -m jrtt`
│   ├── cli.py                        # argparse + role dispatch
│   ├── roles.py                      # decide CLI vs daemon based on argv
│   ├── spawn.py                      # CLI spawns daemon subprocess
│   ├── ipc.py                        # Named Pipe server + client + NDJSON codec
│   ├── protocol.py                   # request/response/event dataclasses
│   ├── jlink/
│   │   ├── __init__.py
│   │   ├── session.py                # JLinkSession facade (real)
│   │   ├── fake_session.py           # FakeJLinkSession (unit tests)
│   │   ├── dll.py                    # JLinkDll Protocol (kept for typecheck)
│   │   ├── fake.py                   # FakeJLinkDll (lower-level fake)
│   │   ├── constants.py              # TIF, RTTCommand
│   │   └── structs.py                # EmuConnectInfo (kept for compatibility)
│   ├── rtt_reader.py                 # line splitting (already implemented)
│   ├── daemon/
│   │   ├── server.py                 # pipe accept loop, broadcaster
│   │   ├── reader.py                 # RTT poll loop (uses RttReader)
│   │   ├── ring_buffer.py            # bounded FIFO
│   │   ├── subscriber.py             # per-CLI state
│   │   └── lifecycle.py              # idle timeout, shutdown
│   └── cli_cmds/
│       ├── tail.py
│       ├── dump.py
│       ├── status.py
│       ├── ping.py
│       └── stop.py
└── tests/
    ├── unit/
    │   ├── test_rtt_reader.py        # ✅ 11/11
    │   ├── test_ring_buffer.py       # TODO
    │   ├── test_protocol.py          # TODO
    │   ├── test_ipc.py               # TODO
    │   └── test_cli_parser.py        # TODO
    └── integration/
        └── test_real_session.py      # ✅ 3/3 on N32G430C8
```

---

## §11 — Open questions / risks

| Risk | Mitigation |
|---|---|
| SEGGER locks probe exclusively → conflicts with `JLinkRTTViewer` etc. | Detect, return exit code 5, message: "J-Link is held by another process. Close JLinkRTTViewer and retry." |
| pylink-square 2.x breaking changes | Pin `pylink-square>=1.0,<3`; integration test catches breakage |
| Probe disconnect mid-session (USB unplug) | RTT read returns empty / error → emit `jlink.disconnected` event, attempt reconnect every 1s, auto-resume when probe returns |
| MCU reset → RTT control block address changes | SEGGER's high-level API re-scans on `rtt_start()`; we call `rtt_stop()` + `rtt_start()` on first sign of stale data |
| RTT block never found (target not yet printing RTT) | Don't error; daemon stays in "RTT starting" state, `status` reports it; once target emits the SEGGER control block, daemon auto-recovers |
| Named Pipe on Windows has 64KB-per-message limit | NDJSON frames capped at 64KB; large buffers chunked with explicit `chunk_seq` (v2; v1 limits RTT read to 4096 bytes anyway) |
| Long-running daemon leaks USB handle | Trust pylink's `close()` + `jlock` release; verified manually |

---

## §12 — Out of scope for v1 (YAGNI list)

- Linux/macOS support (named pipe → unix socket swap, but defer)
- Multi-probe (per-process daemon is fine; multiple daemons on different pipes)
- MCP server (wrap daemon later)
- Config file (CLI only for v1)
- Down-buffer writes (read-only for v1)
- Persistent logs (ring buffer only)
- Web / TUI (none)
- Flash, SWO, JScope, trace (use the official tools)

---

## §13 — Implementation phases (for the upcoming plan)

Each phase ships with unit tests; phases 4+ additionally require an integration
smoke test against real hardware (currently 3/3 passing as the baseline).

| Phase | Deliverable | Exit criterion |
|---|---|---|
| 1 | NDJSON codec (`protocol.py`) | unit tests pass |
| 2 | Named Pipe server + client (`ipc.py`) | two-process loopback test passes |
| 3 | Ring buffer (`daemon/ring_buffer.py`) | unit tests pass (eviction, capacity, peek) |
| 4 | Daemon reader loop (`daemon/reader.py`) | RTT poll loop reads from FakeJLinkSession and fills buffer |
| 5 | Daemon subscriber + broadcaster (`daemon/server.py`) | one CLI tail subscriber receives every line the reader emits |
| 6 | CLI subcommands (`cli_cmds/*`) | `tail`, `dump`, `status`, `ping`, `stop` each round-trip through pipe |
| 7 | Role dispatch + spawn (`roles.py`, `spawn.py`) | CLI auto-spawns daemon if missing; existing daemon is reused |
| 8 | End-to-end manual test | `jrtt -d` + `jrtt tail` + `jrtt dump` concurrently on real hardware |
| 9 | README + `--help` text | self-documenting CLI |