"""How this Exoma was installed, and how to add a package to that same environment.

Three managers put Exoma on a machine: `uv tool install`, `pipx install`, and `pip` into
some environment. uv and pipx build environments that need not carry pip, so
`python -m pip` fails there with `No module named pip` (field report: uv and pipx arms of
`--with-e2e`). The package step is chosen from the environment Exoma runs in, not from
what happens to be on PATH: an unrelated uv or pipx never changes the answer.

Stdlib only, like the rest of the runtime.
"""

import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

from exoma.utils.osutil import run_bounded

DETECTION_TIMEOUT_SECONDS = 10


class DetectionTimeout(TimeoutError):
    """The environment's package manager could not be determined within its deadline."""


# pipx writes this file at the root of every environment it creates.
PIPX_METADATA = "pipx_metadata.json"

# Methods `detect` names. `unknown`: no usable manager, or inconclusive detection.
METHODS = ("uv", "pipx", "pip", "unknown")


def uv_tool() -> str | None:
    """The uv executable when this interpreter is uv's `exoma` tool environment, else None."""
    uv = shutil.which("uv")
    if not uv:
        return None
    try:
        code, out, _err = run_bounded([uv, "tool", "dir"], DETECTION_TIMEOUT_SECONDS)
    except OSError:
        return None
    if code is None:
        raise DetectionTimeout(
            f"uv tool dir timed out after {DETECTION_TIMEOUT_SECONDS}s; "
            "package manager not determined (no fallback to pip)"
        )
    if code or not out.strip():
        return None
    tools_dir = Path(out.strip()).resolve()
    return uv if Path(sys.prefix).resolve() == (tools_dir / "exoma").resolve() else None


def pipx_venv(prefix: str | None = None) -> bool:
    """Whether this interpreter's environment was created by pipx."""
    return (Path(prefix or sys.prefix) / PIPX_METADATA).is_file()


def has_pip() -> bool:
    return importlib.util.find_spec("pip") is not None


def detect(*, raise_on_timeout: bool = False) -> tuple[str, str | None]:
    """(method, manager executable) for the environment this interpreter runs in.

    `("uv", <uv>)`, `("pipx", <pipx or None when it is not on PATH>)`, `("pip", None)`, or
    `("unknown", None)`. uv is asked first because it answers from its own tool directory;
    pipx is recognised by the metadata file it leaves in the environment. Callers that
    report installation failures can request the original detection timeout exception.
    """
    try:
        uv = uv_tool()
    except DetectionTimeout:
        if raise_on_timeout:
            raise
        return "unknown", None
    if uv:
        return "uv", uv
    if pipx_venv():
        return "pipx", shutil.which("pipx")
    if has_pip():
        return "pip", None
    return "unknown", None


def add_command(method: str, tool: str | None, requirements: Path, packages: list[str]) -> list[str] | None:
    """argv that installs `packages` (read from `requirements`) into this environment.

    None when the method has no usable command: pipx not on PATH, or no manager at all.
    """
    if method == "uv" and tool:
        return [tool, "pip", "install", "--python", sys.executable, "-r", str(requirements)]
    if method == "pipx" and tool:
        # inject records the packages in pipx's metadata, so `pipx upgrade exoma` keeps them.
        return [tool, "inject", "exoma", *packages]
    if method == "pip":
        return [sys.executable, "-m", "pip", "install", "-r", str(requirements)]
    return None


def manual_command(method: str, packages: list[str]) -> str:
    """The command a person runs to get `packages` into this install for good."""
    specs = " ".join(packages)
    if method == "uv":
        withs = " ".join(f"--with {p}" for p in packages)
        return f"uv tool install --reinstall {withs} exoma"
    if method == "pipx":
        return f"pipx inject exoma {specs}"
    if method == "pip":
        return f"{sys.executable} -m pip install {specs}"
    return (f"{sys.executable} -m ensurepip, then {sys.executable} -m pip install {specs} "
            "(or reinstall Exoma with uv tool or pipx)")


def update_descriptor() -> dict:
    """Describe the Windows target and prove a stdlib runtime outside that target.

    This is separate from detection: existing callers keep their current contracts.
    A copied python.exe alone is not a standalone runtime.
    """
    prefix = Path(sys.prefix).resolve()
    relative = next((name for name in ("Scripts/python.exe", "python.exe") if (prefix / name).is_file()), None)
    if relative is None:
        raise ValueError("target Windows interpreter is unavailable")
    interpreter = prefix / relative
    probe = ("import sys,os,json; print(json.dumps(dict(executable=sys.executable,"
             "prefix=sys.prefix,stdlib=os.__file__,paths=sys.path,version=list(sys.version_info[:2]))))")
    candidates = [getattr(sys, "_base_executable", ""), str(Path(sys.base_prefix) / "python.exe"),
                  shutil.which("python3"), shutil.which("python")]
    for candidate in dict.fromkeys(candidates):
        if not candidate or not Path(candidate).is_file():
            continue
        executable = Path(candidate).resolve()
        if executable.is_relative_to(prefix) or executable.suffix.lower() != ".exe" or any(
                part.lower() == "windowsapps" for part in executable.parts):
            continue
        code, output, _ = run_bounded([str(executable), "-I", "-S", "-B", "-c", probe],
                                      DETECTION_TIMEOUT_SECONDS)
        try:
            info = json.loads(output)
            paths = [info["executable"], info["prefix"], info["stdlib"], *info["paths"]]
            if code != 0 or info["version"] < [3, 11] or any(
                    Path(path).resolve().is_relative_to(prefix) for path in paths if path):
                continue
            if os.path.normcase(str(executable)) != os.path.normcase(str(Path(info["executable"]).resolve())):
                continue
        except (ValueError, KeyError, TypeError):
            continue
        entrypoints = [interpreter.with_name("exoma.exe")]
        argument = Path(sys.argv[0]).resolve()
        if argument.name.lower() in ("exoma", "exoma.exe"):
            entrypoints.append(argument.with_suffix(".exe"))
        return {"prefix": str(prefix), "python_relative": relative,
                "runtime": str(executable), "entrypoints": [os.path.normcase(str(p)) for p in entrypoints]}
    raise ValueError("no verified independent Python runtime; after Exoma exits, run its package manager directly")
