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
        # Run the guard as an old interpreter would see it.
        old = types.SimpleNamespace(version_info=(3, 9, 18), version="3.9.18 (fake)", executable="/old/python3", exit=sys.exit)
        try:
            exec(compile(match.group(0), name, "exec"), {"sys": old})
        except SystemExit as exc:
            message = str(exc.code)
            assert_true(
                "3.10 or newer" in message and "3.9.18" in message and "/old/python3" in message,
                f"{name}'s guard names the need, the version found and the interpreter: {message!r}",
            )
        else:
            raise AssertionError(f"{name}'s guard let Python 3.9 through")
        new = types.SimpleNamespace(version_info=osutil.MIN_PYTHON + (0,), version="", executable="", exit=sys.exit)
        exec(compile(match.group(0), name, "exec"), {"sys": new})


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
    finally:
        osutil.shutil.which, osutil._probe_python = real_which, real_probe

    found = osutil.find_python()
    assert_true(found is not None, "this machine runs the suite, so it has an interpreter find_python accepts")
    version = osutil._probe_python(found)
    assert_true(version is not None and version >= osutil.MIN_PYTHON, f"find_python's choice runs and is new enough: {found} {version}")


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
