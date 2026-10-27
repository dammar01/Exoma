"""Shared foundation for the installer: paths, receipt, env, and text merging.

Bottom layer — imports nothing else from installer/, so the concern modules above can
depend on it freely. Every bundle path comes from the package (`exoma/_bundle`), never
from a checkout.
"""

import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

from exoma.config.providers import PROVIDER_BUNDLES
from exoma.config.settings import BUNDLE_CONFIG_DIR, BUNDLE_DIR, BUNDLE_MANIFEST

DIST = BUNDLE_DIR
DIST_CONFIG = BUNDLE_CONFIG_DIR
MANIFEST = BUNDLE_MANIFEST
# Install target root. AGENT_HOME lets a fresh machine reproduce the environment into a
# chosen location instead of the real profile; unset falls back to the actual home.
HOME = (
    Path(os.environ["AGENT_HOME"]).expanduser()
    if os.environ.get("AGENT_HOME")
    else Path.home()
)

_SECOND_AGENT_MARKERS = ("WORKFLOW-SECOND-AGENT:START", "WORKFLOW-SECOND-AGENT:END")

# The marker strings are the workflow's, the file they sit in is the provider's — so the
# key is derived from the bundle while the pair stays literal.
# The Agy host: its instruction block in ~/.gemini/GEMINI.md and its hook registration in
# ~/.gemini/config/hooks.json (docs/reference.md, "Hosts"). Installed only where Agy keeps a
# ~/.gemini directory, so a machine without Agy gets no new directory.
AGY_HOST_INSTRUCTIONS = "agy-host/GEMINI.md"
AGY_HOST_HOOKS = "agy-host/hooks.template.json"
# The hook name Exoma owns inside hooks.json; every other name in that file is the user's.
AGY_HOST_HOOK_NAME = "exoma"

MARKERS = {
    "claude/CLAUDE.md": ("WORKFLOW-MAIN-AGENT:START", "WORKFLOW-MAIN-AGENT:END"),
    # Not WORKFLOW-MAIN-AGENT: a GEMINI.md may already hold another workflow's block, and a
    # shared marker pair would splice over it.
    AGY_HOST_INSTRUCTIONS: ("EXOMA-HOST:START", "EXOMA-HOST:END"),
    **{
        f"{name}/{bundle['instructions'][0]}": _SECOND_AGENT_MARKERS
        for name, bundle in PROVIDER_BUNDLES.items()
    },
}

# settings.json keys required by the workflow; other template values stay optional.
SETTINGS_REQUIRED = ("hooks",)


class Plan:
    """Collected actions, so a dry run can print exactly what --apply would do."""

    def __init__(self) -> None:
        self.actions: list[tuple[str, str, str]] = []
        self.warnings: list[str] = []

    def add(self, verb: str, target: Path | str, detail: str = "") -> None:
        self.actions.append((verb, str(target), detail))

    def warn(self, message: str) -> None:
        self.warnings.append(message)


# What --apply actually did, written next to the backups so --rollback does not have to
# guess. A backup directory alone cannot be undone safely: it holds the files that existed
# BEFORE, with no record of which files the install created, and those must be deleted
# rather than restored.
_RECEIPT_SCHEMA_VERSION = 2
_RECEIPT: list[dict] = []
# Where the receipt is being written, and the header that goes with it. The receipt used to
# be written once, after every step had run, so an install that died partway — a locked
# file, a full disk, Ctrl+C — left changed files with no receipt, and --rollback refuses a
# backup without one. It is now rewritten after every recorded step, marked `complete:
# false` until the final write, so whatever did change can always be undone.
_RECEIPT_TARGET: dict = {"path": None, "header": {}}


def _begin_receipt(path: Path, header: dict) -> None:
    """Start the receipt and write it at once, empty, before any destination changes.

    Written up front rather than at the first entry: a failure to write it then surfaces
    before anything was touched, not after a file had already been replaced.
    """
    _RECEIPT_TARGET["path"] = Path(path)
    _RECEIPT_TARGET["header"] = dict(header)
    _flush_receipt(allow_empty=True)


def _discard_empty_receipt() -> None:
    """An apply that changed nothing leaves no receipt, as before this was incremental."""
    path = _RECEIPT_TARGET["path"]
    if path is not None and not _RECEIPT:
        try:
            path.unlink()
        except OSError:
            pass
        try:
            path.parent.rmdir()  # only if the backup dir is empty too
        except OSError:
            pass


