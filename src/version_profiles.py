VERSION_PROFILES = {
    "MongoDB": {
        "6.0": {
            "supports": ["queryPlanner", "executionStats", "allPlansExecution"],
            "notes": "Explain modes are available through db.collection.explain(). Higher verbosity modes can add measurable overhead.",
            "docs": "https://www.mongodb.com/docs/v6.0/reference/method/db.collection.explain/",
        },
        "7.0": {
            "supports": ["queryPlanner", "executionStats", "allPlansExecution"],
            "notes": "Slot-based execution engine improvements can change plan details versus older releases.",
            "docs": "https://www.mongodb.com/docs/v7.0/reference/method/db.collection.explain/",
        },
        "8.0": {
            "supports": ["queryPlanner", "executionStats", "allPlansExecution"],
            "notes": "Planner output and optimizer behavior may differ from 6.x/7.x; validate plans in the exact production version.",
            "docs": "https://www.mongodb.com/docs/v8.0/reference/method/db.collection.explain/",
        },
    },
    "MySQL": {
        "5.7": {
            "supports": ["EXPLAIN", "EXPLAIN FORMAT=JSON"],
            "notes": "Baseline plan inspection. Some modern runtime analysis features are absent in 5.7.",
            "docs": "https://dev.mysql.com/doc/refman/5.7/en/explain.html",
        },
        "8.0": {
            "supports": ["EXPLAIN", "EXPLAIN ANALYZE", "EXPLAIN FORMAT=JSON"],
            "notes": "EXPLAIN ANALYZE executes the statement (8.0.18+) and returns runtime metrics; use cautiously on write queries.",
            "docs": "https://dev.mysql.com/doc/refman/8.0/en/explain.html",
        },
    },
    "PostgreSQL": {
        "12": {
            "supports": ["EXPLAIN", "EXPLAIN (ANALYZE, BUFFERS)"],
            "notes": "ANALYZE executes the statement. Use transaction-safe approach for DML.",
            "docs": "https://www.postgresql.org/docs/12/sql-explain.html",
        },
        "13": {
            "supports": ["EXPLAIN", "EXPLAIN (ANALYZE, BUFFERS)", "EXPLAIN (ANALYZE, BUFFERS, WAL)"],
            "notes": "WAL usage can be inspected with EXPLAIN WAL in modern releases.",
            "docs": "https://www.postgresql.org/docs/13/sql-explain.html",
        },
        "14": {
            "supports": ["EXPLAIN", "EXPLAIN (ANALYZE, BUFFERS)", "EXPLAIN (ANALYZE, BUFFERS, WAL)"],
            "notes": "Runtime node instrumentation continues to evolve.",
            "docs": "https://www.postgresql.org/docs/14/sql-explain.html",
        },
        "15": {
            "supports": ["EXPLAIN", "EXPLAIN (ANALYZE, BUFFERS)", "EXPLAIN (ANALYZE, BUFFERS, WAL)"],
            "notes": "Check planner changes if comparing with older environments.",
            "docs": "https://www.postgresql.org/docs/15/sql-explain.html",
        },
        "16": {
            "supports": ["EXPLAIN", "EXPLAIN (ANALYZE, BUFFERS, WAL)"],
            "notes": "Newer telemetry options available in modern versions.",
            "docs": "https://www.postgresql.org/docs/16/sql-explain.html",
        },
    },
}
