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

from exoma.config.bundle_keys import host_dir, provider_dir
from exoma.config.providers import PROVIDER_BUNDLES
from exoma.config.settings import BUNDLE_CONFIG_DIR, BUNDLE_DIR, BUNDLE_MANIFEST
from exoma.utils.osutil import agent_home

DIST = BUNDLE_DIR
DIST_CONFIG = BUNDLE_CONFIG_DIR
MANIFEST = BUNDLE_MANIFEST
# Install target root. AGENT_HOME lets a fresh machine reproduce the environment into a
# chosen location instead of the real profile; unset falls back to the actual home.
HOME = agent_home()

_SECOND_AGENT_MARKERS = ("WORKFLOW-SECOND-AGENT:START", "WORKFLOW-SECOND-AGENT:END")

# The main-agent hosts an install can target, chosen with --host (docs/reference.md,
# "Installer details"). Second-agent provider bundles are not a host choice: they install
# whichever host is picked.
HOSTS = ("claude", "agy")

# Bundle folders and keys (config/bundle_keys.py): hosts/<name>/ for a host, providers/<name>/
# for a second-agent provider.
CLAUDE_HOST = host_dir("claude")
AGY_HOST = host_dir("agy")
CLAUDE_INSTRUCTIONS = f"{CLAUDE_HOST}/CLAUDE.md"
CLAUDE_SETTINGS = f"{CLAUDE_HOST}/settings.template.json"
CLAUDE_HOOKS_PREFIX = f"{CLAUDE_HOST}/hooks/"

# The marker strings are the workflow's, the file they sit in is the provider's — so the
# key is derived from the bundle while the pair stays literal.
# The Agy host (docs/reference.md, "Hosts"): its instruction block in ~/.gemini/GEMINI.md,
# its hook registration in ~/.gemini/config/hooks.json, and everything those point at under
# Agy's own directories — no Agy file reads ~/.claude.
AGY_HOST_INSTRUCTIONS = "hosts/agy/GEMINI.md"
AGY_HOST_HOOKS = "hosts/agy/hooks.template.json"
AGY_HOST_PERMISSIONS = "hosts/agy/permissions.template.json"
# The hook name Exoma owns inside hooks.json; every other name in that file is the user's.
AGY_HOST_HOOK_NAME = "exoma"
# Keys of the files installed for Agy but rendered from a Claude source, so one source feeds
# both hosts and the two cannot drift: the contract, the hook scripts, and each skill as an
# Agy skill (`hosts/agy/skills/<name>/SKILL.md`, installed as `skills/.<name>/` so it answers
# to `/.<name>` there too).
AGY_CONTRACT = "hosts/agy/exoma/CONTRACT.md"
AGY_HOOKS_PREFIX = "hosts/agy/exoma/hooks/"
AGY_SKILLS_PREFIX = "hosts/agy/skills/"
AGY_DOCTOR_SKILL = f"{AGY_SKILLS_PREFIX}doctor/SKILL.md"
AGY_DOCTOR_FRAGMENT = "hosts/agy/doctor.agy.md"
# What agy-host.py needs beside it: the policy it imports and the intent map it loads.
AGY_HOOK_FILES = ("agy-host.py", "workflow_policy.py", "intent-map.json")