def _reset_receipt() -> None:
    _RECEIPT.clear()
    _RECEIPT_TARGET["path"] = None
    _RECEIPT_TARGET["header"] = {}


def _flush_receipt(complete: bool = False, allow_empty: bool = False) -> None:
    """Rewrite the receipt with every entry so far. No-op until an install begins one."""
    path = _RECEIPT_TARGET["path"]
    if path is None or (not _RECEIPT and not allow_empty):
        return
    # Lazy, like install.py and installer/check.py: the runtime is only needed once an
    # install actually writes.
    from exoma.core.workspace.workspace_paths import atomic_write_text

    atomic_write_text(
        path,
        json.dumps(
            {
                "schema_version": _RECEIPT_SCHEMA_VERSION,
                **_RECEIPT_TARGET["header"],
                "complete": complete,
                "entries": _RECEIPT,
            },
            indent=2,
        )
        + "\n",
    )


def _file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record(
    action: str,
    dest: Path,
    key: str,
    backup: Path | None,
    pre_sha256: str | None,
) -> None:
    post_sha256 = _file_sha256(dest)
    if post_sha256 is None:
        raise OSError(f"installed destination is not a file: {dest}")
    _RECEIPT.append(
        {
            "action": action,
            "key": key,
            "dest": str(dest),
            "backup": str(backup) if backup else None,
            "pre_sha256": pre_sha256,
            "post_sha256": post_sha256,
        }
    )
    _flush_receipt()


_ENV_CACHE: dict | None = None
_MISSING_ENV: set[str] = set()
_ENV_PLACEHOLDER = re.compile(r"\{\{ENV:([A-Za-z0-9_]+)\}\}")


def _load_env_file() -> dict:
    """KEY=VALUE pairs from the install .env (no python-dotenv dependency).

    Location precedence: $AGENT_ENV_FILE, else <data dir>/.env, else <HOME>/.claude/.env.
    First existing file wins; a missing file is not an error (returns {}). Values may be
    wrapped in single or double quotes. This file holds the machine's secrets and must
    NEVER be committed — only .env.example ships in the repo.
    """
    candidates: list[Path] = []
    explicit = os.environ.get("AGENT_ENV_FILE")
    if explicit:
        candidates.append(Path(explicit).expanduser())
    from exoma.config.settings import DATA_DIR

    candidates.append(DATA_DIR / ".env")
    candidates.append(HOME / ".claude" / ".env")
    for path in candidates:
        if not path.is_file():
            continue
        data: dict = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                data.setdefault(key, value)
        return data
    return {}


def _env_values() -> dict:
    """Resolution table for {{ENV:NAME}}: the real environment wins over the .env file."""
    global _ENV_CACHE
    if _ENV_CACHE is None:
        _ENV_CACHE = {**_load_env_file(), **os.environ}
    return _ENV_CACHE


def _resolve_placeholders(text: str, project_root: Path | None) -> str:
    text = text.replace("{{HOME}}", str(HOME))
    if project_root:
        text = text.replace("{{PROJECT_ROOT}}", str(project_root))
    env = _env_values()

    def _sub(match: "re.Match") -> str:
        name = match.group(1)
        if name in env:
            return env[name]
        _MISSING_ENV.add(name)  # left intact; main() aborts before any write
        return match.group(0)

    return _ENV_PLACEHOLDER.sub(_sub, text)


def _scan_missing_env() -> set[str]:
    """{{ENV:NAME}} names referenced by shipped files that don't resolve.

    A preflight so a missing secret aborts the WHOLE install before the first write —
    a half-applied second_agent config (some files env-substituted, some not) is worse
    than none. Every source that _targets/settings install would write is scanned.
    """
    env = _env_values()
    missing: set[str] = set()
    sources = [src for src, _dest, _key in _targets()]
    settings_src = DIST_CONFIG / "claude" / "settings.template.json"
    if settings_src.exists():
        sources.append(settings_src)
    for source in sources:
        try:
            text = source.read_text(encoding="utf-8")
        except OSError:
            continue
        for match in _ENV_PLACEHOLDER.finditer(text):
            if match.group(1) not in env:
                missing.add(match.group(1))
    return missing


