from __future__ import annotations

from collections import Counter
from typing import Any, Optional

import pandas as pd
import plotly.express as px
import streamlit as st

from src.logging_config import configure_logging
from src.mongo_indexes import (
    MongoIndex,
    duplicate_index_rows,
    mongo_index_rows,
    parse_mongo_indexes,
    redundant_index_rows,
    review_row,
    review_suggested_index,
    suggested_index_from_example,
)
from src.parsers import normalize_query, parser_for
from src.recommendations import (
    mongo_collection_for_query,
    mongo_operation_for_query,
    recommendations_for_query,
)
from src.storage import list_upload_batches, load_upload_events
from src.version_profiles import VERSION_PROFILES


st.set_page_config(page_title="Query Execution Analysis", layout="wide")

MAX_LOG_FILES = 20
LOGGER = configure_logging(__name__)


def _batch_label(manifest: dict[str, Any]) -> str:
    created_at = manifest.get("created_at", "unknown time")
    return (
        f"{manifest.get('label', manifest.get('id'))} | "
        f"{manifest.get('db_type')} {manifest.get('db_version')} | "
        f"{manifest.get('event_count', 0)} events | {created_at}"
    )


def _browser_upload_rows(db_type: str, raw_text: str) -> list[dict[str, Any]]:
    parse = parser_for(db_type)
    events = parse(raw_text)
    LOGGER.info(
        "browser_upload_parsed db_type=%s parsed_events=%s input_bytes=%s",
        db_type,
        len(events),
        len(raw_text.encode("utf-8", errors="ignore")),
    )
    rows: list[dict[str, Any]] = []
    for event in events:
        rows.append(
            {
                "timestamp": event.timestamp,
                "query": event.query,
                "normalized_query": normalize_query(event.query, db_type),
                "duration_ms": event.duration_ms,
                "namespace": event.namespace,
                "plan_summary": event.plan_summary,
                "source_line": event.source_line,
                "query_hash": event.query_hash,
                "op_type": event.op_type,
                "file_name": None,
            }
        )
    return rows


def _version_or_default(db_type: str, db_version: Optional[str]) -> str:
    versions = list(VERSION_PROFILES[db_type].keys())
    return db_version if db_version in VERSION_PROFILES[db_type] else versions[-1]


def _render_profile(db_type: str, db_version: str, explain_mode: str) -> None:
    profile = VERSION_PROFILES[db_type][db_version]
    st.info(f"Version note: {profile['notes']}")
    st.markdown(f"Official docs: {profile['docs']}")

    if db_type == "MongoDB" and explain_mode == "allPlansExecution":
        st.warning(
            "`allPlansExecution` executes candidate plans and can be expensive. "
            "Run carefully in production and verify index strategy before/after testing."
        )
    if explain_mode in {"EXPLAIN ANALYZE", "EXPLAIN (ANALYZE, BUFFERS)", "EXPLAIN (ANALYZE, BUFFERS, WAL)"}:
        st.warning(
            "The selected explain mode can execute the query. Use a transaction-safe approach "
            "for writes and run against production only with care."
        )


def _first_namespace(query_df: pd.DataFrame) -> Optional[str]:
    if "namespace" not in query_df:
        return None
    values = query_df["namespace"].dropna()
    return str(values.iloc[0]) if not values.empty else None


def _collection_from_namespace(namespace: Optional[str]) -> Optional[str]:
    if not namespace or "." not in namespace:
        return None
    return namespace.split(".", 1)[1]


def _collection_from_query(query_text: str, namespace: Optional[str]) -> Optional[str]:
    collection = mongo_collection_for_query(query_text, namespace)
    if collection and collection != "<collection>":
        return collection
    return _collection_from_namespace(namespace)


def _mongo_operation_group(operation: str) -> str:
    if operation == "Aggregate":
        return "Aggregate"
    if operation in {"Find", "Count", "Distinct"}:
        return "CRUD read"
    if operation in {"Insert", "Update", "Delete", "FindAndModify"}:
        return "CRUD write"
    return "Unknown"


