"""The interpreter and shell resolvers, and the Python version guard, in utils/osutil.py.

Field notes from two weeks of use: on Linux and macOS a command named `python` where only
`python3` existed, and an interpreter older than the code's syntax failing with a traceback
instead of a message. The resolvers try names by RUNNING them, and the entry points refuse
an old Python before importing anything it cannot parse.
"""

import re
import shutil
import sys
import types
from pathlib import Path

from tests.checks.support import assert_true
from exoma.utils import osutil

REPO = Path(__file__).resolve().parents[2]
_ENTRY_POINTS = ("src/exoma/cli.py", "src/exoma/install.py", "src/exoma/check.py")
_GUARD = re.compile(r"^if sys\.version_info < \((\d+), (\d+)\):\n(?:    .*\n|\n)+", re.MULTILINE)


def _guard(name: str) -> re.Match:
    text = (REPO / name).read_text(encoding="utf-8")
    match = _GUARD.search(text)
    assert_true(match is not None, f"{name} carries the Python version guard")
    return match


def _test_entry_points_refuse_an_old_python() -> None:
    for name in _ENTRY_POINTS:
        match = _guard(name)
        text = (REPO / name).read_text(encoding="utf-8")
        assert_true(
            (int(match.group(1)), int(match.group(2))) == osutil.MIN_PYTHON,
            f"{name}'s guard names osutil.MIN_PYTHON {osutil.MIN_PYTHON}: {match.group(0)!r}",
        )
        first_project_import = re.search(r"^(?:from|import) (?:exoma|config|core|installer|utils|adapters)\b", text, re.MULTILINE)
        assert_true(
            first_project_import is None or match.start() < first_project_import.start(),
            f"{name}'s guard runs before the first project import, or an old python never reaches it",
        )
        # Run the guard as the newest refused interpreter would see it: one minor below.
        below = (osutil.MIN_PYTHON[0], osutil.MIN_PYTHON[1] - 1, 20)
        found = "%d.%d.%d" % below
        old = types.SimpleNamespace(version_info=below, version=f"{found} (fake)", executable="/old/python3", exit=sys.exit)
        try:
            exec(compile(match.group(0), name, "exec"), {"sys": old})
        except SystemExit as exc:
            message = str(exc.code)
            assert_true(
                "%d.%d or newer" % osutil.MIN_PYTHON in message and found in message and "/old/python3" in message,
                f"{name}'s guard names the need, the version found and the interpreter: {message!r}",
            )
            assert_true("uv tool install" in message, f"{name}'s guard says how to get onto a supported Python: {message!r}")
        else:
            raise AssertionError(f"{name}'s guard let Python {found} through")
        new = types.SimpleNamespace(version_info=osutil.MIN_PYTHON + (0,), version="", executable="", exit=sys.exit)
        exec(compile(match.group(0), name, "exec"), {"sys": new})

    # The hook launchers repeat the minimum in their PATH probe (they cannot import osutil).
    probe = re.compile(r"sys\.version_info >= \((\d+), (\d+)\)")
    launchers = sorted((REPO / "src/exoma/_bundle/config/hosts/claude/hooks").glob("*.sh"))
    assert_true(len(launchers) >= 8, f"the hook launchers are found: {launchers}")
    for launcher in launchers:
        text = launcher.read_text(encoding="utf-8")
        tuples = {(int(a), int(b)) for a, b in probe.findall(text)}
        if not tuples:
            continue
        assert_true(tuples == {osutil.MIN_PYTHON},
                    f"{launcher.name}'s probe names osutil.MIN_PYTHON {osutil.MIN_PYTHON}: {tuples}")
        assert_true(">= %d.%d" % osutil.MIN_PYTHON in text,
                    f"{launcher.name}'s header names the same minimum as its probe")


def _test_find_python_runs_each_candidate() -> None:
    real_which, real_probe = osutil.shutil.which, osutil._probe_python
    try:
        def scenario(on_path: dict[str, str], versions: dict[str, tuple[int, int] | None]):
            osutil.shutil.which = lambda name: on_path.get(name)
            osutil._probe_python = lambda argv: versions.get(argv[0])
            return osutil.find_python()

        got = scenario({"python3": "/u/python3", "python": "/u/python"}, {"/u/python3": (3, 12), "/u/python": (3, 13)})
        assert_true(got == ["/u/python3"], f"python3 is tried first: {got}")
        got = scenario({"python": "/u/python"}, {"/u/python": (3, 11)})
        assert_true(got == ["/u/python"], f"`python` is used where python3 is absent: {got}")
        got = scenario({"python3": "/w/WindowsApps/python3", "python": "/u/python"}, {"/w/WindowsApps/python3": None, "/u/python": (3, 12)})
        assert_true(got == ["/u/python"], f"a name that does not run (the Store alias) is skipped: {got}")
        got = scenario({"python3": "/u/python3", "py": "/w/py"}, {"/u/python3": (3, 9), "/w/py": (3, 12)})
        assert_true(got == ["/w/py", "-3"], f"an old python3 is skipped for `py -3`: {got}")
        got = scenario({"python3": "/u/python3"}, {"/u/python3": (3, 8)})
        assert_true(got is None, f"no interpreter new enough is None, not the old one: {got}")
        below = (osutil.MIN_PYTHON[0], osutil.MIN_PYTHON[1] - 1)
        got = scenario({"python3": "/u/python3", "python": "/u/python"}, {"/u/python3": below, "/u/python": osutil.MIN_PYTHON})
        assert_true(got == ["/u/python"], f"one minor below MIN_PYTHON is skipped, MIN_PYTHON itself taken: {got}")
    finally:
        osutil.shutil.which, osutil._probe_python = real_which, real_probe

    found = osutil.find_python()
    assert_true(found is not None, "this machine runs the suite, so it has an interpreter find_python accepts")
    version = osutil._probe_python(found)
    assert_true(version is not None and version >= osutil.MIN_PYTHON, f"find_python's choice runs and is new enough: {found} {version}")


