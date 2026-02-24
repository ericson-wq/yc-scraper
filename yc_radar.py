#!/usr/bin/env python3
"""YC Radar — Monitor YC Directory for new startups and send them to a webhook."""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Algolia credentials (same as ~/yc_scraper.py)
# ---------------------------------------------------------------------------
ALGOLIA_APP_ID = "45BWZJ1SGC"
ALGOLIA_API_KEY = (
    "ZjA3NWMwMmNhMzEwZmMxOThkZDlkMjFmNDAwNTNjNjdkZjdhNWJkOWRjMThiODQwMjUyZTVkYjA4"
    "YjFlMmU2YnJlc3RyaWN0SW5kaWNlcz0lNUIlMjJZQ0NvbXBhbnlfcHJvZHVjdGlvbiUyMiUyQyUy"
    "MllDQ29tcGFueV9CeV9MYXVuY2hfRGF0ZV9wcm9kdWN0aW9uJTIyJTVEJnRhZ0ZpbHRlcnM9JTVC"
    "JTIyeWNkY19wdWJsaWMlMjIlNUQmYW5hbHl0aWNzVGFncz0lNUIlMjJ5Y2RjJTIyJTVE"
)
INDEX_PRODUCTION = "YCCompany_production"
INDEX_BY_LAUNCH = "YCCompany_By_Launch_Date_production"
YC_BASE_URL = "https://www.ycombinator.com/companies"

HEADERS = {
    "X-Algolia-Application-Id": ALGOLIA_APP_ID,
    "X-Algolia-API-Key": ALGOLIA_API_KEY,
    "Content-Type": "application/json",
}

STATE_FILE = "known_companies.json"
PENDING_FILE = "pending_webhook.json"
STATE_VERSION = 1

log = logging.getLogger("yc_radar")


# ---------------------------------------------------------------------------
# Algolia helpers
# ---------------------------------------------------------------------------
def algolia_url(index):
    return f"https://{ALGOLIA_APP_ID}-dsn.algolia.net/1/indexes/{index}/query"


def algolia_query(index, params, max_retries=3):
    """Send a query to Algolia with retries and exponential backoff."""
    url = algolia_url(index)
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, headers=HEADERS, json={"params": params}, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            if attempt == max_retries:
                raise
            wait = 2 ** attempt
            log.warning("Algolia request failed (attempt %d/%d): %s — retrying in %ds",
                        attempt, max_retries, exc, wait)
            time.sleep(wait)


def fetch_count():
    """Quick check: return total number of companies (1 API call)."""
    result = algolia_query(INDEX_PRODUCTION, "hitsPerPage=0&facets=batch")
    count = result.get("nbHits", 0)
    log.info("Current directory count: %d", count)
    return count


def fetch_since(timestamp):
    """Fetch companies launched after the given Unix timestamp."""
    params = f'hitsPerPage=1000&numericFilters=["launched_at>{timestamp}"]'
    result = algolia_query(INDEX_BY_LAUNCH, params)
    hits = result.get("hits", [])
    log.info("Fetched %d hits launched after %d", len(hits), timestamp)
    return hits


def fetch_batch_names():
    """Fetch all batch names from Algolia facets."""
    result = algolia_query(INDEX_PRODUCTION, "hitsPerPage=0&facets=batch")
    batches = result.get("facets", {}).get("batch", {})
    log.info("Found %d batches, total: %d", len(batches), sum(batches.values()))
    return sorted(batches.keys())


def fetch_all_companies():
    """Full fetch: query every batch and return all hits."""
    batches = fetch_batch_names()
    all_hits = []
    for i, batch in enumerate(batches, 1):
        log.debug("  [%d/%d] Fetching batch: %s", i, len(batches), batch)
        params = f'hitsPerPage=1000&facetFilters=["batch:{batch}"]'
        result = algolia_query(INDEX_PRODUCTION, params)
        hits = result.get("hits", [])
        all_hits.extend(hits)
        time.sleep(0.1)
    log.info("Full fetch complete: %d companies across %d batches", len(all_hits), len(batches))
    return all_hits


# ---------------------------------------------------------------------------
# Company extraction
# ---------------------------------------------------------------------------
def extract_company(hit):
    """Extract a structured dict from an Algolia hit for the webhook payload."""
    slug = hit.get("slug", "")
    launched_at = hit.get("launched_at")
    launched_human = ""
    if launched_at:
        try:
            launched_human = datetime.fromtimestamp(launched_at, tz=timezone.utc).strftime("%Y-%m-%d")
        except (OSError, ValueError):
            pass

    return {
        "id": hit.get("objectID") or hit.get("id"),
        "name": hit.get("name", ""),
        "slug": slug,
        "url": f"{YC_BASE_URL}/{slug}" if slug else "",
        "website": hit.get("website", ""),
        "one_liner": hit.get("one_liner", ""),
        "long_description": hit.get("long_description", ""),
        "batch": hit.get("batch", ""),
        "status": hit.get("status", ""),
        "stage": hit.get("stage", ""),
        "industry": hit.get("industry", ""),
        "subindustry": hit.get("subindustry", ""),
        "industries": hit.get("industries", []),
        "tags": hit.get("tags", []),
        "team_size": hit.get("team_size", 0),
        "all_locations": hit.get("all_locations", ""),
        "regions": hit.get("regions", []),
        "is_hiring": hit.get("isHiring", False),
        "nonprofit": hit.get("nonprofit", False),
        "top_company": hit.get("top_company", False),
        "small_logo_thumb_url": hit.get("small_logo_thumb_url", ""),
        "launched_at": launched_at,
        "launched_at_human": launched_human,
    }