def _mongodb_explain_command(operation: str, collection: Optional[str], explain_mode: str) -> str:
    collection_name = collection or "<collection>"
    explain_call = ".explain()" if explain_mode == "queryPlanner" else f'.explain("{explain_mode}")'
    prefix = f'db.getCollection("{collection_name}"){explain_call}'
    if operation == "Aggregate":
        return f'{prefix}.aggregate([{{"$match": <filter>}}, <pipeline stages>])'
    if operation == "Count":
        return f"{prefix}.countDocuments(<filter>)"
    if operation == "Distinct":
        return f'{prefix}.distinct("<field>", <filter>)'
    if operation == "Update":
        return f'{prefix}.updateOne(<filter>, {{"$set": <update>}})'
    if operation == "Delete":
        return f"{prefix}.deleteOne(<filter>)"
    if operation == "FindAndModify":
        return f"{prefix}.findAndModify({{query: <filter>, update: <update>}})"
    return f"{prefix}.find(<filter>)"


def _explain_language(db_type: str) -> str:
    return "javascript" if db_type == "MongoDB" else "sql"


def _render_mongodb_existing_indexes(collection: Optional[str], key_suffix: str) -> list[MongoIndex]:
    collection_label = collection or "<collection>"
    with st.expander(f"Share MongoDB Indexes for {collection_label}"):
        st.caption(
            "Can you share the current collection indexes? Paste `getIndexes()` JSON output "
            "for this query's collection for more accurate suggestions."
        )
        st.code(
            f'JSON.stringify(db.getCollection("{collection_label}").getIndexes())',
            language="javascript",
        )
        raw_indexes = st.text_area(
            f"Paste getIndexes() JSON output for {collection_label}",
            height=160,
            help="Optional. Share current collection indexes so suggestions can be marked as already existing, covered, or new.",
            placeholder='[{"name":"status_1_customerId_1","key":{"status":1,"customerId":1}}]',
            key=f"mongo_indexes_{key_suffix}",
        )

        if not raw_indexes.strip():
            return []

        try:
            existing_indexes = parse_mongo_indexes(raw_indexes, default_collection=collection)
        except ValueError as exc:
            LOGGER.warning("mongo_index_parse_failed error=%s", exc)
            st.error(str(exc))
            return []

        index_rows = mongo_index_rows(existing_indexes)
        if index_rows:
            st.dataframe(pd.DataFrame(index_rows), width="stretch")

        duplicates = duplicate_index_rows(existing_indexes)
        if duplicates:
            st.warning("Duplicate existing indexes found.")
            st.dataframe(pd.DataFrame(duplicates), width="stretch")

        redundant = redundant_index_rows(existing_indexes)
        if redundant:
            st.warning("Potentially redundant existing indexes found.")
            st.dataframe(pd.DataFrame(redundant), width="stretch")

        LOGGER.info(
            "mongo_indexes_loaded collection=%s index_count=%s duplicate_groups=%s redundant_candidates=%s",
            collection_label,
            len(existing_indexes),
            len(duplicates),
            len(redundant),
        )
        return existing_indexes


st.title("Query Execution Analysis Dashboard")
st.caption("Upload logs from the command prompt or browser, then review slow query shapes and tuning suggestions in the UI.")

if "query_analysis_session_logged" not in st.session_state:
    LOGGER.info("streamlit_session_started")
    st.session_state["query_analysis_session_logged"] = True

source = st.radio(
    "Log source",
    ["Command-line uploads", "Browser upload or paste"],
    horizontal=True,
)
LOGGER.info("streamlit_source_selected source=%s", source)

rows: list[dict[str, Any]] = []
selected_manifest: Optional[dict[str, Any]] = None

