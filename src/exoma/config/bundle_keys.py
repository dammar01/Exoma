"""Where a bundle lives under `_bundle/config/`, and what an older install called it.

Two roles, one folder each: `hosts/<name>/` is a main-agent host (the CLI the developer talks
to), `providers/<name>/` a second-agent provider. Before v0.3.0 both sat at the top level —
`claude/`, `agy-host/`, `opencode/`, `codex/`, `agy/` — so a provider named like a host (a
Claude second agent) had nowhere to go.

A bundle key is a path relative to `_bundle/config/`, and keys outlive a release: the install
ledger and every install receipt keep the ones they were written with. `canonical_key` reads
an old key as the current one, so stale cleanup, uninstall and doctor still recognise what an
older install left. Keys are only ever written in the current form.
"""

HOSTS_DIR = "hosts"
PROVIDERS_DIR = "providers"

# Prefixes an installer step puts in front of a key it records (stale prune, uninstall).
_STEP_PREFIXES = ("stale/", "uninstall/")
_LEGACY_HOSTS = {"claude/": "hosts/claude/", "agy-host/": "hosts/agy/"}


def host_dir(name: str) -> str:
    return f"{HOSTS_DIR}/{name}"


def provider_dir(name: str) -> str:
    return f"{PROVIDERS_DIR}/{name}"


def canonical_key(key: str) -> str:
    """`key` in the current layout; a key already in it comes back unchanged."""
    for step in _STEP_PREFIXES:
        if key.startswith(step):
            return step + canonical_key(key[len(step):])
    if key.startswith((f"{HOSTS_DIR}/", f"{PROVIDERS_DIR}/")):
        return key
    for old, new in _LEGACY_HOSTS.items():
        if key.startswith(old):
            return new + key[len(old):]
    from exoma.config.providers import PROVIDER_BUNDLES

    name, sep, _rest = key.partition("/")
    if sep and name in PROVIDER_BUNDLES:
        return f"{PROVIDERS_DIR}/{key}"
    return key
