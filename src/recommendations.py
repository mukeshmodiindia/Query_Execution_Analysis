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


@dataclass
class MongoCommandParts:
    operation: str
    collection: str
    equality_fields: list[str]
    range_fields: list[str]
    sort_fields: list[tuple[str, str]]
    has_or: bool = False
    has_regex: bool = False
    empty_filter: bool = False
    aggregate_match_after_blocking: bool = False
    aggregate_has_match: bool = False


_IDENTIFIER = r"[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?"
_MONGO_OPERATION_KEYS = (
    ("aggregate", "Aggregate"),
    ("find", "Find"),
    ("count", "Count"),
    ("distinct", "Distinct"),
    ("update", "Update"),
    ("delete", "Delete"),
    ("insert", "Insert"),
    ("findAndModify", "FindAndModify"),
    ("findandmodify", "FindAndModify"),
)
_MONGO_RANGE_OPERATORS = {
    "$gt",
    "$gte",
    "$lt",
    "$lte",
    "$ne",
    "$nin",
    "$regex",
    "$not",
    "$geoWithin",
    "$geoIntersects",
}
_MONGO_EQUALITY_OPERATORS = {"$eq", "$in"}


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


def mongo_operation_for_query(query_text: str, namespace: Optional[str] = None) -> str:
    command = _parse_json_object(query_text)
    if not command:
        return "Unknown"
    return _mongo_command_parts(command, namespace).operation


def mongo_collection_for_query(query_text: str, namespace: Optional[str] = None) -> str:
    command = _parse_json_object(query_text)
    if not command:
        return _collection_from_ns(namespace)
    return _mongo_command_parts(command, namespace).collection