if source == "Command-line uploads":
    batches = list_upload_batches()
    if not batches:
        LOGGER.info("command_line_uploads_empty")
        st.info(
            "No command-line uploads were found yet. Run a command like "
            "`python upload_logs.py --db MongoDB --version 8.0 samples/mongodb_sample.log`, "
            "then refresh this page."
        )
        st.stop()

    batch_ids = [batch["id"] for batch in batches]
    batch_by_id = {batch["id"]: batch for batch in batches}
    selected_batch_id = st.selectbox(
        "Uploaded log batch",
        batch_ids,
        format_func=lambda batch_id: _batch_label(batch_by_id[batch_id]),
    )
    selected_manifest = batch_by_id[selected_batch_id]
    db_type = selected_manifest["db_type"]
    db_version = _version_or_default(db_type, selected_manifest.get("db_version"))

    col1, col2, col3 = st.columns(3)
    with col1:
        st.text_input("Database", value=db_type, disabled=True)
    with col2:
        st.text_input("Version", value=db_version, disabled=True)
    with col3:
        explain_mode = st.selectbox(
            "Explain mode",
            VERSION_PROFILES[db_type][db_version]["supports"],
        )

    with st.expander("Selected upload details"):
        st.json(selected_manifest)

    rows = load_upload_events(selected_batch_id)
    LOGGER.info(
        "command_line_upload_selected batch_id=%s db_type=%s db_version=%s rows=%s",
        selected_batch_id,
        db_type,
        db_version,
        len(rows),
    )
else:
    col1, col2, col3 = st.columns(3)
    with col1:
        db_type = st.selectbox("Database", ["MongoDB", "MySQL", "PostgreSQL"])
    with col2:
        db_version = st.selectbox("Version", list(VERSION_PROFILES[db_type].keys()))
    with col3:
        explain_mode = st.selectbox(
            "Explain mode",
            VERSION_PROFILES[db_type][db_version]["supports"],
        )

    uploaded = st.file_uploader(
        "Upload log files",
        type=["log", "txt", "json"],
        accept_multiple_files=True,
        help=f"Upload up to {MAX_LOG_FILES} files in one run.",
    )
    manual_text = st.text_area("Or paste log content", height=180)

    raw_text = ""
    if uploaded:
        if len(uploaded) > MAX_LOG_FILES:
            st.error(f"You uploaded {len(uploaded)} files. The limit is {MAX_LOG_FILES} files per run.")
            st.stop()

        parts = [file.getvalue().decode("utf-8", errors="ignore") for file in uploaded]
        raw_text = "\n".join(parts)

        total_size_mb = sum(getattr(file, "size", 0) for file in uploaded) / (1024 * 1024)
        LOGGER.info(
            "browser_files_loaded file_count=%s combined_size_mb=%.2f db_type=%s db_version=%s",
            len(uploaded),
            total_size_mb,
            db_type,
            db_version,
        )
        st.caption(
            f"Loaded {len(uploaded)} file(s), combined size: {total_size_mb:,.2f} MB. "
            "For very large files, use the command-line uploader or pre-filter logs for better responsiveness."
        )
    elif manual_text.strip():
        raw_text = manual_text

    if not raw_text.strip():
        _render_profile(db_type, db_version, explain_mode)
        st.stop()

    rows = _browser_upload_rows(db_type, raw_text)

_render_profile(db_type, db_version, explain_mode)

if not rows:
    LOGGER.warning("no_query_events_parsed db_type=%s db_version=%s source=%s", db_type, db_version, source)
    st.error("No query events were parsed. Check your log format and selected database.")
    st.stop()

df = pd.DataFrame(rows)
df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
df["duration_ms"] = pd.to_numeric(df["duration_ms"], errors="coerce").fillna(0)
if "plan_summary" not in df:
    df["plan_summary"] = ""
if "source_line" not in df:
    df["source_line"] = ""
if "query_hash" not in df:
    df["query_hash"] = ""
