#!/usr/bin/env python3
from __future__ import annotations
from bakery_version import BAKERY_VERSION, BAKERY_RELEASE

import csv
import json
import os
import queue
import re
import subprocess
import sys
import threading
import psutil
import time
import webbrowser
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from pyproj import CRS

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
PY = sys.executable
SOURCE = ROOT / "source"
CONFIG = SCRIPT_DIR / "workspace_config.json"

BG = "#f4eadc"
PANEL = "#fff8ed"
PANEL_2 = "#f8efe3"
TEXT = "#3b251d"
MUTED = "#7a675d"
ACCENT = "#bd5d32"
ACCENT_DARK = "#8d4024"
GREEN = "#59864c"
GREEN_BG = "#e7f0df"
LINE = "#ddc9b6"
LOG_BG = "#30231f"
LOG_FG = "#f2e9df"

PRODUCTS = [
    ("geometry", "Geometry", "2D wires, Structures & Caydence"),
    ("lidar", "LiDAR", "AOI-clipped LAS"),
    ("pls", "PLS XYZ", "15 feature XYZ outputs"),
    ("tile_index", "Tile Index", "Delivery tile grid"),
    ("management_units", "Management Units", "Client MU attribution"),
]

CRS_OPTIONS = {
    "Connecticut — EPSG:6434": 6434,
    "Massachusetts Mainland — EPSG:6492": 6492,
    "New Hampshire — EPSG:6525": 6525,
    "Other EPSG": None,
}


EPSG_TO_CRS_LABEL = {
    int(code): label
    for label, code in CRS_OPTIONS.items()
    if code is not None
}


SUPPORTED_PROJECT_EPSGS = (6434, 6492, 6525)

_CRS_NAME_ALIASES = {
    # Connecticut
    "NSRS_2011_CONNECTICUT_ZONE_US_FOOT": 6434,
    "NAD_1983_2011_STATEPLANE_CONNECTICUT_FIPS_0600_FT_US": 6434,

    # Massachusetts Mainland
    "NSRS_2011_MASSACHUSETTS_STATE_PLANES_MAINLAND_ZONE_US_FOOT": 6492,
    "NAD_1983_2011_STATEPLANE_MASSACHUSETTS_MAINLAND_FIPS_2001_FT_US": 6492,

    # New Hampshire
    "NSRS_2011_NEW_HAMPSHIRE_ZONE_US_FOOT": 6525,
    "NAD_1983_2011_STATEPLANE_NEW_HAMPSHIRE_FIPS_2800_FT_US": 6525,
}