def _resolve_in_json(value, project_root: Path | None):
    """Substitute placeholders in already-parsed JSON values.

    Doing it on the raw text instead corrupts the file on Windows: `{{HOME}}` expands to
    `C:\\Users\\name`, whose single backslashes are illegal JSON escapes. Parse first,
    substitute inside the string values, then re-serialize.
    """
    if isinstance(value, str):
        return _resolve_placeholders(value, project_root)
    if isinstance(value, dict):
        return {k: _resolve_in_json(v, project_root) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_in_json(v, project_root) for v in value]
    return value


_SECTION_RE = "<!--\\s*{name}:START\\s*-->.*?<!--\\s*{name}:END\\s*-->\\s*"


def _strip_section(text: str, name: str) -> str:
    return re.sub(_SECTION_RE.format(name=re.escape(name)), "", text, flags=re.DOTALL)


def _apply_intent_mode(text: str, only_command: bool) -> str:
    """Keep exactly one of the two mutually exclusive intent stanzas.

    Both live in the same shipped file rather than in two variants of it: two files drift,
    and the drift is invisible until an install ships a stanza nobody reviewed.
    """
    text = (
        _strip_section(text, "AUTO-INTENT")
        if only_command
        else _strip_section(text, "COMMAND-ONLY")
    )
    # The surviving stanza's own markers are scaffolding for this switch, not content —
    # leaving them in ships selector plumbing into the agent's prompt.
    return re.sub(r"[ \t]*<!--\s*(?:AUTO-INTENT|COMMAND-ONLY):(?:START|END)\s*-->\n?", "", text)


def _managed_block(text: str, start: str, end: str) -> str | None:
    """The marker-delimited region, markers included. None when absent."""
    match = re.search(
        rf"<!--\s*{re.escape(start)}.*?-->.*?<!--\s*{re.escape(end)}\s*-->",
        text,
        re.DOTALL,
    )
    return match.group(0) if match else None


class ManagedBlockError(ValueError):
    """The file's workflow markers do not pair up, so its block cannot be found safely."""


def _marker_problem(text: str, start: str, end: str) -> str | None:
    """Why the markers in `text` do not delimit exactly one block, or None when they do (or
    when there are none).

    A START left without its END — a hand edit, a merge that kept half — used to read as "no
    block": the install appended a second one below it, and every later install replaced
    only the new one while the orphan half stayed in the agent's prompt.
    """
    starts = len(re.findall(rf"<!--\s*{re.escape(start)}", text))
    ends = len(re.findall(rf"<!--\s*{re.escape(end)}\s*-->", text))
    if starts == ends == 0:
        return None
    if starts != ends:
        return f"{starts} '{start}' marker(s) against {ends} '{end}' marker(s)"
    if starts > 1:
        return f"{starts} '{start}' ... '{end}' blocks, where one is expected"
    if _managed_block(text, start, end) is None:
        return f"'{end}' comes before '{start}'"
    return None


def _merge_managed(
    existing: str, incoming: str, start: str, end: str
) -> tuple[str, str]:
    """Splice the incoming managed block into `existing`. Returns (result, how).

    Raises ManagedBlockError when `existing` has markers that do not pair up: appending a
    block beside a broken one would leave two.
    """
    block = _managed_block(incoming, start, end)
    if block is None:
        block = incoming.strip()

    problem = _marker_problem(existing, start, end)
    if problem:
        raise ManagedBlockError(problem)
    current = _managed_block(existing, start, end)
    if current is not None:
        return existing.replace(current, block), "replaced managed block"
    separator = "" if existing.endswith("\n\n") or not existing else "\n\n"
    return existing + separator + block + "\n", "appended managed block"


def _backup(
    path: Path, backup_root: Path, plan: Plan, apply: bool, key: str | None = None
) -> Path | None:
    """Copy `path` under `backup_root`, mirroring its manifest key as the sub-path.

    Keyed rather than flat: `claude/skills/plan.md` and `claude/commands/plan.md` share a
    filename, and a flat backup dir silently kept only one of them.
    """
    if not path.exists():
        return None
    target = backup_root / (key or path.name)
    plan.add("backup", target, f"from {path}")
    if apply:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    return target


