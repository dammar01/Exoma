"""The one list of secret-file patterns, and each provider's dialect of it.

The deny-list used to exist once, as JSON, inside `_bundle/config/opencode/opencode.project.json`.
That was fine while opencode was the only provider that could express a read boundary. It
stops being fine the moment a second provider needs the SAME list in a different syntax:
two copies of a security list drift, and the copy that drifts is the one nobody reads.

So the patterns live here in one canonical form, and each provider gets a translator:

* opencode consumes them as its own glob dialect (`*.env`, `*.ssh/*`) written into a
  project-scoped `opencode.json`. That file is still shipped as a static artifact — this
  module does not rewrite it — and `tests/checks/provider.py` asserts the two agree, so a change
  here that never reaches the JSON fails a check instead of silently halving the boundary.
* codex consumes them as TOML filesystem permissions passed with `-c` on every invocation.
  Codex has no project-scoped config file at all (a `.codex/config.toml` in a project root
  is simply not loaded), so its boundary cannot be installed once; it rides on the argv of
  each call. See `codex_filesystem_permissions()`.

  READ THIS BEFORE TRUSTING THE CODEX HALF: as of codex-cli 0.147.0 those permissions stop
  nothing. Denying `**` and `**/*` for `:workspace_roots` and then asking `codex exec` for a
  file returns that file at exit 0 — codex reads by spawning a shell, and `--sandbox
  read-only` bounds writes rather than reads. The flags stay because they are four argv
  elements and become real the day codex gates shell reads, but the enforced boundary today
  is opencode's alone. `adapters/install/codex_install.py` reports this as `not_enforceable`.

Verified against codex-cli 0.147.0: `[permissions.<profile>.filesystem]` is a real config
section, an access value of `"none"` is accepted, `"read-write"` is not (the enum rejects
it), and a `[permissions]` block requires `default_permissions` to name the active profile.

What that probe did NOT establish is whether codex's glob matcher lets `*` match a leading
dot. The distinction is the whole boundary for the files that matter most: if it does not,
`**/*.npmrc` covers `app.npmrc` and leaves `.npmrc` open, and the same hole swallows `.env`,
`.netrc`, `.pgpass` and `.git-credentials`. So `codex_secret_globs()` ships BOTH spellings
of every pattern rather than betting on the permissive reading. The duplicate costs one
extra map entry each; the bet would have cost the secret.
"""

# Canonical patterns, written in opencode's dialect because that is the dialect already
# shipped in dist/. Everything else translates FROM this.
#
# `*.key` is deliberately broad. It costs the agent access to a handful of innocent files
# named `something.key`; it buys refusing every private key that follows the convention.
# `*.tfvars` is the same trade: most of them hold no secret, but the ones that do hold the
# whole cloud account, and nothing in the name tells the two apart.
#
# The directory patterns (`.ssh`, `.aws`, `.gnupg`, `.kube`, `.docker`) deny the directory
# rather than the credential file inside it. `.kube/config` and `.docker/config.json` are
# named `config`, so a file-name pattern precise enough to catch them would also catch every
# ordinary config in the tree. Note the leading dot is literal here: a plain `docker/` full
# of Dockerfiles does not match, only the dotted home-directory form does.
SECRET_READ_PATTERNS: tuple[str, ...] = (
    "*.env",
    "*.env.*",
    "*id_rsa*",
    "*id_ed25519*",
    "*id_ecdsa*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.ppk",
    "*.kdbx",
    "*.ssh/*",
    "*.aws/*",
    "*.gnupg/*",
    "*.kube/*",
    "*.docker/*",
    "*.npmrc",
    "*.netrc",
    "*.pgpass",
    "*.htpasswd",
    "*.tfvars",
    "*.git-credentials",
    "*credentials.json",
    "*credentials.yml",
    "*credentials.yaml",
    "*service-account*.json",
)

# Files that LOOK like secrets and exist to be read. opencode carves them back out with an
# explicit allow; see `codex_filesystem_permissions` for why codex does not.
SECRET_READ_ALLOWLIST: tuple[str, ...] = (
    "*.env.example",
    "*.env.sample",
    "*.env.template",
)

# The codex profile this workflow asserts. Named rather than reusing "default" so a user
# reading `codex doctor` output can tell which profile came from here.
CODEX_PERMISSION_PROFILE = "workflow"

# Codex applies filesystem rules per access scope; `:workspace_roots` is the scope covering
# the directories the run is pointed at, which is exactly the blast radius that matters.
CODEX_FILESYSTEM_SCOPE = ":workspace_roots"

# Codex's access vocabulary, verified by probing `--strict-config` one value at a time
# against codex-cli 0.147.0: none/read/write/deny parse, read-write and read_write do not.
CODEX_ACCESS_DENY = "none"