if "op_type" not in df:
    df["op_type"] = ""
df["plan_summary"] = df["plan_summary"].fillna("")
df["source_line"] = df["source_line"].fillna("")
df["query_hash"] = df["query_hash"].fillna("")
df["op_type"] = df["op_type"].fillna("")
df["is_collection_scan"] = (
    df["plan_summary"].str.contains("COLLSCAN", case=False, na=False)
    | df["source_line"].str.contains("COLLSCAN", case=False, na=False)
)
df["group_key"] = df["query_hash"].where(df["query_hash"].astype(str).str.strip() != "", df["normalized_query"])
if db_type == "MongoDB":
    df["query_operation"] = df.apply(
        lambda row: mongo_operation_for_query(
            str(row["query"]),
            str(row["namespace"]) if pd.notna(row["namespace"]) else None,
        ),
        axis=1,
    )
    df["operation_group"] = df["query_operation"].apply(_mongo_operation_group)
else:
    df["query_operation"] = ""
    df["operation_group"] = ""

agg = (
    df.groupby("group_key", as_index=False)
    .agg(
        normalized_query=("normalized_query", "first"),
        namespace=("namespace", lambda values: next((str(value) for value in values.dropna()), "")),
        operation=("query_operation", lambda values: next((str(value) for value in values.dropna()), "")),
        operation_group=("operation_group", lambda values: next((str(value) for value in values.dropna()), "")),
        log_op_type=("op_type", lambda values: next((str(v) for v in values if str(v).strip()), "")),
        occurrences=("group_key", "count"),
        total_duration_ms=("duration_ms", "sum"),
        avg_duration_ms=("duration_ms", "mean"),
        max_duration_ms=("duration_ms", "max"),
        collection_scans=("is_collection_scan", "sum"),
    )
    .sort_values(["total_duration_ms", "occurrences"], ascending=[False, False])
)
agg["collection_scans"] = agg["collection_scans"].astype(int)
agg["collection_scan_rate"] = (agg["collection_scans"] / agg["occurrences"] * 100).round(1)
agg["collection"] = agg.apply(
    lambda row: _collection_from_query(str(row["normalized_query"]), str(row["namespace"]) or None) or "",
    axis=1,
)
LOGGER.info(
    "analysis_ready db_type=%s db_version=%s rows=%s unique_query_count=%s total_duration_ms=%.2f collection_scans=%s",
    db_type,
    db_version,
    len(df),
    len(agg),
    df["duration_ms"].sum(),
    int(df["is_collection_scan"].sum()),
)

st.subheader("Top Query Review")
metric1, metric2, metric3, metric4, metric5 = st.columns(5)
metric1.metric("Parsed events", len(df))
metric2.metric("Unique query shapes", len(agg))
metric3.metric("Total observed time (ms)", f"{df['duration_ms'].sum():,.2f}")
metric4.metric("Average latency (ms)", f"{df['duration_ms'].mean():,.2f}")
metric5.metric("Collection scans", int(df["is_collection_scan"].sum()))

left, right = st.columns(2)
with left:
    top_total = agg.head(15)
    fig_total = px.bar(
        top_total,
        x="total_duration_ms",
        y="normalized_query",
        orientation="h",
        title="Top queries by total duration",
    )
    fig_total.update_layout(yaxis={"categoryorder": "total ascending"})
    st.plotly_chart(fig_total, width="stretch")

with right:
    top_occ = agg.sort_values(["occurrences", "total_duration_ms"], ascending=[False, False]).head(15)
    fig_occ = px.bar(
        top_occ,
        x="occurrences",
        y="normalized_query",
        orientation="h",
        title="Top queries by occurrences",
    )
    fig_occ.update_layout(yaxis={"categoryorder": "total ascending"})
    st.plotly_chart(fig_occ, width="stretch")

