"""The project's own E2E tests, run only through a command the user wrote (plan §8, §18).

`[E2E SPEC]` may name existing tests that cover a claim. This runtime never guesses how
to run them: `config.json e2e.existing_test_command` is an argv template, executed
without a shell, where `{files}` expands to the selected test files and `{base_url}` to the
run's base URL. It is config-only (`request.CONFIG_ONLY_SETTINGS`): the command runs with
the user's environment, so it comes from the file the user edits, never from a request.
A listed file runs only when it is a plain project-relative path, exists, and matches
`config.json e2e.existing_test_allowlist`. With no command or no
allowlist match nothing runs, and a claim covered only by such a test has to be asserted
by the scenario instead.

Existing tests are not trusted blindly: a pass proves the claims the spec says the test
covers, a failure makes those claims `unknown` — a red project test can be the app or
the environment, and this runtime cannot tell which.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from exoma.core.evidence.e2e import redact as e2e_redact
from exoma.utils.osutil import hidden_run_kwargs, terminate_tree

# Plain paths only: the file names end up as argv, and on Windows a `.cmd` runner would
# re-parse them. Nothing a test path legitimately needs is outside this set.
_PLAIN_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./@+-]*$")
OUTPUT_TAIL_LINES = 200
OUTPUT_TAIL_CHARS = 4000


def select(existing_tests: object, config: dict, project_root: Path) -> tuple[list[dict], list[dict]]:
    """Split listed tests into (runnable, skipped); skipped entries say why in `result`."""
    command = config.get("existing_test_command") or []
    allowlist = [str(pattern) for pattern in config.get("existing_test_allowlist") or []]
    root = Path(project_root).resolve()
    runnable: list[dict] = []
    skipped: list[dict] = []
    for test in existing_tests if isinstance(existing_tests, list) else []:
        if not isinstance(test, dict):
            continue
        path = str(test.get("path") or "")
        entry = {"path": path, "covers": [str(cid) for cid in test.get("covers") or []]}
        if not command:
            reason = "config.json e2e.existing_test_command is not set"
        elif not _PLAIN_PATH.match(path) or ".." in PurePosixPath(path).parts:
            reason = "path is not a plain project-relative path"
        elif not any(fnmatchcase(path, pattern) for pattern in allowlist):
            reason = "not in config.json e2e.existing_test_allowlist"
        elif not (root / path).resolve().is_file():
            reason = "file does not exist"
        else:
            runnable.append(entry)
            continue
        skipped.append({**entry, "result": f"not run ({reason})"})
    return runnable, skipped


def build_argv(command: list, files: list[str], base_url: str) -> list[str]:
    """Expand the template. Without a `{files}` token the files are appended."""
    argv: list[str] = []
    expanded = False
    for part in command:
        part = str(part)
        if part == "{files}":
            argv.extend(files)
            expanded = True
        else:
            argv.append(part.replace("{base_url}", base_url))
    if not expanded:
        argv.extend(files)
    return argv


def run(
    runnable: list[dict],
    config: dict,
    project_root: Path,
    env_values: dict[str, str],
    extra_env: dict[str, str] | None = None,
) -> dict:
    """One invocation for every runnable test, bounded by `existing_test_timeout_s`.

    Returns {"status": passed|failed|timeout|launch_failed, "returncode", "duration_seconds",
    "argv", "files", "covers", "output_tail", "redactions"}. The output tail is scrubbed of
    resolved `${ENV}` values and secret-shaped strings before it leaves this function.
    """
    files = [test["path"] for test in runnable]
    covers = sorted({cid for test in runnable for cid in test["covers"]})
    argv = build_argv(config.get("existing_test_command") or [], files, str(config.get("base_url") or ""))
    base = {"argv": argv, "files": files, "covers": covers, "returncode": None, "duration_seconds": 0.0, "output_tail": "", "redactions": []}
    executable = shutil.which(argv[0]) if argv else None
    if not executable:
        return {**base, "status": "launch_failed", "output_tail": f"{argv[0] if argv else '(empty command)'} was not found"}
    timeout_s = float(config.get("existing_test_timeout_s") or 300)
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            [executable, *argv[1:]],
            cwd=str(project_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            # the user's own command runs with the user's own environment, plus the selected
            # secrets.json profile
            env={**os.environ, **(extra_env or {})},
            **hidden_run_kwargs(),
        )
    except (OSError, ValueError) as exc:
        return {**base, "status": "launch_failed", "output_tail": f"{type(exc).__name__}: {exc}"}

    tail: deque[str] = deque(maxlen=OUTPUT_TAIL_LINES)
    reader = threading.Thread(target=lambda: tail.extend(proc.stdout), daemon=True)
    reader.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_tree(proc)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    reader.join(timeout=5)
    clean, hits = e2e_redact.redact_text(e2e_redact.scrub_resolved("".join(tail)[-OUTPUT_TAIL_CHARS:], env_values))
    if timed_out:
        status = "timeout"
    else:
        status = "passed" if proc.returncode == 0 else "failed"
    return {
        **base,
        "status": status,
        "returncode": proc.returncode,
        "duration_seconds": round(time.monotonic() - started, 3),
        "output_tail": clean,
        "redactions": hits,
    }
