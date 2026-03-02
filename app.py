from __future__ import annotations

import json
from collections import Counter

import pandas as pd
import plotly.express as px
import streamlit as st

from src.parsers import normalize_query, parser_for
from src.version_profiles import VERSION_PROFILES

st.set_page_config(page_title="Query Execution Analysis", layout="wide")

st.title("Query Execution Analysis Dashboard")
st.caption("Top query review from logs with version-aware explain plan guidance.")

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

profile = VERSION_PROFILES[db_type][db_version]
st.info(f"Version note: {profile['notes']}")
st.markdown(f"Official docs: {profile['docs']}")

if db_type == "MongoDB" and explain_mode == "allPlansExecution":
    st.warning(
        "`allPlansExecution` executes candidate plans and can be expensive. "
        "Run carefully in production and verify index strategy before/after testing."
    )

uploaded = st.file_uploader("Upload log file", type=["log", "txt", "json"])
manual_text = st.text_area("Or paste log content", height=180)

raw_text = ""
if uploaded is not None:
    raw_text = uploaded.getvalue().decode("utf-8", errors="ignore")
elif manual_text.strip():
    raw_text = manual_text

if not raw_text.strip():
    st.stop()

parse = parser_for(db_type)
events = parse(raw_text)

if not events:
    st.error("No query events were parsed. Check your log format and selected database.")
    st.stop()

rows = []
for e in events:
    rows.append(
        {
            "timestamp": e.timestamp,
            "query": e.query,
            "normalized_query": normalize_query(e.query),
            "duration_ms": e.duration_ms,
            "namespace": e.namespace,
        }
    )

df = pd.DataFrame(rows)

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
metric1, metric2, metric3 = st.columns(3)
metric1.metric("Parsed events", len(df))
metric2.metric("Unique normalized queries", len(agg))
metric3.metric("Total observed time (ms)", f"{df['duration_ms'].sum():,.2f}")

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
    st.plotly_chart(fig_total, use_container_width=True)

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
    st.plotly_chart(fig_occ, use_container_width=True)

st.dataframe(agg.head(50), use_container_width=True)

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
    f"Showing detailed analysis for queries **{start_idx + 1}–{end_idx}** of "
    f"**{total_queries}** (page {page}/{total_pages})."
)

for rank, (_, selected) in enumerate(page_queries.iterrows(), start=start_idx + 1):
    query_text = selected["normalized_query"]
    with st.container(border=True):
        st.markdown(f"### #{rank} Query")
        st.code(query_text, language="sql")
        m1, m2, m3 = st.columns(3)
        m1.metric("Occurrences", int(selected["occurrences"]))
        m2.metric("Total duration (ms)", f"{selected['total_duration_ms']:.2f}")
        m3.metric("Average duration (ms)", f"{selected['avg_duration_ms']:.2f}")

        query_df = df[df["normalized_query"] == query_text].copy()
        fig_hist = px.histogram(
            query_df,
            x="duration_ms",
            nbins=20,
            title=f"Duration distribution for query #{rank} (ms)",
        )
        st.plotly_chart(fig_hist, use_container_width=True)

        if query_df["timestamp"].notna().any():
            timeline = query_df.dropna(subset=["timestamp"]).sort_values("timestamp")
            fig_line = px.line(
                timeline,
                x="timestamp",
                y="duration_ms",
                title=f"Latency timeline for query #{rank}",
            )
            st.plotly_chart(fig_line, use_container_width=True)

        st.markdown("#### Explain Plan Guidance")

        sample_originals = Counter(df[df["normalized_query"] == query_text]["query"]).most_common(3)
        with st.expander(f"Representative raw queries for query #{rank}"):
            for q, c in sample_originals:
                st.write(f"Count: {c}")
                st.code(q)

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
                    "executionStats.totalDocsExamined": "High docs examined indicates missing/inefficient index",
                    "executionStats.totalKeysExamined": "Compare with docs examined",
                    "executionStats.executionTimeMillis": "Correlate with log latency",
                    "allPlansExecution": "Review rejected plans, index candidates, and score trade-offs",
                }
            )
        else:
            if db_type == "MySQL":
                cmd = (
                    "EXPLAIN ANALYZE " if explain_mode == "EXPLAIN ANALYZE" else "EXPLAIN "
                )
                st.code(f"{cmd}<your_query>;", language="sql")
            elif db_type == "PostgreSQL":
                st.code(f"{explain_mode} <your_query>;", language="sql")

            st.write(
                "Inspect full scan indicators, estimated vs actual rows, index usage, and high-cost plan nodes."
            )
