"""The per-invocation request behind /.verify-browser.

The skill interviews the user, then writes one JSON file for the session:

  .workflow/data/sessions/<session>/e2e/request.json
  {"version": 1, "phase": "draft" | "run", "settings": {...},
   "scenario": {...}, "existing_tests": [...], "spec_notes": [...]}

`draft` asks second_agent for a spec and stops before any browser starts; `run` executes
the scenario the user confirmed. Settings resolve in three layers, each overriding the one
before it: the shipped defaults below, the project's `e2e` section in `.workflow/config.json`
(so a configured project runs without being interviewed again), then the request's own
`settings` for what this one run does differently. Two sessions verifying two apps on one
project still diverge freely — they just start from the same pinned base. Hybrid review is
not a knob — it always runs.

The request is written by an agent, so it cannot also be where the policy that governs it
lives. CONFIG_ONLY_SETTINGS — which remote origins a run may reach and which command runs
the project's own tests — resolve from config.json alone; a request naming one is refused.
A request may still pick its `base_url`, and that URL is judged by the config's policy.

Credentials never travel in the request. `${NAME}` placeholders resolve from one profile of
`.workflow/e2e/secrets.json`, then from the process environment:

  {"default": "qa",
   "profiles": [{"name": "qa", "credentials": {"E2E_USER": "...", "E2E_PASS": "..."}},
                {"name": "admin", "credentials": {"E2E_USER": "...", "E2E_PASS": "..."}}]}

The request picks a profile by name (`settings.secrets_profile`), never by value, so trying
another account is another run with another name. Which NAMES exist is decided here, in
CREDENTIAL_KEYS — not by the scenario, and not by whatever second_agent wrote.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from exoma.core.workspace.workspace_paths import read_json_file
from exoma.core.workspace.workspace_paths import workflow_paths

REQUEST_VERSION = 1
PHASES = ("draft", "run")
SECRETS_FILE = "secrets.json"
_PROFILE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SECRET_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# The only credential names a scenario may reference, a secrets.json profile may hold, and
# the template generator writes. Another credential (an OTP seed, a tenant) is a new entry
# here, reviewed like code — never a name a scenario invents.
CREDENTIAL_KEYS = ("E2E_USER", "E2E_PASS")
MAX_TEMPLATE_PROFILES = 20
# Settings a newer contract removed. Named with the way forward instead of the generic
# "unknown key", so a request written for the old contract says what to change.
REMOVED_SETTINGS = {
    "allowed_mutation_paths": (
        "removed: writes run only with settings.allow_side_effects true against a loopback base_url; "
        "a POST that only reads goes in settings.allowed_read_only_requests"
    ),
}
# Settings that are policy over the request rather than choices within it: the origins a run
# may reach off loopback, and the argv the runtime executes with the user's environment. A
# request that could set them would authorise itself, so they are read only from the
# project's config.json `e2e` section, which the user edits and Git reviews.
CONFIG_ONLY_SETTINGS = (
    "allow_remote",
    "allowed_origins",
    "existing_test_command",
    "existing_test_allowlist",
    "existing_test_timeout_s",
)


def default_settings() -> dict:
    """Smoke-test starting points, not calibrated limits (plan §19)."""
    return {
        "base_url": "http://localhost:8000",
        "browser": "chromium",
        # headed by default: a browser the user can watch is the point of this command,
        # and a run nobody sees is the one whose failure gets argued about. CI pins
        # `headless: true` in the project's config.json e2e section.
        "headless": False,
        # pause after every Playwright action so a headed run can be watched; 0 = full
        # speed. Clamped to 0..5000 ms, and the pauses count against total_timeout_s.
        "slow_mo_ms": 0,
        "nav_timeout_ms": 15000,
        "step_timeout_ms": 8000,
        "idle_timeout_s": 30,
        "total_timeout_s": 300,
        "probe_max_elements": 200,
        # a base_url that is neither loopback nor a .test name needs both; http://app.test
        # is local development and opens on its own, like localhost.
        "allow_remote": False,
        "allowed_origins": [],
        # steps that declare side_effect (creates/modifies/deletes test data) are refused
        # unless this is true — plan §18: data-changing actions need explicit opt-in. Writes
        # then reach loopback and .test hosts, plus any other host preflight resolved to a
        # private address and pinned.
        "allow_side_effects": False,
        # opt-in: data a step creates may be left behind on purpose (the user reviews it by
        # hand afterwards). Only then may a creates_test_data step carry no_cleanup_reason in
        # place of a cleanup; the run reports cleanup `not_planned` with the reasons listed.
        # Needs allow_side_effects; off by default so created data is still always cleaned up.
        "keep_created_data": False,
        # while allow_side_effects is false the player aborts every request whose method is
        # not GET/HEAD/OPTIONS, on any origin, except POSTs that only READ (a search, a
        # filter, a GraphQL query), confirmed by the user from the draft's
        # read_only_requests: "POST /api/search" or "POST http(s)://host/path". Exact
        # endpoint, and a GraphQL body that asks for a mutation is still refused.
        "allowed_read_only_requests": [],
        # Writes to a loopback or .test host run without the global opt-in above. The
        # single `allow_side_effects` switch treated `localhost:8000` and a production
        # host as the same risk, so proving a form works locally meant granting the same
        # permission that lets a run write anywhere preflight approved. A dev server is
        # the place these tests are supposed to write; that is what makes it a dev server.
        # DELETE is NOT covered — see allowed_destructive_requests.
        "allow_local_side_effects": True,
        # DELETE endpoints the user approved, as "DELETE /api/items/:id" or a full URL.
        # Deletes stay refused by default even on a local host and even with side effects
        # on, because the runtime cannot tell a record the scenario created from one that
        # was already there. Empty by default: each entry is a person saying yes to that
        # endpoint. The refusal names the endpoint so the answer can be given and the run
        # repeated.
        "allowed_destructive_requests": [],
        # which secrets.json profile fills ${NAME}; empty = the file's `default`, or its only
        # profile. A name, never a value.
        "secrets_profile": "",
        # profile names the draft writes into a NEW secrets.json (empty slots only); empty =
        # one profile named after secrets_profile, or "default". An existing file is never
        # touched.
        "secrets_template_profiles": [],
        "fail_on_console_error": False,
        # how many times a run that ended WITHOUT a verdict may start the browser again.
        # Only an environmental incomplete is retried (stuck, timeout, harness_error,
        # unknown_origin, output_truncated, launch_failed); an `app` failure is a real bug
        # and retrying it would only hide it, and a run with allow_side_effects true is
        # never retried at all because its first attempt may already have written.
        "max_retries": 2,
        # the repeat brake's one escape hatch. The brake counts environment failures per
        # origin and stops the run when the same bucket arrives E2E_REPEAT_LIMIT times;
        # a preflight that passes clears the environment buckets on its own, but an `app`
        # bucket has no such proof — it is cleared by a passing browser run, and the brake
        # stands in front of the run that would clear it. Someone who has fixed the app
        # sets this once to get past it. Off by default: the brake exists because twelve
        # invocations against one unfixed problem is the shape it was built to stop, and a
        # switch that were on by default would not stop it. Settable from a request, for
        # the one run, and from a project's config.json, where it is every run — which is
        # a project saying it does not want this brake, and is why the run reports the
        # value it used in `meta.e2e.config`.
        "ignore_repeat_brake": False,
        # size budget for one run's player artifacts (trace, screenshots, HTML); the
        # heaviest class is pruned first.
        "artifact_max_mb": 25,
        # the project's own E2E tests (plan §8). Run only through this argv template,
        # without a shell; {files} expands to the allow-listed test files, {base_url} to
        # base_url. Empty: listed tests are recorded, never executed.
        "existing_test_command": [],
        "existing_test_allowlist": [],
        "existing_test_timeout_s": 300,
    }


def request_path(project_root: Path, session_id: str) -> Path:
    return workflow_paths(project_root, session_id)["session_dir"] / "e2e" / "request.json"


def draft_path(project_root: Path, session_id: str) -> Path:
    return workflow_paths(project_root, session_id)["session_dir"] / "e2e" / "draft.json"


def secrets_path(project_root: Path) -> Path:
    return workflow_paths(project_root)["workflow_dir"] / "e2e" / SECRETS_FILE


def permissions_path(project_root: Path) -> Path:
    return workflow_paths(project_root)["e2e_permissions"]


# Any method, or `*` for all of them. The deny list is not about deletes: it names
# endpoints this project does not want a browser run touching at all, whatever the verb —
# a payment, a mailer, an integration that reaches a third party from the dev host.
_BLOCKED_METHODS = ("*", "GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE")
MAX_BLOCKED_REQUESTS = 50


def blocked_request_target(entry: str) -> tuple[str, str] | None:
    """`"* /api/payments"` -> ("*", "/api/payments"); None when it is not that shape."""
    method, _, target = entry.strip().partition(" ")
    target = target.strip()
    if method.upper() not in _BLOCKED_METHODS or not target:
        return None
    return method.upper(), target


def blocked_request_errors(entries: list, where_root: str = "blocked_requests") -> list[str]:
    """Shape of deny entries: `<METHOD|*> <endpoint>`, route templates allowed."""
    errors: list[str] = []
    if len(entries) > MAX_BLOCKED_REQUESTS:
        errors.append(f"{where_root}: at most {MAX_BLOCKED_REQUESTS} entries")
    for index, entry in enumerate(entries):
        where = f"{where_root}[{index}]"
        if not isinstance(entry, str) or not entry.strip():
            errors.append(f"{where}: must be a non-empty string")
            continue
        parsed = blocked_request_target(entry)
        if parsed is None:
            errors.append(
                f"{where}: '<METHOD> <path or URL>' with METHOD one of "
                f"{', '.join(_BLOCKED_METHODS)}"
            )
            continue
        problem = endpoint_error(parsed[1])
        if problem:
            errors.append(f"{where}: {problem}")
    return errors


def default_permissions() -> dict:
    return {"allowed_destructive_requests": [], "blocked_requests": []}


def load_permissions(project_root: Path) -> tuple[dict, list[str], list[str]]:
    """The project's standing e2e permissions: what is approved, and what is never run.

    Returns (permissions, warnings, errors). An absent file is no permissions, not a
    problem — most projects never write one.

    The two keys are NOT held to the same standard, because dropping them does opposite
    things. Dropping an approval leaves the run stricter than the user asked: a warning is
    enough, and that is the old posture kept. Dropping a deny entry leaves the run
    permitted to touch an endpoint the project said to leave alone — the file's failure
    silently removing the protection the file exists to provide. `{"blocked_requests":
    ["* /api/payments", "bad"]}` loaded as NO deny list at all, one warning among the
    run's other warnings, and the run proceeded. So anything that would shorten the deny
    list is an error and the run does not start: unreadable file, malformed JSON, a top
    level that is not an object, `blocked_requests` that is not a list, or any entry in it
    that does not parse. A deny list is not advisory, and neither is its syntax.

    The asymmetry that matters is elsewhere: approvals here are merged with whatever a
    request also approves, while `blocked_requests` is read ONLY from this file. A deny
    list a run could extend would be a deny list a run could also shorten, and a rule that
    the thing it governs can edit is not a rule.
    """
    permissions = default_permissions()
    path = permissions_path(project_root)
    if not path.exists():
        return permissions, [], []
    try:
        loaded = read_json_file(path)
    except (OSError, ValueError) as exc:
        # Present but unreadable. Whether the deny list in it was empty or named every
        # endpoint in the project is exactly what could not be determined.
        return permissions, [], [f"{path.name}: {type(exc).__name__}: {exc}"]
    if not loaded:
        return permissions, [], []
    if not isinstance(loaded, dict):
        return permissions, [], [
            f"{path.name}: {type(loaded).__name__}, expected object"
        ]
    warnings: list[str] = []
    errors: list[str] = []
    for key, checker, fail_closed in (
        ("allowed_destructive_requests", destructive_request_errors, False),
        ("blocked_requests", blocked_request_errors, True),
    ):
        value = loaded.get(key)
        if value is None:
            continue
        if not isinstance(value, list):
            problem = f"{path.name} {key}: {type(value).__name__}, expected list"
            if fail_closed:
                errors.append(problem)
            else:
                warnings.append(f"{problem}; ignored")
            continue
        problems = checker(value, key)
        if problems:
            if fail_closed:
                # Named individually: one unparseable entry used to take the whole list
                # with it, so the rules that were fine stopped applying because of the
                # rule beside them. Now nothing is dropped — the entry is named and fixed.
                errors += [f"{path.name} {problem}" for problem in problems]
            else:
                warnings += [f"{path.name} {problem}; ignored" for problem in problems]
            continue
        permissions[key] = value
    return permissions, warnings, errors


def _type_ok(fallback: object, value: object) -> bool:
    """Whether `value` may stand in for `fallback`.

    `bool` subclasses `int`, so a plain isinstance would let `true` through where a
    timeout is expected and hand the runtime a 1 ms budget. The int case has to exclude
    bool explicitly; nothing else can.
    """
    if isinstance(fallback, bool):
        return isinstance(value, bool)
    if isinstance(fallback, int):
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, type(fallback))


def config_settings(project_root: Path) -> tuple[dict, list[str]]:
    """The project's pinned defaults: the `e2e` section of `.workflow/config.json`.

    This is what lets a configured project run without being interviewed again — the
    section is written once, by hand, and every later /.verify-browser starts from it.

    A bad knob here WARNS and falls back, where the same knob in a request is an error.
    The asymmetry is deliberate: config.json is shared by every command, so one mistyped
    e2e value must not be able to stop work that never reads it, while a request value is
    something the user confirmed for this run and a typo there must not be papered over.
    Unreadable config = no pinned defaults, not a failure.
    """
    settings = default_settings()
    try:
        config = read_json_file(workflow_paths(project_root)["config"])
    except (OSError, ValueError):
        return settings, []
    section = config.get("e2e")
    if section is None:
        return settings, []
    if not isinstance(section, dict):
        return settings, [f"config.json e2e: {type(section).__name__}, expected object; ignored"]
    warnings: list[str] = []
    for key, value in section.items():
        if key in REMOVED_SETTINGS:
            warnings.append(f"config.json e2e.{key}: {REMOVED_SETTINGS[key]}; ignored")
            continue
        if key not in settings:
            warnings.append(f"config.json e2e.{key}: unknown key, ignored")
            continue
        if not _type_ok(settings[key], value):
            warnings.append(
                f"config.json e2e.{key}: {type(value).__name__}, "
                f"expected {type(settings[key]).__name__}; ignored"
            )
            continue
        settings[key] = value
    # A pinned value can be structurally wrong in ways a type check cannot see. Same
    # posture: name it, drop it, keep the shipped default.
    for key, checker in (
        ("allowed_read_only_requests", read_only_request_errors),
        ("allowed_destructive_requests", destructive_request_errors),
        ("secrets_template_profiles", template_profile_errors),
    ):
        problems = checker(settings[key])
        if problems:
            warnings += [f"config.json e2e.{key}: {problem.split(': ', 1)[-1]}; ignored" for problem in problems]
            settings[key] = default_settings()[key]
    if settings["secrets_profile"] and not _PROFILE_NAME.match(settings["secrets_profile"]):
        warnings.append("config.json e2e.secrets_profile: a profile name, [A-Za-z0-9_-], at most 64 characters; ignored")
        settings["secrets_profile"] = ""
    return settings, warnings


def settings_from(overrides: object, base: dict | None = None) -> tuple[dict, list[str]]:
    """`base` (the project's pinned defaults) with the request's overrides applied.

    A wrong key or type is an error, not a silent fallback: the user confirmed these
    values, so a typo must stop the run. A CONFIG_ONLY_SETTINGS key is an error too, not
    an ignored value: a run that silently dropped it would look like it ran with it."""
    settings = dict(base) if base is not None else default_settings()
    if overrides is None:
        return settings, []
    if not isinstance(overrides, dict):
        return settings, ["settings must be an object"]
    errors: list[str] = []
    for key, value in overrides.items():
        if key in REMOVED_SETTINGS:
            errors.append(f"settings.{key}: {REMOVED_SETTINGS[key]}")
            continue
        if key in CONFIG_ONLY_SETTINGS:
            errors.append(
                f"settings.{key}: config-only, a request cannot set it; "
                f"move it to the e2e section of .workflow/config.json"
            )
            continue
        if key not in settings:
            errors.append(f"settings.{key}: unknown key")
            continue
        fallback = default_settings()[key]
        if not _type_ok(fallback, value):
            errors.append(f"settings.{key}: {type(value).__name__}, expected {type(fallback).__name__}")
            continue
        settings[key] = value
    errors += read_only_request_errors(settings["allowed_read_only_requests"])
    errors += destructive_request_errors(settings["allowed_destructive_requests"])
    if settings["secrets_profile"] and not _PROFILE_NAME.match(settings["secrets_profile"]):
        errors.append("settings.secrets_profile: a profile name, [A-Za-z0-9_-], at most 64 characters")
    errors += template_profile_errors(settings["secrets_template_profiles"])
    return settings, errors


def template_profile_errors(entries: list) -> list[str]:
    where_root = "settings.secrets_template_profiles"
    errors: list[str] = []
    if len(entries) > MAX_TEMPLATE_PROFILES:
        errors.append(f"{where_root}: at most {MAX_TEMPLATE_PROFILES} profiles")
    seen: set[str] = set()
    for index, name in enumerate(entries):
        if not isinstance(name, str) or not _PROFILE_NAME.match(name):
            errors.append(f"{where_root}[{index}]: a profile name, [A-Za-z0-9_-], at most 64 characters")
        elif name in seen:
            errors.append(f"{where_root}[{index}]: duplicate profile '{name}'")
        else:
            seen.add(name)
    return errors


def template_profiles(settings: dict) -> list[str]:
    """The profile names a new secrets.json gets, the selected profile first."""
    chosen = str(settings.get("secrets_profile") or "")
    names = [str(n) for n in settings.get("secrets_template_profiles") or []]
    if chosen and chosen not in names:
        names.insert(0, chosen)
    return names or ["default"]


MAX_READ_ONLY_REQUESTS = 20
# A read that has to travel as a request body is a POST. PUT/PATCH/DELETE are writes by
# their own definition, so no entry may name them.
READ_ONLY_METHODS = ("POST",)


def endpoint_error(entry: str) -> str | None:
    """Why `entry` is not an exact endpoint (`/path` or `http(s)://host/path`), or None."""
    if any(mark in entry for mark in ("*", "?", "#")) or any(ch.isspace() for ch in entry):
        return "exact path only, no wildcard, query, fragment or whitespace"
    if entry.startswith("/"):
        return "'//' is a scheme-relative URL, write the full http(s) URL" if entry.startswith("//") else None
    parts = urlsplit(entry)
    if parts.scheme not in ("http", "https") or not parts.netloc or not parts.path.startswith("/"):
        return "a path starting with '/' or a full http(s)://host/path URL"
    return None


def read_only_request_target(entry: str) -> tuple[str, str] | None:
    """`"POST /api/search"` -> ("POST", "/api/search"); None when it is not that shape."""
    method, _, target = entry.strip().partition(" ")
    target = target.strip()
    if method not in READ_ONLY_METHODS or not target:
        return None
    return method, target


MAX_DESTRUCTIVE_REQUESTS = 20
# The mirror of READ_ONLY_METHODS: that list exists to admit a method that usually
# writes, this one to gate a method that always removes.
DESTRUCTIVE_ENTRY_METHODS = ("DELETE",)


def destructive_request_target(entry: str) -> tuple[str, str] | None:
    """`"DELETE /api/items/7"` -> ("DELETE", "/api/items/7"); None when not that shape."""
    method, _, target = entry.strip().partition(" ")
    target = target.strip()
    if method.upper() not in DESTRUCTIVE_ENTRY_METHODS or not target:
        return None
    return method.upper(), target


def destructive_request_errors(
    entries: list, where_root: str = "settings.allowed_destructive_requests"
) -> list[str]:
    """Shape of destructive-request entries: `<METHOD> <endpoint>`, METHOD DELETE.

    Route templates are allowed — `DELETE /api/items/:id` — and wildcards are not. The
    difference matters: `:id` stands for exactly one segment, so the approval is the route
    the user was shown, while `/api/items/*` would reach everything nested beneath it. The
    literal id cannot be required instead, because it changes every run and writing it here
    would put it in the artifacts this package keeps ids out of.
    """
    errors: list[str] = []
    if len(entries) > MAX_DESTRUCTIVE_REQUESTS:
        errors.append(f"{where_root}: at most {MAX_DESTRUCTIVE_REQUESTS} entries")
    for index, entry in enumerate(entries):
        where = f"{where_root}[{index}]"
        if not isinstance(entry, str) or not entry.strip():
            errors.append(f"{where}: must be a non-empty string")
            continue
        parsed = destructive_request_target(entry)
        if parsed is None:
            errors.append(
                f"{where}: '<METHOD> <path or URL>' with METHOD one of "
                f"{', '.join(DESTRUCTIVE_ENTRY_METHODS)}"
            )
            continue
        problem = endpoint_error(parsed[1])
        if problem:
            errors.append(f"{where}: {problem}")
    return errors


def read_only_request_errors(entries: list, where_root: str = "settings.allowed_read_only_requests") -> list[str]:
    """Shape of read-only request entries: `<METHOD> <exact endpoint>`, METHOD POST."""
    errors: list[str] = []
    if len(entries) > MAX_READ_ONLY_REQUESTS:
        errors.append(f"{where_root}: at most {MAX_READ_ONLY_REQUESTS} entries")
    for index, entry in enumerate(entries):
        where = f"{where_root}[{index}]"
        if not isinstance(entry, str) or not entry.strip():
            errors.append(f"{where}: must be a non-empty string")
            continue
        parsed = read_only_request_target(entry)
        if parsed is None:
            errors.append(f"{where}: '<METHOD> <path or URL>' with METHOD one of {', '.join(READ_ONLY_METHODS)}")
            continue
        problem = endpoint_error(parsed[1])
        if problem:
            errors.append(f"{where}: {problem}")
    return errors


def load_request(project_root: Path, session_id: str) -> tuple[dict | None, str | None, list[str]]:
    """(request, reason, errors). reason is `request_missing` or `request_invalid`."""
    path = request_path(project_root, session_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "request_missing", [f"{path} does not exist; /.verify-browser writes it before calling the runtime"]
    except (OSError, ValueError) as exc:
        return None, "request_invalid", [f"{path}: {type(exc).__name__}: {exc}"]
    if not isinstance(data, dict):
        return None, "request_invalid", ["request top level is not an object"]
    errors: list[str] = []
    if data.get("version") != REQUEST_VERSION:
        errors.append(f"version must be {REQUEST_VERSION}")
    phase = data.get("phase")
    if phase not in PHASES:
        errors.append(f"phase must be one of: {', '.join(PHASES)}")
    base, config_warnings = config_settings(project_root)
    settings, setting_errors = settings_from(data.get("settings"), base=base)
    errors += setting_errors
    # The standing permissions file, folded in after the request has had its say. Its
    # approvals ADD to whatever this run approves — an endpoint agreed to in an earlier
    # session does not need agreeing to again. Its deny list replaces nothing and can be
    # replaced by nothing: it is not a settings key, so no request can name it.
    permissions, permission_warnings, permission_errors = load_permissions(project_root)
    config_warnings += permission_warnings
    # A deny list that could not be read in full is not a run that starts with a smaller
    # deny list. It is a run that does not start.
    errors += permission_errors
    persisted = permissions["allowed_destructive_requests"]
    if persisted:
        settings["allowed_destructive_requests"] = list(
            dict.fromkeys([*settings["allowed_destructive_requests"], *persisted])
        )
    blocked = permissions["blocked_requests"]
    if phase == "run" and not isinstance(data.get("scenario"), dict):
        errors.append("phase run needs the confirmed scenario object")
    for key in ("existing_tests", "spec_notes"):
        if key in data and not isinstance(data[key], list):
            errors.append(f"{key} must be a list")
    if errors:
        return None, "request_invalid", errors
    return (
        {
            "phase": phase,
            "settings": settings,
            "scenario": data.get("scenario"),
            "existing_tests": data.get("existing_tests") or [],
            "spec_notes": data.get("spec_notes") or [],
            "path": str(path),
            "config_warnings": config_warnings,
            # Carried beside the settings, never inside them.
            "blocked_requests": blocked,
        },
        None,
        [],
    )


def scrub_request_file(project_root: Path, session_id: str, values: dict[str, str]) -> list[str]:
    """Put `${NAME}` back into request.json wherever a secret value was typed out.

    The request is written by the main agent, usually by copying draft.json, and nothing
    before this point knew the real values to look for. Rewritten in place, because the
    file on disk is the leak: a run that merely ignored the literal would leave it there
    for every later reader of the session directory. Returns the names found, never values.
    """
    from exoma.core.evidence.e2e.redact import scrub_literals

    path = request_path(project_root, session_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    clean, found = scrub_literals(data, values)
    if found:
        from exoma.core.workspace.workspace_paths import atomic_write_text

        atomic_write_text(path, json.dumps(clean, indent=2, ensure_ascii=False))
    return found


def load_secrets(project_root: Path, profile: str = "") -> tuple[dict[str, str], list[str], dict]:
    """(values of the selected profile, errors, info). A missing file is no secrets, not an error.

    Errors name a location — a profile, a key, a line — and never a value: a malformed
    entry may still hold the password. An empty string is an unfilled template slot, so it
    is dropped rather than resolved to an empty credential. `info` carries the path, the
    profile names and the one selected; values never enter it.
    """
    path = secrets_path(project_root)
    info: dict = {"file": str(path), "exists": False, "profile": None, "profiles": []}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, [], info
    except OSError as exc:
        return {}, [f"{SECRETS_FILE}: unreadable ({type(exc).__name__})"], info
    info["exists"] = True
    try:
        data = json.loads(text)
    except ValueError as exc:
        return {}, [f"{SECRETS_FILE}: not valid JSON (line {getattr(exc, 'lineno', '?')})"], info
    if not isinstance(data, dict):
        return {}, [f"{SECRETS_FILE}: top level must be an object ({_SECRETS_SHAPE})"], info
    if isinstance(data.get("profiles"), dict):
        # STRICT: the pre-list shape is refused, never converted. A conversion would rewrite
        # the user's file, and the user's file is the one thing this module never writes.
        return {}, [f"{SECRETS_FILE}: `profiles` is an object (the old format, no longer read); rewrite it as {_SECRETS_SHAPE}"], info
    if not isinstance(data.get("profiles"), list):
        return {}, [f"{SECRETS_FILE}: `profiles` must be a list ({_SECRETS_SHAPE})"], info

    errors: list[str] = []
    profiles: dict[str, dict] = {}
    for position, entry in enumerate(data["profiles"], 1):
        where = f"{SECRETS_FILE}: profiles[{position - 1}]"
        if not isinstance(entry, dict):
            errors.append(f"{where}: must be an object with name and credentials")
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not _PROFILE_NAME.match(name):
            errors.append(f"{where}: name must match [A-Za-z0-9_-], at most 64 characters")
            continue
        if name in profiles:
            errors.append(f"{where}: duplicate profile name '{name}'")
            continue
        credentials = entry.get("credentials")
        if not isinstance(credentials, dict):
            errors.append(f"{SECRETS_FILE}: profile '{name}': credentials must be an object of KEY: value")
            continue
        for key_position, (key, value) in enumerate(credentials.items(), 1):
            if key not in CREDENTIAL_KEYS:
                # The key itself is not echoed: a value pasted into the key slot is the
                # mistake this message exists to report. A near-miss in case is named,
                # because the registered spelling is not a secret.
                upper = str(key).upper() if _SECRET_KEY.match(str(key)) else ""
                hint = f" (did you mean {upper}? names are case-sensitive)" if upper in CREDENTIAL_KEYS else ""
                errors.append(
                    f"{SECRETS_FILE}: profile '{name}': credential #{key_position}: not a registered name "
                    f"({', '.join(CREDENTIAL_KEYS)}){hint}"
                )
            elif not isinstance(value, str):
                errors.append(f"{SECRETS_FILE}: profile '{name}': {key} must be a string")
        profiles[name] = credentials
    info["profiles"] = list(profiles)
    default = data.get("default")
    if default is not None and (not isinstance(default, str) or default not in profiles):
        errors.append(f"{SECRETS_FILE}: default names no profile in profiles")
    if errors:
        return {}, errors, info

    chosen = profile or default or (next(iter(profiles)) if len(profiles) == 1 else None)
    if profile and profile not in profiles:
        return {}, [f"settings.secrets_profile: {SECRETS_FILE} has no profile '{profile}' (has: {', '.join(info['profiles']) or 'none'})"], info
    if chosen is None:
        if profiles:
            return {}, [f"{SECRETS_FILE}: {len(profiles)} profiles and no default; set settings.secrets_profile"], info
        return {}, [], info
    info["profile"] = chosen
    return {key: value for key, value in profiles[chosen].items() if value != ""}, [], info


_SECRETS_SHAPE = '{"default": "<profile>", "profiles": [{"name": "<profile>", "credentials": {"E2E_USER": "", "E2E_PASS": ""}}]}'


def secrets_template(profiles: list[str]) -> dict:
    """The document a new secrets.json starts as. Shape and key names come from this module;
    the only input is the profile names, validated by the caller's settings."""
    names = list(dict.fromkeys(profiles)) or ["default"]
    return {
        "default": names[0],
        "profiles": [{"name": name, "credentials": {key: "" for key in CREDENTIAL_KEYS}} for name in names],
    }


def ensure_secrets_template(project_root: Path, profiles: list[str] | None = None) -> bool:
    """Create secrets.json with empty slots when it does not exist. True if THIS call created it.

    The user fills the values; nothing here ever writes one. An existing file is never
    touched, not even to add a missing key: it is the user's, and a rewrite is how a
    hand-kept file loses what it held. That holds under concurrency too: two drafts that
    both find the file missing write their own temporary file and publish it with a hard
    link, which fails when the name already exists — so the loser, and a file the user
    saved in between, both survive. `os.replace` would overwrite either.
    """
    path = secrets_path(project_root)
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(secrets_template(profiles or ["default"]), indent=2) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{SECRETS_FILE}.", suffix=".tmp", dir=str(path.parent))
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        try:
            os.link(temp, path)
            return True
        except FileExistsError:
            return False
        except (OSError, NotImplementedError):
            # A filesystem without hard links: exclusive create still never overwrites; a
            # reader may briefly see a partly written file, which it reports as invalid JSON.
            try:
                exclusive = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                return False
            with os.fdopen(exclusive, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            return True
    finally:
        try:
            temp.unlink()
        except OSError:
            pass
