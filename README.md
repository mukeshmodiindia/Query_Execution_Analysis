# Query Execution Analysis Dashboard

A Streamlit dashboard for reviewing slow query logs, grouping repeated query shapes, and generating version-aware explain-plan and index guidance.

The app is a log analyzer. It does not connect to MongoDB, MySQL, or PostgreSQL by default, and it does not execute queries against production.

## Supported Databases

- MongoDB 6.0, 7.0, 8.0
- MySQL 5.7, 8.0
- PostgreSQL 12, 13, 14, 15, 16

## Features

- Upload logs through the browser or with the CLI uploader.
- Review top queries by total duration, average latency, and occurrence count.
- Analyze repeated query shapes with histograms, timelines, raw query samples, and tuning suggestions.
- Filter detailed analysis by high occurrence, collection scans, average duration, max duration, and MongoDB operation type, and choose the top 10, 20, or 50 queries to analyze in detail.
- Generate explain-plan commands for MongoDB, MySQL, and PostgreSQL.
- Show MongoDB version notes and official explain documentation links.
- Parse MongoDB `planSummary` values such as `COLLSCAN` from logs and rank query shapes by collection scans.
- Group repeated MongoDB query shapes by the logged `queryHash` when present, falling back to the anonymized filter/sort shape so key-order differences don't split identical queries into separate rows.
- Surface the raw MongoDB log operation type (`command`, `query`, `update`, `remove`, `insert`, `getmore`) alongside the inferred CRUD/aggregate operation.
- Identify MongoDB CRUD reads, CRUD writes, and aggregation pipelines from logged command shapes.
- Suggest MongoDB compound indexes using the ESR guideline: equality fields first, sort fields next, and range fields last.
- Suggest an index on the `$lookup` stage's `foreignField` for aggregation pipelines that join another collection.
- Warn when a filter field looks like an array (matched with `$elemMatch`, `$all`, or an array literal), since indexing it creates a multikey index with its own limitations.
- Suggest query rewrites for risky shapes such as aggregation `$match` stages that appear after blocking stages.
- Optionally paste MongoDB `getIndexes()` output per query collection so suggested indexes can be marked as already existing, covered by a compound index, partially overlapping, or new.
- Detect duplicate and potentially redundant MongoDB indexes from shared per-collection `getIndexes()` output.
- Emit application logs to stdout so systemd can capture them in `journalctl`.

## Install And Run

### 1. Install System Packages

Ubuntu or Debian:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip nginx
```

RHEL, Rocky, or AlmaLinux:

```bash
sudo dnf install -y git python3 python3-pip nginx
```

### 2. Clone And Install Python Packages

```bash
git clone https://github.com/mukeshmodiindia/Query_Execution_Analysis.git
cd Query_Execution_Analysis
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Python 3.10 or newer is recommended. Python dependencies are listed in `requirements.txt`.

### 3. Run Locally

For a local workstation:

```bash
streamlit run app.py
```

Open the URL shown by Streamlit, usually `http://localhost:8501`.

### 4. Run On A Cloud VM

For a server exposed through Nginx, keep Streamlit bound to localhost:

```bash
streamlit run app.py --server.address 127.0.0.1 --server.port 8501
```

Create `/etc/systemd/system/query-analysis.service`:

```ini
[Unit]
Description=Query Execution Analysis Streamlit App
After=network.target

[Service]
User=percona
WorkingDirectory=/home/percona/Query_Execution_Analysis
Environment="PATH=/home/percona/Query_Execution_Analysis/.venv/bin"
Environment="QUERY_ANALYSIS_LOG_LEVEL=INFO"
ExecStart=/home/percona/Query_Execution_Analysis/.venv/bin/streamlit run app.py --server.address 127.0.0.1 --server.port 8501
Restart=always

[Install]
WantedBy=multi-user.target
```

Enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now query-analysis
sudo systemctl status query-analysis
```

Configure Nginx to proxy port 80 to Streamlit. Create `/etc/nginx/sites-available/query-analysis`:

```nginx
server {
    listen 80;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:8501;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400;
    }
}
```

Enable and reload:

```bash
sudo ln -s /etc/nginx/sites-available/query-analysis /etc/nginx/sites-enabled/query-analysis
sudo nginx -t
sudo systemctl reload nginx
```

Open `http://<server-public-ip>/`.

If you want to access Streamlit directly at `http://<server-public-ip>:8501`, change the service to `--server.address 0.0.0.0`, then open TCP port `8501` in the VM firewall and cloud security group.

## Upload Logs From CLI

Use the CLI when logs are already on the server or are too large for browser upload:

```bash
python upload_logs.py --db MongoDB --version 8.0 --label prod-mongo-logs /var/log/mongodb/mongod.log
python upload_logs.py --db MySQL --version 8.0 --label mysql-slow /var/log/mysql/mysql-slow.log
python upload_logs.py --db PostgreSQL --version 16 --label postgres-duration /var/log/postgresql/postgresql.log
```

Then refresh Streamlit and choose `Command-line uploads`.

By default, uploaded batches are stored in `.query_analysis_uploads/`. To share a different location between the CLI and Streamlit service:

```bash
export QUERY_ANALYSIS_UPLOAD_DIR=/data/query-analysis-uploads
python upload_logs.py --db MongoDB --version 8.0 --label nightly /var/log/mongodb/mongod.log
streamlit run app.py
```

For systemd, add the same environment variable to the service file:

```ini
Environment="QUERY_ANALYSIS_UPLOAD_DIR=/data/query-analysis-uploads"
```

## Share MongoDB Indexes Per Query

For better MongoDB recommendations, open a MongoDB query in `Detailed Query Analysis` and share the current indexes for that query's collection from `mongosh`:

```javascript
JSON.stringify(db.getCollection("stories").getIndexes())
```

Paste the output into that query's `Share MongoDB Indexes for <collection>` section. The app uses the query collection as context, so output like this is attributed to `stories` when pasted in the `stories` query card:

```json
[{"v":2,"key":{"diggs":1},"name":"diggs_1"},{"v":2,"key":{"_id":1},"name":"_id_"}]
```

The app then shows whether a suggested index already exists, is covered by a longer compound index, partially overlaps, or appears to be new. It also shows duplicate and potentially redundant indexes for that collection.

You can also share multiple collections as JSON:

```json
{
  "orders": [
    {"name": "status_1_customerId_1", "key": {"status": 1, "customerId": 1}}
  ],
  "inventory": [
    {"name": "sku_1", "key": {"sku": 1}}
  ]
}
```

## MongoDB Query Analysis

MongoDB log analysis recognizes common command shapes:

- CRUD reads: `find`, `count`, and `distinct`
- CRUD writes: `insert`, `update`, `delete`, and `findAndModify`
- Aggregation pipelines: `aggregate` with `$match`, `$sort`, `$group`, `$lookup`, `$unwind`, and `$project` stages

For index recommendations, the app uses the ESR guideline:

- Equality predicates first, for example `tenantId: "acme"` or `status: "ACTIVE"`
- Sort keys next, preserving sort direction such as `createdAt: -1`
- Range predicates last, for example `$gt`, `$gte`, `$lt`, `$lte`, `$ne`, `$nin`, or `$regex`

If a range predicate is very selective, the app also suggests comparing an ERS variant with `executionStats`. For aggregation pipelines, the app warns when `$match` appears after blocking stages and suggests moving it earlier when the match references original collection fields.

## Troubleshooting

### Browser Times Out On `http://<server-ip>:8501`

Your service is bound to `127.0.0.1:8501`, which is only reachable from the server itself. That is expected with the recommended Nginx setup.

Use one of these options:

- Open `http://<server-ip>/` through Nginx on port 80.
- Or change Streamlit to `--server.address 0.0.0.0` and open port `8501` in the VM firewall and cloud security group.

Check from the server:

```bash
curl -I http://127.0.0.1:8501
curl -I http://127.0.0.1
```

Check listening sockets:

```bash
sudo ss -ltnp | grep -E ':80|:8501'
```

### Query Analysis Logs

Application logs go to stdout and are captured by systemd:

```bash
sudo journalctl -u query-analysis -f
sudo journalctl -u query-analysis --since "30 minutes ago"
```

For more detail, set:

```ini
Environment="QUERY_ANALYSIS_LOG_LEVEL=DEBUG"
```

Then reload and restart:

```bash
sudo systemctl daemon-reload
sudo systemctl restart query-analysis
```

### Nginx `conflicting server name "_"` Warning

This means more than one enabled Nginx site is using `server_name _` on port 80. Nginx will ignore one of them.

Inspect enabled sites:

```bash
sudo nginx -T | grep -n "server_name _"
ls -l /etc/nginx/sites-enabled
```

Disable the duplicate default site if needed:

```bash
sudo unlink /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

### No Query Events Parsed

Confirm the selected database matches the log format and rerun the CLI uploader:

```bash
python upload_logs.py --db MongoDB --version 8.0 --label prod-mongo-logs /var/log/mongodb/mongod.log
```

Then check the parsed event count:

```bash
sudo journalctl -u query-analysis --since "30 minutes ago"
```

## Log Upload Limits

- Browser upload accepts up to 20 files per run.
- Large files are processed in memory, so use the CLI uploader for large production logs.
- For very large batches, pre-filter logs or process them in smaller batches.

## Infrastructure Sizing

For large mixed database logs with pandas-based aggregation:

- Minimum: 8 vCPU, 32 GB RAM, 100 GB SSD
- Recommended: 16 vCPU, 64 GB RAM, 200 GB SSD
- Heavy usage: 16 to 32 vCPU, 64 to 128 GB RAM, 300 GB SSD

## Sample Log Formats

MongoDB:

```json
{"ts":"2026-01-01T10:00:00Z","durationMillis":240,"ns":"shop.orders","command":{"find":"orders","filter":{"status":"PENDING"}}}
```

MySQL slow log:

```text
# Query_time: 1.245  Lock_time: 0.002 Rows_sent: 10 Rows_examined: 905
SELECT * FROM orders WHERE status = 'PENDING';
```

PostgreSQL log:

```text
2026-01-01 10:00:00 UTC [123] LOG:  duration: 120.500 ms  statement: SELECT * FROM orders WHERE status='PENDING';
```