def _read_text_lenient(path: Path) -> str:
    """Read a possibly non-UTF-8 dest file without crashing.

    An older install — or a PowerShell `Set-Content` in the machine's ANSI codepage — could
    have written our em-dash-heavy docs as Windows-1252, which strict UTF-8 refuses (byte
    0x97 is the cp1252 em dash, invalid as a UTF-8 start byte). Try the encodings we actually
    emit, then fall back to latin-1, which maps every byte and never raises.
    """
    data = path.read_bytes()
    text: str | None = None
    for enc in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = data.decode("latin-1")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def agy_host_present() -> bool:
    return (HOME / ".gemini").is_dir()


def _agy_hook_value(value, python: str, home: str):
    """The template with {{PYTHON}} and {{HOME}} filled in, string by string.

    Filled after parsing, not as text: a Windows home holds backslashes, which a text
    substitution would leave as invalid JSON escapes.
    """
    if isinstance(value, dict):
        return {k: _agy_hook_value(v, python, home) for k, v in value.items()}
    if isinstance(value, list):
        return [_agy_hook_value(v, python, home) for v in value]
    if isinstance(value, str):
        return value.replace("{{PYTHON}}", python).replace("{{HOME}}", home)
    return value


def agy_hook_entry(src: Path) -> dict:
    """The `exoma` hook definition this install writes into ~/.gemini/config/hooks.json."""
    template = json.loads(src.read_text(encoding="utf-8"))
    return _agy_hook_value(
        template[AGY_HOST_HOOK_NAME], Path(sys.executable).as_posix(), HOME.as_posix()
    )


# A space splits the unquoted command; the rest are cmd.exe operators or expansions.
_AGY_UNSAFE_PATH_CHARS = ' &%^|<>()"'


def _install_agy_hooks(
    src: Path, dest: Path, key: str, plan: Plan, apply: bool, backup_root: Path
) -> None:
    """Merge Exoma's hook definition into Agy's hooks.json, leaving every other name alone.

    Agy runs hook commands through cmd.exe on Windows and passes quotes through literally,
    so a command cannot quote its paths: an interpreter or home path holding a space or a
    cmd.exe metacharacter is refused here rather than installed as a hook that fails every
    tool call. A hooks.json that is not a JSON object is refused too: it is the user's
    file, and replacing it with one holding only `exoma` would discard what was there.
    """
    entry = agy_hook_entry(src)
    paths = (Path(sys.executable).as_posix(), HOME.as_posix())
    if any(ch in path for path in paths for ch in _AGY_UNSAFE_PATH_CHARS):
        plan.add("refused", dest, "a path an Agy hook command cannot carry unquoted")
        plan.warn(
            f"{dest}: the Agy host hooks were not installed — the interpreter ({paths[0]}) or "
            f"the home ({paths[1]}) holds a space or one of {_AGY_UNSAFE_PATH_CHARS.strip()}, "
            "and Agy runs hook commands unquoted. Install from an interpreter on a plain path."
        )
        return
    exists = dest.exists()
    try:
        current = json.loads(_read_text_lenient(dest)) if exists else {}
    except ValueError:
        plan.add("refused", dest, "not valid JSON")
        plan.warn(f"{dest}: refused — not valid JSON; fix or remove it, then install again")
        return
    if not isinstance(current, dict):
        plan.add("refused", dest, "not a JSON object")
        plan.warn(f"{dest}: refused — its root is not a JSON object; fix or remove it, then install again")
        return
    if current.get(AGY_HOST_HOOK_NAME) == entry:
        plan.add("unchanged", dest)
        return
    merged = {**current, AGY_HOST_HOOK_NAME: entry}
    pre_sha256 = _file_sha256(dest)
    saved = _backup(dest, backup_root, plan, apply, key) if exists else None
    plan.add("merge" if exists else "create", dest, f"hook `{AGY_HOST_HOOK_NAME}`")
    if apply:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
        _record("merge" if exists else "create", dest, key, saved, pre_sha256)