if db_type == "MongoDB" and agg["collection_scans"].sum() > 0:
    top_scans = agg.sort_values(["collection_scans", "total_duration_ms"], ascending=[False, False]).head(15)
    fig_scans = px.bar(
        top_scans,
        x="collection_scans",
        y="normalized_query",
        orientation="h",
        title="Top queries by collection scans",
    )
    fig_scans.update_layout(yaxis={"categoryorder": "total ascending"})
    st.plotly_chart(fig_scans, width="stretch")

review_columns = [
    "collection",
    "operation",
    "log_op_type",
    "occurrences",
    "collection_scans",
    "collection_scan_rate",
    "total_duration_ms",
    "avg_duration_ms",
    "max_duration_ms",
    "normalized_query",
]
st.dataframe(agg[[column for column in review_columns if column in agg]].head(50), width="stretch")

st.subheader("Detailed Query Analysis")
focus_options = {
    "High occurrence": ["occurrences", "total_duration_ms"],
    "Collection scan": ["collection_scans", "total_duration_ms", "occurrences"],
    "High total duration": ["total_duration_ms", "occurrences"],
    "High average duration": ["avg_duration_ms", "occurrences"],
    "High max duration": ["max_duration_ms", "occurrences"],
}
default_focus = "Collection scan" if db_type == "MongoDB" and agg["collection_scans"].sum() > 0 else "High total duration"
focus = st.selectbox(
    "Analysis focus",
    list(focus_options.keys()),
    index=list(focus_options.keys()).index(default_focus),
)

filter1, filter2, filter3, filter4, filter5 = st.columns(5)
max_occurrences = int(max(1, agg["occurrences"].max()))
default_min_occ = 2 if focus == "High occurrence" and max_occurrences > 1 else 1
with filter1:
    min_occ = st.number_input(
        "Minimum occurrences",
        min_value=1,
        max_value=max_occurrences,
        value=min(default_min_occ, max_occurrences),
        step=1,
        key=f"min_occ_{focus}",
    )
with filter2:
    min_avg_duration = st.number_input(
        "Minimum average duration (ms)",
        min_value=0.0,
        value=0.0,
        step=10.0,
        key=f"min_avg_duration_{focus}",
    )
with filter3:
    min_max_duration = st.number_input(
        "Minimum max duration (ms)",
        min_value=0.0,
        value=0.0,
        step=10.0,
        key=f"min_max_duration_{focus}",
    )
with filter4:
    analysis_limit = st.radio(
        "Queries to analyze",
        [10, 20, 50],
        horizontal=True,
        key=f"analysis_limit_{focus}",
    )
with filter5:
    operation_filter = "All"
    if db_type == "MongoDB":
        operation_filter = st.selectbox(
            "Mongo operation",
            [
                "All",
                "CRUD read",
                "CRUD write",
                "Aggregate",
                "Find",
                "Count",
                "Distinct",
                "Insert",
                "Update",
                "Delete",
                "FindAndModify",
                "Unknown",
            ],
            key=f"mongo_operation_filter_{focus}",
        )

only_collection_scans = False
if db_type == "MongoDB":
    only_collection_scans = st.checkbox(
        "Only queries with collection scans",
        value=focus == "Collection scan",
        key=f"only_collection_scans_{focus}",
    )

filtered_queries = agg[
    (agg["occurrences"] >= min_occ)
    & (agg["avg_duration_ms"] >= min_avg_duration)
    & (agg["max_duration_ms"] >= min_max_duration)
].copy()
if db_type == "MongoDB" and operation_filter != "All":
    if operation_filter in {"CRUD read", "CRUD write", "Aggregate"}:
        filtered_queries = filtered_queries[filtered_queries["operation_group"] == operation_filter]
    else:
        filtered_queries = filtered_queries[filtered_queries["operation"] == operation_filter]
if only_collection_scans:
    filtered_queries = filtered_queries[filtered_queries["collection_scans"] > 0]

