from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, List, Optional


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
        return None


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
            duration = doc.get("durationMillis")
            ns = doc.get("ns")
            ts = _safe_parse_ts(doc.get("ts") or doc.get("t", {}).get("$date"))
            if "command" in doc:
                query = json.dumps(doc["command"], sort_keys=True)
            elif "query" in doc:
                query = str(doc["query"])
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
    while i < len(lines):
        line = lines[i]
        qt_match = re.search(r"#\s*Query_time:\s*([0-9]*\.?[0-9]+)", line)
        if not qt_match:
            i += 1
            continue

        query_time_s = float(qt_match.group(1))
        duration_ms = query_time_s * 1000.0

        query_lines: List[str] = []
        i += 1
        while i < len(lines) and not lines[i].startswith("# Query_time:"):
            if lines[i].strip() and not lines[i].startswith("#"):
                query_lines.append(lines[i].strip())
            i += 1

        query = " ".join(query_lines).strip()
        if query:
            events.append(
                QueryEvent(
                    timestamp=None,
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
        r"(?P<ts>\d{4}-\d{2}-\d{2}[^\[]+)\[[^\]]+\].*duration:\s*(?P<duration>[0-9]*\.?[0-9]+)\s*ms\s*statement:\s*(?P<query>.*)",
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


def normalize_query(query: str) -> str:
    query = query.strip()
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
