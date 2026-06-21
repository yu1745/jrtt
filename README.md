# jrtt — J-Link RTT Tail

**Agent-first CLI + daemon for SEGGER J-Link RTT.**

Replaces `JLinkRTTViewer.exe` with a CLI tool designed for AI agents: fast,
non-blocking, pipe-friendly, structured output.

```
jrtt -d                           # start daemon (background)
jrtt tail                         # stream RTT lines (GNU tail-compatible)
jrtt dump --last 10               # snapshot recent lines
jrtt status                       # show daemon/JLink state
jrtt ping                         # health probe
jrtt stop                         # shutdown daemon
jrtt skill install                # install SKILL.md to .agents/skills/ (standard)
```

## This repo is also a skill repo

The `SKILL.md` at the repo root is a real Agent skill — it has the
`name`/`description` frontmatter, lives in the conventional layout, and
describes how an agent should use jrtt. You can:

1. **Use it in place.** Open this folder as the agent's working dir — the
   `SKILL.md` is discovered.
2. **Install it.** `jrtt skill install` copies `SKILL.md` (and `LICENSE`) to
   the agent's known skills directories.

   With **no arguments**, the default is the vendor-neutral
   [Agent Skills](https://agentskills.io) standard path (`.agents/skills/`)
   — no Claude/Codex bias:

   ```
   $ jrtt skill install
   jrtt skill: installing 'jrtt' from <repo>
     [agents-user]    installed: ~/.agents/skills/jrtt
     [agents-project] installed: <cwd>/.agents/skills/jrtt
   ```

   To target a specific vendor, pass `--target` (repeatable):

   ```
   $ jrtt skill install --list
   Known skill install targets:
     claude-user       Claude Code (user)            ~/.claude/skills/jrtt/SKILL.md
     claude-project    Claude Code (project)         <cwd>/.claude/skills/jrtt/SKILL.md
     codex-user        Codex CLI (user)              ~/.codex/skills/jrtt/SKILL.md
     agents-user       Agent Skills standard (user)  ~/.agents/skills/jrtt/SKILL.md
     agents-project    Agent Skills standard (proj)  <cwd>/.agents/skills/jrtt/SKILL.md

   $ jrtt skill install --target claude-user --target codex-user
   ```

   The `agents-*` targets hit `~/.agents/skills/` and `<cwd>/.agents/skills/`,
   the canonical path used by the [Agent Skills](https://agentskills.io) open
   standard (adopted by OpenAI Codex and others). Vendor-specific paths
   (`.claude/skills/`, `.codex/skills/`) are kept as separate targets because
   most harnesses do **not** read `.agents/skills/`.

   Flags: `--target {claude-user,claude-project,codex-user,agents-user,agents-project}`
   (repeatable), `--name NAME` (default `jrtt`), `--overwrite`, `--dry-run`.
   `jrtt skill uninstall` reverses it.

The repo is therefore **both** a Python package (the daemon + CLI) **and**
a skill repo (the `SKILL.md`). The `skill` subcommand is the bridge.

## Quick start

```bash
pip install pylink-square>=1.0
python -m jrtt -d --chip N32G430C8     # start daemon
python -m jrtt tail                     # print last 10 lines, exit (GNU default)
python -m jrtt tail -f                  # follow forever
```

## Architecture

```
jrtt tail / dump / ping / stop
          ↕ Named Pipe (NDJSON)
jrtt -d ─── JLinkSession (pylink-square)
           ─── RttDaemonReader (poll 5ms)
           ─── RingBuffer (4096 lines)
           ─── Broadcaster (per-subscriber fan-out)
```

- **Single binary**. `jrtt -d` → daemon role; `jrtt <cmd>` → client role.
- **Auto-spawn**. If no daemon is running, CLI spawns one as a detached child.
- **Single J-Link probe** per daemon. One daemon can serve many CLI clients.
- **Streaming NDJSON** over Windows Named Pipe (`\\.\pipe\jrtt`).

## Tail command

GNU `tail` semantics — `tail` prints and exits by default, add `-f` to follow:

```bash
jrtt tail                     # print last 10 lines from ring buffer, exit (GNU default)
jrtt tail -f                  # print last 10, then follow forever
jrtt tail -n 100              # print last 100, exit
jrtt tail -n 100 -f           # print last 100, then follow forever
jrtt tail --regex '\[ERR\]'   # filter by regex
jrtt tail --channel 1         # different RTT up-buffer
jrtt tail --since 30s         # only show lines from the last 30s
jrtt tail -f --max-lines 10   # follow but exit after 10 lines emitted
jrtt tail --json              # NDJSON output (for agents)
```

## Dependencies

- `pylink-square >= 1.0, < 3` (Apache 2.0)
- Windows (Named Pipes; no Linux/macOS support yet)
- SEGGER J-Link driver (`JLink_x64.dll`, auto-detected in standard locations)

## Example output

```
$ jrtt tail
22:37:27.701 foc[171818999] cnt=171819007 us=37 mode=0 rpm=0 ...
22:37:27.792 foc[171819999] cnt=171820007 us=37 mode=0 rpm=0 ...
22:37:27.893 foc[171820999] cnt=171821007 us=37 mode=0 rpm=0 ...

$ jrtt tail -f
... (follows forever; Ctrl+C to exit)

$ jrtt status
daemon:    up (pid 55440, uptime 14s)
jlink:     connected (SN 123456, SWD, 4000 kHz)
rtt:       active (ch0=1KB, ch1=1KB)
subscribers: 1
ring buffer: 59/4096 lines
```

## Known limitations (v1)

- **Read-only.** Down-buffer writes not supported yet.
- **Single channel.** RTT up-buffer 0 by default (`--channel N` support exists).
- **Windows-only.** Named Pipes are Windows-specific (Unix socket support TBD).
- **No config file.** CLI args only for now.