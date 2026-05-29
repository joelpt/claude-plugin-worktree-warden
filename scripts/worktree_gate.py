#!/usr/bin/env python3
"""Worktree enforcement gate: shared policy plus the grant/opt-out CLI.

The PreToolUse hook (``enforce_worktree_hook.py``) imports :func:`decide` to
rule on each ``Edit``/``Write``. The same machinery is exposed as a CLI so the
agent (and the user) can open a time-boxed exception when an edit legitimately
belongs on the main checkout, end that exception early, opt out of enforcement
per-repo or globally, and tune the exception window.

Persistent settings live in a small JSON config at two scopes:

  * user    -- ``$XDG_CONFIG_HOME/worktree-gate/config.json`` (defaults to
              ``~/.config/worktree-gate/config.json``); applies to every repo.
  * project -- ``<git-common-dir>/worktree-gate-config.json``; personal to this
              clone (it lives inside ``.git`` and is never committed).

Each config may set ``enforce`` (bool) and ``window_seconds`` (int). A project
value overrides the user value key-by-key; absent keys fall back to the user
config and then to the built-in defaults.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

DEFAULT_WINDOW_SECONDS = 15 * 60
MIN_WINDOW_SECONDS = 60
MAX_WINDOW_SECONDS = 8 * 60 * 60

GRANT_FILENAME = "worktree-gate-grant.json"
PROJECT_CONFIG_FILENAME = "worktree-gate-config.json"
USER_CONFIG_BASENAME = "config.json"
AUDIT_BASENAME = "audit.log"


@dataclass(frozen=True)
class GitFacts:
    """Resolved git context for the session's working directory.

    Attributes:
        is_repo: Whether cwd is inside a git working tree.
        repo_root: Absolute path of the working tree's top level, or None.
        git_common_dir: Absolute path of the shared git dir, or None. Identical
            across the main checkout and all of its linked worktrees, so it is
            the natural per-repo key for grant and project-config files.
        in_linked_worktree: Whether cwd sits in a linked worktree rather than
            the main checkout.
    """

    is_repo: bool
    repo_root: str | None
    git_common_dir: str | None
    in_linked_worktree: bool


@dataclass(frozen=True)
class Settings:
    """Effective, scope-resolved enforcement settings.

    Attributes:
        disabled_scope: The scope that turned enforcement off ("user" or
            "project"), or None when enforcement is active.
        window_seconds: Lifetime applied to a freshly granted exception.
    """

    disabled_scope: str | None
    window_seconds: int


@dataclass(frozen=True)
class Decision:
    """Outcome of evaluating one edit against the gate.

    Attributes:
        allow: Whether the edit may proceed.
        reason: Short machine-ish explanation of the ruling.
        log_grant_use: True when the allow was spent against an active
            exception, so the caller should record the use.
    """

    allow: bool
    reason: str
    log_grant_use: bool = False


def run_git(args: list[str], cwd: str) -> tuple[int, str]:
    """Run a git command in cwd and return its exit code and stripped stdout.

    Args:
        args: Arguments following ``git`` (cwd is supplied via ``-C``).
        cwd: Directory to run the command from.

    Returns:
        A ``(returncode, stdout)`` pair; ``(1, "")`` if git cannot be invoked.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return 1, ""
    return proc.returncode, proc.stdout.strip()


def git_facts(cwd: str) -> GitFacts:
    """Resolve the git context for cwd in a single rev-parse call.

    Args:
        cwd: The session working directory.

    Returns:
        Populated GitFacts; a non-repo cwd yields ``is_repo=False``.
    """
    rc, out = run_git(
        ["rev-parse", "--show-toplevel", "--git-dir", "--git-common-dir"], cwd
    )
    if rc != 0:
        return GitFacts(False, None, None, False)
    lines = out.splitlines()
    if len(lines) != 3:
        return GitFacts(False, None, None, False)
    toplevel, git_dir, common_dir = lines
    git_dir_abs = os.path.realpath(os.path.join(cwd, git_dir))
    common_abs = os.path.realpath(os.path.join(cwd, common_dir))
    return GitFacts(
        is_repo=True,
        repo_root=os.path.realpath(toplevel),
        git_common_dir=common_abs,
        in_linked_worktree=git_dir_abs != common_abs,
    )


