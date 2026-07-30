---
name: usage-refresh
description: Refresh the Claude Code usage dashboard data in this repo from ~/.claude/projects transcripts. Use when asked to "update usage", "refresh the dashboard", "pull in last month", "get the missing data", "rebuild usage data", or when the dashboard looks stale. Supports refreshing just one month instead of rescanning everything.
---

# Refresh usage data

This repo stores its own data on the filesystem — there is no database. Monthly
aggregates live in `data/monthly/YYYY-MM.json`, and `data/usage.js` is the
regenerated payload `dashboard.html` reads.

## Pick the right command

| Situation | Command |
|---|---|
| Default — top up the month in progress | `python3 scripts/ingest.py --current-month` |
| Close out the previous calendar month | `python3 scripts/ingest.py --last-month` |
| One specific month | `python3 scripts/ingest.py --month 2026-05` |
| Pricing changed, or you edited the template | `python3 scripts/ingest.py --rebuild-payload` |
| Something looks wrong / you changed the parser | `python3 scripts/ingest.py` |

Run from the repo root. Prefer the narrowest command that covers the situation:
`--current-month` for a routine top-up, `--rebuild-payload` when no transcript
needs re-reading at all. A no-flag rescan is fine but never *necessary* for a
pricing or template change.

## Why history cannot be re-derived

Claude Code deletes transcripts after about 30 days. A rescan therefore sees
strictly less history than it did last week, and `data/monthly/*.json` is the
only surviving record of anything older than the retention window.

`scripts/ingest.py` **merges** rather than overwrites: each
`(date, project, model)` row resolves to whichever version looks more complete
(higher message count, then higher token total), so a rebuild can add rows and
correct undercounts but cannot silently drop captured history. When a run keeps
rows the fresh scan could no longer see, it says so — `(4 rows, 1 kept from
earlier runs)`.

`--replace` opts out and overwrites the targeted month. Use it **only** when a
parser or pricing fix makes the stored rows genuinely wrong, and only for a month
still fully inside the retention window — outside it, `--replace` destroys data
that cannot be rebuilt.

## What a single-month run does

`--month` touches **only** that month's file. It skips transcripts whose mtime
predates the month (they cannot contain its data), rescans the rest, merges the
result into what is already there, and then regenerates `data/usage.js` from
*every* monthly file on disk. Months you did not target are read from disk
untouched.

## A scheduled job already does the routine case

A launchd agent (`com.jasperlouage.ai-agent-usage.refresh`) runs a full rescan
daily at 12:15 and at login, so the data is rarely more than a day stale. Check
what it has been doing before assuming a manual run is needed:

```bash
tail -20 ~/Library/Logs/ai-agent-usage-refresh.log
```

If the last entries are recent and exit cleanly, the dashboard is current and the
only thing left to do may be `open dashboard.html`. If entries stop or show
errors, say so — a silently dead timer is worth more than a manual top-up. See
README, "Scheduled refresh", for the kickstart/uninstall commands.

The same job also regenerates `jobs.html`, an inventory of every launchd job on
the machine (`python3 scripts/jobs.py`, or `--text` for a terminal table). The
user cares about knowing what runs on their machine unprompted — if you install,
change, or remove any scheduled job, regenerate that page and say what changed.

## Steps

1. Run the command that matches the situation above.
2. Read the summary it prints. Sanity-check four things:
   - **Duplicates ignored** should be roughly half of all usage lines. Transcripts
     replay history on session fork/resume; deduplication on `message.id` is what
     keeps totals honest. A count of 0 means dedup broke.
   - **The date range** should extend to today (or to the month you targeted), and
     its **start date must not move forward** between runs. If it does, history was
     dropped — investigate before doing anything else.
   - **The total should not fall** unless you intended it to. Rates are applied to
     stored tokens, so a lower total after a plain refresh means rows went missing.
   - **A `!` warning** naming a model missing from `config/pricing.json` means that
     model was billed at the fallback rate. Add it to `config/pricing.json`
     (`input`/`output` USD per million tokens) and re-run.
3. Report the new total and what changed since the previous run.

## After a pricing change

Rates live in `config/pricing.json`. Editing them does **not** require rescanning
transcripts — but you do need to regenerate the payload:

```bash
python3 scripts/ingest.py --rebuild-payload   # cheapest correct option
```

Cost is computed from the stored token columns, so a rate edit plus a re-render is
enough — no transcript needs re-reading, and skipping the scan means there is no
opportunity to disturb stored history. Note `claude-opus-5` currently carries an `assumed: true` Opus-tier rate
and renders an "est. rate" pill in the dashboard; drop that flag once the real
published rate is known.

## Viewing the result

Every ingest run regenerates `dashboard.html` from
`scripts/dashboard.template.html` with the data **inlined**, so it is a single
self-contained file with no external references:

```bash
open dashboard.html
```

That works in every browser, offline, and the file can be shared as-is. Inlining
is deliberate — browsers treat each `file://` document as its own opaque origin,
so Safari and Firefox block a sibling `<script src>` and would render a blank
page. No local web server is needed.

To change the dashboard itself, edit `scripts/dashboard.template.html` (the
`/*__USAGE_DATA__*/` placeholder is where the payload lands) and re-run the
ingest. Never edit `dashboard.html` directly — it is overwritten.

## Adding a new month of history

Nothing special is required — `scripts/ingest.py` with no flags discovers every
month present in `~/.claude/projects` and writes one file per month. Transcripts
Claude Code has already deleted cannot be recovered; whatever is on disk is the
ceiling.

## Do not

- Do not hand-edit files in `data/` — they are generated. Edit `config/pricing.json`
  or `scripts/ingest.py` instead.
- Do not delete `data/monthly/*.json` to "force a refresh". Those files are the
  only durable record; if Claude Code has since rotated away the underlying
  transcripts, deleting them loses that history permanently. Re-running the
  ingest merges into them in place, which is what you want.
- Do not reach for `--replace` to make a number look right. It is the one command
  here that can lose data. If a total seems too high, find the bad row first.

## History known lost

- **2026-06-29/30, ~$43** — a no-flag rebuild on 2026-07-30 overwrote
  `2026-06.json` (then ~$105.6) with the 3 rows still recoverable that day,
  before merge-on-rebuild existed. No git repo, snapshot, or backup to restore
  from. This section exists so the next unexplained gap is not re-diagnosed from
  scratch.
