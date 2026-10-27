"""Extract the maintainer's live agent config into dist/ so it can ship with the repo.

BOOTSTRAP-ONLY. dist/ is the canonical, Git-reviewed bundle; this tool only *seeds* it
from a maintainer's live ~/.claude the first time (or when deliberately re-importing home
edits). Routine changes are made in dist/ directly. Skill bodies live only in
`src/exoma/_bundle/config/hosts/claude/skills/*.md`; CLAUDE.md contains the command registry and
orchestrator contract.
`tools/maintain/sync_skills.py --check` verifies those files still match CLAUDE.md's command
registry, and `tools/maintain/gen_manifest.py` rebuilds the manifest from dist/. Do NOT run this to
refresh the manifest after editing dist/ — it would overwrite dist/ with home-dir content
and abort on the clobber guard.

Runs on the DEV machine. Reads the live agent directories, copies an explicit allowlist
into `src/exoma/_bundle/config/`, redacts machine-specific paths, and writes a manifest the installer
consumes.

Security posture, in order of importance:

1. **Allowlist, never blocklist.** Agent homes contain credentials, history, session
   transcripts, and project paths. Anything not explicitly listed here stays local.
2. **Fail closed on secrets.** Every extracted byte is scanned for credential-shaped
   content. A hit ABORTS the run and writes nothing — a warning would be read, ignored,
   and committed anyway. Secrets in git history are not removable in practice.
3. **Read-only.** This tool never writes outside `dist/`.

Usage:
    python tools/maintain/extract_config.py                 # extract into dist/
    python tools/maintain/extract_config.py --dry-run       # report only, write nothing
"""

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

from exoma.config.providers import PROVIDER_BUNDLES  # noqa: E402

DIST_DIR = REPO_ROOT / "src" / "exoma" / "_bundle" / "config"
MANIFEST = REPO_ROOT / "src" / "exoma" / "_bundle" / "manifest.json"

# Only these leave the machine. Adding an entry is a deliberate act; forgetting one
# costs a missing feature, whereas a stray glob costs a leaked credential.
ALLOWLIST = [
    # (source relative to home, dest relative to src/exoma/_bundle/config, kind)
    (".claude/CLAUDE.md", "hosts/claude/CLAUDE.md", "file"),
    (".claude/skills", "hosts/claude/skills", "dir:*.md"),
    (".claude/commands", "hosts/claude/commands", "dir:*.md"),
    (".claude/hooks", "hosts/claude/hooks", "dir:*.sh,*.py,intent-map.json"),
    *(
        # One entry per declared provider bundle (config/providers.py) rather than a
        # literal per vendor. Still an explicit allowlist: a provider only appears here
        # once someone declares its bundle, so no glob can widen this by accident.
        (
            f"{bundle['home_dir']}/{bundle['instructions'][1]}",
            f"providers/{name}/{bundle['instructions'][0]}",
            "file",
        )
        for name, bundle in PROVIDER_BUNDLES.items()
    ),
]

# settings.json is filtered key-wise rather than copied: the file mixes shareable
# workflow wiring with machine-local state.
SETTINGS_SOURCE = ".claude/settings.json"
SETTINGS_DEST = "hosts/claude/settings.template.json"
SETTINGS_KEEP_KEYS = (
    "hooks",
    "statusLine",
    "enabledPlugins",
    "extraKnownMarketplaces",
    "model",
    "effortLevel",
    "permissions",
)

# Never copied, even if some future allowlist entry would sweep them in.
DENY_NAMES = {
    ".credentials.json",
    "history.jsonl",
    "session_registry.json",
    "stats-cache.json",
    ".last-update-result.json",
}
DENY_DIR_PARTS = {
    "projects",
    "sessions",
    "session-env",
    "shell-snapshots",
    "debug",
    "cache",
    "tmp",
    "paste-cache",
    "file-history",
    "backups",
    "downloads",
    "jobs",
    "daemon",
    "gateway",
    "ide",
    "plugins",
    "node_modules",
    "__pycache__",
}

# Credential-shaped content. Lives in utils/redact.py so the runtime evidence path uses
# the SAME table — a second copy here would drift, and the copy that fell behind would be
# the one guarding live agent output.
from exoma.utils.redact import SECRET_PATTERNS  # noqa: E402

HOME = Path.home()


def _redact(text: str) -> str:
    """Replace machine-specific absolute paths with installer placeholders.

    Not a security control — the secret scan is. This keeps the shipped config from
    hard-coding one machine's layout, and keeps the maintainer's username out of the
    published files.
    """
    home = str(HOME)
    repo = str(REPO_ROOT)
    for src, token in ((repo, "{{PROJECT_ROOT}}"), (home, "{{HOME}}")):
        text = text.replace(src, token)
        text = text.replace(src.replace("\\", "\\\\"), token)
        text = text.replace(src.replace("\\", "/"), token)
    return text


def _scan_secrets(text: str, label: str) -> list[str]:
    return [
        f"{label}: {why}" for pattern, why in SECRET_PATTERNS if pattern.search(text)
    ]


def _denied(path: Path) -> str | None:
    if path.name in DENY_NAMES:
        return f"denylisted file name: {path.name}"
    hit = DENY_DIR_PARTS.intersection(path.parts)
    if hit:
        return f"denylisted directory: {sorted(hit)[0]}"
    return None


