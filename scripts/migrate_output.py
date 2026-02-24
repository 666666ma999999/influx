#!/usr/bin/env python3
"""One-time migration: move timestamp-based output files into date folders."""

import os
import re
import shutil
from pathlib import Path
from datetime import datetime

OUTPUT_DIR = Path(__file__).parent.parent / "output"

# Files to skip (don't move these)
SKIP_FILES = {"viewer.html", ".gitkeep"}
SKIP_PREFIXES = ("debug_",)

def migrate():
    if not OUTPUT_DIR.exists():
        print("output/ directory not found")
        return

    # Pattern: filename_YYYYMMDD_HHMMSS.ext or filename_YYYYMMDD.ext
    timestamp_pattern = re.compile(r'^(.+?)_(\d{8})(?:_\d{6})?\.(\w+)$')

    files_to_migrate = {}  # {(date_str, base_name): [(priority, full_path), ...]}

    for f in sorted(OUTPUT_DIR.iterdir()):
        if f.is_dir():
            continue
        if f.name in SKIP_FILES:
            continue
        if any(f.name.startswith(p) for p in SKIP_PREFIXES):
            continue

        match = timestamp_pattern.match(f.name)
        if match:
            base_name = match.group(1)
            date_digits = match.group(2)  # YYYYMMDD
            ext = match.group(3)

            # Convert YYYYMMDD to YYYY-MM-DD
            date_str = f"{date_digits[:4]}-{date_digits[4:6]}-{date_digits[6:8]}"
            new_filename = f"{base_name}.{ext}"

            key = (date_str, new_filename)
            if key not in files_to_migrate:
                files_to_migrate[key] = []
            # Use mtime as priority (higher = newer = preferred)
            files_to_migrate[key].append((f.stat().st_mtime, f))

    # Handle inactive_check_result.json (no timestamp in name)
    inactive_file = OUTPUT_DIR / "inactive_check_result.json"
    if inactive_file.exists():
        mtime = inactive_file.stat().st_mtime
        date_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
        key = (date_str, "inactive_check_result.json")
        files_to_migrate[key] = [(mtime, inactive_file)]

    if not files_to_migrate:
        print("No files to migrate.")
        return

    # Perform migration
    for (date_str, new_filename), file_list in sorted(files_to_migrate.items()):
        date_dir = OUTPUT_DIR / date_str
        date_dir.mkdir(exist_ok=True)

        # Pick the latest file (highest mtime) if multiple
        file_list.sort(key=lambda x: x[0], reverse=True)
        _, best_file = file_list[0]

        dest = date_dir / new_filename
        print(f"  {best_file.name} -> {date_str}/{new_filename}")
        shutil.copy2(str(best_file), str(dest))

        # Delete all source files for this key
        for _, src_file in file_list:
            print(f"    Removing: {src_file.name}")
            src_file.unlink()

    print("\nMigration complete!")

if __name__ == "__main__":
    migrate()