def _test_doctor_python_check_follows_the_launchers() -> None:
    """doctor's python check asks what the hooks ask: the pin, then python3/python/py -3.

    Field report: a clean install on macOS had no `python` alias, so doctor's old
    `shutil.which("python")` probe failed and reported NOT_READY while every launcher
    found python3 and ran.
    """
    from exoma.core.audit import diagnostics

    real_pinned, real_find, real_probe = osutil.pinned_python, osutil.find_python, osutil._probe_python
    try:
        # A pin left by an install under an older Python still runs, and the launchers take
        # it without asking its version: doctor names it and how to repin.
        osutil.pinned_python = lambda home=None: sys.executable
        osutil._probe_python = lambda argv: (osutil.MIN_PYTHON[0], osutil.MIN_PYTHON[1] - 1)
        ok, output = diagnostics.python_callable()
        assert_true(not ok and "exoma install" in output and "exoma upgrade" in output,
                    f"a pin older than MIN_PYTHON fails with the repin steps: {output}")
        osutil._probe_python = lambda argv: osutil.MIN_PYTHON
        ok, output = diagnostics.python_callable()
        assert_true(ok, f"a pin at MIN_PYTHON passes: {output}")
        osutil._probe_python = real_probe

        osutil.pinned_python = lambda home=None: None
        osutil.find_python = lambda min_version=osutil.MIN_PYTHON: [sys.executable]
        ok, output = diagnostics.python_callable()
        assert_true(ok, f"python3 alone (no `python` alias) passes doctor's check: {output}")
        assert_true(sys.executable in output, f"the output names the interpreter it ran: {output}")

        osutil.pinned_python = lambda home=None: sys.executable
        osutil.find_python = lambda min_version=osutil.MIN_PYTHON: None
        ok, output = diagnostics.python_callable()
        assert_true(ok, f"the pinned interpreter passes with nothing usable on PATH: {output}")

        osutil.pinned_python = lambda home=None: None
        ok, output = diagnostics.python_callable()
        assert_true(not ok, "no pin and no runnable candidate fails")
        assert_true("python3" in output and "py -3" in output, f"the failure names what was tried: {output}")
    finally:
        osutil.pinned_python, osutil.find_python, osutil._probe_python = real_pinned, real_find, real_probe


def _test_find_bash_and_os_locations() -> None:
    bash = osutil.find_bash()
    if osutil.IS_WINDOWS:
        if shutil.which("git"):
            assert_true(bash is not None, "with git on PATH, find_bash finds the Git Bash beside it")
        assert_true(
            bash is None or ("System32" not in bash and "WindowsApps" not in bash),
            f"on Windows find_bash is Git Bash, never WSL's or the Store alias: {bash}",
        )
    else:
        assert_true(bash == shutil.which("bash"), f"on POSIX find_bash is bash on PATH: {bash}")

    # No bash anywhere is None on both OSes, never a bare `bash` guess: the installer and
    # every tool that runs a hook or runner refuse on it.
    real_which, real_windows = osutil.shutil.which, osutil.IS_WINDOWS
    try:
        osutil.shutil.which = lambda name, *a, **k: None
        osutil.IS_WINDOWS = False
        assert_true(osutil.find_bash() is None, "POSIX without bash on PATH has no bash")
        osutil.IS_WINDOWS = True
        assert_true(osutil.find_bash() is None, "Windows without git on PATH has no Git Bash")
    finally:
        osutil.shutil.which, osutil.IS_WINDOWS = real_which, real_windows

    real_env, real_windows = dict(osutil.os.environ), osutil.IS_WINDOWS
    try:
        osutil.IS_WINDOWS = True
        osutil.os.environ.pop("LOCALAPPDATA", None)
        osutil.os.environ["APPDATA"] = "C:/Users/x/AppData/Roaming"
        assert_true(osutil.user_cache_dir() == Path("C:/Users/x/AppData/Roaming"), "Windows falls back to APPDATA")
        osutil.os.environ.pop("APPDATA", None)
        assert_true(osutil.user_cache_dir() is None, "Windows with neither variable has no cache dir")
        assert_true(osutil.user_env_location(Path("/h"))[0] == "registry", "Windows persists env in the registry")

        osutil.IS_WINDOWS = False
        osutil.os.environ["XDG_CACHE_HOME"] = "/xdg"
        assert_true(osutil.user_cache_dir() == Path("/xdg"), "POSIX honors XDG_CACHE_HOME")
        osutil.os.environ["SHELL"] = "/bin/zsh"
        assert_true(osutil.user_env_location(Path("/h")) == ("profile", str(Path("/h") / ".zshrc")), "zsh users get .zshrc")
        osutil.os.environ["SHELL"] = "/bin/bash"
        assert_true(osutil.user_env_location(Path("/h")) == ("profile", str(Path("/h") / ".bashrc")), "everyone else gets .bashrc")
        assert_true(osutil.split_command("a 'b c' d") == ["a", "b c", "d"], "POSIX quoting outside Windows")
        osutil.IS_WINDOWS = True
        assert_true(osutil.split_command(r'C:\x\y.exe -k') == [r"C:\x\y.exe", "-k"], "Windows keeps backslashes")
    finally:
        osutil.IS_WINDOWS = real_windows
        osutil.os.environ.clear()
        osutil.os.environ.update(real_env)
