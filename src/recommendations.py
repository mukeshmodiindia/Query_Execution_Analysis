from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional


@dataclass
class Recommendation:
    priority: str
    category: str
    title: str
    rationale: str
    example: Optional[str] = None


_IDENTIFIER = r"[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?"


def recommendations_for_query(
    db_type: str,
    query_text: str,
    namespace: Optional[str] = None,
) -> list[Recommendation]:
    if db_type == "MongoDB":
        return _mongodb_recommendations(query_text, namespace)
    return _sql_recommendations(db_type, query_text)


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        clean = value.strip().strip("`\"")
        if not clean or clean in seen:
            continue
        seen.add(clean)
        result.append(clean)
    return result


def _mongodb_recommendations(query_text: str, namespace: Optional[str]) -> list[Recommendation]:
    recommendations: list[Recommendation] = []
    command = _parse_json_object(query_text)

    if command:
        collection, filter_fields, sort_fields, has_or = _mongo_command_parts(command, namespace)
        index_fields = _dedupe(filter_fields + sort_fields)
        if index_fields:
            recommendations.append(
                Recommendation(
                    priority="High",
                    category="Index",
                    title="Create or verify a compound index for the main filter shape",
                    rationale=(
                        "The repeated query filters on these fields. A compound index can reduce "
                        "documents examined when the fields match the common production predicate."
                    ),
                    example=_mongo_index_example(collection, index_fields),
                )
            )
        if sort_fields:
            recommendations.append(
                Recommendation(
                    priority="Medium",
                    category="Index",
                    title="Include sort keys after selective filter fields",
                    rationale=(
                        "MongoDB can avoid an in-memory sort when a compound index matches the "
                        "filter prefix and the requested sort order."
                    ),
                    example=_mongo_index_example(collection, _dedupe(filter_fields + sort_fields)),
                )
            )
        if has_or:
            recommendations.append(
                Recommendation(
                    priority="Medium",
                    category="Index",
                    title="Index each selective `$or` branch",
                    rationale=(
                        "`$or` queries can use indexes per branch. A missing branch index often "
                        "turns the whole shape into a high-scan query."
                    ),
                )
            )

    lower_query = query_text.lower()
    if "$regex" in lower_query:
        recommendations.append(
            Recommendation(
                priority="Medium",
                category="Query rewrite",
                title="Review regex selectivity",
                rationale=(
                    "Unanchored or case-insensitive regex filters are frequently not selective. "
                    "Prefer anchored prefixes, normalized fields, or text/search indexes when appropriate."
                ),
            )
        )
    if "$group" in lower_query or '"aggregate"' in lower_query:
        recommendations.append(
            Recommendation(
                priority="Medium",
                category="Query rewrite",
                title="Push selective `$match` stages early in the pipeline",
                rationale=(
                    "Filtering before `$group`, `$lookup`, or `$sort` reduces the number of "
                    "documents flowing through expensive aggregation stages."
                ),
            )
        )

    if not recommendations:
        recommendations.append(
            Recommendation(
                priority="Low",
                category="Plan review",
                title="Capture executionStats for this query shape",
                rationale=(
                    "No obvious query-shape issue was detected from the log line alone. Confirm "
                    "whether the winning plan is IXSCAN or COLLSCAN and compare docs examined to docs returned."
                ),
            )
        )

    return recommendations


def _parse_json_object(text: str) -> Optional[dict[str, Any]]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _mongo_command_parts(
    command: dict[str, Any],
    namespace: Optional[str],
) -> tuple[str, list[str], list[str], bool]:
    collection = str(command.get("find") or command.get("aggregate") or _collection_from_ns(namespace))
    filter_fields: list[str] = []
    sort_fields: list[str] = []
    has_or = False

    if isinstance(command.get("filter"), dict):
        fields, branch_has_or = _mongo_filter_fields(command["filter"])
        filter_fields.extend(fields)
        has_or = has_or or branch_has_or

    if isinstance(command.get("sort"), dict):
        sort_fields.extend(command["sort"].keys())

    pipeline = command.get("pipeline")
    if isinstance(pipeline, list):
        for stage in pipeline:
            if not isinstance(stage, dict):
                continue
            match_stage = stage.get("$match")
            if isinstance(match_stage, dict):
                fields, branch_has_or = _mongo_filter_fields(match_stage)
                filter_fields.extend(fields)
                has_or = has_or or branch_has_or
            sort_stage = stage.get("$sort")
            if isinstance(sort_stage, dict):
                sort_fields.extend(sort_stage.keys())

    return collection, _dedupe(filter_fields), _dedupe(sort_fields), has_or


