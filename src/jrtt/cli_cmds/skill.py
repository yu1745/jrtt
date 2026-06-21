"""jrtt skill — install the SKILL.md bundled with this repo to a known agent's skills directory.

Supports Claude Code (`~/.claude/skills/<name>/` and `<cwd>/.claude/skills/<name>/`)
and Codex (`~/.codex/skills/<name>/`). Each install drops a single `SKILL.md`
into a folder named after the skill (default: `jrtt`).

The repo is intentionally dual-purpose: the `SKILL.md` at the repo root IS
the skill, and the Python package is the daemon + CLI. The `skill install`
subcommand just copies that one file (plus optional companions) into the
target agent's skills tree so it is auto-discovered as a slash command / skill.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import Iterable

# --- Where skills live for each known agent ---------------------------------

# Each entry: (display_name, install_path_factory)
#   install_path_factory(root: Path) -> Path
# Path is the *directory* that will contain the skill folder.
# The skill folder name (default: "jrtt") is appended to it.

def _claude_user(root: Path) -> Path:
    # Claude Code user-level skills: %USERPROFILE%/.claude/skills/<name>/SKILL.md
    return root / ".claude" / "skills"

def _claude_project(root: Path) -> Path:
    # Project-level: <cwd>/.claude/skills/<name>/SKILL.md
    return Path.cwd() / ".claude" / "skills"

def _codex_user(root: Path) -> Path:
    # Codex CLI: ~/.codex/skills/<name>/SKILL.md
    return root / ".codex" / "skills"


TARGETS: dict[str, tuple[str, callable]] = {
    "claude-user":    ("Claude Code (user)",    _claude_user),
    "claude-project": ("Claude Code (project)", _claude_project),
    "codex-user":     ("Codex CLI (user)",      _codex_user),
}

DEFAULT_TARGETS: tuple[str, ...] = ("claude-user", "claude-project", "codex-user")


# --- Source resolution -------------------------------------------------------

def _find_repo_root(start: Path) -> Path:
    """Walk upward from `start` (this file's dir) until we find a SKILL.md.

    This makes the subcommand work whether jrtt is run from a source checkout
    or an installed wheel — the wheel install copies SKILL.md next to
    `cli_cmds/skill.py` at module-load time via package_data.
    """
    here = start.resolve()
    for candidate in (here, *here.parents):
        if (candidate / "SKILL.md").is_file():
            return candidate
    raise FileNotFoundError(
        "SKILL.md not found relative to jrtt package — repo is not a skill repo"
    )


def _skill_source_files(repo_root: Path) -> list[Path]:
    """Files to copy into the skill folder.

    Keep this minimal — agents read SKILL.md on demand, not at install time.
    LICENSE is included so the skill is properly licensed when redistributed.
    """
    files: list[Path] = []
    skill_md = repo_root / "SKILL.md"
    if skill_md.is_file():
        files.append(skill_md)
    license_file = repo_root / "LICENSE"
    if license_file.is_file():
        files.append(license_file)
    return files


# --- Install / uninstall -----------------------------------------------------

def _install_one(target_key: str, name: str, source_files: list[Path],
                 *, overwrite: bool, dry_run: bool) -> tuple[Path, list[Path]]:
    """Install into one target. Returns (dest_dir, copied_files)."""
    if target_key not in TARGETS:
        raise ValueError(f"unknown target: {target_key}")
    display_name, factory = TARGETS[target_key]
    home = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or Path.home())
    base = factory(home)
    dest_dir = base / name
    copied: list[Path] = []
    if not dry_run:
        dest_dir.mkdir(parents=True, exist_ok=True)
    for src in source_files:
        dst = dest_dir / src.name
        if dst.exists() and not overwrite:
            # Leave existing file alone — don't clobber user edits.
            copied.append(dst)
            continue
        if dry_run:
            copied.append(dst)
            continue
        shutil.copy2(src, dst)
        copied.append(dst)
    return dest_dir, copied


def _uninstall_one(target_key: str, name: str, *, dry_run: bool) -> Path | None:
    if target_key not in TARGETS:
        raise ValueError(f"unknown target: {target_key}")
    _, factory = TARGETS[target_key]
    home = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or Path.home())
    dest_dir = factory(home) / name
    if not dest_dir.exists():
        return None
    if not dry_run:
        shutil.rmtree(dest_dir)
    return dest_dir


# --- argparse plumbing -------------------------------------------------------

def build_skill_parser(sub) -> argparse.ArgumentParser:
    """Attach a `skill` subparser to the top-level parser. Returns the parent."""
    p = sub.add_parser("skill", help="Install/uninstall the jrtt skill to agent skill directories")
    skill_sub = p.add_subparsers(dest="skill_cmd", required=True)

    # install
    inst = skill_sub.add_parser("install", help="Copy SKILL.md into target skill directories")
    inst.add_argument(
        "--target", "-t",
        action="append",
        choices=list(TARGETS.keys()),
        help=f"Target to install to (repeatable). Default: {' + '.join(DEFAULT_TARGETS)}",
    )
    inst.add_argument(
        "--name", default="jrtt",
        help="Skill folder name to create (default: jrtt)",
    )
    inst.add_argument(
        "--overwrite", action="store_true",
        help="Overwrite existing files (default: skip files that already exist)",
    )
    inst.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be done, but make no changes",
    )
    inst.add_argument(
        "--list", action="store_true", dest="list_targets",
        help="List known targets and their resolved paths, then exit",
    )

    # uninstall
    un = skill_sub.add_parser("uninstall", help="Remove the skill folder from target directories")
    un.add_argument(
        "--target", "-t",
        action="append",
        choices=list(TARGETS.keys()),
        help=f"Target to remove from (repeatable). Default: {' + '.join(DEFAULT_TARGETS)}",
    )
    un.add_argument(
        "--name", default="jrtt",
        help="Skill folder name to remove (default: jrtt)",
    )
    un.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be done, but make no changes",
    )

    return p


def _resolve_targets(args: argparse.Namespace) -> list[str]:
    """Return the target list, applying default if --target was not given."""
    raw: Iterable[str] | None = getattr(args, "target", None)
    if not raw:
        return list(DEFAULT_TARGETS)
    # argparse `action="append"` can produce duplicates; preserve order, dedupe
    seen: set[str] = set()
    out: list[str] = []
    for t in raw:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _print_targets() -> None:
    home = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or Path.home())
    for key, (display, factory) in TARGETS.items():
        base = factory(home)
        print(f"  {key:16s}  {display:24s}  {base}/<name>/SKILL.md")


def run(args: argparse.Namespace) -> int:
    # `skill` does NOT need a daemon — short-circuit before ensure_daemon().
    if getattr(args, "list_targets", False):
        print("Known skill install targets:")
        _print_targets()
        return 0

    if args.skill_cmd == "install":
        return _run_install(args)
    if args.skill_cmd == "uninstall":
        return _run_uninstall(args)
    print("jrtt skill: missing subcommand (try: jrtt skill install)", file=sys.stderr)
    return 1


def _run_install(args: argparse.Namespace) -> int:
    try:
        repo_root = _find_repo_root(Path(__file__).parent)
    except FileNotFoundError as e:
        print(f"jrtt skill: {e}", file=sys.stderr)
        return 2

    source_files = _skill_source_files(repo_root)
    if not source_files:
        print("jrtt skill: no source files found to install", file=sys.stderr)
        return 2

    targets = _resolve_targets(args)
    overwrite = getattr(args, "overwrite", False)
    dry_run = getattr(args, "dry_run", False)
    name = args.name

    print(f"jrtt skill: installing {name!r} from {repo_root}")
    for src in source_files:
        print(f"  source: {src.relative_to(repo_root)}")
    if dry_run:
        print("  (dry-run — no changes will be made)")

    failures = 0
    for t in targets:
        try:
            dest_dir, copied = _install_one(t, name, source_files,
                                            overwrite=overwrite, dry_run=dry_run)
        except Exception as e:
            print(f"  [{t}] FAILED: {e}", file=sys.stderr)
            failures += 1
            continue
        verb = "would install" if dry_run else "installed"
        print(f"  [{t}] {verb}: {dest_dir}")
        for f in copied:
            suffix = " (skipped, exists)" if f.exists() and not overwrite and not dry_run else ""
            print(f"           - {f.name}{suffix}")

    if failures:
        print(f"jrtt skill: {failures} target(s) failed", file=sys.stderr)
        return 1
    return 0


def _run_uninstall(args: argparse.Namespace) -> int:
    targets = _resolve_targets(args)
    dry_run = getattr(args, "dry_run", False)
    name = args.name

    print(f"jrtt skill: removing {name!r}")
    if dry_run:
        print("  (dry-run — no changes will be made)")

    removed = 0
    for t in targets:
        path = _uninstall_one(t, name, dry_run=dry_run)
        if path is None:
            print(f"  [{t}] not present")
            continue
        verb = "would remove" if dry_run else "removed"
        print(f"  [{t}] {verb}: {path}")
        removed += 1

    if removed == 0:
        print("jrtt skill: nothing to remove")
    return 0