def _is_within(path: str, root: str) -> bool:
    """Return whether an absolute path is root itself or nested beneath it."""
    return path == root or path.startswith(root + os.sep)


def decide(
    *,
    file_path: str | None,
    facts: GitFacts,
    now: float,
    disabled_scope: str | None,
    grant_expires_at: float | None,
) -> Decision:
    """Rule on a single edit. Pure: all I/O is resolved by the caller.

    The order is deliberate. The opt-out is checked first so the escape hatch
    works even if later logic would misbehave. Non-repo, linked-worktree,
    outside-the-checkout, and inside-.git edits are always allowed -- they are
    never the case worktrees exist to isolate. Only edits to the main
    checkout's own files are gated, and an unexpired exception lifts that gate.

    Args:
        file_path: Absolute or relative path the tool intends to write, if any.
        facts: Resolved git context for the session.
        now: Current epoch seconds (injected for testability).
        disabled_scope: Scope that disabled enforcement, or None.
        grant_expires_at: Expiry epoch of the active exception, or None.

    Returns:
        The allow/block Decision.
    """
    if disabled_scope is not None:
        return Decision(True, f"enforcement disabled ({disabled_scope} scope)")
    if not facts.is_repo or facts.repo_root is None:
        return Decision(True, "not inside a git repository")
    if facts.in_linked_worktree:
        return Decision(True, "already inside a linked worktree")
    if file_path is None:
        return Decision(True, "no file path to evaluate")

    target = os.path.realpath(file_path)
    if not _is_within(target, facts.repo_root):
        return Decision(True, "edit target is outside the main checkout")
    if facts.git_common_dir and _is_within(target, facts.git_common_dir):
        return Decision(True, "edit target is inside the git directory")
    if grant_expires_at is not None and now < grant_expires_at:
        return Decision(True, "active worktree-gate exception", log_grant_use=True)
    return Decision(False, "main-checkout edit requires a worktree or exception")


def user_config_dir() -> Path:
    """Return the user-scope config directory (honoring XDG_CONFIG_HOME)."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return Path(base) / "worktree-gate"


def user_config_path() -> Path:
    """Return the path to the user-scope config file."""
    return user_config_dir() / USER_CONFIG_BASENAME


def project_config_path(git_common_dir: str | None) -> Path | None:
    """Return the project-scope config path, or None outside a repo."""
    if not git_common_dir:
        return None
    return Path(git_common_dir) / PROJECT_CONFIG_FILENAME


def grant_path(git_common_dir: str | None) -> Path | None:
    """Return the active-exception token path, or None outside a repo."""
    if not git_common_dir:
        return None
    return Path(git_common_dir) / GRANT_FILENAME


def _load_json(path: Path | None) -> dict[str, object]:
    """Read a JSON object, returning {} for a missing or malformed file.

    Defensive by design: a corrupt config must never crash the gate nor wedge
    the opt-out. An unreadable file simply contributes no overrides.
    """
    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {}
    return cast(dict[str, object], data) if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict[str, object]) -> None:
    """Write a JSON object atomically (temp + rename), creating parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)  # atomic on POSIX — no torn read of token/config


def resolve_settings(facts: GitFacts) -> Settings:
    """Merge user and project config into effective settings.

    Project values override user values key-by-key; absent keys fall back to
    the user config, then to the built-in defaults.

    Args:
        facts: Resolved git context (project config is keyed by its common dir).

    Returns:
        The effective Settings.
    """
    user_cfg = _load_json(user_config_path())
    proj_cfg = _load_json(project_config_path(facts.git_common_dir))

    disabled_scope: str | None = None
    for scope, cfg in (("user", user_cfg), ("project", proj_cfg)):
        value = cfg.get("enforce")
        if isinstance(value, bool):  # project visited last, so it overrides user
            disabled_scope = None if value else scope

    window = DEFAULT_WINDOW_SECONDS
    for cfg in (user_cfg, proj_cfg):
        value = cfg.get("window_seconds")
        if isinstance(value, int) and not isinstance(value, bool):
            window = _clamp_window(value)
    return Settings(disabled_scope=disabled_scope, window_seconds=window)


def read_grant_expiry(git_common_dir: str | None) -> float | None:
    """Return the active exception's expiry epoch, or None if there is none."""
    data = _load_json(grant_path(git_common_dir))
    expires = data.get("expires_at")
    if isinstance(expires, (int, float)) and not isinstance(expires, bool):
        return float(expires)
    return None


