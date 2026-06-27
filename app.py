from __future__ import annotations

from collections import Counter
from typing import Any, Optional

import pandas as pd
import plotly.express as px
import streamlit as st

from src.parsers import normalize_query, parser_for
from src.recommendations import recommendations_for_query
from src.storage import list_upload_batches, load_upload_events
from src.version_profiles import VERSION_PROFILES


st.set_page_config(page_title="Query Execution Analysis", layout="wide")

MAX_LOG_FILES = 20


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
    rows: list[dict[str, Any]] = []
    for event in events:
        rows.append(
            {
                "timestamp": event.timestamp,
                "query": event.query,
                "normalized_query": normalize_query(event.query, db_type),
                "duration_ms": event.duration_ms,
                "namespace": event.namespace,
                "source_line": event.source_line,
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


def _explain_language(db_type: str) -> str:
    return "javascript" if db_type == "MongoDB" else "sql"


st.title("Query Execution Analysis Dashboard")
st.caption("Upload logs from the command prompt or browser, then review slow query shapes and tuning suggestions in the UI.")

source = st.radio(
    "Log source",
    ["Command-line uploads", "Browser upload or paste"],
    horizontal=True,
)

rows: list[dict[str, Any]] = []
selected_manifest: Optional[dict[str, Any]] = None

if source == "Command-line uploads":
    batches = list_upload_batches()
    if not batches:
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
    st.error("No query events were parsed. Check your log format and selected database.")
    st.stop()

df = pd.DataFrame(rows)
df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
df["duration_ms"] = pd.to_numeric(df["duration_ms"], errors="coerce").fillna(0)

agg = (
    df.groupby("normalized_query", as_index=False)
    .agg(
        occurrences=("normalized_query", "count"),
        total_duration_ms=("duration_ms", "sum"),
        avg_duration_ms=("duration_ms", "mean"),
        max_duration_ms=("duration_ms", "max"),
    )
    .sort_values(["total_duration_ms", "occurrences"], ascending=[False, False])
)

st.subheader("Top Query Review")
metric1, metric2, metric3, metric4 = st.columns(4)
metric1.metric("Parsed events", len(df))
metric2.metric("Unique query shapes", len(agg))
metric3.metric("Total observed time (ms)", f"{df['duration_ms'].sum():,.2f}")
metric4.metric("Average latency (ms)", f"{df['duration_ms'].mean():,.2f}")

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

st.dataframe(agg.head(50), width="stretch")

st.subheader("Detailed Query Analysis")
min_occ = st.slider("Minimum occurrences", 1, int(max(1, agg["occurrences"].max())), 2)
analysis_limit = st.slider("Number of top queries to analyze", 1, 50, 10)
frequent = agg[agg["occurrences"] >= min_occ]
if frequent.empty:
    st.warning("No query meets selected minimum occurrences.")
    st.stop()

ranked = frequent.sort_values(["total_duration_ms", "occurrences"], ascending=[False, False]).head(
    analysis_limit
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
    query_df = df[df["normalized_query"] == query_text].copy()
    sample_originals = Counter(query_df["query"]).most_common(3)
    representative_query = sample_originals[0][0] if sample_originals else query_text
    namespace = _first_namespace(query_df)

    with st.container(border=True):
        st.markdown(f"### #{rank} Query")
        st.code(query_text, language=_explain_language(db_type))
        m1, m2, m3 = st.columns(3)
        m1.metric("Occurrences", int(selected["occurrences"]))
        m2.metric("Total duration (ms)", f"{selected['total_duration_ms']:.2f}")
        m3.metric("Average duration (ms)", f"{selected['avg_duration_ms']:.2f}")

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

        st.markdown("#### Index & Query Suggestions")
        recommendations = recommendations_for_query(db_type, representative_query, namespace)
        for recommendation in recommendations:
            expanded = recommendation.priority == "High"
            with st.expander(
                f"{recommendation.priority} - {recommendation.category}: {recommendation.title}",
                expanded=expanded,
            ):
                st.write(recommendation.rationale)
                if recommendation.example:
                    st.code(recommendation.example, language=_explain_language(db_type))

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
            mode_for_call = explain_mode if explain_mode != "queryPlanner" else None
            if mode_for_call:
                st.code(
                    f"db.<collection>.explain('{mode_for_call}').find(<filter>)",
                    language="javascript",
                )
            else:
                st.code("db.<collection>.explain().find(<filter>)", language="javascript")
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