def _normalise_crs_name(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", str(value).upper()).strip("_")


def resolve_project_epsg(crs: CRS) -> int | None:
    """Resolve a project horizontal CRS robustly from ESRI/WKT metadata.

    Order:
      1. normal EPSG/authority lookup;
      2. direct equivalence against supported project CRSs;
      3. normalised projection-name aliases for known ESRI WKT variants.

    The alias step is deliberately limited to the project-supported CRSs, so the
    tool does not guess arbitrary EPSG codes from names.
    """
    # Normal authority matching.
    try:
        epsg = crs.to_epsg()
        if epsg is not None:
            return int(epsg)
    except Exception:
        pass

    try:
        auth = crs.to_authority()
        if auth and str(auth[0]).upper() == "EPSG":
            return int(auth[1])
    except Exception:
        pass

    # Some ESRI WKT1 definitions only match below pyproj's default 70%
    # confidence threshold.

    # Compare projection semantics rather than requiring an AUTHORITY tag.
    for candidate in SUPPORTED_PROJECT_EPSGS:
        try:
            target = CRS.from_epsg(candidate)
            if crs.equals(target, ignore_axis_order=True):
                return int(candidate)
        except Exception:
            continue

    # Known ArcGIS/ESRI naming variants seen in client .prj files.
    names = []
    try:
        names.append(crs.name)
    except Exception:
        pass
    try:
        names.append(crs.coordinate_operation.name)
    except Exception:
        pass

    # Also inspect the raw WKT names because projected CRS and operation names
    # can differ across WKT1_ESRI / WKT2 serialisations.
    try:
        wkt = crs.to_wkt()
        names.extend(re.findall(r'(?:PROJCS|PROJCRS)\["([^"]+)"', wkt))
    except Exception:
        pass

    for name in names:
        key = _normalise_crs_name(name)
        if key in _CRS_NAME_ALIASES:
            return int(_CRS_NAME_ALIASES[key])

    return None


def detect_aoi_epsg(input_dir: Path):
    """Detect one horizontal EPSG from published AOI Polygon .prj files.

    Returns:
        (epsg, prj_files) when a single consistent CRS is resolved;
        (None, []) when no published Polygon AOI .prj exists.

    Raises ValueError when AOI projection metadata exists but is invalid or
    inconsistent. In that case the GUI must not guess a CRS.
    """
    aoi_dir = Path(input_dir) / "AOI"
    if not aoi_dir.is_dir():
        return None, []

    # Match the same published Polygon convention used by preflight.
    prjs = [
        p for p in sorted(aoi_dir.rglob("*.prj"))
        if re.search(r"(?i)(?:^|[_ -])polygon(?:$|[_ -])", p.stem)
        or p.stem.lower().endswith("polygon")
    ]
    if not prjs:
        return None, []

    resolved = []
    failures = []
    for prj in prjs:
        try:
            wkt = prj.read_text(encoding="utf-8", errors="ignore").strip()
            if not wkt:
                raise ValueError("empty .prj")
            crs = CRS.from_wkt(wkt)
            epsg = resolve_project_epsg(crs)
            if epsg is None:
                raise ValueError(
                    f"could not resolve supported project EPSG from {crs.name}"
                )
            resolved.append((prj, int(epsg)))
        except Exception as exc:
            failures.append(f"{prj.name}: {exc}")

    if failures:
        raise ValueError(
            "Could not read AOI projection metadata: " + "; ".join(failures)
        )

    unique = sorted({epsg for _, epsg in resolved})
    if len(unique) != 1:
        detail = ", ".join(
            f"{p.name}=EPSG:{epsg}" for p, epsg in resolved
        )
        raise ValueError(
            "AOI Polygon files do not use one consistent CRS: " + detail
        )

    return unique[0], [p for p, _ in resolved]


def hidden_process_kwargs():
    if os.name != "nt":
        return {}
    out = {}
    try:
        out["creationflags"] = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        out["startupinfo"] = si
    except Exception:
        pass
    return out


class BakeryGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"NM Group - TX Veg Delivery Bakery {BAKERY_VERSION}")
        # Size the window for the display scaling and keep it on screen.
        try:
            _scale = max(1.0, float(self.tk.call("tk", "scaling")) / (96 / 72))
        except Exception:
            _scale = 1.0
        _w = min(int(1500 * _scale), max(800, self.winfo_screenwidth() - 40))
        _h = min(int(900 * _scale), max(600, self.winfo_screenheight() - 80))
        self.geometry(f"{_w}x{_h}")
        self.minsize(min(980, _w), min(620, _h))
        self.configure(bg=BG)

        self.proc = None
        self.worker_thread = None
        self.events = queue.Queue()
        self.cancel_requested = False
        self.preflight_coverage_rows = None
        self.preflight_payload = None
        self.started_at = None
        self.current_stage = ""
        self.last_progress_total = 0

        # Preflight -> operator RoW selection pause.
        self.row_selection_done = threading.Event()
        self.row_selection_result = None
        self.row_selection_window = None
        self.preflight_coverage_rows = None
        self.preflight_payload = None

        # Whole-process-tree pause/resume.
        self.paused = False
        self.pause_started = None
        self.total_paused_seconds = 0.0

        # AOI .prj is the preferred CRS source for a GUI run.
        self.crs_locked = False
        self.crs_detection_error = ""
        self.crs_prj_files = []
        self.detected_epsg = None
        self.crs_override_confirmed = False

        self._load_vars()
        self._build_styles()
        self._build_ui()
        self._update_crs_from_input(show_errors=False)
        self.after(100, self._drain_events)
        self.after(1000, self._tick_runtime)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # State/config
    # ------------------------------------------------------------------
    def _load_vars(self):
        cfg = {}
        if CONFIG.exists():
            try:
                cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
            except Exception:
                cfg = {}

        self.input_var = tk.StringVar(value=cfg.get("input_dir", ""))
        self.output_var = tk.StringVar(value=cfg.get("output_root", ""))
        self.source_var = tk.StringVar(value=str(SOURCE))
        self.crs_var = tk.StringVar(value="Connecticut — EPSG:6434")
        self.other_epsg_var = tk.StringVar(value="")
        self.year_var = tk.StringVar(value="2026")
        self.workers_var = tk.IntVar(value=12)
        self.status_var = tk.StringVar(value="Ready to preheat")
        self.runtime_var = tk.StringVar(value="00:00:00")
        self.rows_var = tk.StringVar(value="Not checked")
        self.deliverables_var = tk.StringVar(value="—")
        self.stage_detail_var = tk.StringVar(value="Ready")
        self.stage_pct_var = tk.StringVar(value="0%")

        self.product_vars = {}
        for key, _, _ in PRODUCTS:
            self.product_vars[key] = tk.BooleanVar(value=True)

    def _save_paths(self):
        payload = {
            "input_dir": self.input_var.get().strip(),
            "output_root": self.output_var.get().strip(),
        }
        CONFIG.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    # Styles/layout
    # ------------------------------------------------------------------
    def _build_styles(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure("Bakery.TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("Panel2.TFrame", background=PANEL_2)
        style.configure(
            "Title.TLabel", background=BG, foreground=TEXT,
            font=("Segoe UI Semibold", 22)
        )
        style.configure(
            "Subtitle.TLabel", background=BG, foreground=MUTED,
            font=("Segoe UI", 11)
        )
        style.configure(
            "CardTitle.TLabel", background=PANEL, foreground=TEXT,
            font=("Segoe UI Semibold", 12)
        )
        style.configure(
            "Body.TLabel", background=PANEL, foreground=TEXT,
            font=("Segoe UI", 10)
        )
        style.configure(
            "Muted.TLabel", background=PANEL, foreground=MUTED,
            font=("Segoe UI", 9)
        )
        style.configure(
            "Side.TLabel", background=BG, foreground=TEXT,
            font=("Segoe UI Semibold", 11)
        )
        style.configure(
            "SideMuted.TLabel", background=BG, foreground=MUTED,
            font=("Segoe UI", 9)
        )
        style.configure(
            "Accent.TButton",
            background=ACCENT, foreground="white",
            font=("Segoe UI Semibold", 11),
            padding=(16, 10), borderwidth=0
        )
        style.map("Accent.TButton", background=[("active", ACCENT_DARK)])
        style.configure(
            "Secondary.TButton",
            background=PANEL_2, foreground=TEXT,
            font=("Segoe UI", 10),
            padding=(12, 8)
        )
        style.configure(
            "Small.TButton",
            background=PANEL_2, foreground=TEXT,
            font=("Segoe UI", 9),
            padding=(6, 5)
        )
        style.configure(
            "Bakery.Horizontal.TProgressbar",
            troughcolor="#eadccd",
            background=ACCENT,
            bordercolor="#eadccd",
            lightcolor=ACCENT,
            darkcolor=ACCENT,
            thickness=18,
        )

    def _card(self, parent):
        outer = tk.Frame(parent, bg=LINE)
        inner = ttk.Frame(outer, style="Panel.TFrame", padding=14)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        return outer, inner

    def _build_ui(self):
        # Header
        header = ttk.Frame(self, style="Bakery.TFrame", padding=(24, 16, 24, 10))
        header.pack(fill="x")
        left = ttk.Frame(header, style="Bakery.TFrame")
        left.pack(side="left", fill="x", expand=True)
        ttk.Label(left, text="TX VEG DELIVERY BAKERY", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            left,
            text=f"NMIP26068 Eversource Tester {BAKERY_VERSION}  •  {BAKERY_RELEASE}",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(2, 0))
        self.ready_badge = tk.Label(
            header, text="  READY TO PREHEAT  ",
            bg="#eadccd", fg=TEXT, font=("Segoe UI Semibold", 10),
            padx=10, pady=8
        )
        self.ready_badge.pack(side="right")

        # The work area scrolls only when the window is smaller than its
        # content (high display scaling, small screens); otherwise it fills
        # the window exactly as before.
        scroll_host = ttk.Frame(self, style="Bakery.TFrame")
        scroll_host.pack(fill="both", expand=True)
        self._body_canvas = tk.Canvas(scroll_host, bg=BG, highlightthickness=0, borderwidth=0)
        self._body_vsb = ttk.Scrollbar(scroll_host, orient="vertical",
                                       command=self._body_canvas.yview)
        self._body_canvas.configure(yscrollcommand=self._body_vsb.set)
        self._body_canvas.pack(side="left", fill="both", expand=True)
        body = ttk.Frame(self._body_canvas, style="Bakery.TFrame", padding=(18, 4, 18, 16))
        self._body = body
        self._body_win = self._body_canvas.create_window(0, 0, window=body, anchor="nw")
        self._body_canvas.bind("<Configure>", self._fit_body)
        body.bind("<Configure>", self._fit_body)
        self.bind_all("<MouseWheel>", self._on_body_wheel, add="+")
        self.bind_all("<Button-4>", self._on_body_wheel, add="+")
        self.bind_all("<Button-5>", self._on_body_wheel, add="+")

        # Fixed side columns follow the display scaling (Windows 125%/150%),
        # otherwise their text is cut on high-DPI screens.
        try:
            ui_scale = max(1.0, float(self.tk.call("tk", "scaling")) / (96 / 72))
        except Exception:
            ui_scale = 1.0

        # Sidebar
        sidebar = ttk.Frame(body, style="Bakery.TFrame", width=int(190 * ui_scale))
        sidebar.pack(side="left", fill="y", padx=(0, 12))
        sidebar.pack_propagate(False)
        stages = [
            ("PREHEATING", "Setup & validate", "01"),
            ("MIXING", "Geometry", "02"),
            ("BAKING", "LiDAR", "03"),
            ("ICING", "PLS XYZ", "04"),
            ("PACKING", "Deliverables", "05"),
        ]
        self.stage_labels = {}
        for name, sub, num in stages:
            box = tk.Frame(sidebar, bg=PANEL_2, highlightbackground=LINE, highlightthickness=1)
            box.pack(fill="x", pady=5)
            n = tk.Label(box, text=num, bg=PANEL_2, fg=ACCENT_DARK,
                         font=("Segoe UI Semibold", 10), width=3)
            n.pack(side="left", padx=(8, 2), pady=9)
            txtbox = tk.Frame(box, bg=PANEL_2)
            txtbox.pack(side="left", fill="x", expand=True, pady=6)
            lbl = tk.Label(txtbox, text=name, bg=PANEL_2, fg=TEXT,
                           font=("Segoe UI Semibold", 10))
            lbl.pack(anchor="w")
            tk.Label(txtbox, text=sub, bg=PANEL_2, fg=MUTED,
                     font=("Segoe UI", 8)).pack(anchor="w")
            self.stage_labels[name] = (box, lbl)

        # Always-visible help action, bottom of left navigation.
        ttk.Button(
            sidebar, text="Operator Manual", style="Secondary.TButton",
            command=self._open_operator_manual
        ).pack(side="bottom", fill="x", pady=(12, 0))

        # Right column is packed before the main area so it keeps its width
        # when the window is narrow; the main area takes what is left.
        right = ttk.Frame(body, style="Bakery.TFrame", width=int(285 * ui_scale))
        right.pack(side="right", fill="y", padx=(12, 0))
        right.pack_propagate(False)
        main = ttk.Frame(body, style="Bakery.TFrame")
        main.pack(side="left", fill="both", expand=True)

        # Top row cards
        top = ttk.Frame(main, style="Bakery.TFrame")
        top.pack(fill="x")
        work_outer, work = self._card(top)
        prod_outer, prod = self._card(top)
        top.columnconfigure(0, weight=1)
        self._top_cards = (top, work_outer, prod_outer)
        self._top_wide = None
        self._layout_top_cards(wide=True)
        top.bind("<Configure>", self._on_top_resize)

        ttk.Label(work, text="Workspace Setup", style="CardTitle.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 10)
        )
        self._path_row(work, 1, "Input Folder", self.input_var, self._browse_input)
        self._path_row(work, 2, "Source Folder", self.source_var, None, disabled=True)
        self._path_row(work, 3, "Output Folder", self.output_var, self._browse_output)

        ttk.Label(work, text="Project CRS", style="Body.TLabel").grid(
            row=4, column=0, sticky="w", pady=6
        )
        self.crs_combo = ttk.Combobox(
            work, textvariable=self.crs_var,
            values=list(CRS_OPTIONS), state="readonly", width=22
        )
        self.crs_combo.grid(row=4, column=1, sticky="ew", padx=(8, 6), pady=6)
        self.crs_combo.bind(
            "<<ComboboxSelected>>",
            lambda e: self._toggle_other_epsg()
        )
        self.other_epsg_entry = ttk.Entry(
            work, textvariable=self.other_epsg_var, width=8
        )
        self.other_epsg_entry.grid(row=4, column=2, sticky="ew", pady=6)

        self.crs_source_var = tk.StringVar(value="CRS: manual selection")
        ttk.Label(
            work,
            textvariable=self.crs_source_var,
            style="Muted.TLabel",
        ).grid(row=5, column=1, columnspan=2, sticky="w", padx=(8, 0), pady=(0, 4))

        self._toggle_other_epsg()

        opts = ttk.Frame(work, style="Panel.TFrame")
        opts.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Label(opts, text="Capture year", style="Muted.TLabel").pack(side="left")
        ttk.Spinbox(opts, from_=2020, to=2035, textvariable=self.year_var, width=6).pack(
            side="left", padx=(6, 18)
        )
        ttk.Label(opts, text="Workers", style="Muted.TLabel").pack(side="left")
        ttk.Spinbox(opts, from_=1, to=64, textvariable=self.workers_var, width=5).pack(
            side="left", padx=6
        )
        work.columnconfigure(1, weight=1)

        ttk.Label(prod, text="Product Selection", style="CardTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10)
        )
        for i, (key, label, sub) in enumerate(PRODUCTS):
            row, col = 1 + i // 2, i % 2
            f = tk.Frame(prod, bg=PANEL_2, highlightbackground=LINE, highlightthickness=1)
            f.grid(row=row, column=col, sticky="nsew", padx=4, pady=4)
            cb = tk.Checkbutton(
                f, text=label, variable=self.product_vars[key],
                bg=PANEL_2, activebackground=PANEL_2, fg=TEXT,
                selectcolor=PANEL, font=("Segoe UI Semibold", 9),
                anchor="w"
            )
            cb.pack(fill="x", padx=8, pady=(6, 0))
            tk.Label(f, text=sub, bg=PANEL_2, fg=MUTED,
                     font=("Segoe UI", 8), anchor="w").pack(fill="x", padx=10, pady=(0, 6))
        prod.columnconfigure(0, weight=1)
        prod.columnconfigure(1, weight=1)

        # Progress card
        prog_outer, prog = self._card(main)
        prog_outer.pack(fill="x", pady=12)
        ttk.Label(prog, text="Production Progress", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(prog, textvariable=self.stage_detail_var, style="Body.TLabel").pack(
            anchor="w", pady=(10, 5)
        )
        progress_line = ttk.Frame(prog, style="Panel.TFrame")
        progress_line.pack(fill="x")
        self.progress = ttk.Progressbar(
            progress_line, style="Bakery.Horizontal.TProgressbar",
            maximum=100, value=0
        )
        self.progress.pack(side="left", fill="x", expand=True)
        ttk.Label(progress_line, textvariable=self.stage_pct_var, style="Body.TLabel",
                  width=6).pack(side="left", padx=(10, 0))

        self.subprogress = ttk.Progressbar(
            prog, style="Bakery.Horizontal.TProgressbar",
            maximum=100, value=0
        )
        self.subprogress.pack(fill="x", pady=(10, 0))

        # Log card
        log_outer, log_card = self._card(main)
        log_outer.pack(fill="both", expand=True)
        log_head = ttk.Frame(log_card, style="Panel.TFrame")
        log_head.pack(fill="x")
        ttk.Label(log_head, text="Run Log", style="CardTitle.TLabel").pack(side="left")
        ttk.Button(log_head, text="Clear", style="Secondary.TButton",
                   command=self._clear_log).pack(side="right")
        self.log = tk.Text(
            log_card, bg=LOG_BG, fg=LOG_FG, insertbackground="white",
            font=("Consolas", 9), wrap="word", height=8,
            relief="flat", padx=10, pady=10
        )
        self.log.pack(fill="both", expand=True, pady=(8, 0))
        self.log.configure(state="disabled")

        # Right summary
        badge = tk.Label(
            right, text="BATCH READY FOR DELIVERY",
            bg=GREEN_BG, fg="#2e5f2c",
            font=("Segoe UI Semibold", 11), padx=10, pady=12
        )
        badge.pack(fill="x", pady=(0, 10))
        self.summary_badge = badge

        sum_outer, summary = self._card(right)
        sum_outer.pack(fill="x")
        ttk.Label(summary, text="Batch Summary", style="CardTitle.TLabel").pack(anchor="w")
        self._summary_row(summary, "Status", self.status_var)
        self._summary_row(summary, "Selected RoWs", self.rows_var)
        self._summary_row(summary, "Deliverables", self.deliverables_var)
        self._summary_row(summary, "Workers", tk.StringVar(value=str(self.workers_var.get())))
        self._summary_row(summary, "Runtime", self.runtime_var)

        qa_outer, qa = self._card(right)
        qa_outer.pack(fill="x", pady=10)
        ttk.Label(qa, text="QA Checklist", style="CardTitle.TLabel").pack(anchor="w")
        self.qa_labels = {}
        for key, label in [
            ("input", "Input folder exists"),
            ("source", "Project source present"),
            ("output", "Output path writable"),
            ("crs", "Project CRS selected"),
            ("products", "Products selected"),
            ("preflight", "Preflight validated"),
        ]:
            lbl = tk.Label(qa, text="○  " + label, bg=PANEL, fg=MUTED,
                           font=("Segoe UI", 9), anchor="w")
            lbl.pack(fill="x", pady=3)
            self.qa_labels[key] = (lbl, label)

        btn_outer, btns = self._card(right)
        btn_outer.pack(fill="x")
        btns.columnconfigure(0, weight=1, uniform="btn")
        btns.columnconfigure(1, weight=1, uniform="btn")
        self.start_btn = ttk.Button(
            btns, text="▶  Start Batch", style="Accent.TButton",
            command=self._start_batch
        )
        self.start_btn.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.pause_btn = ttk.Button(
            btns, text="Pause", style="Small.TButton",
            command=self._toggle_pause, state="disabled"
        )
        self.pause_btn.grid(row=1, column=0, sticky="ew", padx=(0, 3), pady=3)
        self.stop_btn = ttk.Button(
            btns, text="Stop", style="Small.TButton",
            command=self._stop_batch, state="disabled"
        )
        self.stop_btn.grid(row=1, column=1, sticky="ew", padx=(3, 0), pady=3)
        self.rows_btn = ttk.Button(
            btns, text="Select RoWs", style="Small.TButton",
            command=self._reopen_row_selector, state="disabled"
        )
        self.rows_btn.grid(row=2, column=0, columnspan=2, sticky="ew", pady=3)
        ttk.Button(
            btns, text="Deliverables", style="Small.TButton",
            command=self._open_deliverables
        ).grid(row=3, column=0, sticky="ew", padx=(0, 3), pady=3)
        ttk.Button(
            btns, text="QA", style="Small.TButton",
            command=self._open_qa
        ).grid(row=3, column=1, sticky="ew", padx=(3, 0), pady=3)
        self.gpkg_export_btn = ttk.Button(
            btns, text="Re-export from GPKG", style="Small.TButton",
            command=self._start_gpkg_export
        )
        self.gpkg_export_btn.grid(row=4, column=0, columnspan=2, sticky="ew", pady=3)

        self._refresh_qa()

    def _fit_body(self, event=None):
        """Stretch the work area to the window; scroll only if it does not fit."""
        c = self._body_canvas
        cw, ch = c.winfo_width(), c.winfo_height()
        if cw <= 1 or ch <= 1:
            return
        need = self._body.winfo_reqheight()
        h = max(ch, need)
        c.itemconfigure(self._body_win, width=cw, height=h)
        c.configure(scrollregion=(0, 0, cw, h))
        if need > ch + 1:
            if not self._body_vsb.winfo_ismapped():
                self._body_vsb.pack(side="right", fill="y", before=c)
        elif self._body_vsb.winfo_ismapped():
            self._body_vsb.pack_forget()
            c.yview_moveto(0)

    def _on_body_wheel(self, event):
        if not self._body_vsb.winfo_ismapped():
            return
        w = event.widget
        if isinstance(w, (tk.Text, tk.Listbox, ttk.Treeview)):
            return  # the log and tables scroll themselves
        try:
            if w.winfo_toplevel() is not self:
                return  # dialogs (RoW selector) scroll themselves
        except Exception:
            return
        if getattr(event, "num", None) == 4:
            step = -1
        elif getattr(event, "num", None) == 5:
            step = 1
        else:
            step = -1 if event.delta > 0 else 1
        self._body_canvas.yview_scroll(step * 3, "units")

    def _layout_top_cards(self, wide):
        """Workspace and Product cards side by side, or stacked when narrow."""
        top, work_outer, prod_outer = self._top_cards
        if wide == self._top_wide:
            return
        self._top_wide = wide
        work_outer.grid_forget()
        prod_outer.grid_forget()
        if wide:
            top.columnconfigure(1, weight=0)
            work_outer.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
            prod_outer.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        else:
            work_outer.grid(row=0, column=0, columnspan=2, sticky="nsew")
            prod_outer.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(12, 0))

    def _on_top_resize(self, event=None):
        top, work_outer, prod_outer = self._top_cards
        need = work_outer.winfo_reqwidth() + prod_outer.winfo_reqwidth() + 12
        self._layout_top_cards(wide=top.winfo_width() >= need)

    def _path_row(self, parent, row, label, var, browse, disabled=False):
        ttk.Label(parent, text=label, style="Body.TLabel").grid(
            row=row, column=0, sticky="w", pady=6
        )
        ent = ttk.Entry(parent, textvariable=var, width=12)
        ent.grid(row=row, column=1, sticky="ew", padx=(8, 6), pady=6)
        if disabled:
            ent.configure(state="readonly")
        if browse:
            ttk.Button(parent, text="Browse…", style="Secondary.TButton",
                       command=browse).grid(row=row, column=2, sticky="ew", pady=6)
        else:
            ttk.Label(parent, text="Fixed", style="Muted.TLabel").grid(
                row=row, column=2, sticky="w", pady=6
            )

    def _summary_row(self, parent, name, var):
        f = ttk.Frame(parent, style="Panel.TFrame")
        f.pack(fill="x", pady=5)
        ttk.Label(f, text=name, style="Muted.TLabel").pack(side="left")
        ttk.Label(f, textvariable=var, style="Body.TLabel").pack(side="right")

    # ------------------------------------------------------------------
    # UI actions
    # ------------------------------------------------------------------
    def _browse_input(self):
        d = filedialog.askdirectory(
            title="Select project input folder",
            initialdir=self.input_var.get() or None
        )
        if d:
            self.input_var.set(d)
            self._save_paths()
            self._update_crs_from_input(show_errors=True)
            self._refresh_qa()

    def _browse_output(self):
        d = filedialog.askdirectory(
            title="Select project output folder",
            initialdir=self.output_var.get() or None
        )
        if d:
            self.output_var.set(d)
            self._save_paths()
            self._refresh_qa()

    def _toggle_other_epsg(self):
        # CRS is always editable by the operator. Auto-detection is a suggested
        # default, not a hard lock.
        self.crs_combo.configure(state="readonly")
        state = (
            "normal"
            if self.crs_var.get() == "Other EPSG"
            else "disabled"
        )
        self.other_epsg_entry.configure(state=state)

    def _set_detected_crs(self, epsg: int, prj_files):
        epsg = int(epsg)
        self.detected_epsg = epsg
        self.crs_override_confirmed = False

        label = EPSG_TO_CRS_LABEL.get(epsg)
        if label is not None:
            self.crs_var.set(label)
            self.other_epsg_var.set("")
        else:
            self.crs_var.set("Other EPSG")
            self.other_epsg_var.set(str(epsg))

        self.crs_locked = False
        self.crs_detection_error = ""
        self.crs_prj_files = list(prj_files or [])
        self._toggle_other_epsg()

        names = ", ".join(p.name for p in self.crs_prj_files[:3])
        if len(self.crs_prj_files) > 3:
            names += f" +{len(self.crs_prj_files) - 3} more"

        self.crs_source_var.set(
            f"Detected from AOI .prj: EPSG:{epsg}"
            + (f"  ({names})" if names else "")
            + "  — operator may override"
        )

    def _clear_detected_crs(self, message="CRS: manual selection"):
        self.detected_epsg = None
        self.crs_override_confirmed = False
        self.crs_locked = False
        self.crs_prj_files = []
        self.crs_combo.configure(state="readonly")
        self.crs_source_var.set(message)
        self._toggle_other_epsg()

    def _update_crs_from_input(self, show_errors=False):
        raw = self.input_var.get().strip()
        if not raw:
            self.crs_detection_error = ""
            self._clear_detected_crs(
                "CRS: select an input folder to detect AOI projection"
            )
            return None

        input_dir = Path(raw)
        if not input_dir.is_dir():
            self.crs_detection_error = ""
            self._clear_detected_crs("CRS: input folder does not exist")
            return None

        try:
            epsg, prj_files = detect_aoi_epsg(input_dir)
        except Exception as exc:
            self.crs_detection_error = str(exc)
            self._clear_detected_crs("CRS: AOI projection conflict/error")
            if show_errors:
                messagebox.showerror(
                    "AOI projection error",
                    str(exc),
                    parent=self,
                )
            return None

        if epsg is None:
            self.crs_detection_error = ""
            self._clear_detected_crs(
                "CRS: no published Polygon AOI .prj found — manual selection"
            )
            return None

        self._set_detected_crs(epsg, prj_files)
        return int(epsg)

    def _get_epsg(self):
        val = CRS_OPTIONS.get(self.crs_var.get())
        if val:
            return int(val)
        raw = self.other_epsg_var.get().strip()
        if not raw.isdigit():
            raise ValueError("Enter a numeric EPSG code.")
        return int(raw)

    def _confirm_crs_override_if_needed(self, used_epsg: int) -> bool:
        detected = self.detected_epsg
        if detected is None or int(used_epsg) == int(detected):
            self.crs_override_confirmed = False
            return True

        ok = messagebox.askyesno(
            "CRS override",
            (
                f"AOI .prj suggests EPSG:{detected}, but you selected "
                f"EPSG:{used_epsg}.\\n\\n"
                "Continue with the operator override?"
            ),
            parent=self,
        )
        self.crs_override_confirmed = bool(ok)
        return bool(ok)


    def _selected_products(self):
        return [k for k, _, _ in PRODUCTS if self.product_vars[k].get()]

    def _refresh_qa(self):
        inp = Path(self.input_var.get()) if self.input_var.get().strip() else None
        out = Path(self.output_var.get()) if self.output_var.get().strip() else None
        checks = {
            "input": bool(inp and inp.is_dir()),
            "source": SOURCE.is_dir(),
            "output": bool(out),
            "crs": bool(self.crs_var.get()) and not self.crs_detection_error,
            "products": bool(self._selected_products()),
            "preflight": False,
        }
        if out:
            sel = out / "00_preflight" / "00_preflight_selection.json"
            if sel.exists():
                try:
                    d = json.loads(sel.read_text(encoding="utf-8"))
                    checks["preflight"] = bool(d.get("selected_rows"))
                except Exception:
                    pass

        for key, ok in checks.items():
            lbl, label = self.qa_labels[key]
            lbl.configure(
                text=("✓  " if ok else "○  ") + label,
                fg=(GREEN if ok else MUTED),
            )

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _open_folder(self, p):
        path = Path(p)
        path.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(path))
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def _open_deliverables(self):
        if not self.output_var.get().strip():
            return
        self._open_folder(Path(self.output_var.get()) / "deliverables")

    def _open_qa(self):
        if not self.output_var.get().strip():
            return
        self._open_folder(Path(self.output_var.get()) / "QA")

    def _open_operator_manual(self):
        manual = ROOT / "docs" / "operator_manual.html"
        if not manual.is_file():
            messagebox.showerror(
                "Operator Manual",
                f"Manual not found:\n{manual}\n\nReinstall the complete Eversource Tester package.",
            )
            return
        try:
            opened = webbrowser.open(manual.resolve().as_uri())
            if not opened:
                raise RuntimeError("No browser accepted the manual URL")
        except Exception as exc:
            messagebox.showerror("Operator Manual", f"Could not open manual:\n{exc}")

    def _validate_start(self):
        inp = Path(self.input_var.get().strip())
        out = Path(self.output_var.get().strip())
        if not inp.is_dir():
            raise ValueError("Choose a valid input folder.")
        if not SOURCE.is_dir():
            raise ValueError(f"Fixed source folder is missing: {SOURCE}")
        out.mkdir(parents=True, exist_ok=True)

        self._update_crs_from_input(show_errors=False)
        if self.crs_detection_error:
            raise ValueError(self.crs_detection_error)

        epsg = self._get_epsg()
        products = self._selected_products()
        if not products:
            raise ValueError("Select at least one product.")
        return inp, out, epsg, products

    # ------------------------------------------------------------------
    # Pipeline execution
    # ------------------------------------------------------------------
    def _start_batch(self):
        if self.worker_thread and self.worker_thread.is_alive():
            return
        try:
            inp, out, epsg, products = self._validate_start()
        except Exception as exc:
            messagebox.showerror("Cannot start batch", str(exc))
            return

        if not self._confirm_crs_override_if_needed(epsg):
            return

        self._save_paths()
        self.cancel_requested = False
        self.paused = False
        self.pause_started = None
        self.total_paused_seconds = 0.0
        self.started_at = time.monotonic()
        self.progress["value"] = 0
        self.subprogress["value"] = 0
        self.stage_pct_var.set("0%")
        self.rows_var.set("Checking…")
        self.deliverables_var.set("—")
        self.status_var.set("Running")
        self.start_btn.configure(state="disabled")
        self.pause_btn.configure(state="normal", text="Pause")
        self.stop_btn.configure(state="normal")
        self.ready_badge.configure(text="  BATCH IN THE OVEN  ", bg="#f6ddc8", fg=ACCENT_DARK)
        self.summary_badge.configure(text="BATCH IN PROGRESS", bg="#f6ddc8", fg=ACCENT_DARK)
        self._set_stage("PREHEATING", "Validating inputs, AOIs and LiDAR coverage")
        self._clear_log()
        self._append_log(f"TX Veg Delivery Bakery {BAKERY_VERSION} batch started.")
        self._append_log(f"Release: {BAKERY_RELEASE}")
        self._append_log(f"Input:  {inp}")
        self._append_log(f"Source: {SOURCE}")
        self._append_log(f"Output: {out}")
        self._append_log(f"EPSG used: {epsg}")
        if self.detected_epsg is not None:
            self._append_log(f"EPSG detected from AOI .prj: {self.detected_epsg}")
            if int(epsg) != int(self.detected_epsg):
                self._append_log("CRS source: OPERATOR_OVERRIDE")
            else:
                self._append_log("CRS source: AOI_PRJ")
        else:
            self._append_log("CRS source: OPERATOR_MANUAL")

        self._append_log("Products: " + ", ".join(products))

        args = (
            inp,
            out,
            epsg,
            products,
            int(self.year_var.get()),
            int(self.workers_var.get()),
            self.detected_epsg,
        )
        self.worker_thread = threading.Thread(
            target=self._pipeline_worker, args=args, daemon=True
        )
        self.worker_thread.start()


    def _start_gpkg_export(self):
        """R01.05: rebuild Circuit/Caydence deliverables from the edited QA GPKG."""
        if self.worker_thread and self.worker_thread.is_alive():
            return
        out_txt = self.output_var.get().strip()
        if not out_txt:
            messagebox.showerror("Export from edited GPKG", "Choose the output folder first.")
            return
        out = Path(out_txt)
        gpkg = out / "01_geometry" / "01_eversource_geometry.gpkg"
        if not gpkg.is_file():
            messagebox.showerror(
                "Export from edited GPKG",
                f"Geometry GPKG not found:\n{gpkg}\n\nRun Geometry first.",
            )
            return
        if not messagebox.askyesno(
            "Export from edited GPKG",
            "Rebuild the Circuit and Caydence deliverables from:\n"
            f"{gpkg}\n\nClose the GPKG in QGIS (or save your edits) first.\n"
            "The current Circuit/Caydence folders are copied to QA before they are replaced.\n"
            "Tile Index, Management Units and LAS are not touched.\n\nContinue?",
        ):
            return
        self.cancel_requested = False
        self.started_at = time.monotonic()
        self.total_paused_seconds = 0.0
        self.status_var.set("Running")
        self.start_btn.configure(state="disabled")
        self.gpkg_export_btn.configure(state="disabled")
        self._clear_log()
        self._append_log(f"TX Veg Delivery Bakery {BAKERY_VERSION}: export from edited GPKG.")
        self._append_log(f"GPKG: {gpkg}")

        def work():
            try:
                rc = self._run_process(
                    "RE-PACKING",
                    ROOT / "scripts" / "04_export_geometry_from_gpkg.py",
                    "--output-root", str(out),
                )
                if rc != 0:
                    raise RuntimeError("Export from GPKG failed.")
                self.events.put(("done", None))
            except Exception as exc:
                self.events.put(("error", str(exc)))
            finally:
                self.after(0, lambda: self.gpkg_export_btn.configure(state="normal"))

        self.worker_thread = threading.Thread(target=work, daemon=True)
        self.worker_thread.start()

    def _read_preflight_rows(self, output_root: Path):
        """Read the preflight coverage table for the GUI RoW selection dialog."""
        csv_path = Path(output_root) / "00_preflight" / "00_preflight_coverage.csv"
        rows = []
        if csv_path.exists():
            with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
                for rec in csv.DictReader(f):
                    rows.append(dict(rec))
        return rows

    @staticmethod
    def _fmt_pct(value):
        try:
            v = float(value)
            if not (v == v):
                return "—"
            # Coverage columns are a mix of 0..1 and already-percent values.
            if abs(v) <= 1.000001:
                v *= 100.0
            return f"{v:.1f}%"
        except Exception:
            return "—"

    def _reopen_row_selector(self):
        if self.row_selection_window and self.row_selection_window.winfo_exists():
            self.row_selection_window.lift()
            return
        if self.preflight_coverage_rows is not None and self.preflight_payload is not None:
            self._show_row_selector(
                self.preflight_coverage_rows,
                self.preflight_payload,
            )

    def _show_row_selector(self, coverage_rows, selection_payload):
        """Mandatory RoW selection embedded inside the main GUI."""
        try:
            if self.row_selection_window and self.row_selection_window.winfo_exists():
                self.row_selection_window.lift()
                return

            ready_from_selection = set(selection_payload.get("selected_rows") or [])

            overlay = tk.Frame(
                self,
                bg=BG,
                highlightbackground=ACCENT_DARK,
                highlightthickness=2,
                bd=0,
            )
            self.row_selection_window = overlay
            overlay.place(
                relx=0.5,
                rely=0.5,
                anchor="center",
                relwidth=0.78,
                relheight=0.72,
            )
            overlay.lift()

            header = tk.Frame(overlay, bg=BG)
            header.pack(fill="x", padx=22, pady=(20, 8))

            tk.Label(
                header,
                text="PREHEAT COMPLETE — SELECT RoWs",
                bg=BG,
                fg=TEXT,
                font=("Segoe UI Semibold", 18),
            ).pack(anchor="w")

            tk.Label(
                header,
                text=(
                    "Select the RoWs to process. Rows without a published Polygon "
                    "AOI are shown for reference but cannot be selected."
                ),
                bg=BG,
                fg=MUTED,
                font=("Segoe UI", 10),
                justify="left",
                wraplength=1000,
            ).pack(anchor="w", pady=(4, 0))

            table_shell = tk.Frame(overlay, bg=LINE)
            table_shell.pack(fill="both", expand=True, padx=22, pady=10)

            table_frame = tk.Frame(table_shell, bg=PANEL)
            table_frame.pack(fill="both", expand=True, padx=1, pady=1)

            columns = ("row", "state", "mu", "aoi", "candidate", "action")
            tree = ttk.Treeview(
                table_frame,
                columns=columns,
                show="headings",
                selectmode="extended",
                height=14,
            )

            headings = {
                "row": "RoW",
                "state": "State",
                "mu": "MU coverage",
                "aoi": "AOI coverage",
                "candidate": "Candidate LiDAR in AOI",
                "action": "Status",
            }
            widths = {
                "row": 150,
                "state": 80,
                "mu": 120,
                "aoi": 120,
                "candidate": 180,
                "action": 150,
            }

            for col in columns:
                tree.heading(col, text=headings[col])
                tree.column(
                    col,
                    width=widths[col],
                    minwidth=70,
                    anchor=("w" if col in {"row", "action"} else "center"),
                )

            yscroll = ttk.Scrollbar(
                table_frame,
                orient="vertical",
                command=tree.yview,
            )
            tree.configure(yscrollcommand=yscroll.set)

            tree.pack(
                side="left",
                fill="both",
                expand=True,
                padx=(10, 0),
                pady=10,
            )
            yscroll.pack(
                side="right",
                fill="y",
                padx=(0, 10),
                pady=10,
            )

            ready_items = []
            row_by_iid = {}

            for idx, rec in enumerate(coverage_rows or []):
                row_name = str(rec.get("ROW") or rec.get("RoW") or "").strip()
                if not row_name:
                    continue

                state = str(rec.get("STATE") or rec.get("State") or "").strip()
                action = str(rec.get("ACTION") or "").strip()
                aoi_raw = str(rec.get("AOI_OK") or "").strip().lower()

                is_ready = (
                    aoi_raw in {"1", "true", "yes"}
                    or action.upper() == "READY"
                )

                iid = f"row_{idx}"
                row_by_iid[iid] = row_name

                tree.insert(
                    "",
                    "end",
                    iid=iid,
                    values=(
                        row_name,
                        state,
                        self._fmt_pct(rec.get("MU_COVERAGE")),
                        self._fmt_pct(rec.get("AOI_COVERAGE_PCT")),
                        self._fmt_pct(rec.get("CANDIDATE_LIDAR_IN_AOI_PCT")),
                        action or ("READY" if is_ready else "NOT READY"),
                    ),
                    tags=("ready",) if is_ready else ("blocked",),
                )

                if is_ready:
                    ready_items.append(iid)

            tree.tag_configure("blocked", foreground="#9b8b82")
            tree.tag_configure("ready", foreground=TEXT)

            for iid in ready_items:
                row_name = row_by_iid.get(iid, "")
                if row_name in ready_from_selection:
                    tree.selection_add(iid)

            footer = tk.Frame(overlay, bg=BG)
            footer.pack(fill="x", padx=22, pady=(0, 20))

            info_var = tk.StringVar(
                value=f"{len(tree.selection())} RoW(s) selected"
            )

            tk.Label(
                footer,
                textvariable=info_var,
                bg=BG,
                fg=MUTED,
                font=("Segoe UI", 10),
            ).pack(side="left")

            button_frame = tk.Frame(footer, bg=BG)
            button_frame.pack(side="right")

            _selection_guard = {"busy": False}

            def _normalise_selection(_event=None):
                # Treeview.selection_set() itself fires <<TreeviewSelect>>.
                # Only write the selection back when blocked rows actually need
                # to be removed, otherwise this can recurse/flood Tk's event loop.
                if _selection_guard["busy"]:
                    return

                selected = list(tree.selection())
                valid = [
                    iid for iid in selected
                    if "ready" in tree.item(iid, "tags")
                ]

                if selected != valid:
                    _selection_guard["busy"] = True
                    try:
                        tree.selection_set(valid)
                    finally:
                        _selection_guard["busy"] = False

                info_var.set(f"{len(valid)} RoW(s) selected")

            def _select_all_ready():
                _selection_guard["busy"] = True
                try:
                    tree.selection_set(ready_items)
                finally:
                    _selection_guard["busy"] = False
                info_var.set(f"{len(ready_items)} RoW(s) selected")

            def _clear():
                _selection_guard["busy"] = True
                try:
                    tree.selection_remove(tree.selection())
                finally:
                    _selection_guard["busy"] = False
                info_var.set("0 RoW(s) selected")

            def _close_selector():
                try:
                    overlay.place_forget()
                    overlay.destroy()
                except Exception:
                    pass
                self.row_selection_window = None

            def _cancel():
                self.row_selection_result = None
                self.row_selection_done.set()
                _close_selector()

            def _continue():
                chosen = [
                    row_by_iid[iid]
                    for iid in tree.selection()
                    if iid in row_by_iid
                    and "ready" in tree.item(iid, "tags")
                ]
                if not chosen:
                    messagebox.showwarning(
                        "No RoWs selected",
                        "Select at least one READY RoW before continuing.",
                        parent=self,
                    )
                    return

                self.row_selection_result = chosen
                self.row_selection_done.set()
                _close_selector()

            tree.bind("<<TreeviewSelect>>", _normalise_selection)

            ttk.Button(
                button_frame,
                text="Select all ready",
                style="Secondary.TButton",
                command=_select_all_ready,
            ).pack(side="left", padx=(0, 6))

            ttk.Button(
                button_frame,
                text="Clear",
                style="Secondary.TButton",
                command=_clear,
            ).pack(side="left", padx=6)

            ttk.Button(
                button_frame,
                text="Cancel batch",
                style="Secondary.TButton",
                command=_cancel,
            ).pack(side="left", padx=6)

            ttk.Button(
                button_frame,
                text="Continue Batch",
                style="Accent.TButton",
                command=_continue,
            ).pack(side="left", padx=(6, 0))

            self.status_var.set("Waiting for RoW selection")
            self.rows_var.set("Select RoWs…")
            self.stage_detail_var.set(
                "Preflight complete | Waiting for operator RoW selection"
            )
            self.ready_badge.configure(
                text="  SELECT RoWs TO CONTINUE  ",
                bg="#f5e5c8",
                fg=ACCENT_DARK,
            )
            self.summary_badge.configure(
                text="WAITING FOR RoW SELECTION",
                bg="#f5e5c8",
                fg=ACCENT_DARK,
            )

            overlay.update_idletasks()
            tree.focus_set()

        except Exception as exc:
            try:
                if self.row_selection_window and self.row_selection_window.winfo_exists():
                    self.row_selection_window.destroy()
            except Exception:
                pass
            self.row_selection_window = None

            detail = f"RoW selector error: {type(exc).__name__}: {exc}"
            self._append_log(detail)
            self.status_var.set("RoW selector error")
            self.stage_detail_var.set(detail)
            self.ready_badge.configure(
                text="  RoW SELECTOR NEEDS ATTENTION  ",
                bg="#f4d8d2",
                fg="#8b3427",
            )
            self.summary_badge.configure(
                text="RoW SELECTOR ERROR",
                bg="#f4d8d2",
                fg="#8b3427",
            )
            messagebox.showerror(
                "RoW selector error",
                detail,
                parent=self,
            )


    def _pipeline_worker(
        self, inp, out, epsg, products, year, workers, detected_epsg
    ):
        try:
            common = [
                "--input-dir", str(inp),
                "--source-dir", str(SOURCE),
                "--output-root", str(out),
                "--epsg", str(epsg),
                "--year", str(year),
            ]

            # PREHEAT computes coverage first. It may temporarily mark all AOI-ready
            # RoWs as selected, but the GUI always pauses here and requires an
            # explicit operator selection before any production stage starts.
            rc = self._run_process(
                "PREHEATING",
                ROOT / "scripts" / "00_preflight_eversource.py",
                *common,
                "--all-ready",
                "--boundary-workers", str(workers),
            )
            if rc != 0:
                raise RuntimeError("Preflight failed.")

            selection = out / "00_preflight" / "00_preflight_selection.json"
            if not selection.exists():
                raise RuntimeError("Preflight did not create selection file.")

            sel = json.loads(selection.read_text(encoding="utf-8"))

            # Record CRS provenance for QA before the operator chooses RoWs.
            crs_source = (
                "AOI_PRJ"
                if detected_epsg is not None and int(epsg) == int(detected_epsg)
                else (
                    "OPERATOR_OVERRIDE"
                    if detected_epsg is not None
                    else "OPERATOR_MANUAL"
                )
            )
            crs_qa = {
                "DETECTED_EPSG": (
                    int(detected_epsg)
                    if detected_epsg is not None
                    else None
                ),
                "USED_EPSG": int(epsg),
                "CRS_SOURCE": crs_source,
                "OPERATOR_OVERRIDE": bool(
                    detected_epsg is not None
                    and int(epsg) != int(detected_epsg)
                ),
                "AOI_PRJ_FILES": [
                    str(p)
                    for p in self.crs_prj_files
                ],
            }

            preflight_dir = out / "00_preflight"
            preflight_dir.mkdir(parents=True, exist_ok=True)
            (preflight_dir / "00_crs_selection.json").write_text(
                json.dumps(crs_qa, indent=2),
                encoding="utf-8",
            )

            sel["crs"] = crs_qa
            selection.write_text(
                json.dumps(sel, indent=2),
                encoding="utf-8",
            )

            ready_rows = list(sel.get("selected_rows") or [])
            if not ready_rows:
                raise RuntimeError("No AOI-ready RoWs available.")

            coverage_rows = self._read_preflight_rows(out)

            self.row_selection_done.clear()
            self.row_selection_result = None
            self.events.put(("preflight_ready", coverage_rows, sel))

            # Explicit pause: the worker does nothing until the GUI operator
            # confirms the RoW selection or cancels.
            while not self.row_selection_done.wait(0.2):
                if self.cancel_requested:
                    return

            chosen_rows = self.row_selection_result
            if not chosen_rows:
                raise RuntimeError("Batch cancelled at RoW selection.")

            # Filter the preflight selection payload to the operator's choice.
            chosen_set = set(chosen_rows)
            sel["selected_rows"] = chosen_rows
            sel["rows"] = [
                rec for rec in (sel.get("rows") or [])
                if str(rec.get("row") or "") in chosen_set
            ]
            sel["status"] = "READY"
            selection.write_text(
                json.dumps(sel, indent=2),
                encoding="utf-8",
            )
            self.events.put(("rows", chosen_rows))
            self.events.put(("row_selection_confirmed", chosen_rows))

            if self.cancel_requested:
                return

            # Geometry first.
            if "geometry" in products:
                rc = self._run_process(
                    "MIXING",
                    ROOT / "scripts" / "01_build_eversource_geometry.py",
                    *common,
                    "--selection", str(selection),
                )
                if rc != 0:
                    raise RuntimeError("Geometry stage failed.")

            if self.cancel_requested:
                return

            point_products = [p for p in products if p != "geometry"]
            if point_products:
                rc = self._run_process(
                    "BAKING / ICING / PACKING",
                    ROOT / "scripts" / "02_build_eversource_points.py",
                    *common,
                    "--selection", str(selection),
                    "--products", ",".join(point_products),
                    "--workers", str(workers),
                )
                if rc != 0:
                    raise RuntimeError("Point-product stage failed.")

            self.events.put(("done", None))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _run_process(self, stage, script, *args):
        if self.cancel_requested:
            return 1

        self.events.put(("stage", stage))
        cmd = [PY, str(script), *map(str, args)]
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=0,
            env=env,
            **hidden_process_kwargs(),
        )

        # Read both \n and \r terminated progress messages.
        buf = []
        assert self.proc.stdout is not None
        while True:
            ch = self.proc.stdout.read(1)
            if ch == "" and self.proc.poll() is not None:
                if buf:
                    self.events.put(("log", "".join(buf)))
                break
            if ch in ("\n", "\r"):
                if buf:
                    line = "".join(buf)
                    buf.clear()
                    self.events.put(("log", line))
                    self._parse_progress_to_queue(line, stage)
            else:
                buf.append(ch)

            if self.cancel_requested and self.proc.poll() is None:
                self.proc.terminate()

        rc = self.proc.wait()
        self.proc = None
        return rc

    def _parse_progress_to_queue(self, line, stage):
        # New console_progress output.
        m = re.search(r"\]\s+(\d{1,3})%\s+(\d+)\s*/\s*(\d+)", line)
        if m:
            pct, current, total = map(int, m.groups())
            self.events.put(("progress", pct, current, total, line.strip()))
            return

        # Legacy [31/124] counters.
        m = re.search(r"\[(\d+)\s*/\s*(\d+)\]", line)
        if m:
            current, total = map(int, m.groups())
            pct = int(round(100 * current / max(total, 1)))
            self.events.put(("progress", pct, current, total, line.strip()))


    def _process_tree(self):
        """Return current stage process + descendants, children first on resume/suspend."""
        if psutil is None:
            return []
        if not self.proc or self.proc.poll() is not None:
            return []
        try:
            parent = psutil.Process(self.proc.pid)
            children = parent.children(recursive=True)
            return children + [parent]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return []

    def _suspend_process_tree(self):
        procs = self._process_tree()
        if not procs:
            return 0
        suspended = 0
        # Children first so parent cannot spawn more work while being suspended.
        for proc in procs:
            try:
                proc.suspend()
                suspended += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return suspended

    def _resume_process_tree(self):
        procs = self._process_tree()
        if not procs:
            return 0
        resumed = 0
        # Parent first, then children.
        for proc in reversed(procs):
            try:
                proc.resume()
                resumed += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return resumed

    def _toggle_pause(self):
        if not (self.worker_thread and self.worker_thread.is_alive()):
            return

        # During the RoW selection pause there is no active processing tree.
        if self.row_selection_window and self.row_selection_window.winfo_exists():
            messagebox.showinfo(
                "Already paused",
                "Processing is already waiting for the RoW selection.",
                parent=self,
            )
            return

        if not self.paused:
            count = self._suspend_process_tree()
            if count <= 0:
                messagebox.showwarning(
                    "Pause unavailable",
                    "No active processing process could be suspended at this moment.",
                    parent=self,
                )
                return

            self.paused = True
            self.pause_started = time.monotonic()
            self.pause_btn.configure(text="Resume")
            self.status_var.set("Paused")
            self.ready_badge.configure(
                text="  OVEN PAUSED  ",
                bg="#eee0c5", fg=ACCENT_DARK
            )
            self.summary_badge.configure(
                text="BATCH PAUSED",
                bg="#eee0c5", fg=ACCENT_DARK
            )
            self.stage_detail_var.set(
                f"Paused | {count} process(es) suspended — computer released for other work"
            )
            self._append_log(
                f"PAUSE: suspended {count} active process(es)."
            )
        else:
            count = self._resume_process_tree()

            if self.pause_started is not None:
                self.total_paused_seconds += max(
                    0.0, time.monotonic() - self.pause_started
                )
            self.pause_started = None
            self.paused = False

            self.pause_btn.configure(text="Pause")
            self.status_var.set("Running")
            self.ready_badge.configure(
                text="  BATCH IN THE OVEN  ",
                bg="#f6ddc8", fg=ACCENT_DARK
            )
            self.summary_badge.configure(
                text="BATCH IN PROGRESS",
                bg="#f6ddc8", fg=ACCENT_DARK
            )
            self.stage_detail_var.set(
                f"{self.current_stage.title()} | Processing resumed"
            )
            self._append_log(
                f"RESUME: resumed {count} active process(es)."
            )

    def _stop_batch(self):
        if not (self.worker_thread and self.worker_thread.is_alive()):
            return
        if not messagebox.askyesno(
            "Stop batch",
            "Stop the current batch? Completed QA/temp outputs will remain on disk."
        ):
            return
        if self.paused:
            self._resume_process_tree()
            self.paused = False
            self.pause_started = None
        self.cancel_requested = True
        self.row_selection_result = None
        self.row_selection_done.set()
        self.status_var.set("Stopping…")
        if self.row_selection_window and self.row_selection_window.winfo_exists():
            try:
                self.row_selection_window.destroy()
            except Exception:
                pass
            self.row_selection_window = None
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Event updates
    # ------------------------------------------------------------------
    def _set_stage(self, stage, detail=None):
        self.current_stage = stage
        base = stage.split(" / ")[0]
        for name, (box, lbl) in self.stage_labels.items():
            active = name in stage
            box.configure(bg=(ACCENT if active else PANEL_2))
            lbl.configure(
                bg=(ACCENT if active else PANEL_2),
                fg=("white" if active else TEXT)
            )
            for child in box.winfo_children():
                try:
                    if isinstance(child, tk.Frame):
                        child.configure(bg=(ACCENT if active else PANEL_2))
                        for gc in child.winfo_children():
                            gc.configure(bg=(ACCENT if active else PANEL_2))
                            if gc is not lbl:
                                gc.configure(fg=("white" if active else MUTED))
                    else:
                        child.configure(bg=(ACCENT if active else PANEL_2))
                except Exception:
                    pass

        if detail:
            self.stage_detail_var.set(f"{stage.title()} | {detail}")
        else:
            self.stage_detail_var.set(stage.title())
        self.progress["value"] = 0
        self.subprogress["value"] = 0
        self.stage_pct_var.set("0%")

    def _drain_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "log":
                    line = event[1]
                    if line.strip():
                        self._append_log(line)
                elif kind == "stage":
                    st = event[1]
                    detail = {
                        "PREHEATING": "Checking source data, AOIs and LiDAR coverage",
                        "MIXING": "Building geometry and Caydence products",
                        "BAKING / ICING / PACKING": "Building selected point-cloud products",
                        "RE-PACKING": "Rebuilding Circuit/Caydence from the edited GPKG",
                    }.get(st, "Processing")
                    self._set_stage(st, detail)
                elif kind == "progress":
                    pct, current, total, line = event[1:]
                    self.subprogress["value"] = pct
                    self.stage_pct_var.set(f"{pct}%")
                    # overall is deliberately coarse by stage.
                    base = 5
                    if "PREHEATING" in self.current_stage:
                        base = 0
                        overall = 20 * pct / 100
                    elif "MIXING" in self.current_stage:
                        overall = 20 + 20 * pct / 100
                    else:
                        overall = 40 + 60 * pct / 100
                    self.progress["value"] = min(100, overall)
                    self.stage_detail_var.set(line[:130])
                elif kind == "preflight_ready":
                    coverage_rows, selection_payload = event[1], event[2]
                    self.preflight_coverage_rows = coverage_rows
                    self.preflight_payload = selection_payload
                    self.progress["value"] = 20
                    self.subprogress["value"] = 100
                    self.stage_pct_var.set("100%")
                    self.status_var.set("Waiting for RoW selection")
                    self.rows_var.set("Select RoWs…")
                    self.stage_detail_var.set("Preflight complete | Operator RoW selection required")
                    if hasattr(self, "rows_btn"):
                        self.rows_btn.configure(state="normal")
                    self.after(50, lambda: self._show_row_selector(coverage_rows, selection_payload))
                    self._refresh_qa()
                elif kind == "row_selection_confirmed":
                    rows = event[1]
                    if hasattr(self, "rows_btn"):
                        self.rows_btn.configure(state="disabled")
                    self.status_var.set("Running")
                    self.ready_badge.configure(
                        text="  BATCH IN THE OVEN  ",
                        bg="#f6ddc8", fg=ACCENT_DARK
                    )
                    self.summary_badge.configure(
                        text="BATCH IN PROGRESS",
                        bg="#f6ddc8", fg=ACCENT_DARK
                    )
                    self._append_log(
                        "Operator selected RoWs: " + ", ".join(rows)
                    )
                elif kind == "rows":
                    rows = event[1]
                    if len(rows) <= 3:
                        self.rows_var.set(", ".join(rows))
                    else:
                        self.rows_var.set(f"{len(rows)} RoWs")
                    self._refresh_qa()
                elif kind == "done":
                    self._finish_success()
                elif kind == "error":
                    self._finish_error(event[1])
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _tick_runtime(self):
        if self.started_at is not None:
            now = self.pause_started if (self.paused and self.pause_started is not None) else time.monotonic()
            sec = int(max(0.0, now - self.started_at - self.total_paused_seconds))
            h, rem = divmod(sec, 3600)
            m, s = divmod(rem, 60)
            self.runtime_var.set(f"{h:02d}:{m:02d}:{s:02d}")
        self.after(1000, self._tick_runtime)

    def _finish_success(self):
        self.started_at = None
        self.status_var.set("Complete")
        self.progress["value"] = 100
        self.subprogress["value"] = 100
        self.stage_pct_var.set("100%")
        self.stage_detail_var.set("Batch ready for delivery")
        self.ready_badge.configure(text="  BATCH READY FOR DELIVERY  ", bg=GREEN_BG, fg="#2e5f2c")
        self.summary_badge.configure(text="BATCH READY FOR DELIVERY", bg=GREEN_BG, fg="#2e5f2c")
        self.start_btn.configure(state="normal")
        self.pause_btn.configure(state="disabled", text="Pause")
        self.stop_btn.configure(state="disabled")
        self.paused = False
        self.pause_started = None
        self._append_log("BAKE COMPLETE.")
        out = Path(self.output_var.get())
        deliverables = out / "deliverables"
        try:
            n = sum(1 for p in deliverables.rglob("*") if p.is_file())
            self.deliverables_var.set(f"{n} files")
        except Exception:
            self.deliverables_var.set("Complete")
        self._refresh_qa()

    def _finish_error(self, msg):
        self.started_at = None
        self.status_var.set("Failed")
        self.ready_badge.configure(text="  BATCH NEEDS ATTENTION  ", bg="#f4d8d2", fg="#8b3427")
        self.summary_badge.configure(text="BATCH NEEDS ATTENTION", bg="#f4d8d2", fg="#8b3427")
        self.start_btn.configure(state="normal")
        self.pause_btn.configure(state="disabled", text="Pause")
        self.stop_btn.configure(state="disabled")
        self.paused = False
        self.pause_started = None
        self._append_log("ERROR: " + msg)
        messagebox.showerror("Batch failed", msg)
        self._refresh_qa()

    def _on_close(self):
        if self.worker_thread and self.worker_thread.is_alive():
            if not messagebox.askyesno("Exit", "A batch is running. Stop it and exit?"):
                return
            if self.paused:
                self._resume_process_tree()
                self.paused = False
            self.cancel_requested = True
            self.row_selection_result = None
            self.row_selection_done.set()
            if self.proc and self.proc.poll() is None:
                try:
                    self.proc.terminate()
                except Exception:
                    pass
        self.destroy()


def main():
    app = BakeryGUI()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
