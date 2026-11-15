"""Package specs such as `apt:jq`, `npm:eslint@9.12.0`, `pip:pyflakes==3.2.0`: parsing and validation only (nothing is installed here).

A registry class installs software for real, so npm and pip entries must name an exact version: "latest" would let whoever controls the
package decide what runs tomorrow. apt entries are exempt because the distribution archive is signed and drops old versions, so an apt
pin would stop installing after a security update.
"""
import re

PKG_RES = {
    "apt": re.compile(r"[a-z0-9][a-z0-9+.-]{1,60}"),
    "npm": re.compile(r"(?:@[a-z0-9][a-z0-9._-]{0,60}/)?[a-z0-9][a-z0-9._-]{0,80}(?:@[0-9A-Za-z][0-9A-Za-z.+-]{0,40})?"),
    "pip": re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}(?:==[0-9A-Za-z][0-9A-Za-z.+!-]{0,40})?"),
}
PIN_RES = {"npm": re.compile(r"^(?:@[^/@]+/)?[^@/]+@\d+(?:\.\d+){0,2}(?:[-+][0-9A-Za-z.+-]+)?$"),
           "pip": re.compile(r"^[^=]+==\d[0-9A-Za-z.+!-]*$")}
MAX_PACKAGES = 8


def check_packages(value):
    """Validate a `packages` list: at most 8 entries like 'apt:jq', 'npm:eslint', 'pip:pyflakes'. Returns a sorted, unique list."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_PACKAGES:
        raise ValueError(f"packages must be a list of at most {MAX_PACKAGES} entries like 'apt:jq', 'npm:eslint', 'pip:pyflakes'")
    out = set()
    for p in value:
        mgr, sep, name = p.partition(":") if isinstance(p, str) else ("", "", "")
        if not sep or mgr not in PKG_RES or not PKG_RES[mgr].fullmatch(name):
            raise ValueError(f"bad package {p!r}: use apt:<name>, npm:<name>[@version] or pip:<name>[==version]")
        out.add(f"{mgr}:{name}")
    return sorted(out)


def split_pkgs(pkgs):
    """['apt:jq', 'pip:x==1'] -> {'apt': ['jq'], 'pip': ['x==1']}"""
    by = {}
    for p in pkgs:
        mgr, _, name = p.partition(":")
        by.setdefault(mgr, []).append(name)
    return by


def is_pinned(entry):
    mgr, _, name = entry.partition(":")
    rx = PIN_RES.get(mgr)
    return True if rx is None else bool(rx.match(name))


def npm_split(name):
    """'@scope/pkg@1.2.3' -> ['@scope/pkg', '1.2.3']; an unpinned name gives an empty version."""
    return name.rsplit("@", 1) if "@" in name[1:] else [name, ""]


def pip_norm(name):
    return re.sub(r"[-_.]+", "_", name).lower()