def _mongodb_recommendations(query_text: str, namespace: Optional[str]) -> list[Recommendation]:
    recommendations: list[Recommendation] = []
    command = _parse_json_object(query_text)

    if command:
        parts = _mongo_command_parts(command, namespace)
        esr_keys = _mongo_esr_index_keys(parts)
        if esr_keys:
            recommendations.append(
                Recommendation(
                    priority="High",
                    category="Index",
                    title="Create or verify an ESR compound index",
                    rationale=(
                        "This query shape has equality, sort, or range predicates. Apply MongoDB's "
                        "ESR guideline: equality fields first, sort fields next, and range fields last. "
                        "If a range predicate is highly selective, compare the ESR index against an ERS "
                        "variant with explain output before applying it."
                    ),
                    example=_mongo_index_example(parts.collection, esr_keys),
                )
            )
        if parts.sort_fields:
            recommendations.append(
                Recommendation(
                    priority="Medium",
                    category="Index",
                    title="Validate sort coverage in the compound index",
                    rationale=(
                        "A sort can be served from the index when the preceding index keys are satisfied "
                        "by equality predicates and the sort keys appear in the same order and direction."
                    ),
                    example=_mongo_index_example(parts.collection, esr_keys),
                )
            )
        if parts.range_fields and parts.sort_fields:
            ers_keys = _mongo_ers_index_keys(parts)
            recommendations.append(
                Recommendation(
                    priority="Low",
                    category="Plan review",
                    title="Compare ESR with an ERS variant if the range is highly selective",
                    rationale=(
                        "MongoDB's ESR guidance keeps sort fields before range fields to avoid in-memory "
                        "sorts. If the range predicate dramatically reduces the result set, an equality, "
                        "range, sort order can sometimes win; verify both with executionStats."
                    ),
                    example=_mongo_index_example(parts.collection, ers_keys),
                )
            )
        if parts.has_or:
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
        if parts.empty_filter and parts.operation in {"Find", "Count", "Update", "Delete", "FindAndModify"}:
            recommendations.append(
                Recommendation(
                    priority="High" if parts.operation in {"Update", "Delete", "FindAndModify"} else "Medium",
                    category="Query rewrite",
                    title="Add a selective predicate or confirm the full-collection operation is intentional",
                    rationale=(
                        f"This {parts.operation} command has no selective filter in the log shape. "
                        "For hot paths, add an indexed predicate; for maintenance jobs, keep it isolated "
                        "from latency-sensitive traffic and validate the plan."
                    ),
                    example=_mongo_selective_predicate_example(parts),
                )
            )
        if parts.operation == "Insert":
            recommendations.append(
                Recommendation(
                    priority="Low",
                    category="Index",
                    title="Review write overhead from secondary indexes",
                    rationale=(
                        "Insert commands do not benefit from a predicate index. For write-heavy collections, "
                        "remove duplicate or unused secondary indexes because every insert must maintain them."
                    ),
                )
            )
        if parts.aggregate_match_after_blocking:
            recommendations.append(
                Recommendation(
                    priority="High",
                    category="Query rewrite",
                    title="Move `$match` before blocking aggregation stages when semantics allow",
                    rationale=(
                        "The pipeline has a `$match` after `$sort`, `$group`, `$lookup`, `$unwind`, or "
                        "`$project`. Push filters earlier when they reference original fields so fewer "
                        "documents reach expensive stages."
                    ),
                    example=_mongo_match_first_example(parts.collection),
                )
            )
        elif parts.operation == "Aggregate" and not parts.aggregate_has_match:
            recommendations.append(
                Recommendation(
                    priority="Medium",
                    category="Query rewrite",
                    title="Add an early `$match` stage if the aggregation does not need the full collection",
                    rationale=(
                        "Aggregation pipelines that start from the full collection can be expensive. "
                        "Use an early indexed `$match` for user, tenant, time-window, or status filters "
                        "when the business result permits it."
                    ),
                    example=_mongo_match_first_example(parts.collection),
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
) -> MongoCommandParts:
    operation_key, operation = _mongo_operation(command)
    collection = str(command.get(operation_key) or _collection_from_ns(namespace))
    parts = MongoCommandParts(
        operation=operation,
        collection=collection,
        equality_fields=[],
        range_fields=[],
        sort_fields=[],
    )

    if operation in {"Find", "Count", "Distinct", "Unknown"}:
        _extend_filter_parts(parts, command.get("filter") or command.get("query"))
        _extend_sort_parts(parts, command.get("sort"))
        if operation == "Distinct" and isinstance(command.get("key"), str):
            parts.equality_fields.append(command["key"])

    if operation == "Update":
        updates = command.get("updates")
        if isinstance(updates, list):
            for update_spec in updates:
                if isinstance(update_spec, dict):
                    _extend_filter_parts(parts, update_spec.get("q"))
                    _extend_sort_parts(parts, update_spec.get("sort"))
        else:
            _extend_filter_parts(parts, command.get("q") or command.get("query") or command.get("filter"))

    if operation == "Delete":
        deletes = command.get("deletes")
        if isinstance(deletes, list):
            for delete_spec in deletes:
                if isinstance(delete_spec, dict):
                    _extend_filter_parts(parts, delete_spec.get("q"))
        else:
            _extend_filter_parts(parts, command.get("q") or command.get("query") or command.get("filter"))

    if operation == "FindAndModify":
        _extend_filter_parts(parts, command.get("query") or command.get("filter"))
        _extend_sort_parts(parts, command.get("sort"))

    pipeline = command.get("pipeline")
    if isinstance(pipeline, list):
        blocking_stage_seen = False
        for stage in pipeline:
            if not isinstance(stage, dict):
                continue
            match_stage = stage.get("$match")
            if isinstance(match_stage, dict):
                parts.aggregate_has_match = True
                if blocking_stage_seen:
                    parts.aggregate_match_after_blocking = True
                _extend_filter_parts(parts, match_stage)
            sort_stage = stage.get("$sort")
            if isinstance(sort_stage, dict):
                _extend_sort_parts(parts, sort_stage)
            if any(key in stage for key in {"$sort", "$group", "$lookup", "$unwind", "$project"}):
                blocking_stage_seen = True

    parts.equality_fields = _dedupe(parts.equality_fields)
    parts.range_fields = _dedupe(parts.range_fields)
    parts.sort_fields = _dedupe_index_keys(parts.sort_fields)
    if (
        operation in {"Find", "Count", "Distinct", "Update", "Delete", "FindAndModify"}
        and not parts.equality_fields
        and not parts.range_fields
    ):
        parts.empty_filter = True
    return parts


def _mongo_operation(command: dict[str, Any]) -> tuple[str, str]:
    for key, label in _MONGO_OPERATION_KEYS:
        if key in command:
            return key, label
    return "", "Unknown"


def _extend_filter_parts(parts: MongoCommandParts, filter_doc: Any) -> None:
    if not isinstance(filter_doc, dict):
        return
    equality_fields, range_fields, has_or, has_regex = _mongo_filter_fields(filter_doc)
    parts.equality_fields.extend(equality_fields)
    parts.range_fields.extend(range_fields)
    parts.has_or = parts.has_or or has_or
    parts.has_regex = parts.has_regex or has_regex


def _extend_sort_parts(parts: MongoCommandParts, sort_doc: Any) -> None:
    if not isinstance(sort_doc, dict):
        return
    parts.sort_fields.extend(_mongo_sort_fields(sort_doc))


def _collection_from_ns(namespace: Optional[str]) -> str:
    if not namespace:
        return "<collection>"
    parts = namespace.split(".", 1)
    return parts[1] if len(parts) == 2 else namespace


def _mongo_filter_fields(filter_doc: dict[str, Any], prefix: str = "") -> tuple[list[str], list[str], bool, bool]:
    equality_fields: list[str] = []
    range_fields: list[str] = []
    has_or = False
    has_regex = False
    for key, value in filter_doc.items():
        if key in {"$or", "$and", "$nor"}:
            has_or = has_or or key == "$or"
            if isinstance(value, list):
                for branch in value:
                    if isinstance(branch, dict):
                        branch_equalities, branch_ranges, branch_has_or, branch_has_regex = _mongo_filter_fields(
                            branch,
                            prefix,
                        )
                        equality_fields.extend(branch_equalities)
                        range_fields.extend(branch_ranges)
                        has_or = has_or or branch_has_or
                        has_regex = has_regex or branch_has_regex
            continue
        if key.startswith("$"):
            continue

        dotted_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            operator_keys = {child_key for child_key in value if child_key.startswith("$")}
            if operator_keys:
                if operator_keys & _MONGO_EQUALITY_OPERATORS:
                    equality_fields.append(dotted_key)
                if operator_keys & _MONGO_RANGE_OPERATORS:
                    range_fields.append(dotted_key)
                if "$regex" in operator_keys:
                    has_regex = True
                if not (operator_keys & _MONGO_EQUALITY_OPERATORS) and not (operator_keys & _MONGO_RANGE_OPERATORS):
                    range_fields.append(dotted_key)
            else:
                nested_equalities, nested_ranges, nested_has_or, nested_has_regex = _mongo_filter_fields(
                    value,
                    dotted_key,
                )
                if nested_equalities or nested_ranges:
                    equality_fields.extend(nested_equalities)
                else:
                    equality_fields.append(dotted_key)
                range_fields.extend(nested_ranges)
                has_or = has_or or nested_has_or
                has_regex = has_regex or nested_has_regex
        else:
            equality_fields.append(dotted_key)

    return _dedupe(equality_fields), _dedupe(range_fields), has_or, has_regex


def _mongo_sort_fields(sort_doc: dict[str, Any]) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = []
    for field, direction in sort_doc.items():
        fields.append((str(field), _normalize_mongo_index_direction(direction)))
    return fields


def _mongo_esr_index_keys(parts: MongoCommandParts) -> list[tuple[str, str]]:
    keys = [(field, "1") for field in parts.equality_fields]
    keys.extend(parts.sort_fields)
    keys.extend((field, "1") for field in parts.range_fields)
    return _dedupe_index_keys(keys)


def _mongo_ers_index_keys(parts: MongoCommandParts) -> list[tuple[str, str]]:
    keys = [(field, "1") for field in parts.equality_fields]
    keys.extend((field, "1") for field in parts.range_fields)
    keys.extend(parts.sort_fields)
    return _dedupe_index_keys(keys)


def _dedupe_index_keys(keys: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for field, direction in keys:
        clean_field = field.strip().strip("`\"")
        if not clean_field or clean_field in seen:
            continue
        seen.add(clean_field)
        result.append((clean_field, direction))
    return result


def _normalize_mongo_index_direction(direction: Any) -> str:
    if isinstance(direction, float) and direction.is_integer():
        direction = int(direction)
    if direction in {-1, "-1", "desc", "DESC"}:
        return "-1"
    if direction in {1, "1", "asc", "ASC"}:
        return "1"
    return str(direction)


def _format_mongo_index_direction(direction: str) -> str:
    if direction in {"1", "-1"}:
        return direction
    return json.dumps(direction)


def _mongo_index_example(collection: str, fields: list[str] | list[tuple[str, str]]) -> str:
    if not fields:
        fields = ["<field>"]
    index_fields: list[tuple[str, str]] = []
    for field in fields:
        if isinstance(field, tuple):
            index_fields.append(field)
        else:
            index_fields.append((field, "1"))
    key_spec = ", ".join(
        f'"{field}": {_format_mongo_index_direction(direction)}'
        for field, direction in _dedupe_index_keys(index_fields)
    )
    return f'db.getCollection("{collection or "<collection>"}").createIndex({{{key_spec}}})'


def _mongo_match_first_example(collection: str) -> str:
    return (
        f'db.getCollection("{collection or "<collection>"}").aggregate(['
        '{"$match": {"<selectiveField>": "<value>"}}, '
        '"<remaining pipeline stages>"'
        "])"
    )


def _mongo_selective_predicate_example(parts: MongoCommandParts) -> str:
    collection = parts.collection or "<collection>"
    if parts.operation == "Aggregate":
        return _mongo_match_first_example(collection)
    if parts.operation == "Update":
        return f'db.getCollection("{collection}").updateOne({{"<selectiveField>": "<value>"}}, {{"$set": {{"<field>": "<value>"}}}})'
    if parts.operation == "Delete":
        return f'db.getCollection("{collection}").deleteOne({{"<selectiveField>": "<value>"}})'
    if parts.operation == "Count":
        return f'db.getCollection("{collection}").countDocuments({{"<selectiveField>": "<value>"}})'
    return f'db.getCollection("{collection}").find({{"<selectiveField>": "<value>"}}).limit(100)'


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
