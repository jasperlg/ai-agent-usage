#!/bin/sh
# Unattended usage refresh, run by the launchd agent
# com.jasperlouage.ai-agent-usage.refresh (see README, "Scheduled refresh").
# Safe to run by hand at any time.
#
# A full rescan -- no --current-month -- is deliberate. ingest.py merges rather
# than overwrites, so a rescan cannot shrink the monthly files, and it needs no
# knowledge of where a month boundary falls: a run on the 2nd still captures the
# tail of the previous month, which --current-month would silently skip.
#
# /usr/bin/python3 rather than a PATH lookup: launchd starts with a minimal
# environment, and the system interpreter does not move when Homebrew upgrades.
set -eu

cd "$(dirname "$0")/.."

printf '\n===== %s =====\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')"
/usr/bin/python3 scripts/ingest.py

# Keep the jobs overview current too. It reports on this very job, so its own
# timestamp doubles as the liveness signal: if jobs.html stops being rewritten,
# this schedule has stopped running.
exec /usr/bin/python3 scripts/jobs.py
