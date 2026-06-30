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


def _safe_parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, dict):
        value = value.get("$date") or value.get("date")
    if not isinstance(value, str):
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


def _first_value(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _duration_ms(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"\d+(?:\.\d+)?", value)
        if match:
            return float(match.group(0))
    return None


def _mongo_command_namespace(command: Any) -> Optional[str]:
    if not isinstance(command, dict):
        return None

    db_name = command.get("$db") or command.get("db")
    collection = _first_value(
        command.get("find"),
        command.get("aggregate"),
        command.get("count"),
        command.get("distinct"),
        command.get("insert"),
        command.get("update"),
        command.get("delete"),
        command.get("findAndModify"),
        command.get("findandmodify"),
        command.get("collection"),
    )
    if db_name and isinstance(collection, str):
        return f"{db_name}.{collection}"
    return None


def _format_mongo_query(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    if isinstance(value, list):
        return json.dumps(value, sort_keys=True)
    if isinstance(value, str):
        clean = value.strip()
        return clean or None
    return None


def _mongo_query_from_container(container: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    command = container.get("command")
    originating_command = container.get("originatingCommand")

    candidates: list[Any] = []
    if isinstance(command, dict) and "getMore" in command and originating_command is not None:
        candidates.extend([originating_command, command])
    else:
        candidates.extend([command, originating_command])
    candidates.extend(
        [
            container.get("query"),
            container.get("filter"),
            container.get("pipeline"),
        ]
    )

    for candidate in candidates:
        query = _format_mongo_query(candidate)
        if query:
            return query, _mongo_command_namespace(candidate)
    return None, None


def _extract_balanced_braces(text: str, start: int) -> Optional[str]:
    depth = 0
    in_string = False
    quote_char = ""
    escaped = False

    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote_char:
                in_string = False
            continue

        if char in {"'", '"'}:
            in_string = True
            quote_char = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _parse_mongodb_text_log(raw: str) -> tuple[Optional[float], Optional[str], Optional[str]]:
    duration_match = re.search(
        r"\b(?:durationMillis|duration|millis)\s*[:=]\s*(\d+(?:\.\d+)?)\s*(?:ms)?\b",
        raw,
        re.IGNORECASE,
    )
    duration = float(duration_match.group(1)) if duration_match else None
    if duration is None:
        trailing_ms = re.findall(r"\b(\d+(?:\.\d+)?)ms\b", raw, flags=re.IGNORECASE)
        if trailing_ms:
            duration = float(trailing_ms[-1])

    ns = None
    ns_match = re.search(r"\bns\s*[:=]\s*\"?([^\"\s,]+)", raw)
    if ns_match:
        ns = ns_match.group(1)
    else:
        ns_match = re.search(
            r"\b(?:command|query|getmore|update|remove|delete)\s+([A-Za-z0-9_.-]+\.[A-Za-z0-9_.-]+)\b",
            raw,
            re.IGNORECASE,
        )
        if ns_match:
            ns = ns_match.group(1)

    query = None
    for marker in ("originatingCommand", "command", "query", "filter"):
        marker_match = re.search(rf"\b{marker}\s*[:=]", raw, re.IGNORECASE)
        if not marker_match:
            continue
        brace_start = raw.find("{", marker_match.end())
        if brace_start == -1:
            continue
        query = _extract_balanced_braces(raw, brace_start)
        if query:
            break

    return duration, ns, query


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
            duration = _duration_ms(
                _first_value(
                    doc.get("durationMillis"),
                    attr.get("durationMillis"),
                    doc.get("millis"),
                    attr.get("millis"),
                )
            )
            ts = _safe_parse_ts(
                doc.get("ts")
                or _nested_get(doc, ["t", "$date"])
                or attr.get("ts")
                or _nested_get(attr, ["t", "$date"])
            )
            query, command_ns = _mongo_query_from_container(attr)
            if query is None:
                query, command_ns = _mongo_query_from_container(doc)
            ns = _first_value(doc.get("ns"), attr.get("ns"), command_ns)
        except json.JSONDecodeError:
            # Try line-oriented MongoDB log pattern fallback.
            duration, ns, query = _parse_mongodb_text_log(raw)

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
