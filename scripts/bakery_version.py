"""Bakery version and build identity.

BAKERY_VERSION names the release line. The build identity (package build name
and git commit) tells exactly which code an operator ran:

1. BUILD_INFO.json next to this file. It is written when a test/release package
   is built, because a package has no .git folder.
2. Otherwise `git` in the repository (developer runs); "-dirty" marks local
   uncommitted changes.
3. Otherwise "unknown".

The label is shown in the GUI title and header, in the run log banner, in every
QA_RUN_MANIFEST.json and in the export_settings layer of the QA GeoPackage.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

BAKERY_VERSION = "R01.05-test"
BAKERY_RELEASE = "2D central phase, circuit-named levels, duplicate DXF wires"

_HERE = Path(__file__).resolve().parent


def _from_build_file() -> dict:
    p = _HERE / "BUILD_INFO.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return {k: str(d.get(k, "") or "") for k in ("build", "commit", "branch", "built_utc")}
    except Exception:
        return {}


def _from_git() -> dict:
    def git(*args):
        kw = {}
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        return subprocess.run(
            ["git", *args], cwd=_HERE, capture_output=True, text=True, timeout=5, **kw
        ).stdout.strip()

    try:
        commit = git("rev-parse", "HEAD")
        if not commit:
            return {}
        branch = git("rev-parse", "--abbrev-ref", "HEAD")
        dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
        return {"build": "git", "commit": commit + ("-dirty" if dirty else ""), "branch": branch, "built_utc": ""}
    except Exception:
        return {}


BUILD_INFO = _from_build_file() or _from_git() or {"build": "unknown", "commit": "unknown", "branch": "", "built_utc": ""}
BAKERY_BUILD = BUILD_INFO.get("build") or "unknown"
BAKERY_COMMIT = BUILD_INFO.get("commit") or "unknown"
BAKERY_COMMIT_SHORT = BAKERY_COMMIT[:7] + ("-dirty" if BAKERY_COMMIT.endswith("-dirty") else "") if BAKERY_COMMIT != "unknown" else "unknown"


def bakery_build_label() -> str:
    """e.g. 'R01.05-test build 20261119b (commit 18eee36)'."""
    if BAKERY_BUILD == "git":
        return f"{BAKERY_VERSION} (commit {BAKERY_COMMIT_SHORT})"
    return f"{BAKERY_VERSION} build {BAKERY_BUILD} (commit {BAKERY_COMMIT_SHORT})"


def bakery_build_dict() -> dict:
    return {
        "bakery_version": BAKERY_VERSION,
        "build": BAKERY_BUILD,
        "commit": BAKERY_COMMIT,
        "branch": BUILD_INFO.get("branch", ""),
        "built_utc": BUILD_INFO.get("built_utc", ""),
    }
