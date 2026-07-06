from __future__ import annotations

import gzip
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from src.logging_config import configure_logging
from src.parsers import QueryEvent, normalize_query, parser_for


DEFAULT_UPLOAD_DIR = Path(".query_analysis_uploads")
EVENTS_FILE = "events.jsonl"
MANIFEST_FILE = "manifest.json"
LOGGER = configure_logging(__name__)


def resolve_upload_dir(upload_dir: Optional[str] = None) -> Path:
    configured = upload_dir or os.environ.get("QUERY_ANALYSIS_UPLOAD_DIR")
    return Path(configured) if configured else DEFAULT_UPLOAD_DIR


def decode_log_bytes(raw_bytes: bytes, file_name: str) -> str:
    if file_name.lower().endswith(".gz"):
        try:
            raw_bytes = gzip.decompress(raw_bytes)
        except OSError as exc:
            raise ValueError(f"Failed to decompress gzip file '{file_name}': {exc}") from exc
    return raw_bytes.decode("utf-8", errors="ignore")


def create_upload_batch(
    files: Iterable[str],
    db_type: str,
    db_version: str,
    label: Optional[str] = None,
    upload_dir: Optional[str] = None,
    max_files: int = 20,
) -> dict[str, Any]:
    paths = [Path(file_name) for file_name in files]
    if not paths:
        raise ValueError("At least one log file is required.")
    if len(paths) > max_files:
        raise ValueError(f"At most {max_files} files can be uploaded in one batch.")

    for path in paths:
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"Log file not found: {path}")

    parser = parser_for(db_type)
    now = datetime.utcnow()
    batch_label = label or paths[0].stem
    batch_id = f"{now.strftime('%Y%m%d_%H%M%S')}_{_slugify(batch_label)}_{uuid.uuid4().hex[:8]}"
    root = resolve_upload_dir(upload_dir)
    batch_dir = root / batch_id
    batch_dir.mkdir(parents=True, exist_ok=False)
    LOGGER.info(
        "upload_batch_started id=%s db_type=%s db_version=%s files=%s upload_dir=%s",
        batch_id,
        db_type,
        db_version,
        len(paths),
        root,
    )

    event_count = 0
    total_duration_ms = 0.0
    normalized_queries: set[str] = set()
    file_summaries: list[dict[str, Any]] = []

    with (batch_dir / EVENTS_FILE).open("w", encoding="utf-8") as event_file:
        for path in paths:
            text = decode_log_bytes(path.read_bytes(), path.name)
            events = parser(text)
            LOGGER.info(
                "upload_file_parsed batch_id=%s file=%s size_bytes=%s parsed_events=%s",
                batch_id,
                path,
                path.stat().st_size,
                len(events),
            )
            file_summaries.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "size_bytes": path.stat().st_size,
                    "parsed_events": len(events),
                }
            )
            for event in events:
                normalized_query = normalize_query(event.query, db_type)
                normalized_queries.add(normalized_query)
                total_duration_ms += event.duration_ms
                event_count += 1
                event_file.write(
                    json.dumps(
                        _event_to_record(event, db_type, normalized_query, path.name),
                        sort_keys=True,
                    )
                    + "\n"
                )

    manifest = {
        "id": batch_id,
        "label": batch_label,
        "db_type": db_type,
        "db_version": db_version,
        "created_at": now.isoformat(timespec="seconds") + "Z",
        "upload_dir": str(root),
        "files": file_summaries,
        "event_count": event_count,
        "unique_query_count": len(normalized_queries),
        "total_duration_ms": total_duration_ms,
    }

    (batch_dir / MANIFEST_FILE).write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    LOGGER.info(
        "upload_batch_completed id=%s event_count=%s unique_query_count=%s total_duration_ms=%.2f",
        batch_id,
        event_count,
        len(normalized_queries),
        total_duration_ms,
    )
    return manifest


def list_upload_batches(upload_dir: Optional[str] = None) -> list[dict[str, Any]]:
    root = resolve_upload_dir(upload_dir)
    if not root.exists():
        return []

    manifests: list[dict[str, Any]] = []
    for child in root.iterdir():
        manifest_path = child / MANIFEST_FILE
        if not child.is_dir() or not manifest_path.exists():
            continue
        try:
            manifests.append(json.loads(manifest_path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue

    return sorted(manifests, key=lambda item: item.get("created_at", ""), reverse=True)


def load_upload_events(batch_id: str, upload_dir: Optional[str] = None) -> list[dict[str, Any]]:
    safe_batch_id = Path(batch_id).name
    if safe_batch_id != batch_id:
        raise ValueError("Invalid batch id.")

    events_path = resolve_upload_dir(upload_dir) / batch_id / EVENTS_FILE
    if not events_path.exists():
        raise FileNotFoundError(f"Upload batch events not found: {batch_id}")

    events: list[dict[str, Any]] = []
    with events_path.open("r", encoding="utf-8") as event_file:
        for line in event_file:
            if line.strip():
                events.append(json.loads(line))
    LOGGER.info("upload_events_loaded batch_id=%s event_count=%s", batch_id, len(events))
    return events


def _event_to_record(
    event: QueryEvent,
    db_type: str,
    normalized_query: str,
    file_name: str,
) -> dict[str, Any]:
    return {
        "timestamp": event.timestamp.isoformat() if event.timestamp else None,
        "query": event.query,
        "normalized_query": normalized_query,
        "duration_ms": event.duration_ms,
        "namespace": event.namespace,
        "plan_summary": event.plan_summary,
        "source_line": event.source_line,
        "query_hash": event.query_hash,
        "op_type": event.op_type,
        "file_name": file_name,
        "db_type": db_type,
    }


def _slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return slug[:48] or "logs"