def hit_id(hit):
    """Get a stable string ID from an Algolia hit."""
    return str(hit.get("objectID") or hit.get("id") or "")


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------
def load_state(data_dir):
    """Load known-companies state from disk. Returns None if no state file."""
    path = Path(data_dir) / STATE_FILE
    if not path.exists():
        log.info("No state file found at %s — first run", path)
        return None
    with open(path, "r") as f:
        state = json.load(f)
    log.info("Loaded state: %d known IDs, last run %s",
             len(state.get("known_ids", [])), state.get("last_run_at", "never"))
    return state


def save_state(data_dir, known_ids, total_count):
    """Persist state to disk."""
    now = datetime.now(timezone.utc)
    state = {
        "last_run_at": now.isoformat(),
        "last_run_timestamp": int(now.timestamp()),
        "total_count": total_count,
        "known_ids": sorted(known_ids),
        "version": STATE_VERSION,
    }
    path = Path(data_dir) / STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f, indent=2)
    log.info("State saved: %d IDs, count=%d", len(known_ids), total_count)


# ---------------------------------------------------------------------------
# Pending webhook (retry queue)
# ---------------------------------------------------------------------------
def load_pending(data_dir):
    path = Path(data_dir) / PENDING_FILE
    if not path.exists():
        return None
    with open(path, "r") as f:
        return json.load(f)


def save_pending(data_dir, payload):
    path = Path(data_dir) / PENDING_FILE
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    log.warning("Saved pending webhook payload to %s", path)


def clear_pending(data_dir):
    path = Path(data_dir) / PENDING_FILE
    if path.exists():
        path.unlink()
        log.info("Cleared pending webhook file")


# ---------------------------------------------------------------------------
# Webhook delivery
# ---------------------------------------------------------------------------
def send_webhook(url, payload, max_retries=3):
    """POST JSON to webhook URL with retries. Returns True on success."""
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, json=payload, timeout=30)
            resp.raise_for_status()
            log.info("Webhook delivered successfully (HTTP %d)", resp.status_code)
            return True
        except requests.RequestException as exc:
            if attempt == max_retries:
                log.error("Webhook delivery failed after %d attempts: %s", max_retries, exc)
                return False
            wait = 2 ** attempt
            log.warning("Webhook attempt %d/%d failed: %s — retrying in %ds",
                        attempt, max_retries, exc, wait)
            time.sleep(wait)


def build_payload(hit):
    """Build a single webhook JSON payload from one Algolia hit."""
    company = extract_company(hit)
    company["event"] = "new_yc_company"
    company["detected_at"] = datetime.now(timezone.utc).isoformat()
    return company


def send_all_webhooks(url, new_hits, data_dir):
    """Send one webhook per company. Returns (sent_count, failed_hits)."""
    sent = 0
    failed = []
    for h in new_hits:
        payload = build_payload(h)
        name = h.get("name", "Unknown")
        if send_webhook(url, payload):
            sent += 1
            log.info("Sent webhook for %s", name)
        else:
            failed.append(h)
            log.error("Failed to send webhook for %s", name)
        time.sleep(0.2)  # polite rate limiting
    return sent, failed


# ---------------------------------------------------------------------------
# Core detection logic
# ---------------------------------------------------------------------------
def seed(data_dir):
    """First run: fetch everything, save state, send nothing."""
    log.info("Seeding — fetching all companies to establish baseline...")
    all_hits = fetch_all_companies()
    total = fetch_count()
    known_ids = [hit_id(h) for h in all_hits]
    # Deduplicate
    known_ids = list(set(kid for kid in known_ids if kid))
    save_state(data_dir, known_ids, total)
    log.info("Seed complete: %d unique companies stored", len(known_ids))
    return known_ids


