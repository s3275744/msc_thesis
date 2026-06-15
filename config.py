"""Central paths and constants for the replication package.

Every path is built from the location of this file, so nothing is hardcoded.
If you clone the repository to any folder, the code still finds its data and
output folders.
"""

from pathlib import Path

# Root of the repository (the folder that contains this file).
ROOT = Path(__file__).resolve().parent

# Data folders.
PUBLIC_DATA = ROOT / "data" / "public"        # small public files, tracked by git
LICENSED_DATA = ROOT / "data" / "licensed"    # restricted files, ignored by git

# Output folders.
OUTPUT_DIR = ROOT / "outputs"
TABLE_DIR = OUTPUT_DIR / "tables"
FIGURE_DIR = OUTPUT_DIR / "figures"

# Thesis sample period (first valid filing day to last day in the data).
START = "2018-11-05"
END = "2024-12-31"

# Create the output folders if they do not exist yet.
for _folder in (TABLE_DIR, FIGURE_DIR):
    _folder.mkdir(parents=True, exist_ok=True)
