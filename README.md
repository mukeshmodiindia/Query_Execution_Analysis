# Query Execution Analysis Dashboard

A Streamlit dashboard that reviews top queries from database logs, then analyzes the highest-time queries by occurrence count.

## Supported databases
- MongoDB (v6.0, v7.0, v8.0 profiles)
- MySQL (v5.7, v8.0 profiles)
- PostgreSQL (v12, v13, v14, v15, v16 profiles)

## Features
- Upload logs and parse query events per database type.
- Top query review by:
  - total duration
  - average duration
  - occurrence count
- Detailed view for the highest-time query among frequently occurring queries.
- Explain-plan simulation workflow:
  - MongoDB `explain()` (`queryPlanner`)
  - MongoDB `explain('executionStats')`
  - MongoDB `explain('allPlansExecution')` with warning banner
- UI dashboards:
  - bar charts for top queries
  - latency distribution histograms
  - timeline and table views
- Version-aware notes with links to official docs for MongoDB, MySQL, PostgreSQL.

> Note: This tool does not execute queries directly against your database by default. It generates the exact commands and guidance for running explain plans safely. You can wire in live execution in your environment.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

## Sample log formats

### MongoDB
```json
{"ts":"2026-01-01T10:00:00Z","durationMillis":240,"ns":"shop.orders","command":{"find":"orders","filter":{"status":"PENDING"}}}
```

### MySQL slow log (single line)
```text
# Query_time: 1.245  Lock_time: 0.002 Rows_sent: 10 Rows_examined: 905
SELECT * FROM orders WHERE status = 'PENDING';
```

### PostgreSQL log_line_prefix style sample
```text
2026-01-01 10:00:00 UTC [123] LOG:  duration: 120.500 ms  statement: SELECT * FROM orders WHERE status='PENDING';
```