def _collection_from_ns(namespace: Optional[str]) -> str:
    if not namespace:
        return "<collection>"
    parts = namespace.split(".", 1)
    return parts[1] if len(parts) == 2 else namespace


def _mongo_filter_fields(filter_doc: dict[str, Any], prefix: str = "") -> tuple[list[str], bool]:
    fields: list[str] = []
    has_or = False
    for key, value in filter_doc.items():
        if key in {"$or", "$and", "$nor"}:
            has_or = has_or or key == "$or"
            if isinstance(value, list):
                for branch in value:
                    if isinstance(branch, dict):
                        branch_fields, branch_has_or = _mongo_filter_fields(branch, prefix)
                        fields.extend(branch_fields)
                        has_or = has_or or branch_has_or
            continue
        if key.startswith("$"):
            continue

        dotted_key = f"{prefix}.{key}" if prefix else key
        fields.append(dotted_key)

        if isinstance(value, dict) and not any(child_key.startswith("$") for child_key in value):
            nested_fields, nested_has_or = _mongo_filter_fields(value, dotted_key)
            fields.extend(nested_fields)
            has_or = has_or or nested_has_or

    return _dedupe(fields), has_or


def _mongo_index_example(collection: str, fields: list[str]) -> str:
    if not fields:
        fields = ["<field>"]
    key_spec = ", ".join(f'"{field}": 1' for field in fields)
    return f'db.getCollection("{collection or "<collection>"}").createIndex({{{key_spec}}})'


def _sql_recommendations(db_type: str, query_text: str) -> list[Recommendation]:
    recommendations: list[Recommendation] = []
    sql = " ".join(query_text.strip().rstrip(";").split())
    lowered = sql.lower()

    table = _primary_table(sql)
    where_columns = _where_columns(sql)
    join_columns = _join_columns(sql)
    order_columns = _order_columns(sql)

    index_columns = _dedupe(where_columns + join_columns + order_columns)
    if index_columns:
        recommendations.append(
            Recommendation(
                priority="High",
                category="Index",
                title="Create or verify an index for filter and join columns",
                rationale=(
                    "The logged query filters, joins, or sorts on these columns. A matching "
                    "index can reduce full scans and expensive sort/hash operations."
                ),
                example=_sql_index_example(db_type, table, index_columns),
            )
        )

    if join_columns:
        recommendations.append(
            Recommendation(
                priority="Medium",
                category="Index",
                title="Check indexes on both sides of joins",
                rationale=(
                    "Join latency often comes from one unindexed side or stale row estimates. "
                    "Verify indexes on the referenced keys and compare estimated rows with actual rows."
                ),
            )
        )

    if "select *" in lowered:
        recommendations.append(
            Recommendation(
                priority="Medium",
                category="Query rewrite",
                title="Project only required columns",
                rationale=(
                    "`SELECT *` increases I/O, memory, network transfer, and can prevent "
                    "covering-index plans on hot paths."
                ),
            )
        )

    if re.search(r"\blike\s+['\"]%", lowered):
        recommendations.append(
            Recommendation(
                priority="Medium",
                category="Query rewrite",
                title="Avoid leading-wildcard LIKE on high-volume paths",
                rationale=(
                    "Patterns such as `LIKE '%abc'` usually cannot seek into a normal B-tree index. "
                    "Consider prefix search, full-text search, trigram indexes in PostgreSQL, or a search engine."
                ),
            )
        )

    if re.search(r"\b(lower|upper|date|cast|coalesce|substring)\s*\(", lowered):
        recommendations.append(
            Recommendation(
                priority="Medium",
                category="Query rewrite",
                title="Avoid wrapping indexed columns in functions",
                rationale=(
                    "Function calls around predicates can stop normal indexes from being used. "
                    "Use stored/generated columns, expression indexes, or rewrite the predicate range."
                ),
            )
        )

    if " order by " in lowered and " limit " not in lowered:
        recommendations.append(
            Recommendation(
                priority="Low",
                category="Query rewrite",
                title="Add pagination or a bounded LIMIT when possible",
                rationale=(
                    "Large unbounded sorts are expensive even when an index helps. Page through "
                    "stable ordering keys for UI/reporting workloads."
                ),
            )
        )

    if " or " in lowered:
        recommendations.append(
            Recommendation(
                priority="Low",
                category="Query rewrite",
                title="Review OR predicates for selectivity",
                rationale=(
                    "OR-heavy predicates can cause weak plans. Compare a composite/partial index "
                    "against a UNION ALL rewrite for the most selective branches."
                ),
            )
        )

    if not recommendations:
        recommendations.append(
            Recommendation(
                priority="Low",
                category="Plan review",
                title="Capture an execution plan for this query shape",
                rationale=(
                    "No obvious query-shape issue was detected from the log line alone. Use the "
                    "selected explain mode to verify scans, row estimates, sort nodes, and index usage."
                ),
            )
        )

    return recommendations


