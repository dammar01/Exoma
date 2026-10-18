"""Which provider CLI release a call ran on, and whether this workflow version was tested on it.

Each provider bundle in config/providers.py names the release it was tested against
(`stable_version`). A delegated call and doctor read the installed release with
`<cli> --version` and compare: `stable` when it matches, `untested` when it does not,
`unreadable` when the CLI did not answer. Advisory only: an untested release still runs,
because refusing would block a user on a release that may work; the status is what a failure
report has to name, and the stable release is the one to pin back to (CASE-015).

Reading costs a process spawn on the delegated path (measured: about 1.2 s for opencode's
node shim, 0.3 s for codex), so a reading is also kept on disk across runs, keyed by the
resolved executable's path, mtime and size: an upgrade that rewrites the file is a new key.
A shim an upgrade leaves untouched would keep an old reading, so an entry also expires after
`_CACHE_MAX_AGE_SECONDS`. A CLI that exits without a readable version is cached too (as
null), so it is not asked again on every run; a timeout or a failed spawn is not. Fail-open
throughout: a cache that cannot be read or written only means the version is read again.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from config.providers import provider_stable_version
from core.workspace.workspace_paths import atomic_write_text
from utils import osutil

_CACHE: dict[str, str | None] = {}
_SEMVER = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?")
_CACHE_MAX_AGE_SECONDS = 24 * 3600
# Overridable for tests; None resolves the per-user location below.
CACHE_PATH: Path | None = None


def _cache_path() -> Path | None:
    """`provider-versions.json` in the user's cache directory, or None when there is none.

    Per user rather than per project: the key is an executable on this machine, which every
    project on it shares.
    """
    if CACHE_PATH is not None:
        return Path(CACHE_PATH)
    try:
        root = osutil.user_cache_dir()
        return root / "agent-workflow" / "provider-versions.json" if root else None
    except Exception:
        return None


def _cache_key(exe: str) -> str | None:
    """`path|mtime_ns|size` of the resolved executable; None when it cannot be stat'ed (a
    bare name not on PATH), which is never cached on disk."""
    try:
        stat = os.stat(exe)
    except OSError:
        return None
    return f"{os.path.normcase(os.path.abspath(exe))}|{stat.st_mtime_ns}|{stat.st_size}"


def _load_disk() -> dict:
    path = _cache_path()
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _disk_lookup(key: str) -> tuple[bool, str | None]:
    entry = _load_disk().get(key)
    if not isinstance(entry, dict):
        return False, None
    read_at = entry.get("read_at")
    version = entry.get("version")
    if not isinstance(read_at, (int, float)) or time.time() - read_at > _CACHE_MAX_AGE_SECONDS:
        return False, None
    if version is not None and not isinstance(version, str):
        return False, None
    return True, version


def _disk_store(key: str, version: str | None) -> None:
    path = _cache_path()
    if path is None:
        return
    try:
        data = _load_disk()
        # Entries for an executable's earlier mtime/size are dead weight once it changed.
        prefix = key.rsplit("|", 2)[0] + "|"
        data = {k: v for k, v in data.items() if not k.startswith(prefix)}
        data[key] = {"version": version, "read_at": time.time()}
        atomic_write_text(path, json.dumps(data, indent=1))
    except Exception:
        pass


def _parse(stdout: str, stderr: str) -> str | None:
    """The first line carrying a semantic version, stdout first and then stderr (some CLIs
    print a banner or a warning before it, or write it to stderr); else None. A banner with
    no version in it is not the CLI's version, and a usage row would carry it as one."""
    for stream in (stdout, stderr):
        for line in (stream or "").splitlines():
            if _SEMVER.search(line):
                return line.strip()[:80] or None
    return None


def read_version(command: str) -> str | None:
    """The version line of `<command> --version`; None on failure.

    Read once per binary per process, and across processes through the on-disk cache (see
    the module docstring). Bounded and tree-killed: on Windows these CLIs are `.cmd` shims
    over node, and a plain run(timeout=) waits unbounded on pipes the node child still holds.
    """
    exe = osutil.resolve_exe(command)
    if exe in _CACHE:
        return _CACHE[exe]
    key = None
    try:
        key = _cache_key(exe)
        if key is not None:
            hit, version = _disk_lookup(key)
            if hit:
                _CACHE[exe] = version
                return version
    except Exception:
        key = None
    version = None
    answered = False
    try:
        code, stdout, stderr = osutil.run_bounded([exe, "--version"], 15)
        answered = code is not None
        if code == 0:
            version = _parse(stdout, stderr)
    except Exception:
        version = None
    _CACHE[exe] = version
    # Only a CLI that ran to an exit is remembered across runs. A timeout or a failed spawn
    # says nothing about the binary, and persisting it would read `unreadable` for a day.
    if key is not None and answered:
        _disk_store(key, version)
    return version


def forget(command: str) -> None:
    """Drop every reading of `command`'s executable, in this process and on disk.

    For a caller that just changed the release (install.py --update-providers): a `.cmd`
    shim can keep its path, mtime and size across an npm update, and its old reading would
    otherwise answer for `_CACHE_MAX_AGE_SECONDS`.
    """
    exe = osutil.resolve_exe(command)
    same = os.path.normcase(os.path.abspath(exe))
    for key in [k for k in _CACHE if os.path.normcase(os.path.abspath(k)) == same]:
        _CACHE.pop(key, None)
    path = _cache_path()
    if path is None:
        return
    try:
        prefix = same + "|"
        data = _load_disk()
        kept = {k: v for k, v in data.items() if not k.startswith(prefix)}
        if len(kept) != len(data):
            atomic_write_text(path, json.dumps(kept, indent=1))
    except Exception:
        pass


def release(version: str | None) -> str | None:
    """The semantic version inside a `--version` line (`codex-cli 0.160.0` -> `0.160.0`)."""
    match = _SEMVER.search(version or "")
    return match.group(0) if match else None


def status(provider: str, version: str | None) -> str:
    """`stable`, `untested`, or `unreadable` for `version` of `provider`'s CLI."""
    found = release(version)
    if found is None:
        return "unreadable"
    stable = provider_stable_version(provider)
    return "stable" if stable and found == stable else "untested"


def describe(provider: str, command: str) -> dict:
    """What doctor and a call record: the version line, its status, and the stable release."""
    version = read_version(command)
    return {
        "provider": provider,
        "version": version,
        "status": status(provider, version),
        "stable": provider_stable_version(provider),
    }
