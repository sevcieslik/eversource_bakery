#!/usr/bin/env python3
"""Independent Geometry QA Inspector GUI for previously processed Bakery projects.

This tool is read-only. It never launches Preflight, Geometry or re-export.
The selected legacy output must already contain the Geometry GPKG and the
45/80/267 deliverables; otherwise the engine cannot perform full QA v0.3.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG = SCRIPT_DIR / "qa_inspector_workspace.json"
RUNNER = SCRIPT_DIR / "05_run_geometry_qa.py"


def read_config():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class InspectorWindow(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Eversource QA Inspector v0.3 - Existing Geometry")
        self.geometry("930x700")
        self.minsize(740, 570)
        self.queue = queue.Queue()
        self.process = None
        self.worker = None
        self.config_data = read_config()
        self.inputs = {
            "input_dir": tk.StringVar(value=self.config_data.get("input_dir", str(ROOT / "input"))),
            "source_dir": tk.StringVar(value=self.config_data.get("source_dir", str(ROOT / "source"))),
            "output_root": tk.StringVar(value=self.config_data.get("output_root", str(ROOT / "output"))),
        }
        self.status = tk.StringVar(value="Ready - select an existing Bakery run")
        self._build()
        self.after(100, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _build(self):
        base = ttk.Frame(self, padding=18)
        base.pack(fill="both", expand=True)
        ttk.Label(base, text="Geometry QA Inspector", font=("Segoe UI", 19, "bold")).pack(anchor="w")
        ttk.Label(base, text="Validate an existing Bakery output. No Preflight or Geometry processing is performed.",
                  wraplength=850).pack(anchor="w", pady=(3, 15))
        fields = [
            ("Original input folder (Wires and POA DXF)", "input_dir"),
            ("Original source folder (Structures + SoW SQLite)", "source_dir"),
            ("Existing output folder (GPKG + deliverables)", "output_root"),
        ]
        for title, key in fields:
            ttk.Label(base, text=title).pack(anchor="w", pady=(7, 2))
            row = ttk.Frame(base)
            row.pack(fill="x")
            ttk.Entry(row, textvariable=self.inputs[key]).pack(side="left", fill="x", expand=True)
            ttk.Button(row, text="Browse...", command=lambda k=key: self._browse(k)).pack(side="left", padx=(8, 0))
        ttk.Label(
            base,
            text="Required output: 01_geometry/01_eversource_geometry.gpkg plus deliverables/45, 80 and 267. "
                 "Old Shapefiles alone cannot be fully checked by this version.",
            wraplength=850, foreground="#72572d",
        ).pack(anchor="w", pady=(15, 8))

        actions = ttk.Frame(base)
        actions.pack(fill="x", pady=(7, 12))
        self.run_button = ttk.Button(actions, text="Run Geometry QA", command=self._run)
        self.run_button.pack(side="left")
        ttk.Button(actions, text="Open QA Report", command=self._open_report).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Open QA Folder", command=self._open_folder).pack(side="left", padx=(8, 0))
        ttk.Label(base, textvariable=self.status).pack(anchor="w", pady=(0, 8))

        log_frame = ttk.Frame(base)
        log_frame.pack(fill="both", expand=True)
        scrollbar = ttk.Scrollbar(log_frame)
        scrollbar.pack(side="right", fill="y")
        self.log = tk.Text(log_frame, wrap="word", state="disabled", bg="#30231f", fg="#f2e9df",
                           font=("Consolas", 9), yscrollcommand=scrollbar.set)
        self.log.pack(fill="both", expand=True)
        scrollbar.configure(command=self.log.yview)

    def _browse(self, key):
        old = self.inputs[key].get()
        chosen = filedialog.askdirectory(parent=self, initialdir=old if Path(old).is_dir() else str(ROOT))
        if chosen:
            self.inputs[key].set(chosen)

    def _append(self, line):
        self.log.configure(state="normal")
        self.log.insert("end", line.rstrip("\n") + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _paths(self):
        paths = {k: Path(v.get().strip()).expanduser() for k, v in self.inputs.items()}
        for key, p in paths.items():
            if not p.is_dir():
                raise ValueError(f"{key}: folder does not exist: {p}")
        gpkg = paths["output_root"] / "01_geometry" / "01_eversource_geometry.gpkg"
        if not gpkg.is_file():
            raise ValueError(
                f"Existing Bakery Geometry GPKG not found:\n{gpkg}\n\n"
                "The full QA Inspector requires the GPKG; source and Shapefiles alone are not supported."
            )
        if not (paths["output_root"] / "deliverables").is_dir():
            raise ValueError("Existing output has no deliverables/ folder.")
        if not RUNNER.is_file():
            raise ValueError(f"QA launcher missing: {RUNNER}")
        if not (SCRIPT_DIR / "qa_inspector.py").is_file():
            raise ValueError("Bundled qa_inspector.py missing from scripts/.")
        return paths

    def _run(self):
        if self.worker is not None and self.worker.is_alive():
            return
        try:
            paths = self._paths()
        except ValueError as exc:
            messagebox.showerror("Cannot run Geometry QA", str(exc), parent=self)
            return
        self.config_data = {k: str(p) for k, p in paths.items()}
        CONFIG.write_text(json.dumps(self.config_data, indent=2), encoding="utf-8")
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.status.set("Geometry QA running")
        self.run_button.configure(state="disabled")
        self._append("Independent validation - existing Bakery outputs only.")
        self._append("Output: " + str(paths["output_root"] / "QA" / "geometry_validation"))
        command = [sys.executable, str(RUNNER),
                   "--input-dir", str(paths["input_dir"]),
                   "--source-dir", str(paths["source_dir"]),
                   "--output-root", str(paths["output_root"])]
        self.worker = threading.Thread(target=self._execute, args=(command, paths["output_root"]), daemon=True)
        self.worker.start()

    def _execute(self, command, out):
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            self.process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, errors="replace", bufsize=1, creationflags=flags
            )
            assert self.process.stdout is not None
            for line in self.process.stdout:
                self.queue.put(("line", line))
            return_code = self.process.wait()
            report = out / "QA" / "geometry_validation" / "QA_Report.html"
            if return_code == 0 and report.is_file():
                self.queue.put(("success", str(report)))
            else:
                self.queue.put(("failure", f"QA returned exit code {return_code}. Check the log and QA_Execution.log."))
        except Exception as exc:
            self.queue.put(("failure", f"{type(exc).__name__}: {exc}"))
        finally:
            self.process = None

    def _drain(self):
        try:
            while True:
                event, data = self.queue.get_nowait()
                if event == "line":
                    self._append(data)
                elif event == "success":
                    self.status.set("QA complete - operator review required")
                    self.run_button.configure(state="normal")
                    webbrowser.open(Path(data).resolve().as_uri())
                elif event == "failure":
                    self.status.set("Geometry QA failed")
                    self.run_button.configure(state="normal")
                    self._append("ERROR: " + data)
                    messagebox.showerror("Geometry QA", data, parent=self)
        except queue.Empty:
            pass
        self.after(100, self._drain)

    def _report_path(self):
        return Path(self.inputs["output_root"].get().strip()) / "QA" / "geometry_validation" / "QA_Report.html"

    def _open_report(self):
        report = self._report_path()
        if report.is_file():
            webbrowser.open(report.resolve().as_uri())
        else:
            messagebox.showinfo("QA report", "No successful report exists for the selected output folder.", parent=self)

    def _open_folder(self):
        folder = self._report_path().parent
        if not folder.is_dir():
            messagebox.showinfo("QA results", "No QA results folder exists yet.", parent=self)
            return
        if os.name == "nt":
            os.startfile(str(folder))
        else:
            subprocess.Popen(["xdg-open", str(folder)])

    def _close(self):
        if self.worker is not None and self.worker.is_alive():
            if not messagebox.askyesno("Exit QA Inspector", "Validation is running. Stop it and exit?", parent=self):
                return
            if self.process and self.process.poll() is None:
                self.process.terminate()
        self.destroy()


if __name__ == "__main__":
    InspectorWindow().mainloop()
