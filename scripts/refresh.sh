#!/bin/sh
# Unattended usage refresh, run hourly by the launchd agent
# com.jasperlouage.ai-agent-usage.refresh on the Mac and the systemd user timer
# ai-agent-usage-refresh.timer on the devbox (see README, "Scheduled refresh").
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

# Save this machine's changed days to the shared dashboard page. A failed save
# is retried next hour, so it must not stop the rest of the refresh.
/usr/bin/python3 scripts/publish.py || echo "  ! publish.py failed; the next run retries"

# Keep the jobs overview current too. It reports on this very job, so its own
# timestamp doubles as the liveness signal: if jobs.html stops being rewritten,
# this schedule has stopped running. It reads launchd, so macOS only.
[ "$(uname)" = Darwin ] || exit 0
exec /usr/bin/python3 scripts/jobs.py
