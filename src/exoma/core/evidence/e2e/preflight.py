"""What has to be true before a browser run is worth starting.

Each check names the `incomplete` reason it produces, so a run that cannot happen says
why in one word the user can act on. None of this touches Playwright at import time;
`browser_available` imports it inside the call and only after `playwright_available`
said it was there.

Where a run may point and what it may write to are two different questions. Navigation
policy is about names: loopback, a `.test` name (reserved for local development, so it
cannot be a public host), or an origin the user typed into `allowed_origins` behind
`allow_remote`. Write policy treats loopback and `.test` alike — both are local
development by definition and take writes without a lookup. Every other host that may
receive a write is judged by address: it has to resolve to loopback or a private range,
and the address it resolved to here is pinned into the browser so the answer cannot
change afterwards.
"""

from __future__ import annotations

import importlib.util
import ipaddress
import re
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})
_SCHEMES = frozenset({"http", "https"})
# Reserved for local testing by RFC 6761 §6.2: `.test` never resolves on the public
# internet, so a name under it can only be something the user pointed at their own
# machine (Valet, Herd, a hosts file). It is treated exactly like localhost: no opt-in to
# open it and no lookup before writing to it. The one thing that can still point it
# elsewhere is the user's own resolver, and that is their machine to configure.
_VIRTUAL_DEV_SUFFIXES = (".test",)
# Browsers whose resolver this runtime can pin to the addresses preflight approved.
# Without pinning, the name could resolve to something else between the check and the
# write, which is the whole DNS-rebinding move.
_PINNABLE_BROWSERS = frozenset({"chromium"})
# The installer's pin (`exoma install --with-e2e`). Read, never imported: doctor reports it
# next to what is actually installed so a drifted Playwright is visible before a run misbehaves.
from exoma.config.settings import E2E_REQUIREMENTS_FILE as E2E_REQUIREMENTS  # noqa: E402


def is_loopback_host(host: str | None) -> bool:
    """localhost, *.localhost, and any loopback address (127.0.0.0/8, ::1)."""
    host = (host or "").lower().strip("[]")
    if not host:
        return False
    if host in _LOOPBACK or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_virtual_dev_host(host: str | None) -> bool:
    """A name under a suffix reserved for local development (`.test`)."""
    host = (host or "").lower().strip("[]").rstrip(".")
    return bool(host) and any(host.endswith(suffix) and len(host) > len(suffix) for suffix in _VIRTUAL_DEV_SUFFIXES)


def is_local_dev_host(host: str | None) -> bool:
    """Loopback or `.test`: the hosts that are local development by definition."""
    return is_loopback_host(host) or is_virtual_dev_host(host)


def host_addresses(host: str) -> list[str]:
    """Every address `host` resolves to right now.

    A module-level function on purpose: it is the one place this package touches DNS, so
    it is also the one place a test has to replace to describe a network it does not have.
    """
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return sorted({str(info[4][0]) for info in infos})


def address_class(address: str) -> str:
    """loopback | private | public for one address.

    `ipaddress.is_private` alone would answer "private" for loopback, link-local and a
    handful of reserved ranges, so the order matters: loopback first, then the ranges no
    app should be reachable on (link-local carries cloud metadata services), then private.
    """
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return "public"
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return "public"
    return "private" if ip.is_private else "public"


def classify_host(host: str) -> dict:
    """{"host", "addresses", "class", "detail"} — class is loopback|private|public|unresolved.

    A loopback NAME needs no lookup; anything else is judged by what it actually resolves
    to, because a hostname says nothing about where the packets go. One public address is
    enough to classify the whole name public: a name that resolves to both is a name that
    can hand the browser the public one.
    """
    if is_loopback_host(host):
        return {"host": host, "addresses": [], "class": "loopback", "detail": "loopback name"}
    try:
        addresses = host_addresses(host)
    except (OSError, UnicodeError) as exc:
        return {"host": host, "addresses": [], "class": "unresolved", "detail": f"{type(exc).__name__}: {exc}"}
    if not addresses:
        return {"host": host, "addresses": [], "class": "unresolved", "detail": "no address"}
    classes = {address: address_class(address) for address in addresses}
    public = [address for address, kind in classes.items() if kind == "public"]
    if public:
        return {"host": host, "addresses": addresses, "class": "public", "detail": f"resolves to public address {public[0]}"}
    kind = "loopback" if set(classes.values()) == {"loopback"} else "private"
    return {"host": host, "addresses": addresses, "class": kind, "detail": f"resolves to {', '.join(addresses)}"}


