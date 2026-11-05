"""How this Exoma was installed, and how to add a package to that same environment.

Three managers put Exoma on a machine: `uv tool install`, `pipx install`, and `pip` into
some environment. uv and pipx build environments that need not carry pip, so
`python -m pip` fails there with `No module named pip` (field report: uv and pipx arms of
`--with-e2e`). The package step is chosen from the environment Exoma runs in, not from
what happens to be on PATH: an unrelated uv or pipx never changes the answer.

Stdlib only, like the rest of the runtime.
"""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

from exoma.utils.osutil import hidden_run_kwargs

# pipx writes this file at the root of every environment it creates.
PIPX_METADATA = "pipx_metadata.json"

# Methods `detect` names. `unknown`: no uv tool, no pipx environment, and no pip module.
METHODS = ("uv", "pipx", "pip", "unknown")


def uv_tool() -> str | None:
    """The uv executable when this interpreter is uv's `exoma` tool environment, else None."""
    uv = shutil.which("uv")
    if not uv:
        return None
    try:
        result = subprocess.run([uv, "tool", "dir"], capture_output=True, text=True,
                                **hidden_run_kwargs())
    except OSError:
        return None
    if result.returncode or not result.stdout.strip():
        return None
    tools_dir = Path(result.stdout.strip()).resolve()
    return uv if Path(sys.prefix).resolve() == (tools_dir / "exoma").resolve() else None


def pipx_venv(prefix: str | None = None) -> bool:
    """Whether this interpreter's environment was created by pipx."""
    return (Path(prefix or sys.prefix) / PIPX_METADATA).is_file()


def has_pip() -> bool:
    return importlib.util.find_spec("pip") is not None


def detect() -> tuple[str, str | None]:
    """(method, manager executable) for the environment this interpreter runs in.

    `("uv", <uv>)`, `("pipx", <pipx or None when it is not on PATH>)`, `("pip", None)`, or
    `("unknown", None)`. uv is asked first because it answers from its own tool directory;
    pipx is recognised by the metadata file it leaves in the environment.
    """
    uv = uv_tool()
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