def _install_text(
    src: Path,
    dest: Path,
    key: str,
    plan: Plan,
    apply: bool,
    backup_root: Path,
    project_root: Path | None,
    only_command: bool = False,
) -> None:
    if key == AGY_HOST_HOOKS:
        _install_agy_hooks(src, dest, key, plan, apply, backup_root)
        return
    incoming = _resolve_placeholders(src.read_text(encoding="utf-8"), project_root)
    if key == "claude/CLAUDE.md":
        incoming = _apply_intent_mode(incoming, only_command)

    if key in MARKERS:
        # A first install writes the managed block alone, like every later one splices it:
        # text outside the markers was written once, never refreshed, and never removable
        # by --uninstall, because nothing could tell it from the user's own.
        start, end = MARKERS[key]
        exists = dest.exists()
        existing = _read_text_lenient(dest) if exists else ""
        try:
            merged, how = _merge_managed(existing, incoming, start, end)
        except ManagedBlockError as exc:
            message = (
                f"{dest}: refused — its workflow markers do not pair up ({exc}). "
                f"Fix the <!-- {start} --> / <!-- {end} --> lines by hand (keep one pair, or "
                "delete both with the text between them), then install again"
            )
            plan.add("refused", dest, "unpaired workflow markers")
            plan.warn(message)
            return
        if exists and merged == existing:
            plan.add("unchanged", dest)
            return
        pre_sha256 = _file_sha256(dest)
        saved = _backup(dest, backup_root, plan, apply, key) if exists else None
        plan.add("merge" if exists else "create", dest, how if exists else "managed block only")
        if apply:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(merged, encoding="utf-8")
            _record("merge" if exists else "create", dest, key, saved, pre_sha256)
        return

    if dest.exists() and _read_text_lenient(dest) == incoming:
        plan.add("unchanged", dest)
        return

    pre_sha256 = _file_sha256(dest)
    if dest.exists():
        saved = _backup(dest, backup_root, plan, apply, key)
        plan.add("replace", dest)
    else:
        saved = None
        plan.add("create", dest)
    if apply:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(incoming, encoding="utf-8")
        _record("replace" if saved else "create", dest, key, saved, pre_sha256)



def _targets() -> list[tuple[Path, Path, str]]:
    """(source, destination, manifest-key) for everything in dist/config.

    Every entry here is global. The only project-scoped file the workflow ships is
    <project_root>/opencode.json, and that one is installed by init/upgrade, not here.
    """
    mapping = [
        (
            DIST_CONFIG / "claude" / "CLAUDE.md",
            HOME / ".claude" / "CLAUDE.md",
            "claude/CLAUDE.md",
        ),
    ]
    dir_families = [
        ("claude", "skills", HOME / ".claude" / "skills"),
        ("claude", "commands", HOME / ".claude" / "commands"),
        ("claude", "hooks", HOME / ".claude" / "hooks"),
    ]
    # Per-provider bundles come from config/providers.py rather than being spelled out
    # here, so a new provider is a dist/ folder plus one declaration.
    for name, bundle in PROVIDER_BUNDLES.items():
        provider_home = HOME.joinpath(*bundle["home_dir"].split("/"))
        dist_name, dest_name = bundle["instructions"]
        mapping.append(
            (
                DIST_CONFIG / name / dist_name,
                provider_home / dest_name,
                f"{name}/{dist_name}",
            )
        )
        # The file stem becomes the custom agent's name. One global roster: the subagents
        # belong to the workflow, so every project it manages reads the same set instead
        # of each worktree carrying its own copy to keep in sync.
        if bundle.get("agents_dir"):
            dir_families.append(
                (name, bundle["agents_dir"], provider_home / bundle["agents_dir"])
            )
    # The Agy host, where Agy is installed: its hooks run from ~/.claude/hooks (agy-host.py
    # beside workflow_policy.py), so only the instruction block and the registration land
    # under ~/.gemini.
    if agy_host_present():
        mapping.append((DIST_CONFIG / AGY_HOST_INSTRUCTIONS, HOME / ".gemini" / "GEMINI.md", AGY_HOST_INSTRUCTIONS))
        mapping.append((DIST_CONFIG / AGY_HOST_HOOKS, HOME / ".gemini" / "config" / "hooks.json", AGY_HOST_HOOKS))
    # One set of files for every OS: hooks are bash launchers (Git Bash on Windows) beside
    # the .py they run.
    for family, sub, dest_dir in dir_families:
        source_dir = DIST_CONFIG / family / sub
        if source_dir.is_dir():
            for path in sorted(source_dir.iterdir()):
                if path.is_file():
                    mapping.append(
                        (path, dest_dir / path.name, f"{family}/{sub}/{path.name}")
                    )
    return [(s, d, k) for s, d, k in mapping if s.exists()]


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
