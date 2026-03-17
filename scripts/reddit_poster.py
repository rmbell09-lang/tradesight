#!/usr/bin/env python3
"""
TradeSight Reddit Poster — autonomous posting via OAuth Script app flow.
See: docs/REDDIT_POSTING_PLAN.md
Usage: python3 reddit_poster.py [--test] [--content daily|strategy]
"""

import json, sys, argparse, logging
from datetime import datetime, date
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
CREDS_FILE = BASE_DIR / "config" / "reddit_creds.json"
LOG_FILE   = BASE_DIR / "logs" / "reddit_posts.jsonl"
REPORTS_DIR = BASE_DIR / "reports"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reddit_poster")

DISCLAIMER = "\n\n---\n*Automated analysis from TradeSight. Not financial advice.*"

def load_creds():
    if not CREDS_FILE.exists():
        log.error(f"No credentials at {CREDS_FILE}. See docs/REDDIT_POSTING_PLAN.md.")
        sys.exit(1)
    return json.loads(CREDS_FILE.read_text())

def get_reddit(creds):
    try:
        import praw
    except ImportError:
        log.error("praw not installed. Run: pip install praw")
        sys.exit(1)
    return praw.Reddit(
        client_id=creds["client_id"],
        client_secret=creds["client_secret"],
        username=creds["username"],
        password=creds["password"],
        user_agent=creds.get("user_agent", "TradeSight/1.0"),
    )

def log_post(subreddit, title, url, dry_run=False):
    LOG_FILE.parent.mkdir(exist_ok=True)
    entry = {"ts": datetime.utcnow().isoformat(), "subreddit": subreddit,
             "title": title, "url": url, "dry_run": dry_run}
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")

def build_daily_post():
    report_path = REPORTS_DIR / f"daily_{date.today().strftime('%Y%m%d')}.txt"
    if not report_path.exists():
        log.warning(f"No daily report at {report_path}")
        return None, None, None
    content = report_path.read_text()[:2000]
    title = f"TradeSight Daily Scan — {date.today().strftime('%b %d, %Y')} — Top Opportunities"
    return title, content + DISCLAIMER, "stocks"

def build_strategy_post():
    files = sorted((BASE_DIR / "logs").glob("strategy_evolution_*.json"), reverse=True)
    if not files:
        log.warning("No strategy evolution results found.")
        return None, None, None
    data = json.loads(files[0].read_text())
    title = f"TradeSight Strategy Evolution — Week of {date.today().strftime('%b %d')}"
    body = f"## AI Strategy Evolution Results\n\n{json.dumps(data, indent=2)[:1800]}"
    return title, body + DISCLAIMER, "algotrading"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="Dry run only")
    parser.add_argument("--content", choices=["daily", "strategy"], default="daily")
    args = parser.parse_args()
    creds = load_creds()
    title, body, subreddit = build_daily_post() if args.content == "daily" else build_strategy_post()
    if not title:
        log.error("Nothing to post.")
        sys.exit(1)
    log.info(f"Posting to r/{subreddit}: {title}")
    if args.test:
        print(f"[DRY RUN] r/{subreddit} | {title}\n{body[:400]}...")
        log_post(subreddit, title, "DRY_RUN", dry_run=True)
        return
    reddit = get_reddit(creds)
    submission = reddit.subreddit(subreddit).submit(title, selftext=body)
    url = f"https://reddit.com{submission.permalink}"
    log.info(f"Posted: {url}")
    log_post(subreddit, title, url)

if __name__ == "__main__":
    main()
