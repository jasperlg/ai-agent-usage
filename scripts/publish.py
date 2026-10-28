#!/usr/bin/env python3
"""
Publish this machine's usage to the shared Mustry Pages dashboard.

  python3 scripts/publish.py               # save the days that changed
  python3 scripts/publish.py --dry-run     # list them, save nothing
  python3 scripts/publish.py --page        # publish the page itself (Mac only)

The page is published once and holds no usage. Each machine saves one
shared-data record per day it has usage for, keyed `<machine>:<YYYY-MM-DD>`,
and the page reads every record when it opens. So two machines never
overwrite each other, and an hourly run does not create a new page revision.

The document's save history is capped at 20 MB and never shrinks, so a run
saves only the days whose content changed since the last successful save
(tracked in data/published-<machine>.json), and the CLI's --if-changed skips
anything the server already holds.

Run ingest.py first: this reads data/monthly/, never the transcripts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import ingest

CONFIG_PATH = ingest.REPO / "config" / "shared-page.json"
PAGE_DIR = ingest.DATA / "page"
MAX_VALUE = 16 * 1024          # the sharing service's per-record limit
PAUSE = 2.5                    # each save is two gateway calls; the gateway allows 60 a minute


def machine_name(override: str | None) -> str:
    name = override or os.environ.get("USAGE_MACHINE") or socket.gethostname().split(".")[0]
    return name.lower()


def mustry() -> str:
    """launchd and systemd start with a minimal PATH, so look in ~/.local/bin too."""
    found = shutil.which("mustry") or str(Path.home() / ".local" / "bin" / "mustry")
    if not Path(found).exists():
        sys.exit("The Mustry CLI is not installed: see mustry-cockpit/packages/cli/README.md")
    return found


def cli(*args: str, stdin: str | None = None) -> dict:
    run = subprocess.run([mustry(), *args, "--json"], input=stdin, capture_output=True, text=True)
    if run.returncode:
        detail = run.stderr.strip().splitlines()[-1] if run.stderr.strip() else "no output"
        raise RuntimeError(f"mustry {' '.join(args[:3])}: {detail}")
    return json.loads(run.stdout)


def day_records(machine: str) -> dict[str, dict]:
    """One value per date: its fact rows, without the date, and each agent's side stats."""
    days: dict[str, dict] = {}
    for path in sorted(ingest.MONTHLY.glob("*.json")):
        month = json.loads(path.read_text())
        for row in month["rows"]:
            row = ingest.normalized_row(row)
            day = days.setdefault(row[0], {"v": 1, "machine": machine, "date": row[0],
                                           "columns": ingest.COLUMNS[1:], "rows": [], "agents": {}})
            day["rows"].append(row[1:])
        for agent, stats in ingest.agent_stats(month).items():
            for field in ingest.SIDE_FIELDS:
                for date, value in (stats.get(field) or {}).items():
                    if date in days:
                        days[date]["agents"].setdefault(agent, {})[field] = value
    for day in days.values():
        day["rows"].sort()
    return days


def digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(f"Missing {CONFIG_PATH.relative_to(ingest.REPO)}: run publish.py --page on the Mac first")
    return json.loads(CONFIG_PATH.read_text())


def save_days(machine: str, dry_run: bool, everything: bool) -> int:
    config = load_config()
    state_path = ingest.DATA / f"published-{machine}.json"
    state = {} if everything or not state_path.exists() else json.loads(state_path.read_text())
    if state.get("publication") != config["publication"]:
        state = {}
    saved = state.setdefault("days", {})
    state["publication"] = config["publication"]

    days = day_records(machine)
    changed = sorted(d for d, v in days.items() if saved.get(d) != digest(v))
    print(f"  {machine}: {len(days)} days with usage, {len(changed)} to save")
    failures = 0
    for i, date in enumerate(changed):
        value = days[date]
        size = len(json.dumps(value, separators=(",", ":")).encode())
        if size > MAX_VALUE:
            print(f"  ! {date}: {size // 1024} KB is over the 16 KB record limit; skipped")
            failures += 1
            continue
        if dry_run:
            print(f"    {date}  {size:,} bytes")
            continue
        if i:
            time.sleep(PAUSE)
        try:
            result = cli("pages", "data", "put", config["publication"], config["collection"],
                         f"{machine}:{date}", "--file", "-", "--if-changed",
                         stdin=json.dumps(value))
        except RuntimeError as error:
            print(f"  ! {date}: {error}")
            failures += 1
            continue
        saved[date] = digest(value)
        state_path.write_text(json.dumps(state, indent=1, sort_keys=True))
        version = (result.get("record") or {}).get("version")
        print(f"    {date}  {'saved' if result.get('changed') else 'unchanged'}, version {version}")
    return failures


def publish_page() -> None:
    """Render the template without inlined data and publish it, once or as an update."""
    pricing = ingest.load_pricing()
    config = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {
        "collection": "usage",
        "machines": {machine_name(None): socket.gethostname().split(".")[0]},
    }
    shared = {
        "collection": config["collection"],
        "machines": config["machines"],
        "model_labels": {k: v.get("label", k) for k, v in pricing["models"].items()},
        "models": pricing["models"],
        "fallback": pricing["fallback"],
        "codex_fallback": pricing.get("codex_fallback"),
        "cache_multipliers": pricing["cache_multipliers"],
    }
    blob = json.dumps(shared, separators=(",", ":")).replace("</", "<\\/")
    html = ingest.TEMPLATE.read_text().replace(
        "<script>/*__USAGE_DATA__*/</script>",
        '<script src="/_mustry/data.js"></script>\n<script>window.USAGE_SHARED=' + blob + ";</script>")
    PAGE_DIR.mkdir(parents=True, exist_ok=True)
    page = PAGE_DIR / "index.html"
    page.write_text(html)

    # The CLI keeps its publication manifest beside the file, so later runs update it.
    result = cli("pages", "publish", str(page), "--title", "AI agent usage",
                 "--ready-selector", '[data-print-ready="true"]')
    # The repo is public, so the password-free link stays in data/, not in config/.
    url_path = PAGE_DIR / "url.txt"
    if "publication" not in config:
        config["publication"] = result["id"]
        cli("pages", "settings", result["id"], "--data-enabled")
        url_path.write_text(cli("pages", "share", result["id"], "--public")["url"] + "\n")
        CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")
    url = url_path.read_text().strip() if url_path.exists() else "(see mustry pages info)"
    print(f"  published revision {result.get('revisionId')}  ->  {url}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--machine", help="record name for this machine (default: USAGE_MACHINE or the hostname)")
    ap.add_argument("--dry-run", action="store_true", help="list the days that would be saved")
    ap.add_argument("--all", action="store_true",
                    help="reconsider every day, not only those changed since the last run")
    ap.add_argument("--page", action="store_true",
                    help="publish the page itself; needed once, and after template or pricing changes")
    args = ap.parse_args()
    if args.page:
        publish_page()
        return
    sys.exit(1 if save_days(machine_name(args.machine), args.dry_run, args.all) else 0)


if __name__ == "__main__":
    main()