MARKERS = {
    CLAUDE_INSTRUCTIONS: ("WORKFLOW-MAIN-AGENT:START", "WORKFLOW-MAIN-AGENT:END"),
    # Not WORKFLOW-MAIN-AGENT: a GEMINI.md may already hold another workflow's block, and a
    # shared marker pair would splice over it.
    AGY_HOST_INSTRUCTIONS: ("EXOMA-HOST:START", "EXOMA-HOST:END"),
    # A provider whose instructions are copied whole (Agy's custom agent) has no block.
    **{
        f"{provider_dir(name)}/{bundle['instructions'][0]}": _SECOND_AGENT_MARKERS
        for name, bundle in PROVIDER_BUNDLES.items()
        if not bundle.get("instructions_whole")
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
    _AGY_SNAPSHOT.clear()
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
    settings_src = DIST_CONFIG / CLAUDE_SETTINGS
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
    """Whether this machine keeps an Agy directory. Only a hint for the host prompt and the
    0.3.0 migration: which hosts get installed is the user's choice, not this."""
    return (HOME / ".gemini").is_dir()


def agy_cli_dir() -> Path:
    return HOME / ".gemini" / "antigravity-cli"


def agy_exoma_dir() -> Path:
    """Exoma's own files for the Agy host: the contract and the hook scripts."""
    return agy_cli_dir() / "exoma"


def agy_settings_dest() -> Path:
    """Agy's settings.json, where the opt-in permission rules are merged."""
    return agy_cli_dir() / "settings.json"


def bundle_key(source: Path) -> str:
    """The manifest key of a shipped source. A target rendered for Agy from a Claude source
    carries its own key (its backup path, its ledger family) but is stamped as that source."""
    return source.relative_to(DIST_CONFIG).as_posix()


def runner_prefix() -> str:
    """How an Agy session starts bash for the runner. Agy runs commands through PowerShell
    on Windows, where a bare `bash` can be WSL's (it cannot read E:/ paths) and a quoted
    path is only run with `&`; elsewhere the quoted path runs as it is."""
    from exoma.utils import osutil

    found = osutil.find_bash()
    if not found:
        return "bash"
    path = Path(found).as_posix()
    return f"& '{path}'" if osutil.IS_WINDOWS else f"'{path}'"


# `bash '<anything>/.workflow/run.sh'` as the Claude text writes the runner call.
_BARE_RUNNER = re.compile(r"\bbash (?='[^'\n]*\.workflow/(?:run|check|inspect)\.sh')")


def _skill_md(text: str, name: str) -> str:
    """A Claude skill as an Agy SKILL.md: YAML frontmatter Agy registers it by, then the
    skill unchanged. The description is the skill's own `description:` line."""
    description = ""
    for line in text.splitlines()[:6]:
        if line.startswith("description:"):
            description = line.partition(":")[2].strip()
            break
    return f"---\nname: .{name}\ndescription: {json.dumps(description or name, ensure_ascii=False)}\n---\n\n{text}"


def _agy_text(text: str, key: str) -> str:
    """Claude-host text made fit for Agy: the runner call through runner_prefix(), the
    contract as its managed block alone, a skill with Agy's frontmatter."""
    prefix = runner_prefix()
    text = text.replace("{{RUNNER}}", prefix).replace("{{AGY_EXOMA}}", agy_exoma_dir().as_posix())
    if key == AGY_CONTRACT:
        text = _managed_block(text, *MARKERS[CLAUDE_INSTRUCTIONS]) or text
    if key == AGY_CONTRACT or key.startswith(AGY_SKILLS_PREFIX):
        text = _BARE_RUNNER.sub(prefix + " ", text)
        # Where a skill is read: Agy's own skill, not the Claude copy.
        text = text.replace("~/.claude/skills/<name>.md", "~/.gemini/antigravity-cli/skills/.<name>/SKILL.md")
    if key == AGY_DOCTOR_SKILL:
        # The Agy /.doctor adds what only an Agy session can observe (skills, hooks, the
        # runner's permission and sandbox), so the Claude /.doctor never carries it.
        fragment = (DIST_CONFIG / AGY_DOCTOR_FRAGMENT).read_text(encoding="utf-8")
        text = text.rstrip("\n") + "\n" + fragment.replace("{{RUNNER}}", prefix)
    if key.startswith(AGY_SKILLS_PREFIX) and not text.startswith("---"):
        text = _skill_md(text, key[len(AGY_SKILLS_PREFIX):].split("/", 1)[0])
    return text


def _render_text(src: Path, key: str, project_root: Path | None, only_command: bool = False) -> str:
    """The text an install writes for `key` from `src`. One function for the install and
    for --check, so the check compares against exactly what an install would write."""
    text = _resolve_placeholders(src.read_text(encoding="utf-8"), project_root)
    if key in (CLAUDE_INSTRUCTIONS, AGY_CONTRACT):
        text = _apply_intent_mode(text, only_command)
    if key.startswith(AGY_HOST + "/"):
        text = _agy_text(text, key)
    return text


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


def agy_hooks_dest() -> Path:
    return HOME / ".gemini" / "config" / "hooks.json"


def agy_hooks_state(dest: Path) -> tuple[dict | None, tuple[str, str] | None]:
    """(the parsed hooks.json, None) when Exoma's hook can go into it — `{}` for a file not
    there yet — else (None, (plan detail, warning)).

    One read serves the decision and the merge after it: a second read could find the file
    changed in between and raise where the first one refused cleanly.

    Agy runs hook commands through cmd.exe on Windows and passes quotes through literally,
    so a command cannot quote its paths: an interpreter or home path holding a space or a
    cmd.exe metacharacter is refused rather than installed as a hook that fails every tool
    call. A hooks.json that is not a JSON object is refused too: it is the user's file, and
    replacing it with one holding only `exoma` would discard what was there. Asked before
    either Agy file is written, so GEMINI.md never lands without the hook that enforces it.
    """
    paths = (Path(sys.executable).as_posix(), HOME.as_posix())
    if any(ch in path for path in paths for ch in _AGY_UNSAFE_PATH_CHARS):
        return None, (
            "a path an Agy hook command cannot carry unquoted",
            f"the Agy host hooks were not installed — the interpreter ({paths[0]}) or "
            f"the home ({paths[1]}) holds a space or one of {_AGY_UNSAFE_PATH_CHARS.strip()}, "
            "and Agy runs hook commands unquoted. Install from an interpreter on a plain path.",
        )
    if not dest.exists():
        return {}, None
    try:
        current = json.loads(_read_text_lenient(dest))
    except ValueError:
        return None, ("not valid JSON", "refused — not valid JSON; fix or remove it, then install again")
    except OSError as exc:
        # Permission denied, or a directory under that name: a refusal to report, not a
        # traceback out of --check.
        return None, ("not readable", f"refused — it cannot be read ({exc.__class__.__name__}); fix it, then install again")
    if not isinstance(current, dict):
        return None, (
            "not a JSON object",
            "refused — its root is not a JSON object; fix or remove it, then install again",
        )
    return current, None


def agy_hooks_refusal(dest: Path) -> tuple[str, str] | None:
    """(plan detail, warning) when Exoma's hook cannot go into Agy's hooks.json, else None."""
    return agy_hooks_state(dest)[1]


# The hooks.json state the GEMINI.md decision was made on, handed to the hook merge that
# follows it in the same install (_targets() puts the instructions first). Both targets then
# act on one read: a hooks.json changed between them can no longer leave the instructions
# installed and the hook refused. Taken once; a hook merge with no snapshot reads afresh.
_AGY_SNAPSHOT: dict[Path, tuple[dict | None, tuple[str, str] | None]] = {}


def _install_agy_hooks(
    src: Path, dest: Path, key: str, plan: Plan, apply: bool, backup_root: Path
) -> None:
    """Merge Exoma's hook definition into Agy's hooks.json, leaving every other name alone."""
    exists = dest.exists()
    current, refusal = _AGY_SNAPSHOT.pop(dest, None) or agy_hooks_state(dest)
    if refusal:
        plan.add("refused", dest, refusal[0])
        plan.warn(f"{dest}: {refusal[1]}")
        return
    entry = agy_hook_entry(src)
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


def _regex_literal(text: str) -> str:
    """`text` as a regex matching itself. Not re.escape: that escapes spaces too, which a
    stricter engine than Python's (Agy's) may refuse."""
    return re.sub(r"([\\.^$|?*+()\[\]{}])", r"\\\1", text)


def agy_permission_rules() -> list[str]:
    """The opt-in allow rules for this machine: the runner through this machine's bash, and
    writes to the runtime's data directory, which Agy's sandbox otherwise refuses."""
    from exoma.config.settings import DATA_DIR

    template = json.loads((DIST_CONFIG / AGY_HOST_PERMISSIONS).read_text(encoding="utf-8"))
    values = {"{{RUNNER_RE}}": _regex_literal(runner_prefix()), "{{DATA_DIR}}": Path(DATA_DIR).as_posix()}
    rules = []
    for rule in template["permissions"]["allow"]:
        for name, value in values.items():
            rule = rule.replace(name, value)
        rules.append(rule)
    return rules


def _install_agy_permissions(
    enabled: bool, owned: list[str], plan: Plan, apply: bool, backup_root: Path
) -> list[str]:
    """Bring Agy's permissions.allow to this install's rules (none when not `enabled`),
    touching only rules Exoma added: `owned` names them, and the rules owned afterwards are
    returned for the install mode file. A rule the user wrote is never claimed or removed,
    even when it is identical to one of ours."""
    dest = agy_settings_dest()
    desired = agy_permission_rules() if enabled else []
    if not dest.exists() and not desired:
        return []
    try:
        current = json.loads(_read_text_lenient(dest)) if dest.exists() else {}
    except (OSError, ValueError):
        plan.warn(f"{dest}: not readable JSON — permission rules not changed; fix it, then install again")
        return list(owned)
    permissions = current.get("permissions", {}) if isinstance(current, dict) else None
    allow = permissions.get("allow", []) if isinstance(permissions, dict) else None
    if not isinstance(allow, list):
        plan.warn(f"{dest}: `permissions.allow` is not a list — permission rules not changed")
        return list(owned)
    kept = [rule for rule in allow if not (rule in owned and rule not in desired)]
    added = [rule for rule in desired if rule not in kept]
    now_owned = [rule for rule in desired if rule in added or rule in owned]
    merged_allow = kept + added
    if merged_allow == allow:
        plan.add("unchanged", dest, "permission rules")
        return now_owned
    merged = dict(current)
    merged_permissions = dict(permissions)
    if merged_allow:
        merged_permissions["allow"] = merged_allow
    else:
        merged_permissions.pop("allow", None)
    if merged_permissions:
        merged["permissions"] = merged_permissions
    else:
        merged.pop("permissions", None)
    exists = dest.exists()
    pre_sha256 = _file_sha256(dest)
    key = f"{AGY_HOST}/settings.json"
    saved = _backup(dest, backup_root, plan, apply, key) if exists else None
    detail = f"added {len(added)} permission rule(s)" if added else "removed Exoma's permission rules"
    plan.add("merge" if exists else "create", dest, detail)
    if apply:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
        _record("merge" if exists else "create", dest, key, saved, pre_sha256)
    return now_owned


def _withdraw_agy_instructions(dest: Path, key: str, plan: Plan, apply: bool, backup_root: Path) -> None:
    """GEMINI.md when its hook is refused: install nothing, and take out the block an earlier
    install left, since instructions with no hook enforcing them are a half install."""
    from exoma.installer.uninstall import _uninstall_text  # uninstall imports this module

    start, end = MARKERS[key]
    text = _read_text_lenient(dest) if dest.is_file() else ""
    if _marker_problem(text, start, end) is None and _managed_block(text, start, end) is None:
        plan.add("refused", dest, "its Agy hook cannot be installed")
        return
    # A block to take out, or markers that do not pair up: _uninstall_text removes the
    # first and warns about the second rather than leaving either unreported.
    _uninstall_text(dest, key, plan, apply, backup_root)


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
    if key == AGY_HOST_INSTRUCTIONS:
        hooks_dest = agy_hooks_dest()
        _AGY_SNAPSHOT[hooks_dest] = agy_hooks_state(hooks_dest)
        if _AGY_SNAPSHOT[hooks_dest][1]:
            _withdraw_agy_instructions(dest, key, plan, apply, backup_root)
            return
    incoming = _render_text(src, key, project_root, only_command)

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



def _agy_targets() -> list[tuple[Path, Path, str]]:
    """The Agy host's files, all under ~/.gemini: the instruction block first (its hook
    decision is taken there, see _AGY_SNAPSHOT), then the hook registration, the contract,
    the hook scripts, and the skills."""
    claude = DIST_CONFIG / CLAUDE_HOST
    mapping = [
        (DIST_CONFIG / AGY_HOST_INSTRUCTIONS, HOME / ".gemini" / "GEMINI.md", AGY_HOST_INSTRUCTIONS),
        (DIST_CONFIG / AGY_HOST_HOOKS, agy_hooks_dest(), AGY_HOST_HOOKS),
        (claude / "CLAUDE.md", agy_exoma_dir() / "CONTRACT.md", AGY_CONTRACT),
    ]
    for name in AGY_HOOK_FILES:
        mapping.append((claude / "hooks" / name, agy_exoma_dir() / "hooks" / name, AGY_HOOKS_PREFIX + name))
    skills = {}
    if (claude / "skills").is_dir():
        skills.update({p.stem: p for p in (claude / "skills").iterdir() if p.suffix == ".md"})
    shipped = DIST_CONFIG / AGY_HOST / "skills"
    if shipped.is_dir():
        skills.update({p.parent.name: p for p in shipped.glob("*/SKILL.md")})
    for name, path in sorted(skills.items()):
        mapping.append(
            (path, agy_cli_dir() / "skills" / f".{name}" / "SKILL.md", f"{AGY_SKILLS_PREFIX}{name}/SKILL.md")
        )
    return mapping


def _targets(hosts=HOSTS) -> list[tuple[Path, Path, str]]:
    """(source, destination, key) for everything in dist/config that the given hosts take.

    Every entry here is global. The only project-scoped file the workflow ships is
    <project_root>/opencode.json, and that one is installed by init/upgrade, not here.
    Every host by default: the bundle check and --uninstall cover all of them, an install
    and --check pass the selection.
    """
    mapping = []
    dir_families = []
    if "claude" in hosts:
        mapping.append(
            (
                DIST_CONFIG / CLAUDE_INSTRUCTIONS,
                HOME / ".claude" / "CLAUDE.md",
                CLAUDE_INSTRUCTIONS,
            )
        )
        dir_families += [
            (CLAUDE_HOST, "skills", HOME / ".claude" / "skills"),
            (CLAUDE_HOST, "commands", HOME / ".claude" / "commands"),
            (CLAUDE_HOST, "hooks", HOME / ".claude" / "hooks"),
        ]
    # Per-provider bundles come from config/providers.py rather than being spelled out
    # here, so a new provider is a dist/ folder plus one declaration.
    for name, bundle in PROVIDER_BUNDLES.items():
        provider_home = HOME.joinpath(*bundle["home_dir"].split("/"))
        dist_name, dest_name = bundle["instructions"]
        mapping.append(
            (
                DIST_CONFIG / provider_dir(name) / dist_name,
                provider_home / dest_name,
                f"{provider_dir(name)}/{dist_name}",
            )
        )
        # The file stem becomes the custom agent's name. One global roster: the subagents
        # belong to the workflow, so every project it manages reads the same set instead
        # of each worktree carrying its own copy to keep in sync.
        if bundle.get("agents_dir"):
            dir_families.append(
                (provider_dir(name), bundle["agents_dir"], provider_home / bundle["agents_dir"])
            )
    if "agy" in hosts:
        mapping += _agy_targets()
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