def _origin_hosts(config: dict) -> list[str]:
    """base_url's host first, then every allow-listed origin's, without repeats."""
    hosts: list[str] = []
    for url in [str(config.get("base_url") or ""), *[str(o) for o in config.get("allowed_origins") or []]]:
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            continue
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def write_network(config: dict) -> tuple[str | None, dict]:
    """Which hosts may receive a write this run, and why the settings are refused if they are.

    Returns `(error, decision)`. The decision is computed ONCE, here, and travels to the
    player; the guard never re-resolves a name. That is deliberate: re-checking at request
    time would ask DNS a second question and accept a second answer, which is exactly the
    rebinding this is meant to stop.

    With `allow_side_effects` false there is nothing to approve — the guard still refuses
    every write on its own.
    """
    decision: dict = {"hosts": [], "write_hosts": [], "pins": {}}
    if not config.get("allow_side_effects"):
        return None, decision
    hosts = _origin_hosts(config)
    if not hosts:
        return "settings.allow_side_effects is true but base_url names no host", decision
    for host in hosts:
        if is_virtual_dev_host(host):
            # Like localhost: approved by name, no lookup, no pin.
            decision["hosts"].append({"host": host, "addresses": [], "class": "local_dev", "detail": ".test name"})
            decision["write_hosts"].append(host)
            continue
        verdict = classify_host(host)
        decision["hosts"].append(verdict)
        if verdict["class"] in ("public", "unresolved"):
            return (
                f"settings.allow_side_effects is true but host '{host}' {verdict['detail']}: "
                "a data-changing run reaches only loopback, .test, or private addresses"
            ), decision
        decision["write_hosts"].append(host)
        # A loopback NAME resolves nowhere else, so it needs no pin. Any other name does,
        # including one that resolved to loopback just now: `127.0.0.1.nip.io` answers
        # 127.0.0.1 today and whatever its owner likes on the browser's own lookup, which
        # is the rebinding this pin exists to stop.
        if not is_loopback_host(host):
            decision["pins"][host] = verdict["addresses"][0]
    if decision["pins"]:
        browser = str(config.get("browser") or "chromium")
        if browser not in _PINNABLE_BROWSERS:
            return (
                f"settings.browser '{browser}' cannot have its resolver pinned, so "
                f"{', '.join(sorted(decision['pins']))} could resolve elsewhere between this check and the write: "
                f"use {' or '.join(sorted(_PINNABLE_BROWSERS))}, or a loopback base_url"
            ), decision
    return None, decision


# Addresses that answer as cloud instance-metadata services. Link-local covers AWS/GCP/Azure
# (169.254.169.254) and fe80::; these are the ones outside it: AWS's IPv6 endpoint is a ULA
# (so "private"), and Alibaba's sits in carrier-grade NAT space (neither private nor public
# to `ipaddress`).
_METADATA_ADDRESSES = frozenset({"fd00:ec2::254", "100.100.100.200"})

# IPv6 forms that carry an IPv4 address a translator or relay may deliver to: NAT64's
# well-known prefix (RFC 6052), its local-use range (RFC 8215) and the deprecated
# IPv4-compatible range (RFC 4291). A network-specific NAT64 prefix an operator picks for
# itself cannot be recognised from here; that residual is recorded in the v3.7.1 changelog.
_NAT64_WELL_KNOWN = ipaddress.IPv6Network("64:ff9b::/96")
_NAT64_LOCAL_USE = ipaddress.IPv6Network("64:ff9b:1::/48")
_IPV4_COMPATIBLE = ipaddress.IPv6Network("::/96")

