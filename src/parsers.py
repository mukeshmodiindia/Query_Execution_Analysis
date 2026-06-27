from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, List, Optional


@dataclass
class QueryEvent:
    timestamp: Optional[datetime]
    query: str
    duration_ms: float
    namespace: Optional[str] = None
    source_line: Optional[str] = None


def _safe_parse_ts(value: str | None) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        pass

    for fmt in ("%y%m%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _nested_get(doc: dict[str, Any], path: list[str]) -> Any:
    current: Any = doc
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def parse_mongodb_logs(text: str) -> List[QueryEvent]:
    events: List[QueryEvent] = []
    for line in text.splitlines():
        raw = line.strip()
        if not raw:
            continue
        duration = None
        ns = None
        query = None
        ts = None

        try:
            doc = json.loads(raw)
            attr = doc.get("attr") if isinstance(doc.get("attr"), dict) else {}
            duration = doc.get("durationMillis") or attr.get("durationMillis")
            ns = doc.get("ns") or attr.get("ns")
            ts = _safe_parse_ts(
                doc.get("ts")
                or _nested_get(doc, ["t", "$date"])
                or attr.get("ts")
                or _nested_get(attr, ["t", "$date"])
            )
            command = doc.get("command") or attr.get("command")
            if command is not None:
                query = json.dumps(command, sort_keys=True)
            elif "query" in doc:
                query = str(doc["query"])
            elif "query" in attr:
                query = str(attr["query"])
        except json.JSONDecodeError:
            # Try line-oriented MongoDB log pattern fallback.
            dur_match = re.search(r"(durationMillis|ms):\s*(\d+(?:\.\d+)?)", raw)
            if dur_match:
                duration = float(dur_match.group(2))
            ns_match = re.search(r"\bns\s*[:=]\s*([^\s,]+)", raw)
            if ns_match:
                ns = ns_match.group(1)
            query_match = re.search(r"(?:command|query)\s*[:=]\s*(\{.*\})", raw)
            if query_match:
                query = query_match.group(1)

        if duration is None or query is None:
            continue

        events.append(
            QueryEvent(
                timestamp=ts,
                query=query,
                duration_ms=float(duration),
                namespace=ns,
                source_line=raw,
            )
        )

    return events


def parse_mysql_logs(text: str) -> List[QueryEvent]:
    events: List[QueryEvent] = []
    lines = [ln.rstrip() for ln in text.splitlines()]

    i = 0
    current_ts: Optional[datetime] = None
    while i < len(lines):
        line = lines[i]
        time_match = re.search(r"#\s*Time:\s*(?P<ts>.+)", line)
        if time_match:
            current_ts = _safe_parse_ts(time_match.group("ts").strip())
            i += 1
            continue

        qt_match = re.search(r"#\s*Query_time:\s*([0-9]*\.?[0-9]+)", line)
        if not qt_match:
            i += 1
            continue

        query_time_s = float(qt_match.group(1))
        duration_ms = query_time_s * 1000.0

        query_lines: List[str] = []
        i += 1
        while i < len(lines):
            stripped = lines[i].strip()
            if stripped.startswith("# Time:") or stripped.startswith("# Query_time:"):
                break
            if stripped and not stripped.startswith("#") and not stripped.lower().startswith("set timestamp"):
                query_lines.append(stripped)
            i += 1

        query = " ".join(query_lines).strip()
        if query:
            events.append(
                QueryEvent(
                    timestamp=current_ts,
                    query=query,
                    duration_ms=duration_ms,
                    namespace=None,
                    source_line=line,
                )
            )

    return events


def parse_postgres_logs(text: str) -> List[QueryEvent]:
    events: List[QueryEvent] = []
    pattern = re.compile(
        r"(?P<ts>\d{4}-\d{2}-\d{2}[^\[]+)\[[^\]]+\].*duration:\s*(?P<duration>[0-9]*\.?[0-9]+)\s*ms\s*(?:statement|execute\s+[^:]+):\s*(?P<query>.*)",
        re.IGNORECASE,
    )

    for line in text.splitlines():
        raw = line.strip()
        if not raw:
            continue
        match = pattern.search(raw)
        if not match:
            continue
        ts = _safe_parse_ts(match.group("ts").strip().replace(" UTC", ""))
        events.append(
            QueryEvent(
                timestamp=ts,
                query=match.group("query").strip(),
                duration_ms=float(match.group("duration")),
                namespace=None,
                source_line=raw,
            )
        )

    return events


def _anonymize_json(value: Any, context: Optional[str] = None) -> Any:
    if isinstance(value, dict):
        return {
            key: _anonymize_json(child, "sort_value" if context == "sort" else key)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_anonymize_json(child, context) for child in value]
    if context in {"find", "aggregate", "count", "distinct"}:
        return value
    if context == "sort_value" and value in {1, -1, "asc", "desc", "ASC", "DESC"}:
        return value
    return "?"


def normalize_query(query: str, db_type: Optional[str] = None) -> str:
    query = query.strip()
    if db_type == "MongoDB" or query.startswith("{"):
        try:
            parsed = json.loads(query)
            return json.dumps(_anonymize_json(parsed), sort_keys=True)
        except json.JSONDecodeError:
            pass

    query = re.sub(r"\s+", " ", query)
    query = re.sub(r"'[^']*'", "?", query)
    query = re.sub(r'"[^"]*"', "?", query)
    query = re.sub(r"\b\d+\b", "?", query)
    return query


def parser_for(db_type: str):
    return {
        "MongoDB": parse_mongodb_logs,
        "MySQL": parse_mysql_logs,
        "PostgreSQL": parse_postgres_logs,
    }[db_type]
