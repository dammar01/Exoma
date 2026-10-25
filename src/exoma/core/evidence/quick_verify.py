"""Local, cheap verification for `/.verify` when commands.verify_mode is "syntax".

Answers one narrow question: do the files this session touched still parse, and do
they reference names that exist? It deliberately does NOT run the project's test
suite — that is the whole point of the "syntax" mode.

Anything it cannot check is reported as `not_checked` / `skipped`, never as a pass:
a silent gap here would be exactly the false-safety signal this command exists to avoid.
"""
import json
import shutil
import subprocess
from pathlib import Path

from exoma.core.workspace.workspace_paths import now_iso
from exoma.utils import osutil

FILE_TIMEOUT_SECONDS = 20
# Bound parse-only checks so generated bundles cannot exhaust the verifier.
MAX_FILE_BYTES = 2 * 1024 * 1024

# Build artifacts and vendored trees: never the subject of a verification.
_IGNORED_PARTS = {"__pycache__", "node_modules", ".git", "vendor", ".venv", "venv"}


def _path_arg(rel: str) -> str:
    """A git-reported path as a checker argument that cannot be read as an option.

    File names come from `git diff --name-only`, so a file named `--eval=...` or `-r x`
    reached the checker's argv as a flag. Anchoring every path at `./` makes it a path to
    node, php and pyflakes alike; `--` would do it for some of them and not others.
    """
    return rel if rel.startswith(("./", ".\\")) else f"./{rel}"


# extension -> (language, argv builder). argv is run with cwd=project_root, no shell.
# Python is checked in-process instead: `py_compile` would drop .pyc files into the
# user's tree, and a verification command must not leave artifacts behind.
_SYNTAX_CHECKS: dict[str, tuple[str, object]] = {
    ".js": ("node", lambda rel: ["node", "--check", _path_arg(rel)]),
    ".mjs": ("node", lambda rel: ["node", "--check", _path_arg(rel)]),
    ".cjs": ("node", lambda rel: ["node", "--check", _path_arg(rel)]),
    ".php": ("php", lambda rel: ["php", "-l", _path_arg(rel)]),
}

# Syntax checkers prove the file parses; they do not catch a misspelled name.
# These optional linters do — when the toolchain happens to be present.
_NAME_CHECKS: dict[str, tuple[str, object]] = {
    ".py": ("pyflakes", lambda rel: ["pyflakes", _path_arg(rel)]),
}


def _run(argv: list[str], project_root: Path) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            argv,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=FILE_TIMEOUT_SECONDS,
            check=False,
            **osutil.hidden_run_kwargs(),  # Windows: no console flash per checker/git call
        )
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {FILE_TIMEOUT_SECONDS}s"
    except OSError as exc:
        return 127, str(exc)
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, output.strip()


def _git_lines(argv: list[str], project_root: Path) -> tuple[list[str], str | None]:
    code, output = _run(argv, project_root)
    if code != 0:
        detail = output or f"git exited {code}"
        return [], f"{' '.join(argv)}: {detail[:300]}"
    return [line.strip() for line in output.splitlines() if line.strip()], None