def detect_new(data_dir, force_full=False):
    """
    Detect new companies since last run.

    Returns (new_hits, all_known_ids, current_count):
      - new_hits: list of Algolia hit dicts for newly detected companies
      - all_known_ids: updated set of all known IDs (for saving state)
      - current_count: latest nbHits from Algolia
    """
    state = load_state(data_dir)

    if state is None:
        # First run — seed and return no new hits
        known_ids = seed(data_dir)
        return [], set(known_ids), len(known_ids)

    known_ids = set(str(kid) for kid in state.get("known_ids", []))
    stored_count = state.get("total_count", 0)
    last_timestamp = state.get("last_run_timestamp", 0)

    # Phase 1: Quick count check
    current_count = fetch_count()

    if current_count == stored_count and not force_full:
        log.info("Count unchanged (%d) — no new companies", current_count)
        return [], known_ids, current_count

    if current_count != stored_count:
        log.info("Count changed: %d → %d (delta: %+d)",
                 stored_count, current_count, current_count - stored_count)

    if force_full:
        log.info("Full fetch forced via --full-fetch")

    # Phase 2: Timestamp-based fetch
    new_hits = []
    if not force_full and last_timestamp > 0:
        recent_hits = fetch_since(last_timestamp)
        new_hits = [h for h in recent_hits if hit_id(h) not in known_ids]

        if new_hits:
            log.info("Found %d new companies via timestamp query", len(new_hits))
            new_ids = set(hit_id(h) for h in new_hits)
            known_ids = known_ids | new_ids
            return new_hits, known_ids, current_count

        if current_count > stored_count:
            log.info("Count increased but no new IDs via timestamp — falling back to full fetch")
        else:
            # Count decreased or timestamp found nothing — just update state
            return [], known_ids, current_count

    # Fallback: Full batch-partitioned fetch
    log.info("Running full fetch to find new companies...")
    all_hits = fetch_all_companies()
    all_ids = set(hit_id(h) for h in all_hits)
    new_ids = all_ids - known_ids
    new_hits = [h for h in all_hits if hit_id(h) in new_ids]

    if new_hits:
        log.info("Found %d new companies via full fetch", len(new_hits))
    else:
        log.info("Full fetch complete — no genuinely new companies found")

    known_ids = known_ids | all_ids
    return new_hits, known_ids, current_count


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def load_env(data_dir_default):
    """Load .env file if present (simple key=value parser)."""
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())


def parse_args():
    parser = argparse.ArgumentParser(
        description="YC Radar — Monitor YC Directory for new startups",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--webhook-url", help="Webhook URL (overrides WEBHOOK_URL env var)")
    parser.add_argument("--dry-run", action="store_true", help="Detect but don't send webhook")
    parser.add_argument("--seed", action="store_true", help="Force re-seed (fetch all, save state, no webhook)")
    parser.add_argument("--full-fetch", action="store_true", help="Force full batch fetch instead of timestamp shortcut")
    parser.add_argument("--data-dir", help="State directory (default: ./data or DATA_DIR env var)")
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    return parser.parse_args()


def main():
    load_env("./data")
    args = parse_args()

    # Logging
    level = logging.DEBUG if args.verbose else getattr(logging, os.environ.get("LOG_LEVEL", "INFO"), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    data_dir = args.data_dir or os.environ.get("DATA_DIR", "./data")
    webhook_url = args.webhook_url or os.environ.get("WEBHOOK_URL", "")

    log.info("YC Radar starting — data_dir=%s, dry_run=%s", data_dir, args.dry_run)

    # Retry any pending webhooks from a previous failed run
    if webhook_url and not args.dry_run:
        pending = load_pending(data_dir)
        if pending:
            log.info("Retrying pending webhooks (%d companies)...", len(pending))
            sent, failed = send_all_webhooks(webhook_url, pending, data_dir)
            if failed:
                save_pending(data_dir, failed)
                log.warning("Retried pending: %d sent, %d still failing", sent, len(failed))
            else:
                clear_pending(data_dir)
                log.info("All pending webhooks delivered")

    # Force re-seed
    if args.seed:
        log.info("Force re-seed requested")
        seed(data_dir)
        print("Seed complete.")
        return

    # Detection
    try:
        new_hits, known_ids, current_count = detect_new(data_dir, force_full=args.full_fetch)
    except requests.RequestException as exc:
        log.error("Algolia API error: %s", exc)
        sys.exit(1)

    # Save updated state
    save_state(data_dir, list(known_ids), current_count)

    if not new_hits:
        print(f"No new companies detected. Total tracked: {len(known_ids)}")
        return

    # Report findings
    print(f"\nDetected {len(new_hits)} new companies:")
    for h in new_hits:
        name = h.get("name", "Unknown")
        batch = h.get("batch", "?")
        one_liner = h.get("one_liner", "")
        print(f"  - {name} ({batch}): {one_liner}")

    if args.dry_run:
        print("\n[DRY RUN] Webhook not sent.")
        for h in new_hits:
            log.debug("Payload: %s", json.dumps(build_payload(h), indent=2))
        return

    if not webhook_url:
        log.warning("No WEBHOOK_URL configured — skipping webhook delivery")
        print("Set WEBHOOK_URL in .env or pass --webhook-url to send results.")
        return

    # Deliver webhooks (one per company)
    sent, failed = send_all_webhooks(webhook_url, new_hits, data_dir)
    if failed:
        save_pending(data_dir, failed)
        print(f"Webhook: {sent} sent, {len(failed)} failed (saved for retry).")
        sys.exit(1)
    else:
        clear_pending(data_dir)
        print(f"Webhook delivered: {sent} companies sent to {webhook_url}")


if __name__ == "__main__":
    main()
