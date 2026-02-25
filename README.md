# YC Radar

Monitor the [YC Directory](https://www.ycombinator.com/companies) for new startups and send them to a webhook. Designed to run on a schedule (e.g., via GitHub Actions) and notify you when new YC companies are added.

## Features

- **Efficient detection** — Uses Algolia's YC Directory API with timestamp-based queries to avoid full fetches when possible
- **Webhook delivery** — POSTs structured JSON for each new company to your webhook URL
- **Retry queue** — Failed webhooks are saved and retried on the next run
- **Persistent state** — Tracks known companies in `data/known_companies.json` to detect only new entries

## Requirements

- Python 3.12+
- [requests](https://pypi.org/project/requests/) library

## Installation

```bash
git clone https://github.com/yourusername/Scraper.git
cd Scraper
pip install requests
```

## Configuration

1. Copy the example env file:
   ```bash
   cp .env.example .env
   ```

2. Edit `.env` and set your webhook URL:
   ```
   WEBHOOK_URL=https://your-webhook-endpoint.com/...
   ```

### Environment Variables

| Variable | Description |
|----------|-------------|
| `WEBHOOK_URL` | URL to POST new company payloads to |
| `DATA_DIR` | Directory for state files (default: `./data`) |
| `LOG_LEVEL` | Logging level: `DEBUG`, `INFO`, etc. |

## Usage

### Basic run

```bash
python3 yc_radar.py
```

### CLI Options

| Option | Description |
|--------|-------------|
| `--webhook-url URL` | Override `WEBHOOK_URL` env var |
| `--dry-run` | Detect new companies but don't send webhooks |
| `--seed` | Force re-seed: fetch all companies, save state, send nothing |
| `--full-fetch` | Force full batch fetch instead of timestamp shortcut |
| `--data-dir DIR` | Override state directory |
| `--verbose` | Enable debug logging |

### First-time setup

On first run, YC Radar will fetch all companies to establish a baseline and save state. No webhooks are sent during this seed. Subsequent runs will only detect and report new companies.

To force a fresh seed:

```bash
python3 yc_radar.py --seed
```

## GitHub Actions

A workflow is included to run YC Radar on a schedule and commit updated state:

- **Schedule**: Daily at 8am PST (cron: `0 16 * * *`)
- **Manual trigger**: Run from the **Actions** tab via "workflow_dispatch"

### Setup

1. Add `WEBHOOK_URL` as a repository secret: **Settings → Secrets and variables → Actions**
2. Ensure the workflow has permission to push (the workflow commits updated `data/known_companies.json`)

## Webhook Payload

Each new company is sent as a JSON object with the following structure:

```json
{
  "event": "new_yc_company",
  "detected_at": "2025-02-25T12:00:00+00:00",
  "id": "abc123",
  "name": "Company Name",
  "slug": "company-name",
  "url": "https://www.ycombinator.com/companies/company-name",
  "website": "https://company.com",
  "one_liner": "One-line description",
  "long_description": "Full description...",
  "batch": "W25",
  "status": "Public",
  "stage": "Seed",
  "industry": "AI",
  "tags": ["B2B", "SaaS"],
  "is_hiring": true,
  "launched_at": 1737734400,
  "launched_at_human": "2025-01-24"
}
```

## Project Structure

```
.
├── yc_radar.py          # Main script
├── .env.example         # Example environment config
├── .github/workflows/
│   └── yc_radar.yml     # GitHub Actions workflow
└── data/
    ├── known_companies.json   # Persistent state (created on first run)
    └── pending_webhook.json   # Failed webhooks for retry (created when needed)
```