def _clamp_window(seconds: int) -> int:
    """Clamp a window length to the supported range."""
    return max(MIN_WINDOW_SECONDS, min(MAX_WINDOW_SECONDS, seconds))


def parse_duration(text: str) -> int:
    """Parse a duration like ``900``, ``30s``, ``15m``, or ``1h`` to seconds.

    Args:
        text: A bare integer (seconds) or an integer with an s/m/h suffix.

    Returns:
        The clamped duration in seconds.

    Raises:
        ValueError: If text is not a recognized duration.
    """
    text = text.strip().lower()
    units = {"s": 1, "m": 60, "h": 3600}
    multiplier = 1
    if text and text[-1] in units:
        multiplier = units[text[-1]]
        text = text[:-1]
    try:
        value = int(text)
    except ValueError as exc:
        raise ValueError(f"invalid duration: {text!r}") from exc
    return _clamp_window(value * multiplier)


def cli_path() -> str:
    """Return the runnable ``python3 /abs/worktree_gate.py`` command prefix."""
    return f"{sys.executable} {Path(__file__).resolve()}"


def block_message(facts: GitFacts, file_path: str | None) -> str:
    """Compose the stderr text shown to the agent when an edit is blocked."""
    cli = cli_path()
    root = facts.repo_root or "the main checkout"
    target = f" ({file_path})" if file_path else ""
    return (
        f"🌳 Worktree gate: editing the main checkout blocked{target}.\n"
        f"You are in the main checkout of {root}, not a linked worktree.\n"
        "Choose one:\n"
        "  • Preferred — call EnterWorktree to isolate this work, then retry.\n"
        "  • If this edit is legitimately main-side (conflict resolution, "
        "landing to main, or the user asked for it), open a timed exception:\n"
        f'      {cli} grant "<why this must happen on main>"\n'
        f"    then run `{cli} finished` as soon as the main-side work is done.\n"
        f"  • To stop enforcing here: {cli} disable   (add --user for global).\n"
    )


def log_event(action: str, reason: str, file_path: str | None) -> None:
    """Append an audit line; never raises (auditing must not block work)."""
    try:
        path = user_config_dir() / AUDIT_BASENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with path.open("a") as handle:
            handle.write(f"{stamp}\t{action}\t{reason}\t{file_path or ''}\n")
    except Exception:
        pass


def _scope_config_path(args: argparse.Namespace, facts: GitFacts) -> Path | None:
    """Resolve the config path for the scope selected by ``--user``."""
    if args.user:
        return user_config_path()
    return project_config_path(facts.git_common_dir)


def _update_config(path: Path, **changes: object) -> None:
    """Merge changes into the JSON config at path."""
    data = _load_json(path)
    data.update(changes)
    _write_json(path, data)


def cmd_grant(args: argparse.Namespace) -> int:
    """Open a timed exception for main-checkout edits."""
    facts = git_facts(os.getcwd())
    target = grant_path(facts.git_common_dir)
    if target is None:
        print("worktree-gate: not inside a git repository; nothing to grant.")
        return 1
    settings = resolve_settings(facts)
    reason = " ".join(args.reason).strip() or "(no reason given)"
    now = time.time()
    expires_at = now + settings.window_seconds
    _write_json(
        target,
        {"reason": reason, "granted_at": now, "expires_at": expires_at},
    )
    log_event("grant", reason, None)
    minutes = settings.window_seconds // 60
    until = time.strftime("%H:%M:%S", time.localtime(expires_at))
    print(
        f"worktree-gate: exception OPEN for ~{minutes} min (until {until}).\n"
        f"Reason: {reason}\n"
        "IMPORTANT: this is a deliberate, time-boxed bypass. As soon as the "
        "main-side work is finished, end it early by running:\n"
        f"    {cli_path()} finished"
    )
    return 0


def cmd_finished() -> int:
    """End the active exception immediately."""
    facts = git_facts(os.getcwd())
    target = grant_path(facts.git_common_dir)
    existed = target is not None and target.exists()
    if target is not None:
        target.unlink(missing_ok=True)  # missing_ok: tolerate a racing finished/expiry
    if existed:
        log_event("finished", "", None)
        print("worktree-gate: exception closed. Main-checkout edits gated again.")
    else:
        print("worktree-gate: no active exception.")
    return 0


