"""The environment an OpenCode child process gets: the machine's, not the user's secrets.

OpenCode used to inherit `os.environ` whole, so every credential in the shell that started
the runtime — cloud keys, database URLs, tokens for unrelated projects — was readable by
the second agent's process, whose own read boundary (opencode.project.json) guards files,
not its environment. The child now starts from an allowlist of variables the OS, the
network stack and OpenCode itself need, plus exactly the keys in the active project's
`.env`: the project the call is about is the one whose credentials it may use.

What stays reachable without the parent environment:
- keys added with `opencode auth login` / `/connect`: they live in
  `~/.local/share/opencode/auth.json`, found through HOME / USERPROFILE / XDG_*;
- OpenCode's own config: `~/.config/opencode`, found the same way, and `OPENCODE_*`;
- a provider key referenced from config as `{env:NAME}`: put it in the project's `.env`.

Accepted exceptions, passed through by name or prefix because OpenCode cannot work without
them: `OPENCODE_*` (its own config switches — a secret put there is put there for
OpenCode), and the proxy/CA variables (a proxy URL can carry `user:pass@`, but without it
the provider is unreachable on a proxied network). Everything else from the parent is
dropped; `parent_env_dropped` in the call meta counts it.

Codex and agy are deliberately NOT routed through this. They are trusted providers with
no enforceable read boundary (see core/audit/diagnostics.py): trimming their environment
would imply a boundary they do not have.
"""

import os
from pathlib import Path

# Upper-cased: Windows environment names are case-insensitive and os.environ reports them
# upper-cased there; POSIX names are compared as given after the same folding.
_BASE_KEYS = frozenset(
    {
        # process launch and executable lookup
        "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
        "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
        "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)",
        "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "SHELL",
        # where OpenCode finds its config and auth.json
        "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
        "USER", "USERNAME", "LOGNAME", "COMPUTERNAME",
        # scratch space
        "TEMP", "TMP", "TMPDIR",
        # text and time
        "LANG", "LANGUAGE", "TZ", "TERM", "COLORTERM", "NO_COLOR",
        "PYTHONUTF8", "PYTHONIOENCODING",
        # reaching the provider through a corporate proxy / private CA
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE",
    }
)
_BASE_PREFIXES = ("LC_", "XDG_", "OPENCODE_")

DOTENV_FILENAME = ".env"


def parse_dotenv(text: str) -> dict[str, str]:
    """KEY=VALUE lines. `export ` prefixes, comments, and matching quotes are handled.

    Deliberately small: no interpolation and no multi-line values. A value this parser
    cannot read is skipped rather than guessed at — a guessed credential is worse than a
    missing one, which at least fails as "auth unavailable".
    """
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key or not (key[0].isalpha() or key[0] == "_"):
            continue
        if not all(ch.isalnum() or ch == "_" for ch in key):
            continue
        value = value.strip()
        if value[:1] in ("\"", "'"):
            # Quoted: the value ends at the matching quote, so `KEY="abc" # note` is abc.
            # No matching quote means the line is not one this parser understands.
            closing = value.find(value[0], 1)
            if closing < 0:
                continue
            value = value[1:closing]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def _is_base(name: str) -> bool:
    upper = name.upper()
    return upper in _BASE_KEYS or upper.startswith(_BASE_PREFIXES)


def project_scoped_env(
    project_root: Path | str | None, parent: dict | None = None
) -> tuple[dict[str, str], dict]:
    """(env, meta) for an OpenCode child. meta names counts and the .env state, never values."""
    source = os.environ if parent is None else parent
    env = {name: value for name, value in source.items() if _is_base(name)}
    meta: dict = {"env_policy": "project_scoped"}

    dotenv = Path(project_root) / DOTENV_FILENAME if project_root else None
    if dotenv is None or not dotenv.is_file():
        meta["project_dotenv"] = "absent"
        project_values: dict[str, str] = {}
    else:
        try:
            project_values = parse_dotenv(dotenv.read_text(encoding="utf-8-sig"))
            meta["project_dotenv"] = "loaded"
        except (OSError, UnicodeDecodeError) as exc:
            project_values = {}
            meta["project_dotenv"] = f"unreadable: {type(exc).__name__}"
    env.update(project_values)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    meta["project_dotenv_keys"] = len(project_values)
    meta["parent_env_dropped"] = sum(
        1 for name in source if not _is_base(name) and name not in project_values
    )
    return env, meta
