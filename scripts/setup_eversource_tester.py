#!/usr/bin/env python3
"""One-time Windows setup for NM Group TX Veg Delivery Bakery."""
from __future__ import annotations

import os
import platform
import struct
import subprocess
import sys
import venv
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
VENV = SCRIPT_DIR / ".venv"
REQ = SCRIPT_DIR / "requirements_eversource.txt"
GUI = SCRIPT_DIR / "tx_veg_bakery_gui.py"
MAIN = SCRIPT_DIR / "run_eversource_pipeline.py"

BINARY_STACK = [
    "numpy", "pandas", "scipy", "shapely",
    "pyproj", "pyogrio", "geopandas", "lazrs",
]
BASE = ["ezdxf", "laspy"]


def fail(msg: str, code: int = 1):
    print(f"\nSETUP ERROR: {msg}")
    raise SystemExit(code)


def run(cmd):
    cmd = [str(x) for x in cmd]
    print("\n> " + " ".join(f'"{x}"' if " " in x else x for x in cmd))
    subprocess.run(cmd, check=True)


def pyvenv() -> Path:
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def verify(py: Path):
    code = """
import ezdxf
import geopandas
import shapely
import pyproj
import pyogrio
import pandas
import numpy
import scipy
import laspy
import lazrs
import psutil
import tkinter

print("Environment imports: OK")
print("Python geospatial stack: OK")
h = laspy.LasHeader(point_format=6, version="1.4")
print("LAS 1.4 write support:", h.version)
drivers = pyogrio.list_drivers()
missing = [
    x for x in ["ESRI Shapefile", "GPKG", "OpenFileGDB"]
    if not drivers.get(x)
]
assert not missing, "Missing GDAL drivers: " + ", ".join(missing)
"""
    run([py, "-c", code])


def main():
    print("=" * 72)
    print("NM GROUP - TX VEG DELIVERY BAKERY - ENVIRONMENT SETUP")
    print("=" * 72)
    print("Python:", sys.version.split()[0])
    print("Executable:", sys.executable)
    print("Architecture:", struct.calcsize("P") * 8, "bit")
    print("Machine:", platform.machine())
    print("Environment location:", VENV)

    if sys.version_info < (3, 10):
        fail("Python 3.10+ is required.")
    if sys.version_info >= (3, 15):
        fail(
            "Python 3.15 is not supported by the current lazrs Windows wheel. "
            "Use 64-bit Python 3.14 or earlier."
        )
    if os.name == "nt" and struct.calcsize("P") * 8 != 64:
        fail("64-bit Python is required.")

    for required in (REQ, GUI, MAIN):
        if not required.exists():
            fail(f"Missing {required}")

    if not VENV.exists():
        print("\nCreating scripts\\.venv ...")
        venv.EnvBuilder(with_pip=True).create(VENV)

    py = pyvenv()

    try:
        run([py, "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"])
        run([
            py, "-m", "pip", "install", "--upgrade",
            "--only-binary=:all:", *BINARY_STACK
        ])
        run([
            py, "-m", "pip", "install", "--upgrade",
            "--prefer-binary", *BASE
        ])
        run([
            py, "-m", "pip", "install",
            "--prefer-binary", "--only-binary=:all:",
            "-r", REQ
        ])
        verify(py)
    except subprocess.CalledProcessError as exc:
        fail(
            f"Package installation or verification failed "
            f"(exit code {exc.returncode})."
        )

    print("\nSETUP COMPLETE")
    print(f"Environment: {VENV}")
    print("Run the tool with: run_eversource.bat")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
