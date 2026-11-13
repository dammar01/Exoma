"""Read-only release information. This is not a package-manager eligibility check."""

import importlib.metadata
import json
from pathlib import Path
import re
import urllib.request

MAX_METADATA_BYTES = 2 * 1024 * 1024
PUBLIC_METADATA_URL = "https://pypi.org/pypi/exoma/json"


def distribution_info():
    from exoma.config.settings import TOOL_VERSION

    running = Path(__file__).resolve().parent
    try:
        distribution = importlib.metadata.distribution("exoma")
        location = Path(distribution.locate_file("exoma")).resolve()
        installed = distribution.version
    except importlib.metadata.PackageNotFoundError:
        installed, location = None, None
    return {"installed": installed, "running": TOOL_VERSION,
            "distribution_path": str(location) if location else None,
            "running_path": str(running), "shadowed": location is None or location != running}


def stable_version(value):
    # Do not pretend to implement PEP 440 with a partial parser. Unknown is explicit.
    if not isinstance(value, str) or len(value) > 100 or not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*))*", value):
        return None
    parts = list(map(int, value.split(".")))
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def recognized_prerelease(value):
    """Recognize simple canonical previews, without comparing arbitrary PEP 440.

    A post-release is not a preview and must not disappear from the public result.
    Other formats remain unknown rather than being guessed or ordered as strings.
    """
    return isinstance(value, str) and len(value) <= 100 and re.fullmatch(
        r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*))*(?:a|b|rc|\.dev)[0-9]+", value) is not None


def check_release(timeout=10):
    result = distribution_info()
    request = urllib.request.Request(PUBLIC_METADATA_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read(MAX_METADATA_BYTES + 1)
    if len(payload) > MAX_METADATA_BYTES:
        raise ValueError("release metadata exceeded the size limit")
    metadata = json.loads(payload)
    if not isinstance(metadata, dict) or not isinstance(metadata.get("releases"), dict):
        raise ValueError("invalid release metadata schema")
    releases = metadata["releases"]
    published = [version for version, files in releases.items() if isinstance(files, list)
                 and any(isinstance(item, dict) and not item.get("yanked", False) for item in files)]
    available = [version for version in published if stable_version(version) is not None]
    unsupported = sorted(version for version in published
                         if stable_version(version) is None and not recognized_prerelease(version))
    if not available and not unsupported:
        raise ValueError("no stable public release in metadata")
    latest_supported = max(available, key=stable_version) if available else None
    latest = None if unsupported else latest_supported
    installed = stable_version(result["installed"])
    result.update(latest_public=latest, latest_supported_stable=latest_supported,
                  unsupported_public_versions=unsupported, comparison="unknown", source=PUBLIC_METADATA_URL,
                  advisory="Public release only; manager constraints, index, Python compatibility and resolver determine eligibility.")
    if unsupported:
        result["comparison_reason"] = "Public versions outside the supported numeric format prevent determining the latest stable release."
    elif installed is not None and not result["shadowed"]:
        newest = stable_version(latest)
        result["comparison"] = "newer_public_release" if newest > installed else "current_or_ahead"
    return result