def _collect() -> tuple[list[dict], list[str]]:
    """Gather allowlisted files. Returns (entries, problems)."""
    entries: list[dict] = []
    problems: list[str] = []

    for rel_src, rel_dest, kind in ALLOWLIST:
        source = HOME / rel_src
        if not source.exists():
            problems.append(f"missing (skipped): {rel_src}")
            continue

        if kind == "file":
            candidates = [(source, rel_dest)]
        else:
            patterns = kind.split(":", 1)[1].split(",")
            candidates = []
            for pattern in patterns:
                for found in sorted(source.glob(pattern)):
                    if found.is_file():
                        candidates.append((found, f"{rel_dest}/{found.name}"))

        for path, dest in candidates:
            reason = _denied(path)
            if reason:
                problems.append(f"REFUSED {path.name}: {reason}")
                continue
            try:
                raw = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                problems.append(f"unreadable (skipped): {path} — {exc}")
                continue
            entries.append({"dest": dest, "text": _redact(raw), "source": str(path)})

    settings = HOME / SETTINGS_SOURCE
    if settings.exists():
        try:
            loaded = json.loads(settings.read_text(encoding="utf-8"))
            kept = {k: v for k, v in loaded.items() if k in SETTINGS_KEEP_KEYS}
            dropped = sorted(set(loaded) - set(kept))
            entries.append(
                {
                    "dest": SETTINGS_DEST,
                    "text": _redact(json.dumps(kept, indent=2) + "\n"),
                    "source": str(settings),
                    "dropped_keys": dropped,
                }
            )
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"settings.json unreadable: {exc}")

    return entries, problems


def _clobber_risks(entries: list[dict]) -> list[str]:
    """dist/ files that were edited directly and would be silently overwritten.

    This tool copies one direction only: ~/.claude -> dist/. That is fine while dist/ is
    purely a build output, but it is also the place someone reasonably edits when they
    want a config change to land in the repo without touching their own machine. Those
    edits look identical to stale build output, and the copy would erase them with no
    diff, no prompt, and no trace.

    The signal is deliberately conservative: content differs AND the dist/ file is newer
    than the home file it came from. Same content is not a conflict, and a dist/ file
    older than its source is exactly what a normal rebuild looks like.
    """
    risks: list[str] = []
    for entry in entries:
        target = DIST_DIR / entry["dest"]
        if not target.exists():
            continue
        try:
            if target.read_text(encoding="utf-8") == entry["text"]:
                continue
            source_mtime = Path(entry["source"]).stat().st_mtime
            if target.stat().st_mtime > source_mtime:
                risks.append(entry["dest"])
        except OSError:
            continue
    return risks


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract live agent config into dist/")
    parser.add_argument(
        "--dry-run", action="store_true", help="report only, write nothing"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite dist/ files that were edited directly (see the abort message)",
    )
    args = parser.parse_args()

    entries, problems = _collect()
    if not entries:
        print("[EXTRACT] nothing to extract")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    # Fail closed: one credential hit stops everything. Publishing is irreversible.
    findings: list[str] = []
    for entry in entries:
        findings.extend(_scan_secrets(entry["text"], entry["dest"]))
    if findings:
        print("[EXTRACT] ABORTED — credential-shaped content found:")
        for finding in findings:
            print(f"  !! {finding}")
        print(
            "Nothing was written. Remove the secret or narrow the allowlist, then rerun."
        )
        return 2

    leaked_home = [e["dest"] for e in entries if str(HOME) in e["text"]]
    if leaked_home:
        print("[EXTRACT] ABORTED — absolute home paths survived redaction:")
        for dest in leaked_home:
            print(f"  !! {dest}")
        return 3

    clobber = _clobber_risks(entries)
    if clobber and not args.force:
        print(
            "[EXTRACT] ABORTED — these dist/ files are NEWER than the ~/.claude files"
        )
        print("          they would be overwritten with, and their content differs:")
        for dest in clobber:
            print(f"  !! {dest}")
        print(
            "\nThat pattern means the change was authored in dist/ directly, and copying\n"
            "over it would erase it silently. Pick one:\n"
            "  - keep the dist/ version: install it first (exoma install --apply),\n"
            "    then rerun this to extract it back as a normal build\n"
            "  - discard the dist/ version: rerun with --force\n"
            "Nothing was written."
        )
        return 4

    files_meta = []
    for entry in entries:
        digest = hashlib.sha256(entry["text"].encode("utf-8")).hexdigest()
        files_meta.append(
            {
                "path": entry["dest"],
                "sha256": digest,
                "bytes": len(entry["text"].encode("utf-8")),
                "merge": (
                    "replace"
                    if "/skills/" in entry["dest"]
                    or "/commands/" in entry["dest"]
                    or "/hooks/" in entry["dest"]
                    else "merge"
                ),
                **(
                    {"dropped_keys": entry["dropped_keys"]}
                    if "dropped_keys" in entry
                    else {}
                ),
            }
        )

    print(f"[EXTRACT] {'DRY RUN — ' if args.dry_run else ''}{len(entries)} file(s)")
    for meta in files_meta:
        extra = (
            f" (dropped {len(meta['dropped_keys'])} local key(s))"
            if meta.get("dropped_keys")
            else ""
        )
        print(f"  {meta['merge']:8} {meta['path']:44} {meta['bytes']:>7}B{extra}")
    for problem in problems:
        print(f"  - {problem}")

    if args.dry_run:
        print("[EXTRACT] dry run — nothing written")
        return 0

    for entry in entries:
        target = DIST_DIR / entry["dest"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(entry["text"], encoding="utf-8")

    from tools.maintain.gen_manifest import _build

    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(
        json.dumps(
            _build(),
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"[EXTRACT] wrote {DIST_DIR} + {MANIFEST.name}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(REPO_ROOT))
    sys.path.insert(0, str(REPO_ROOT / "src"))
    raise SystemExit(main())