def cmd_disable(args: argparse.Namespace) -> int:
    """Turn enforcement off at the chosen scope."""
    facts = git_facts(os.getcwd())
    path = _scope_config_path(args, facts)
    if path is None:
        print("worktree-gate: not in a git repo; use --user for global opt-out.")
        return 1
    _update_config(path, enforce=False)
    scope = "user" if args.user else "project"
    print(f"worktree-gate: enforcement DISABLED ({scope} scope) -> {path}")
    return 0


def cmd_enable(args: argparse.Namespace) -> int:
    """Turn enforcement back on at the chosen scope."""
    facts = git_facts(os.getcwd())
    path = _scope_config_path(args, facts)
    if path is None:
        print("worktree-gate: not in a git repo; use --user for global scope.")
        return 1
    _update_config(path, enforce=True)
    scope = "user" if args.user else "project"
    print(f"worktree-gate: enforcement ENABLED ({scope} scope) -> {path}")
    still = resolve_settings(facts).disabled_scope
    if still is not None:
        print(
            f"worktree-gate: NOTE — the {still} scope still disables enforcement and "
            f"takes precedence; re-enable that scope too to fully turn the gate on."
        )
    return 0


def cmd_set_window(args: argparse.Namespace) -> int:
    """Persist the exception window length at the chosen scope."""
    facts = git_facts(os.getcwd())
    path = _scope_config_path(args, facts)
    if path is None:
        print("worktree-gate: not in a git repo; use --user for global scope.")
        return 1
    try:
        seconds = parse_duration(args.duration)
    except ValueError as exc:
        print(f"worktree-gate: {exc}")
        return 1
    _update_config(path, window_seconds=seconds)
    scope = "user" if args.user else "project"
    print(
        f"worktree-gate: exception window set to {seconds // 60} min "
        f"({seconds}s, {scope} scope) -> {path}"
    )
    return 0


def cmd_status() -> int:
    """Print effective settings and any active exception."""
    facts = git_facts(os.getcwd())
    settings = resolve_settings(facts)
    state = "DISABLED" if settings.disabled_scope else "ENABLED"
    scope = f" ({settings.disabled_scope} scope)" if settings.disabled_scope else ""
    print(f"worktree-gate: enforcement {state}{scope}")
    print(f"  exception window: {settings.window_seconds // 60} min")
    if not facts.is_repo:
        print("  repo: not inside a git repository")
        return 0
    print(f"  repo root: {facts.repo_root}")
    print(f"  in linked worktree: {facts.in_linked_worktree}")
    expiry = read_grant_expiry(facts.git_common_dir)
    if expiry is not None and time.time() < expiry:
        until = time.strftime("%H:%M:%S", time.localtime(expiry))
        print(f"  active exception: yes (until {until})")
    else:
        print("  active exception: none")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Construct the worktree-gate argument parser."""
    parser = argparse.ArgumentParser(prog="worktree-gate")
    sub = parser.add_subparsers(dest="command", required=True)

    grant = sub.add_parser("grant", help="open a timed main-checkout exception")
    grant.add_argument("reason", nargs="*", help="why this edit must be on main")

    sub.add_parser("finished", help="end the active exception now")

    disable = sub.add_parser("disable", help="turn enforcement off")
    disable.add_argument("--user", action="store_true", help="global scope")

    enable = sub.add_parser("enable", help="turn enforcement on")
    enable.add_argument("--user", action="store_true", help="global scope")

    set_window = sub.add_parser("set-window", help="set the exception window")
    set_window.add_argument("duration", help="e.g. 900, 30s, 15m, 1h")
    set_window.add_argument("--user", action="store_true", help="global scope")

    sub.add_parser("status", help="show effective settings")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: parse arguments and dispatch to the subcommand."""
    args = build_parser().parse_args(argv)
    handlers_with_args = {
        "grant": cmd_grant,
        "disable": cmd_disable,
        "enable": cmd_enable,
        "set-window": cmd_set_window,
    }
    if args.command in handlers_with_args:
        return handlers_with_args[args.command](args)
    if args.command == "finished":
        return cmd_finished()
    return cmd_status()


if __name__ == "__main__":
    sys.exit(main())