# Where RFC 6052 section 2.2 puts the IPv4 bytes for each prefix length; byte 8 is the
# reserved "u" octet and never carries address bits.
_RFC6052_IPV4_BYTES = {
    32: (4, 5, 6, 7),
    40: (5, 6, 7, 9),
    48: (6, 7, 9, 10),
    56: (7, 9, 10, 11),
    64: (9, 10, 11, 12),
    96: (12, 13, 14, 15),
}


def _rfc6052_ipv4(ip: ipaddress.IPv6Address, prefix_length: int) -> ipaddress.IPv4Address:
    packed = ip.packed
    return ipaddress.IPv4Address(bytes(packed[i] for i in _RFC6052_IPV4_BYTES[prefix_length]))


def _nat64_local_use_blocked(ip: ipaddress.IPv6Address) -> str | None:
    """A local-use NAT64 address that lands on metadata under any prefix length it allows.

    Inside `64:ff9b:1::/48` the operator picks the prefix length, and with it where the
    IPv4 bytes sit, so every layout RFC 6052 allows there is decoded. Only metadata and
    link-local results refuse: a zero run decodes as 0.0.0.0 under the layouts that do not
    apply, and refusing on that would turn every ordinary NAT64 target away.
    """
    if ip not in _NAT64_LOCAL_USE:
        return None
    for prefix_length in (48, 56, 64, 96):
        embedded = _rfc6052_ipv4(ip, prefix_length)
        if str(embedded) in _METADATA_ADDRESSES:
            return "a cloud metadata address"
        if embedded.is_link_local:
            return "link-local (where cloud metadata services answer)"
    return None


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address an IPv6 address stands in for, or None.

    `::ffff:169.254.169.254`, `64:ff9b::a9fe:a9fe`, `::a9fe:a9fe` and `2002:a9fe:a9fe::`
    can all end at 169.254.169.254, and before 3.13 `ipaddress` judged them by their IPv6
    bits alone. `::1` and `::` sit inside the IPv4-compatible range but are loopback and
    unspecified in their own right, so they are left as they are.
    """
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip in _NAT64_WELL_KNOWN or (ip in _IPV4_COMPATIBLE and not (ip.is_loopback or ip.is_unspecified)):
        return _rfc6052_ipv4(ip, 96)
    return None


def _read_blocked(address: str) -> str | None:
    """Why the browser may not be pointed at `address` at all, or None."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address):
        reason = _nat64_local_use_blocked(ip)
        if reason:
            return reason
        ip = _embedded_ipv4(ip) or ip
    if str(ip) in _METADATA_ADDRESSES:
        return "a cloud metadata address"
    if ip.is_link_local:
        return "link-local (where cloud metadata services answer)"
    if ip.is_unspecified or ip.is_multicast:
        return "unspecified or multicast"
    return None


def read_network(config: dict) -> tuple[str | None, list[dict]]:
    """Refuse a remote origin that resolves somewhere no browser run should read from.

    `allow_remote` + `allowed_origins` is the opt-in to reach another host, and it came
    from config.json, not from the agent-written request. But the opt-in names a HOST, and
    a host is only a name: one that resolves to 169.254.169.254 turns an allow-listed
    staging URL into a read of the machine's cloud credentials. Writes were already judged
    by address (write_network); reads now are too. Private addresses stay allowed here —
    an internal staging server is the ordinary reason to set allow_remote — and loopback
    and `.test` names need no lookup. A name that does not resolve is left to the
    reachability check, which reports it in the user's terms.
    """
    verdicts: list[dict] = []
    for host in _origin_hosts(config):
        if is_local_dev_host(host):
            continue
        verdict = classify_host(host)
        verdicts.append(verdict)
        for address in verdict["addresses"]:
            why = _read_blocked(address)
            if why:
                return (
                    f"origin host '{host}' resolves to {address}, {why}: a browser run never "
                    "reads from it, allow-listed or not"
                ), verdicts
        literal = _read_blocked(host)
        if literal:
            return f"origin host '{host}' is {literal}: a browser run never reads from it", verdicts
    return None, verdicts