if filtered_queries.empty:
    st.warning("No query meets the selected operation, occurrence, collection scan, and duration filters.")
    st.stop()

rank_columns = focus_options[focus]
ranked = filtered_queries.sort_values(rank_columns, ascending=[False] * len(rank_columns)).head(
    int(analysis_limit)
)
if ranked.empty:
    st.warning("No query available for detailed analysis.")
    st.stop()

page_size = 5
total_queries = len(ranked)
total_pages = max(1, (total_queries + page_size - 1) // page_size)
page = st.number_input("Analysis page", min_value=1, max_value=total_pages, value=1, step=1)
start_idx = (page - 1) * page_size
end_idx = min(start_idx + page_size, total_queries)
page_queries = ranked.iloc[start_idx:end_idx]

st.write(
    f"Showing detailed analysis for queries **{start_idx + 1}-{end_idx}** of "
    f"**{total_queries}** (page {page}/{total_pages})."
)

for rank, (_, selected) in enumerate(page_queries.iterrows(), start=start_idx + 1):
    query_text = selected["normalized_query"]
    query_df = df[df["group_key"] == selected["group_key"]].copy()
    sample_originals = Counter(query_df["query"]).most_common(3)
    representative_query = sample_originals[0][0] if sample_originals else query_text
    namespace = _first_namespace(query_df)
    collection = str(selected.get("collection") or "") or _collection_from_query(representative_query, namespace)
    operation = str(selected.get("operation") or "") or mongo_operation_for_query(representative_query, namespace)
    plan_summaries = [str(value) for value in query_df["plan_summary"].dropna().unique() if str(value).strip()]

    with st.container(border=True):
        st.markdown(f"### #{rank} Query")
        st.code(query_text, language=_explain_language(db_type))
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Occurrences", int(selected["occurrences"]))
        m2.metric("Total duration (ms)", f"{selected['total_duration_ms']:.2f}")
        m3.metric("Average duration (ms)", f"{selected['avg_duration_ms']:.2f}")
        m4.metric("Max duration (ms)", f"{selected['max_duration_ms']:.2f}")
        m5.metric("Collection scans", int(selected.get("collection_scans", 0)))
        if db_type == "MongoDB":
            log_op_type = str(selected.get("log_op_type") or "")
            st.write(
                f"Operation: `{operation or 'Unknown'}` | "
                f"Collection: `{collection or '<unknown>'}` | "
                f"Collection scan rate: `{selected.get('collection_scan_rate', 0):.1f}%`"
                + (f" | Log op type: `{log_op_type}`" if log_op_type else "")
            )
            if plan_summaries:
                st.write(f"Observed plan summaries: `{', '.join(plan_summaries[:5])}`")

        fig_hist = px.histogram(
            query_df,
            x="duration_ms",
            nbins=20,
            title=f"Duration distribution for query #{rank} (ms)",
        )
        st.plotly_chart(fig_hist, width="stretch")

        if query_df["timestamp"].notna().any():
            timeline = query_df.dropna(subset=["timestamp"]).sort_values("timestamp")
            fig_line = px.line(
                timeline,
                x="timestamp",
                y="duration_ms",
                title=f"Latency timeline for query #{rank}",
            )
            st.plotly_chart(fig_line, width="stretch")

        existing_mongo_indexes: list[MongoIndex] = []
        if db_type == "MongoDB":
            existing_mongo_indexes = _render_mongodb_existing_indexes(collection, str(rank))

        st.markdown("#### Index & Query Suggestions")
        recommendations = recommendations_for_query(db_type, representative_query, namespace)
        recommendation_reviews = []
        for recommendation in recommendations:
            index_review = None
            if db_type == "MongoDB" and existing_mongo_indexes and recommendation.example:
                suggested_index = suggested_index_from_example(recommendation.example)
                if suggested_index:
                    index_review = review_suggested_index(
                        suggested_index,
                        existing_mongo_indexes,
                        namespace,
                    )
            recommendation_reviews.append((recommendation, index_review))

        for recommendation, index_review in recommendation_reviews:
            expanded = recommendation.priority == "High"
            with st.expander(
                f"{recommendation.priority} - {recommendation.category}: {recommendation.title}",
                expanded=expanded,
            ):
                st.write(recommendation.rationale)
                if index_review:
                    st.write(f"Existing index check: {index_review.status}. {index_review.detail}")
                if recommendation.example:
                    st.code(recommendation.example, language=_explain_language(db_type))

        index_review_rows = [
            review_row(recommendation.title, index_review)
            for recommendation, index_review in recommendation_reviews
            if index_review
        ]
        if index_review_rows:
            st.markdown("#### Suggested Index Review")
            st.dataframe(pd.DataFrame(index_review_rows), width="stretch")

        st.markdown("#### Explain Plan Guidance")
        st.markdown(
            "Before final tuning, confirm whether supporting indexes already exist and compare "
            "estimated rows/documents with actual work done by the plan."
        )

        with st.expander(f"Representative raw queries for query #{rank}"):
            for query, count in sample_originals:
                st.write(f"Count: {count}")
                st.code(query, language=_explain_language(db_type))

        if db_type == "MongoDB":
            st.markdown("**Run in Mongo shell / mongosh**")
            st.code(
                _mongodb_explain_command(operation, collection, explain_mode),
                language="javascript",
            )
            st.write("Expected output fields to inspect:")
            st.json(
                {
                    "winningPlan": "Check COLLSCAN vs IXSCAN",
                    "executionStats.totalDocsExamined": "High docs examined indicates missing or inefficient index",
                    "executionStats.totalKeysExamined": "Compare with docs examined and docs returned",
                    "executionStats.executionTimeMillis": "Correlate with log latency",
                    "allPlansExecution": "Review rejected plans, index candidates, and score trade-offs",
                }
            )
        else:
            if db_type == "MySQL":
                command = "EXPLAIN ANALYZE " if explain_mode == "EXPLAIN ANALYZE" else "EXPLAIN "
                st.code(f"{command}<your_query>;", language="sql")
            elif db_type == "PostgreSQL":
                st.code(f"{explain_mode} <your_query>;", language="sql")

            st.write(
                "Inspect full scan indicators, estimated vs actual rows, index usage, sort nodes, "
                "temporary tables, and high-cost plan nodes."
            )

        st.markdown("#### Index & Sample Data Checklist")
        if db_type == "MongoDB":
            st.code("db.<collection>.getIndexes()", language="javascript")
            if operation == "Aggregate":
                st.code('db.<collection>.aggregate([{"$match": <filter>}, <pipeline stages>]).limit(5)', language="javascript")
            elif operation in {"Update", "Delete", "FindAndModify"}:
                st.code("db.<collection>.find(<same filter>).limit(5)", language="javascript")
            elif operation == "Insert":
                st.code("db.<collection>.estimatedDocumentCount()", language="javascript")
            else:
                st.code("db.<collection>.find(<filter>).limit(5)", language="javascript")
            st.markdown(
                "Share existing index definitions, estimated document count/cardinality for filtered fields, "
                "and a few representative documents before applying index changes."
            )
        elif db_type == "MySQL":
            st.code("SHOW INDEX FROM <table>;", language="sql")
            st.code("SELECT * FROM <table> WHERE <same_predicate> LIMIT 5;", language="sql")
            st.markdown(
                "Share current indexes, table row count estimate, and sample rows for columns used in "
                "WHERE/JOIN/ORDER BY."
            )
        else:
            st.code(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = '<schema>' AND tablename = '<table>';",
                language="sql",
            )
            st.code("SELECT * FROM <schema>.<table> WHERE <same_predicate> LIMIT 5;", language="sql")
            st.markdown(
                "Share index definitions, table statistics freshness, and sample rows for key predicates."
            )
