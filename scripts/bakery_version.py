"""Bakery version and build identity.

BAKERY_VERSION names the release line. The build identity (package build name
and git commit) tells exactly which code an operator ran:

1. BUILD_INFO.json next to this file. It is written when a test/release package
   is built, because a package has no .git folder.
2. Otherwise `git` in the repository (developer runs); "-dirty" marks local
   uncommitted changes.
3. Otherwise the .git folder read directly (checkout without the git program).
4. Otherwise BUILD_COMMIT.txt, which GitHub "Download ZIP" fills with the commit
   (export-subst in .gitattributes).
5. Otherwise "unknown": the scripts were copied without any of these; use an
   official package.

The label is shown in the GUI title and header, in the run log banner, in every
QA_RUN_MANIFEST.json and in the export_settings layer of the QA GeoPackage.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

BAKERY_VERSION = "R01.06"
BAKERY_RELEASE = "Geometry QA integration, RoW boundary recovery, validated batch statuses"

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


def _from_git_dir() -> dict:
    """Read .git directly, for checkouts on machines without the git program."""
    for root in (_HERE, *_HERE.parents):
        g = root / ".git"
        if g.is_file():  # worktree / submodule: "gitdir: <path>"
            try:
                g = (root / g.read_text(encoding="utf-8").split(":", 1)[1].strip()).resolve()
            except Exception:
                return {}
        if not g.is_dir():
            continue
        try:
            head = (g / "HEAD").read_text(encoding="utf-8").strip()
            if head.startswith("ref:"):
                ref = head.split(":", 1)[1].strip()
                branch = ref.rsplit("/", 1)[-1]
                rp = g / ref
                if rp.is_file():
                    commit = rp.read_text(encoding="utf-8").strip()
                else:
                    commit = ""
                    pr = g / "packed-refs"
                    if pr.is_file():
                        for line in pr.read_text(encoding="utf-8").splitlines():
                            if line.endswith(" " + ref):
                                commit = line.split()[0]
                                break
            else:
                branch, commit = "", head
            if commit:
                return {"build": "git", "commit": commit, "branch": branch, "built_utc": ""}
        except Exception:
            return {}
        return {}
    return {}


def _from_archive() -> dict:
    """GitHub "Download ZIP" (git archive) fills BUILD_COMMIT.txt via export-subst."""
    p = _HERE / "BUILD_COMMIT.txt"
    try:
        parts = p.read_text(encoding="utf-8").split()
    except Exception:
        return {}
    if not parts or "$Format" in parts[0] or len(parts[0]) < 7:
        return {}
    return {"build": "github-zip", "commit": parts[0], "branch": "", "built_utc": " ".join(parts[1:])}


BUILD_INFO = (_from_build_file() or _from_git() or _from_git_dir() or _from_archive()
              or {"build": "unknown", "commit": "unknown", "branch": "", "built_utc": ""})
BAKERY_BUILD = BUILD_INFO.get("build") or "unknown"
BAKERY_COMMIT = BUILD_INFO.get("commit") or "unknown"
BAKERY_COMMIT_SHORT = BAKERY_COMMIT[:7] + ("-dirty" if BAKERY_COMMIT.endswith("-dirty") else "") if BAKERY_COMMIT != "unknown" else "unknown"


def bakery_build_label() -> str:
    """e.g. 'R01.05-test build 20261119b (commit 18eee36)'."""
    if BAKERY_BUILD in ("git", "github-zip"):
        return f"{BAKERY_VERSION} (commit {BAKERY_COMMIT_SHORT})"
    if BAKERY_BUILD == "unknown":
        return f"{BAKERY_VERSION} (build unknown - not an official package)"
    return f"{BAKERY_VERSION} build {BAKERY_BUILD} (commit {BAKERY_COMMIT_SHORT})"


def bakery_build_dict() -> dict:
    return {
        "bakery_version": BAKERY_VERSION,
        "build": BAKERY_BUILD,
        "commit": BAKERY_COMMIT,
        "branch": BUILD_INFO.get("branch", ""),
        "built_utc": BUILD_INFO.get("built_utc", ""),
    }