def _discover_changed_files(project_root: Path) -> tuple[list[str], list[str]]:
    commands = (
        ["git", "diff", "--name-only", "--"],
        ["git", "diff", "--cached", "--name-only", "--"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    )
    discovered: list[str] = []
    errors: list[str] = []
    for argv in commands:
        lines, error = _git_lines(argv, project_root)
        if error:
            errors.append(error)
        discovered.extend(lines)

    seen: list[str] = []
    for rel in discovered:
        if rel in seen:
            continue
        if _IGNORED_PARTS & set(Path(rel).parts):
            continue
        seen.append(rel)
    return seen, errors


def changed_files(project_root: Path) -> list[str]:
    """Staged, unstaged, and untracked files relative to project_root.

    Untracked files are included so newly created files are verified too.
    """
    files, _errors = _discover_changed_files(project_root)
    return files


# A change this wide is not necessarily wrong, but it is no longer the change that was
# planned, and that is worth saying out loud. The signal is "this grew past what a
# reviewer holds in their head", not a budget to spend down to.
#
# Measured, not guessed. Across 44 commits from two real projects over two weeks:
# files   median 8-17, p75 18-34, p90 32-41
# lines   median 681-824, p75 1127-1806, p90 3366-3773
# The first numbers tried here were 15 files / 600 lines, which would have fired on the
# median commit of one project and most of the other's — a warning that arrives on
# ordinary work is a warning nobody reads. These sit near p75-p90 instead, so the signal
# stays rare enough to mean something. Both are ceilings on the whole working tree.
SCOPE_WIDE_FILES = 30
SCOPE_WIDE_LINES = 2000


def changed_file_stats(project_root: Path) -> dict[str, tuple[int, int]]:
    """Per-file (added, deleted) line counts for the working tree, keyed like
    `changed_files`.

    File COUNT was already known; magnitude was not. The runtime kept only the one-line
    `--shortstat` summary for the prompt, so nothing downstream could tell a rename
    sweep across twelve files from a rewrite of one. `--numstat` is the same git call
    with the per-file rows kept.

    Untracked files count as wholly added, which is what they are. Binary files report
    `-` in numstat and land as (0, 0): they have no line count to report, and inventing
    one would put weight on the exact files whose size says least about review effort.

    Silent on failure, like `_changed_files_block`: no git, no repo, or a detached
    worktree means no measurement, never a wrong one.
    """
    stats: dict[str, tuple[int, int]] = {}
    lines, _error = _git_lines(["git", "diff", "--numstat", "HEAD", "--"], project_root)
    for row in lines:
        parts = row.split("\t")
        if len(parts) < 3:
            continue
        added, deleted, rel = parts[0], parts[1], parts[-1]
        if _IGNORED_PARTS & set(Path(rel).parts):
            continue
        stats[rel] = (
            int(added) if added.isdigit() else 0,
            int(deleted) if deleted.isdigit() else 0,
        )

    untracked, _ = _git_lines(
        ["git", "ls-files", "--others", "--exclude-standard"], project_root
    )
    for rel in untracked:
        if rel in stats or _IGNORED_PARTS & set(Path(rel).parts):
            continue
        try:
            text = (project_root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        stats[rel] = (len(text.splitlines()), 0)
    return stats


def _within_scope(rel: str, scope: list[str]) -> bool:
    """Whether `rel` is covered by any entry of a plan's `editable:` list.

    Scope entries are written by a human through the plan, so they arrive as whatever
    reads naturally — a file, a directory with or without its trailing slash, a
    backslash path on Windows. Prefix matching on a normalised form is the only
    comparison that survives all of those without asking the plan to be machine-shaped.
    """
    target = rel.replace("\\", "/").strip().lower()
    for entry in scope:
        prefix = str(entry).replace("\\", "/").strip().strip("`").lower()
        if not prefix:
            continue
        if target == prefix or target.startswith(prefix.rstrip("/") + "/"):
            return True
    return False


def scope_width(project_root: Path, editable_scope: list[str] | None) -> dict:
    """How wide the working tree has grown, and how much of it the plan claimed.

    Returned even when there is no plan scope to compare against: the file and line
    totals are the part that says a task is widening, and they are true whether or not
    anyone wrote down what the task was supposed to touch. `outside_scope` stays empty
    in that case rather than accusing every file of being unplanned.
    """
    stats = changed_file_stats(project_root)
    scope = [s for s in (editable_scope or []) if str(s).strip()]
    outside = (
        sorted(rel for rel in stats if not _within_scope(rel, scope)) if scope else []
    )
    total_lines = sum(added + deleted for added, deleted in stats.values())
    return {
        "files": len(stats),
        "lines": total_lines,
        "outside_scope": outside,
        "scope_declared": bool(scope),
        "wide": len(stats) > SCOPE_WIDE_FILES or total_lines > SCOPE_WIDE_LINES,
    }


def _check_json(project_root: Path, rel: str) -> tuple[bool, str]:
    try:
        json.loads((project_root / rel).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, str(exc)
    return True, ""


def _check_python(project_root: Path, rel: str) -> tuple[bool, str]:
    """Parse-only, in-process: no bytecode written next to the user's source."""
    try:
        source = (project_root / rel).read_text(encoding="utf-8")
    except OSError as exc:
        return False, str(exc)
    try:
        compile(source, rel, "exec")
    except SyntaxError as exc:
        return False, f"line {exc.lineno}: {exc.msg}"
    except ValueError as exc:  # e.g. source containing null bytes
        return False, str(exc)
    return True, ""


def verify_files(project_root: Path, files: list[str]) -> dict:
    passed: list[str] = []
    failed: list[dict] = []
    not_checked: list[dict] = []
    skipped: list[dict] = []
    name_findings: list[dict] = []

    for rel in files:
        suffix = Path(rel).suffix.lower()

        path = project_root / rel
        if not path.exists():
            not_checked.append({"file": rel, "reason": "deleted or missing"})
            continue
        if not path.is_file():
            not_checked.append({"file": rel, "reason": "not a regular file"})
            continue

        try:
            size = path.stat().st_size
        except OSError as exc:
            not_checked.append({"file": rel, "reason": f"unreadable: {exc}"})
            continue
        if size > MAX_FILE_BYTES:
            not_checked.append(
                {"file": rel, "reason": f"{size} bytes exceeds {MAX_FILE_BYTES}-byte check limit"}
            )
            continue

        if suffix in (".json", ".py"):
            check = _check_json if suffix == ".json" else _check_python
            ok, detail = check(project_root, rel)
            if not ok:
                failed.append({"file": rel, "check": f"{suffix.lstrip('.')} syntax", "detail": detail})
                continue
            passed.append(rel)
        else:
            entry = _SYNTAX_CHECKS.get(suffix)
            if entry is None:
                not_checked.append(
                    {"file": rel, "reason": f"no syntax checker for '{suffix or 'no extension'}'"}
                )
                continue

            language, build_argv = entry
            argv = build_argv(rel)
            if shutil.which(argv[0]) is None:
                skipped.append({"file": rel, "reason": f"{language} toolchain not on PATH"})
                continue

            code, output = _run(argv, project_root)
            if code != 0:
                failed.append({"file": rel, "check": f"{language} syntax", "detail": output[:600]})
                continue
            passed.append(rel)

        name_entry = _NAME_CHECKS.get(suffix)
        if name_entry is None:
            continue
        tool, build_name_argv = name_entry
        if shutil.which(tool) is None:
            skipped.append({"file": rel, "reason": f"{tool} name checker not on PATH"})
            continue
        code, output = _run(build_name_argv(rel), project_root)
        if code != 0 and output:
            name_findings.append({"file": rel, "tool": tool, "detail": output[:600]})

    return {
        "passed": passed,
        "failed": failed,
        "not_checked": not_checked,
        "skipped": skipped,
        "name_findings": name_findings,
        "name_check_available": shutil.which("pyflakes") is not None,
    }


def run(project_root: Path, session_id: str | None = None) -> dict:
    """Contract-shaped result, mirroring a delegated call so callers stay uniform."""
    files, discovery_errors = _discover_changed_files(project_root)
    if not files:
        verdict = "incomplete" if discovery_errors else "skipped"
        reason = (
            "git change discovery failed"
            if discovery_errors
            else "no changed, staged, or untracked files detected"
        )
        lines = ["[QUICK VERIFY]", f"verdict: {verdict}", f"reason: {reason}"]
        if discovery_errors:
            lines.append("discovery_errors:")
            lines.extend(f"- {error}" for error in discovery_errors)
        lines.append("note: verify_mode=syntax — no test suite was run")
        return {
            "ok": True,
            "content": "\n".join(lines),
            "meta": {
                "mode": "quick",
                "verdict": verdict,
                "checked_at": now_iso(),
                "project_root": str(project_root),
                "session_id": session_id,
                "discovery_errors": discovery_errors,
            },
        }

    report = verify_files(project_root, files)
    if report["failed"] or report["name_findings"]:
        verdict = "fail"
    elif report["not_checked"] or report["skipped"] or discovery_errors:
        verdict = "incomplete"
    else:
        verdict = "pass"

    lines = [
        "[QUICK VERIFY]",
        f"verdict: {verdict}",
        "mode: local syntax/name check only — verify_mode=syntax, no test suite run",
        f"files_considered: {len(files)}",
        f"passed: {len(report['passed'])}",
    ]
    if report["failed"]:
        lines.append("failed:")
        lines.extend(f"- {item['file']} [{item['check']}] {item['detail']}" for item in report["failed"])
    if report["name_findings"]:
        lines.append("name_findings:")
        lines.extend(f"- {item['file']} [{item['tool']}] {item['detail']}" for item in report["name_findings"])
    if report["not_checked"]:
        lines.append("not_checked:")
        lines.extend(f"- {item['file']} — {item['reason']}" for item in report["not_checked"])
    if report["skipped"]:
        lines.append("skipped:")
        lines.extend(f"- {item['file']} — {item['reason']}" for item in report["skipped"])
    if discovery_errors:
        lines.append("discovery_errors:")
        lines.extend(f"- {error}" for error in discovery_errors)
    if not report["name_check_available"]:
        lines.append("name_check: unavailable (pyflakes not installed) — syntax only")
    lines.append(
        "coverage: syntax/parse level. Runtime behaviour, tests and the exact user case are NOT covered."
    )

    return {
        "ok": True,
        "content": "\n".join(lines),
        "meta": {
            "mode": "quick",
            "verdict": verdict,
            "checked_at": now_iso(),
            "project_root": str(project_root),
            "session_id": session_id,
            "quick_verify": report,
            "discovery_errors": discovery_errors,
        },
    }