def _to_codex_glob(pattern: str) -> str:
    """One opencode pattern in codex's glob dialect.

    opencode anchors nothing: `*.env` means "any path ending in .env". Codex matches against
    the whole relative path, so the same intent needs a `**/` prefix. Directory patterns
    (`*.ssh/*`) become `**/.ssh/**`: the leading `*` in opencode's spelling is standing in
    for the dot, and the trailing `*` has to recurse or a key one level down stays readable.
    """
    if pattern.endswith("/*"):
        body = pattern[:-2].lstrip("*")
        return f"**/{body}/**"
    return f"**/{pattern}"


def _dot_variant(pattern: str) -> str | None:
    """The same pattern spelled so it matches the dotfile form, or None if it already does.

    `*.npmrc` is one pattern doing two jobs: naming a suffix (`app.npmrc`) and naming a
    dotfile (`.npmrc`). opencode's matcher treats the leading `*` as able to match nothing,
    so both fall out of one line. Codex's matcher may not — glob implementations commonly
    refuse to let `*` cross a leading dot, and that behaviour is unverified here (see the
    module docstring). Rather than assume, emit the dotfile spelling as its own rule.

    Directory patterns already anchor on a literal dot after translation (`**/.ssh/**`), so
    they get nothing extra.
    """
    if pattern.endswith("/*") or not pattern.startswith("*"):
        return None
    body = pattern[1:]
    dotted = body if body.startswith(".") else f".{body}"
    return f"**/{dotted}"


def codex_secret_globs() -> tuple[str, ...]:
    """Every canonical pattern, translated, order preserved and duplicates dropped.

    One canonical pattern can yield two globs — the suffix spelling and the dotfile spelling.
    Callers must therefore size themselves off this function rather than off
    `len(SECRET_READ_PATTERNS)`; the counts are deliberately no longer equal.
    """
    seen: dict[str, None] = {}
    for pattern in SECRET_READ_PATTERNS:
        seen.setdefault(_to_codex_glob(pattern), None)
        dotted = _dot_variant(pattern)
        if dotted is not None:
            seen.setdefault(dotted, None)
    return tuple(seen)


def codex_filesystem_permissions() -> dict[str, str]:
    """The `{glob: access}` map codex takes for one filesystem scope.

    The allowlist is NOT re-added here, and that is a decision rather than an omission.
    `*.env.*` translated is `**/*.env.*`, which matches `.env.example` too, and codex's
    precedence between an overlapping deny and allow in the same map has not been verified
    against a running agent. Carving the exception out on an unverified precedence rule
    would risk opening `.env.local` in order to open `.env.example`. Until precedence is
    proven, this fails closed: the sample files are unreadable, which costs the agent a
    template and costs the repository nothing.
    """
    return {glob: CODEX_ACCESS_DENY for glob in codex_secret_globs()}


def _toml_inline_table(mapping: dict[str, str]) -> str:
    """A TOML inline table, one line, for passing through `codex -c key=value`.

    Hand-rolled because the value has to survive as a single argv element and `tomllib` is
    read-only in the standard library. It does NO escaping, and the guard below is what makes
    that safe rather than a comment claiming it is: every key is a glob from this module and
    every value comes from a fixed vocabulary, so nothing that needs escaping can arrive.
    The day someone routes a user-supplied pattern here, this raises instead of quietly
    emitting a `"` that ends the string early and turns the rest of the boundary into
    syntax codex either rejects or, worse, reads as something else.
    """
    for key, value in mapping.items():
        if any(character in f"{key}{value}" for character in '"\\'):
            raise ValueError(
                f"secret-boundary entry is not safe to inline into TOML unescaped: "
                f"{key!r}={value!r}"
            )
    parts = [f'"{key}"="{value}"' for key, value in mapping.items()]
    return "{" + ",".join(parts) + "}"


def codex_permission_args() -> list[str]:
    """The `-c` pairs that DECLARE the read boundary on a single codex call.

    Declare, not enforce — see the module docstring. Codex accepts and ignores them for the
    shell reads it actually performs. They ship anyway: the cost is four argv elements, and
    the alternative is having nothing in place when codex closes the gap.

    Two overrides, not one: naming a profile in `[permissions]` without setting
    `default_permissions` makes codex refuse to start with `config defines [permissions]
    profiles but does not set default_permissions`, so the profile and its selection travel
    together or neither works.

    This overrides whatever profile the user's own `config.toml` selects, for the duration
    of this process only — nothing is written to their config. A user who has arranged their
    own permission profile gets ours instead while the workflow's agent runs, which is the
    trade the boundary is worth.
    """
    table = _toml_inline_table(codex_filesystem_permissions())
    return [
        "-c",
        f'default_permissions="{CODEX_PERMISSION_PROFILE}"',
        "-c",
        f"permissions.{CODEX_PERMISSION_PROFILE}.filesystem="
        f'{{"{CODEX_FILESYSTEM_SCOPE}"={table}}}',
    ]