def display_available() -> bool:
    """Whether a headed browser has a screen to open on.

    Only Linux can lack one in the normal case: Windows and macOS always have a session a
    window can open in, while a Linux box without X11 or Wayland (CI, a container, SSH)
    fails the launch outright. A module-level function so tests can describe a machine
    they are not running on; the OS rule itself is osutil.has_display().
    """
    from exoma.utils import osutil

    return osutil.has_display()


def safe_base_url(url: str, config: dict) -> tuple[bool, str]:
    """Local by default. A `.test` name is local by convention. Anything else needs
    `allow_remote` AND an allowed origin — the deliberate, typed-out opt-in."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False, f"base_url '{url}' does not parse"
    if parts.scheme not in _SCHEMES or not parts.hostname:
        return False, f"base_url '{url}' must be http(s)://host[:port]"
    host = parts.hostname.lower()
    if is_loopback_host(host):
        return True, "loopback"
    if is_virtual_dev_host(host):
        return True, "virtual dev host"
    if not config.get("allow_remote"):
        return False, f"base_url host '{host}' is not loopback or a .test name, and config.json e2e.allow_remote is false"
    origin = f"{parts.scheme}://{parts.netloc}".lower()
    allowed = {str(o).lower().rstrip("/") for o in config.get("allowed_origins") or []}
    if origin not in allowed:
        return False, f"origin '{origin}' is not in config.json e2e.allowed_origins"
    return True, "remote origin allow-listed"


def same_origin(url: str, base_url: str) -> bool:
    try:
        a, b = urlsplit(url), urlsplit(base_url)
    except ValueError:
        return False
    return (a.scheme, a.netloc.lower()) == (b.scheme, b.netloc.lower())


def playwright_available() -> tuple[bool, str]:
    try:
        spec = importlib.util.find_spec("playwright")
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        return False, "python package 'playwright' is not installed (pip install playwright)"
    return True, "playwright importable"


def browser_available(browser: str) -> tuple[bool, str]:
    """Whether the named browser's binary is provisioned for this Playwright install."""
    try:
        from playwright.sync_api import sync_playwright  # lazy: optional dependency
    except ImportError as exc:
        return False, f"playwright.sync_api import failed: {exc}"
    try:
        with sync_playwright() as pw:
            kind = getattr(pw, browser, None)
            if kind is None:
                return False, f"browser '{browser}' is not a Playwright browser type"
            path = getattr(kind, "executable_path", None)
            if path and not Path(str(path)).exists():
                return False, f"{browser} binary missing at {path} (playwright install {browser})"
    except Exception as exc:  # any driver failure is a provisioning failure here
        return False, f"{type(exc).__name__}: {exc}"
    return True, f"{browser} provisioned"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Report a redirect instead of following it.

    urlopen followed redirects to any host, so an allow-listed origin answering 302 to
    http://169.254.169.254/ made this probe fetch the metadata service itself. A redirect
    is already proof that a server answered, which is all this check asks.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_PROBE_OPENER = urllib.request.build_opener(_NoRedirect)


def base_url_reachable(url: str, timeout_s: float = 3.0) -> tuple[bool, str]:
    """Any HTTP response counts — a 404 is a server, no server is the failure."""
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "exoma-e2e-preflight"})
    try:
        with _PROBE_OPENER.open(request, timeout=timeout_s) as response:  # noqa: S310 - loopback/allow-listed only, redirects not followed
            return True, f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return True, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}"