def _primary_table(sql: str) -> str:
    match = re.search(rf"\bfrom\s+({_IDENTIFIER})", sql, re.IGNORECASE)
    return _strip_identifier(match.group(1)) if match else "<table>"


def _where_columns(sql: str) -> list[str]:
    where_match = re.search(
        r"\bwhere\b(?P<where>.*?)(\border\s+by\b|\bgroup\s+by\b|\blimit\b|$)",
        sql,
        re.IGNORECASE,
    )
    if not where_match:
        return []

    where_clause = where_match.group("where")
    matches = re.findall(
        rf"({_IDENTIFIER})\s*(=|>|<|>=|<=|<>|!=|\blike\b|\bin\b|\bbetween\b)",
        where_clause,
        re.IGNORECASE,
    )
    return [_column_name(match[0]) for match in matches]


def _join_columns(sql: str) -> list[str]:
    columns: list[str] = []
    for left, right in re.findall(
        rf"\bon\s+({_IDENTIFIER})\s*=\s*({_IDENTIFIER})",
        sql,
        re.IGNORECASE,
    ):
        columns.append(_column_name(left))
        columns.append(_column_name(right))
    return columns


def _order_columns(sql: str) -> list[str]:
    order_match = re.search(r"\border\s+by\b(?P<order>.*?)(\blimit\b|$)", sql, re.IGNORECASE)
    if not order_match:
        return []
    columns = []
    for part in order_match.group("order").split(","):
        match = re.search(rf"({_IDENTIFIER})", part.strip())
        if match:
            columns.append(_column_name(match.group(1)))
    return columns


def _column_name(identifier: str) -> str:
    return _strip_identifier(identifier).split(".")[-1]


def _strip_identifier(identifier: str) -> str:
    return identifier.strip().strip("`\"")


def _sql_index_example(db_type: str, table: str, columns: list[str]) -> str:
    table_name = _strip_identifier(table) or "<table>"
    clean_columns = _dedupe(_column_name(column) for column in columns) or ["<column>"]
    index_name = "idx_" + re.sub(r"\W+", "_", f"{table_name}_{'_'.join(clean_columns)}").strip("_")
    keyword = "CREATE INDEX CONCURRENTLY" if db_type == "PostgreSQL" else "CREATE INDEX"
    column_list = ", ".join(clean_columns)
    return f"{keyword} {index_name} ON {table_name} ({column_list});"
