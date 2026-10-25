"""`install.py --update-providers`: bring each provider CLI to the release it was tested on.

Opt-in, and only ever to `stable_version` (config/providers.py), never "latest": provider
releases have broken delegated calls before (CASE-015, CASE-016), and the stable release is
the one this workflow version was run against. DEC-057 revises DEC-028, which had the
installer touch no provider at all, for exactly this flag.

A provider is updated only when every one of these holds; otherwise it is reported and left
alone:
  1. its bundle declares how (`update`); agy declares none, its installer is undocumented,
  2. its CLI is on PATH; installing a provider that is not there is not an update,
  3. it is not already on the stable release,
  4. the CLI on PATH is the one npm installed globally (its shim sits in npm's global bin,
     or it resolves into npm's global node_modules) and that package directory exists;
     a Homebrew, standalone or hand-built CLI belongs to whoever installed it,
  5. npm's registry has the stable release of that package.
No `sudo`, no fallback release. An update is not atomic and npm has no undo, so each one is
verified by reading `--version` again, and the journal written beside the install backups
names the command that pins back to the release that was there before.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from exoma.config.providers import (
    bundled_providers,
    provider_command_default,
    provider_stable_version,
    provider_update,
)
from exoma.core.provider import versions
from exoma.utils import osutil

_NPM_TIMEOUT_SECONDS = 60
_INSTALL_TIMEOUT_SECONDS = 600


def _run(args: list[str], timeout: float) -> tuple[int | None, str, str]:
    return osutil.run_bounded(args, timeout)


def _npm_global(npm: str, run) -> tuple[Path, list[Path]] | None:
    """npm's global node_modules and the directories its global shims go to, or None."""
    code, stdout, _ = run([npm, "root", "-g"], _NPM_TIMEOUT_SECONDS)
    root = (stdout or "").strip().splitlines()[-1:] if code == 0 else []
    if not root:
        return None
    modules = Path(root[0])
    # Windows: <prefix>\node_modules, shims in <prefix>. POSIX: <prefix>/lib/node_modules,
    # shims in <prefix>/bin. Only the one for this OS: <prefix>/lib is no bin on POSIX.
    return modules, [modules.parent if osutil.IS_WINDOWS else modules.parent.parent / "bin"]


def _owned_by_npm(exe: str, package: str, npm_global: tuple[Path, list[Path]]) -> bool:
    modules, bins = npm_global
    if not (modules / package).is_dir():
        return False
    path = Path(exe)
    real = Path(os.path.realpath(exe))

    def under(child: Path, parent: Path) -> bool:
        try:
            child.resolve().relative_to(parent.resolve())
            return True
        except (OSError, ValueError):
            return False

    return under(real, modules) or any(
        os.path.normcase(str(path.parent.resolve())) == os.path.normcase(str(b.resolve()))
        for b in bins
        if b.exists()
    )


def plan_updates(run=_run, getenv=os.getenv) -> list[dict]:
    """One entry per provider: what --apply would do and why. Runs nothing that changes
    anything: `--version`, `npm root -g` and `npm view` only."""
    npm = osutil.resolve_exe("npm")
    npm_found = bool(osutil.provider_callable("npm")[0])
    npm_global = None
    entries = []
    for provider in bundled_providers():
        command = provider_command_default(provider, getenv)
        stable = provider_stable_version(provider)
        update = provider_update(provider)
        entry = {
            "provider": provider,
            "command": command,
            "stable": stable,
            "installed": None,
            "action": "skip",
            "reason": "",
            "argv": None,
            "pin_back": None,
        }
        entries.append(entry)
        if not update or update.get("method") != "npm" or not stable:
            entry.update(action="unsupported", reason="no documented installer to pin it with; pin it by hand")
            continue
        on_path, exe = osutil.provider_callable(command)
        if not on_path:
            entry["reason"] = "not on PATH; --update-providers updates, it does not install"
            continue
        # A fresh reading: the cache keeps one for a day, and an update made outside the
        # workflow behind an unchanged shim would plan from the release before it. Fresh
        # also writes no cache, so planning stays the read-only step its docstring says.
        installed = versions.release(versions.read_version(command, fresh=True))
        entry["installed"] = installed
        if installed == stable:
            entry.update(action="current", reason=f"already {stable}")
            continue
        package = update["package"]
        if not npm_found:
            entry["reason"] = "npm not on PATH"
            continue
        if npm_global is None:
            npm_global = _npm_global(npm, run) or False
        if not npm_global or not _owned_by_npm(exe, package, npm_global):
            entry["reason"] = f"{exe} is not npm's global {package}; it belongs to whoever installed it"
            continue
        code, stdout, _ = run([npm, "view", f"{package}@{stable}", "version"], _NPM_TIMEOUT_SECONDS)
        if code != 0 or (stdout or "").strip().splitlines()[-1:] != [stable]:
            entry["reason"] = f"npm registry has no {package}@{stable} (offline, or no such release)"
            continue
        entry.update(
            action="update",
            reason=f"{installed or 'unreadable'} -> {stable}",
            argv=[npm, "install", "-g", f"{package}@{stable}"],
            pin_back=f"npm install -g {package}@{installed}" if installed else None,
        )
    return entries


def apply_updates(entries: list[dict], run=_run) -> list[dict]:
    """Run each planned update in turn and verify it; every entry gets `result`.

    One provider failing does not stop the next: they are separate tools, and a partial
    result is reported per provider rather than rolled back (npm has no undo).
    """
    from exoma.core.provider import model_catalog

    for entry in entries:
        if entry["action"] != "update":
            continue
        code, _stdout, stderr = run(entry["argv"], _INSTALL_TIMEOUT_SECONDS)
        entry["returncode"] = code
        versions.forget(entry["command"])
        model_catalog.forget(entry["provider"])
        after = versions.release(versions.read_version(entry["command"]))
        entry["after"] = after
        if code == 0 and after == entry["stable"]:
            entry["result"] = "updated"
        else:
            tail = (stderr or "").strip().splitlines()[-1:] or ["timed out" if code is None else ""]
            entry["result"] = "failed"
            entry["error"] = tail[0][:200]
    return entries


def write_journal(entries: list[dict], backup_root: Path) -> Path:
    """What --update-providers did, beside the install backups. Not a receipt: --rollback
    restores files, and a provider release is not one; the journal names the pin-back."""
    path = backup_root / "provider_update.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"updated_at": datetime.now(timezone.utc).isoformat(), "providers": entries},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path