def pinned_playwright() -> str | None:
    try:
        text = E2E_REQUIREMENTS.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        match = re.match(r"^\s*playwright\s*==\s*([\w.]+)\s*$", line)
        if match:
            return match.group(1)
    return None


def installed_playwright() -> str | None:
    try:
        from importlib.metadata import version

        return version("playwright")
    except Exception:  # PackageNotFoundError, or a broken dist-info: both mean "not usable"
        return None


def readiness() -> dict:
    """What `doctor` reports about /.verify-browser. Metadata only, never blocking.

    The browser binary and the app URL are checked by each run's own preflight, against
    the settings that run was given: doctor has no URL to check, and starting a Playwright
    driver here would slow doctor down for every project that never opens a browser.
    """
    python = sys.executable
    installed = installed_playwright()
    pinned = pinned_playwright()
    out = {
        "python": python,
        "playwright_installed": installed,
        "playwright_pinned": pinned,
        "detail": "installed; browser and base URL are checked by each /.verify-browser run",
        "fix": None,
        "blocking": False,
    }
    if not installed:
        out["detail"] = (
            "playwright is not installed; /.verify-browser stops at preflight with playwright_missing "
            f"until `python install.py --apply --with-e2e` installs it into {python}"
        )
    elif pinned and installed != pinned:
        out["fix"] = (
            f"playwright {installed} is installed but {pinned} is pinned; "
            "rerun `python install.py --apply --with-e2e` if browser runs misbehave"
        )
    return out


def preflight(config: dict, *, fake: bool = False) -> dict:
    """Run every check in order; stop at the first failure.

    Returns {"ok", "reason", "detail", "checks": [{"name", "ok", "detail"}]}. With
    `fake=True` (a fake player under test) the dependency and URL checks are skipped:
    the URL policy still applies, because it is a policy, not an environment fact.
    """
    checks: list[dict] = []

    def fail(reason: str, detail: str) -> dict:
        checks.append({"name": reason, "ok": False, "detail": detail})
        return {"ok": False, "reason": reason, "detail": detail, "checks": checks}

    ok, detail = safe_base_url(str(config.get("base_url") or ""), config)
    checks.append({"name": "base_url_policy", "ok": ok, "detail": detail})
    if not ok:
        return {"ok": False, "reason": "spec_invalid", "detail": detail, "checks": checks}
    blocked, read_hosts = read_network(config)
    checks.append(
        {
            "name": "read_policy",
            "ok": blocked is None,
            "detail": blocked or (f"{len(read_hosts)} remote host(s) resolved outside link-local/metadata" if read_hosts else "no remote origins"),
        }
    )
    if blocked:
        return {"ok": False, "reason": "spec_invalid", "detail": blocked, "checks": checks}
    refused, network = write_network(config)
    detail = refused or (
        f"writes pinned to {', '.join(f'{h}={ip}' for h, ip in sorted(network['pins'].items()))}"
        if network["pins"]
        else "no writes, or writes to a loopback app"
    )
    checks.append({"name": "write_policy", "ok": refused is None, "detail": detail})
    if refused:
        return {"ok": False, "reason": "spec_invalid", "detail": refused, "checks": checks, "network": network}
    if fake:
        return {"ok": True, "reason": None, "detail": "fake player: dependency checks skipped", "checks": checks, "network": network}

    ok, detail = playwright_available()
    if not ok:
        return fail("playwright_missing", detail)
    checks.append({"name": "playwright", "ok": True, "detail": detail})

    ok, detail = browser_available(str(config.get("browser") or "chromium"))
    if not ok:
        return fail("browser_missing", detail)
    checks.append({"name": "browser", "ok": True, "detail": detail})

    ok, detail = base_url_reachable(str(config.get("base_url")))
    if not ok:
        return fail("base_url_unreachable", detail)
    checks.append({"name": "base_url", "ok": True, "detail": detail})
    return {"ok": True, "reason": None, "detail": "ready", "checks": checks, "network": network}
