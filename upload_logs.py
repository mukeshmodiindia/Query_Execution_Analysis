from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from src.storage import create_upload_batch, resolve_upload_dir
from src.version_profiles import VERSION_PROFILES


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Upload database log files for analysis in the Streamlit UI.",
    )
    parser.add_argument(
        "files",
        nargs="+",
        help="One or more MongoDB, MySQL, or PostgreSQL log files.",
    )
    parser.add_argument(
        "--db",
        "--database",
        dest="db_type",
        choices=sorted(VERSION_PROFILES.keys()),
        required=True,
        help="Database type for the uploaded logs.",
    )
    parser.add_argument(
        "--version",
        dest="db_version",
        help="Database version profile. Defaults to the newest profile for the selected database.",
    )
    parser.add_argument(
        "--label",
        help="Friendly name shown in the UI.",
    )
    parser.add_argument(
        "--uploads-dir",
        help=(
            "Directory used by both the CLI and Streamlit UI. "
            "Defaults to .query_analysis_uploads or QUERY_ANALYSIS_UPLOAD_DIR."
        ),
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=20,
        help="Maximum number of files accepted in one batch.",
    )
    args = parser.parse_args(argv)

    versions = list(VERSION_PROFILES[args.db_type].keys())
    db_version = args.db_version or versions[-1]
    if db_version not in VERSION_PROFILES[args.db_type]:
        parser.error(
            f"Unsupported {args.db_type} version '{db_version}'. "
            f"Choose one of: {', '.join(versions)}"
        )

    try:
        manifest = create_upload_batch(
            files=args.files,
            db_type=args.db_type,
            db_version=db_version,
            label=args.label,
            upload_dir=args.uploads_dir,
            max_files=args.max_files,
        )
    except Exception as exc:
        print(f"Upload failed: {exc}", file=sys.stderr)
        return 1

    upload_root = resolve_upload_dir(args.uploads_dir)
    print("Upload complete")
    print(f"  Batch id: {manifest['id']}")
    print(f"  Label: {manifest['label']}")
    print(f"  Database: {manifest['db_type']} {manifest['db_version']}")
    print(f"  Parsed events: {manifest['event_count']}")
    print(f"  Unique queries: {manifest['unique_query_count']}")
    print(f"  Stored in: {Path(upload_root) / manifest['id']}")
    print("Open or refresh the Streamlit UI and choose this batch under Command-line uploads.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
