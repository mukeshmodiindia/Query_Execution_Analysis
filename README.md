# Query Execution Analysis Dashboard

A Streamlit dashboard that reviews top queries from database logs, then analyzes the highest-time queries by occurrence count.

## Supported databases
- MongoDB (v6.0, v7.0, v8.0 profiles)
- MySQL (v5.7, v8.0 profiles)
- PostgreSQL (v12, v13, v14, v15, v16 profiles)

## Features
- Upload one or multiple logs and parse query events per database type.
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

## Log file upload limits

- The UI supports uploading **up to 20 log files per run**.
- You can upload **10 or 20 files** directly from the uploader.
- If more than 20 files are selected, the app stops with a clear error.
- There is no hard per-file size check in code, but very large files consume memory because they are combined and processed in Python.

### Practical sizing guidance
For large datasets (for example 10 files × 2 GB each), process logs in batches or pre-filter logs first, because in-memory parsing and DataFrame aggregation can use significant RAM.

## Quick start (local)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

## OS and package requirements

### Supported OS
- Ubuntu 22.04/24.04 LTS
- Debian 12+
- RHEL/Rocky/AlmaLinux 8+

### Required system packages (Linux)
Install common runtime packages before Python dependencies:

```bash
# Ubuntu/Debian
sudo apt update
sudo apt install -y python3 python3-venv python3-pip nginx

# RHEL/Rocky/AlmaLinux
sudo dnf install -y python3 python3-pip nginx
python3 -m venv .venv
```

### Python packages
Python dependencies are listed in `requirements.txt`:
- `streamlit>=1.34.0`
- `pandas>=2.0.0`
- `plotly>=5.20.0`

Install with:

```bash
pip install -r requirements.txt
```

## Cloud installation guide (AWS/Azure/GCP Linux VM)

The steps are the same across AWS EC2, Azure VM, and GCP Compute Engine.

### 1) Provision VM
- Linux VM (Ubuntu 22.04 LTS recommended)
- Open ports:
  - `22` for SSH
  - `80` for Nginx

### 2) Clone and install

```bash
git clone https://github.com/mukeshmodiindia/Query_Execution_Analysis.git
cd Query_Execution_Analysis
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 3) Run Streamlit on localhost

```bash
streamlit run app.py --server.address 127.0.0.1 --server.port 8501
```

For persistent service, use systemd (`/etc/systemd/system/query-analysis.service`):

```ini
[Unit]
Description=Query Execution Analysis Streamlit App
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/Query_Execution_Analysis
Environment="PATH=/home/ubuntu/Query_Execution_Analysis/.venv/bin"
ExecStart=/home/ubuntu/Query_Execution_Analysis/.venv/bin/streamlit run app.py --server.address 127.0.0.1 --server.port 8501
Restart=always

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now query-analysis
sudo systemctl status query-analysis
```

### 4) Configure Nginx to expose UI
Create `/etc/nginx/sites-available/query-analysis`:

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

Now open: `http://<your-server-public-ip>/`

## Infrastructure sizing recommendation (10 logs × 2 GB = 20 GB total)

For mixed MongoDB/MySQL/PostgreSQL logs with pandas-based parsing in this app:

- **Minimum (batch processing, lower concurrency):**
  - 8 vCPU
  - 32 GB RAM
  - 100+ GB SSD
- **Recommended (stable parsing/aggregation, safer headroom):**
  - 16 vCPU
  - 64 GB RAM
  - 200+ GB SSD
- **Heavy/production with multiple users:**
  - 16–32 vCPU
  - 64–128 GB RAM
  - 300+ GB SSD

### Why these numbers?
- Raw logs (20 GB) expand in memory during parsing.
- DataFrame/group-by operations add overhead.
- Concurrent users and larger query cardinality increase peak RAM/CPU demand.

### Example cloud instance families
- AWS: `m6i.2xlarge` (8 vCPU/32 GB) minimum, `m6i.4xlarge` (16 vCPU/64 GB) recommended
- Azure: `D8s v5` minimum, `D16s v5` recommended
- GCP: `n2-standard-8` minimum, `n2-standard-16` recommended

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
