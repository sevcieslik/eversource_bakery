from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_NAME = "workspace_config.json"


def _clean_user_path(raw: str) -> Path:
    s = str(raw or "").strip().strip('"').strip("'")
    return Path(os.path.expandvars(os.path.expanduser(s))).resolve()


def _pick_folder(title: str, initial: Path | None = None) -> Path | None:
    try:
        import tkinter as tk
        from tkinter import filedialog

        app = tk.Tk()
        app.withdraw()
        app.attributes("-topmost", True)
        chosen = filedialog.askdirectory(
            title=title,
            initialdir=str(initial) if initial and initial.exists() else None,
            mustexist=True,
        )
        app.destroy()
        if chosen:
            return Path(chosen).resolve()
    except Exception:
        pass
    return None


def _load_last(tool_root: Path) -> dict:
    path = Path(tool_root) / CONFIG_NAME
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    out = {}
    for key in ("input_dir", "output_root"):
        val = raw.get(key)
        if not val:
            continue
        try:
            out[key] = str(_clean_user_path(val))
        except Exception:
            pass
    return out


def _save_last(tool_root: Path, input_dir: Path, output_root: Path) -> None:
    path = Path(tool_root) / CONFIG_NAME
    path.write_text(
        json.dumps(
            {
                "input_dir": str(Path(input_dir).resolve()),
                "output_root": str(Path(output_root).resolve()),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _choose_existing_input(last: Path | None) -> Path:
    while True:
        print("\nPROJECT INPUT")
        if last is not None:
            print(f"Last input: {last}")
            raw = input(
                "Press Enter to use last input, type a new path, or B to browse: "
            ).strip()
            if raw == "":
                if last.is_dir():
                    return last
                print("Last input folder no longer exists.")
                last = None
                continue
        else:
            raw = input("Enter input folder path, or B to browse: ").strip()

        if raw.upper() == "B":
            picked = _pick_folder("Select project input folder", last)
            if picked is not None and picked.is_dir():
                return picked
            print("No folder selected.")
            continue

        p = _clean_user_path(raw)
        if p.is_dir():
            return p
        print(f"Input folder does not exist: {p}")


def _choose_output(last: Path | None) -> Path:
    while True:
        print("\nPROJECT OUTPUT")
        if last is not None:
            print(f"Last output: {last}")
            raw = input(
                "Press Enter to use last output, type a new path, or B to browse: "
            ).strip()
            if raw == "":
                try:
                    last.mkdir(parents=True, exist_ok=True)
                    return last
                except Exception as exc:
                    print(f"Could not use last output: {exc}")
                    last = None
                    continue
        else:
            raw = input("Enter output folder path, or B to browse: ").strip()

        if raw.upper() == "B":
            picked = _pick_folder("Select project output folder", last)
            if picked is not None:
                try:
                    picked.mkdir(parents=True, exist_ok=True)
                    return picked
                except Exception as exc:
                    print(f"Could not use selected output: {exc}")
                    continue
            print("No folder selected.")
            continue

        p = _clean_user_path(raw)
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception as exc:
            print(f"Could not create/use output folder {p}: {exc}")


def choose_workspace(tool_root: Path) -> dict | None:
    """Prompt for input/output every run. Source is fixed inside the tool."""
    tool_root = Path(tool_root).resolve()
    source_dir = tool_root / "source"

    if not source_dir.is_dir():
        raise RuntimeError(f"Missing fixed source folder: {source_dir}")

    last_cfg = _load_last(tool_root)
    last_input = (
        Path(last_cfg["input_dir"])
        if last_cfg.get("input_dir")
        else None
    )
    last_output = (
        Path(last_cfg["output_root"])
        if last_cfg.get("output_root")
        else None
    )

    print("\n" + "=" * 80)
    print("PROJECT WORKSPACE")
    print("=" * 80)
    print(f"Fixed source: {source_dir}")
    print("Input and output are confirmed on every run.")

    input_dir = _choose_existing_input(last_input)
    output_root = _choose_output(last_output)

    _save_last(tool_root, input_dir, output_root)

    print("\nRun paths:")
    print(f"  Input:  {input_dir}")
    print(f"  Source: {source_dir}")
    print(f"  Output: {output_root}")

    return {
        "input_dir": str(input_dir),
        "source_dir": str(source_dir),
        "output_root": str(output_root),
    }
