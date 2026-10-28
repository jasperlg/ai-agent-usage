# AI agents · Usage & Cost

A self-contained dashboard for local Claude Code and Codex usage: tokens per
day, spend per model, project activity, and estimated cost at public API rates.
Use the agent filter to view Claude Code, Codex, or both together.

No database, no server, no network calls. Aggregates are plain JSON files in
`data/`, and `dashboard.html` is a single static page.

```bash
python3 scripts/ingest.py     # scan transcripts -> data/
open dashboard.html           # look at it
```

## What it shows

- **API cost over time**, shown as stacked bars by model or token type. Switch
  between cost and tokens, and group by day, week or month. Auto uses daily bars
  for up to 45 days, weekly bars for up to 180 days, and monthly bars for longer
  ranges. Weeks start on Monday; months follow the calendar. Partial periods
  show only dates inside the filter and are marked in tooltips and the table.
- **Input vs output per day** — two panels with two scales, deliberately not one
  axis (input outweighs output ~240:1, so a shared scale erases output).
- **Cost by model** — share of spend, with the rate applied to each model.
- **Where the time went** — projects ranked by cost, tokens, or messages.
- **What you used most** — tool calls made on your behalf.
- **When you work** — token volume by weekday and hour, local time.

Every chart has a table view, the date filter scopes all of them at once, and the
view options are encoded in the URL hash. Each visit starts with the 30-day
filter and Cost selected. You can change both while viewing the dashboard.

## Layout

```
config/pricing.json           per-model $/M-token rates + cache multipliers  (edit this)
scripts/ingest.py             transcript scanner and aggregator
scripts/dashboard.template.html  dashboard source                        (edit this)
data/monthly/YYYY-MM.json     durable per-month aggregates               (generated)
data/usage.json               readable payload, for diffing              (generated)
data/usage.js                 same payload as a script tag               (generated)
dashboard.html                single self-contained file                 (generated)
.claude/skills/usage-refresh/ skill for topping up the data
```

`dashboard.html` is **generated** with its data inlined — edit
`scripts/dashboard.template.html` and re-run the ingest. Inlining is what makes
it work by double-clicking: browsers treat every `file://` document as its own
opaque origin, so Safari and Firefox refuse to load a sibling `<script src>`.
The built file has no external references at all, so you can also just email it
to someone.

## Refreshing

```bash
python3 scripts/ingest.py --current-month     # top up the month in progress
python3 scripts/ingest.py --last-month        # close out the previous month
python3 scripts/ingest.py --month 2026-05     # one specific month
python3 scripts/ingest.py --rebuild-payload   # re-render only, no rescan
python3 scripts/ingest.py                     # rescan every month (~1s)
```

A single-month run touches only that month's file, then regenerates the payload
from every monthly file on disk. Or just ask Claude: *"update last month's
usage"* — the `usage-refresh` skill covers it.

Runs **merge** into the monthly files rather than overwriting them. This matters
because Claude Code deletes transcripts after ~30 days: past the retention
window, `data/monthly/*.json` is the only record that survives, so a rescan must
never be able to shrink it. `--replace` overrides that, for when a parser fix
makes stored rows wrong.

## Codex support

The same refresh scans both agents automatically. Either can be installed on its
own. Codex transcripts come from `~/.codex/sessions/` and
`~/.codex/archived_sessions/`, including nested directories. Set `CODEX_HOME` if
your Codex data lives elsewhere. `CLAUDE_PROJECTS_DIR` overrides the Claude path.
The [official Codex documentation](https://learn.chatgpt.com/docs/reference/troubleshooting)
lists these transcript locations.

Codex response records are deduplicated by response ID. Older logs use
`token_count` events, with repeated cumulative updates and copied fork history
counted once. When a turn has response records, those take precedence over its
cumulative events, whose counters can reset after compaction. Cached input is
part of input, and reasoning output is part of output; neither is added twice.
The current turn context supplies the model and project. Tool calls and logged
user-message events feed the activity charts. Only locally available usage is
included, so cloud runs without a local transcript are absent.

Existing monthly files remain usable. Rows without an agent are treated as
Claude Code, and a refresh preserves their historical totals. New files retain
activity statistics per agent so the filter scopes every chart and summary tile.
The hourly job picks up Codex automatically through `scripts/ingest.py`.

OpenAI rates in `config/pricing.json` use
[standard API pricing](https://developers.openai.com/api/docs/pricing), verified
on 2026-09-18. These are short-context estimates, excluding fast mode,
long-context premiums, regional surcharges and tool fees. Unknown Codex models,
including internal and third-party models, retain their token counts but show
"no rate" and are excluded from costs. Add a rate to the config to price them.
The `cached_input` field sets a model's cache-read price per million tokens.

Run the regression checks with:

```bash
python3 -m unittest discover -s scripts -p 'test_*.py'
node scripts/dashboard.smoke.js
```

## Scheduled refresh

A launchd agent runs `scripts/refresh.sh` (a full rescan) at **:15 past every
hour, Monday to Friday**, plus at login so a run missed while the machine was
asleep still lands. Nothing here is worth an LLM invocation — it is two seconds
of Python.

```bash
tail -20 ~/Library/Logs/ai-agent-usage-refresh.log      # what it has been doing
launchctl kickstart -p gui/$UID/com.jasperlouage.ai-agent-usage.refresh   # run now
launchctl print gui/$UID/com.jasperlouage.ai-agent-usage.refresh | grep exit
```

Frequent rather than weekly because the retention window is the real deadline —
Claude Code deletes transcripts after ~30 days, and every missed run spends part
of that budget. Hourly also means the dashboard is never more than an hour behind
the work you just did.

The schedule is a **list** of `StartCalendarInterval` dicts, one per weekday,
because launchd has no weekday range and an absent key is a wildcard: `Minute`
alone, with no `Hour`, means ":15 past every hour". So five dicts express
hourly-on-weekdays, not 120.

```xml
<dict><key>Weekday</key><integer>1</integer><key>Minute</key><integer>15</integer></dict>
```

Weekday runs Monday (1) to Friday (5); launchd counts Sunday as 0 or 7. Drop
entries to narrow the days, or add `<key>Hour</key>` back to pin it to one time.

Two gotchas, both learned here:

- **Do not put `--` inside an XML comment** in the plist. `plutil -lint` and
  launchd both accept it, but Python's `plistlib` — which `scripts/jobs.py` uses
  to read every plist — rejects the file, so the job silently vanishes from
  `jobs.html` while continuing to run.
- Weekday-only means the longest healthy gap is **Fri 23:15 → Mon 00:15, ~49
  hours**. `jobs.html` raises its own staleness banner past 2.5 days, chosen to
  clear that gap; a tighter threshold would fire every Sunday.

Uninstall:

```bash
launchctl bootout gui/$UID/com.jasperlouage.ai-agent-usage.refresh
rm ~/Library/LaunchAgents/com.jasperlouage.ai-agent-usage.refresh.plist
```

After editing the plist, `bootout` then `bootstrap gui/$UID <plist>` to reload it.

## Docker cleanup

A second launchd agent, `com.jasperlouage.docker-gc`, runs `scripts/docker_gc.py
--apply` daily at 12:15 and reclaims Docker disk. It deletes things, so:

**A dry run is the default.** Without `--apply` nothing is removed, and the
script prints every volume it would delete with its size. Only the launchd job
passes `--apply`.

```bash
python3 scripts/docker_gc.py                      # show what would go
python3 scripts/docker_gc.py --apply              # actually reclaim
tail -40 ~/Library/Logs/docker-gc.log             # what it has been doing
launchctl kickstart -p gui/$UID/com.jasperlouage.docker-gc    # run now
```

What it removes, in the order it matters:

| | policy | why |
|---|---|---|
| Stopped containers | older than 30d | first, because a stopped container counts as an image reference — pruning images before them spares every image they pin |
| Anonymous volumes | all unreferenced | 64-hex names nobody chose; `docker volume prune` already means anonymous-only, and this never passes `-a` |
| Named volumes | 60d with no writes | compose volumes — gateway config, dev databases — for stacks that are merely stopped |
| Images | unused, built 30d+ ago | `until` filters on *build* date, not last use, since Docker records no last-use |
| Build cache | capped at 8 GB, LRU | see below |

The named-volume rule is the one worth understanding. "Idle" is the newest file
mtime *inside* the volume, not the volume's `CreatedAt` — creation date says
nothing about whether a stack still matters, while a gateway writes logs and its
internal db continuously when up, so the newest mtime is genuinely "when did this
stack last run". A single `alpine` container mounts every candidate read-only at
once and compares against a reference file, rather than paying 80 container
startups. Anything unmeasurable is kept: a probe failure keeps the volume.

To spare a volume regardless of idle time, add a regex to `KEEP` at the top of
the script. Worth doing for any volume whose data is *seeded* rather than
generated — a restored database that is never written to looks idle from outside
no matter how current it is.

Build cache is capped by size rather than age because an age filter barely works:
`--filter until=168h` reclaimed 24.8 MB out of 20 GB here, since the bulk sat in
reusable cache mounts (six 2.8 GB `go mod download` layers last touched a year
ago) that only `--max-used-space` evicts. The cap keeps the most recently used
8 GB and drops the rest.

Unlike the refresh agent this one sets **no `RunAtLoad`** — it deletes, and
nothing about it is urgent enough to fire at every login on top of the daily run.
A missed 12:15 still runs once the machine wakes. If Docker is not running the
script logs that and exits; it never starts Docker itself.

Uninstall:

```bash
launchctl bootout gui/$UID/com.jasperlouage.docker-gc
rm ~/Library/LaunchAgents/com.jasperlouage.docker-gc.plist
```

## Seeing every job on the machine

`jobs.html` is a self-contained inventory of every launchd job on this Mac — what
triggers it, when it next fires, whether it is still working, and the exact
command to remove it. Regenerated by the daily job, or on demand:

```bash
python3 scripts/jobs.py                  # write jobs.html
python3 scripts/jobs.py --text           # same inventory as a terminal table
python3 scripts/jobs.py --include-apple  # also list Apple's ~700 built-ins
python3 scripts/jobs.py --run LABEL      # start it now, watch it, show what it logged
python3 scripts/jobs.py --remove LABEL   # unload it, delete its plist, rewrite the page
open jobs.html
```

Both row buttons copy a command to paste into a terminal. A `file://` page cannot
execute anything itself, so that paste is unavoidable — but each subcommand
handles what a hand-written one-liner gets wrong.

The **remove…** button copies a `--remove` command:

- `/Library/LaunchAgents` and `/Library/LaunchDaemons` are **root:wheel**, so the
  delete needs `sudo` even when unloading a user agent does not. Its output is
  never captured, so the password prompt is actually visible.
- The steps must **not** be chained with `&&`. Once a job is unloaded, `bootout`
  exits non-zero, which would skip the delete forever — leaving a job stuck
  reading "not loaded" no matter how many times you retry. It uses `;` semantics
  and continues to the delete regardless.

It prints the exact commands, asks for confirmation (`--yes` skips), and rewrites
`jobs.html` afterwards even if the delete failed, so the page never disagrees with
reality.

The **run now…** button copies a `--run` command — `launchctl kickstart` on the
right service target, plus the three things that make the answer trustworthy:

- It **waits**. `kickstart` returns as soon as the job is launched, so on its own
  it reports success before the job has done anything. `--run` polls until the
  job stops running and prints its exit code, which is the thing you actually
  wanted to know.
- It shows **only this run's log lines**, sliced from the log's size recorded
  before the kick. These logs are months long; `tail` answers a different question.
- It refuses when the job is **not loaded** and prints the `bootstrap` command
  instead, because `kickstart` only starts jobs launchd already knows about — a
  bare `kickstart` just fails with "Could not find service".

Daemons get `sudo` and the `system/` domain automatically. Since `launchctl` will
not report on a root-domain job without sudo, `--run` says so rather than
inventing an exit code, and lets the log speak. A job that is already running is
left alone rather than started twice. Afterwards the page is rewritten, so the
new exit code and log time land on it.

**Do not make either button execute directly.** This was considered and declined:
a clickable button needs a local HTTP server that runs shell commands, and an
endpoint on localhost that can delete system files or start arbitrary jobs is
something any other browser tab could try to reach. The paste is the security
boundary, and it is deliberate.

`launchctl list` is not a substitute: it prints several hundred labels, and nearly
all of them are on-demand helpers that only wake when something calls them. This
filters to the keys that actually create a timer (`StartCalendarInterval`,
`StartInterval`) and puts liveness next to each one: last exit code from
launchctl, plus the mtime of whatever log the job declares, since a schedule that
has quietly died is indistinguishable from a healthy one otherwise.

**Yours, and why it runs.** A job is marked yours when it is in
`config/known-jobs.json`, or its label starts with `com.jasperlouage.` /
`com.mustry.`. The catalog also holds the one-line *why* shown under the label.
Add a new personal job there; do not infer it from the command path (that is
how anything outside this repo used to vanish from the count).

Apple's own jobs are hidden by default as noise; the page states how many it
dropped rather than pretending the list is complete.

### Seeing what a job actually does

Click any row to expand it. You get three things:

- **Declares** — the full plist, parsed and pretty-printed, so every key it sets
  is visible rather than just the schedule.
- **Executable** — size, whether it is text or a compiled binary, and for binaries
  the `codesign` authority and team. That signature is the real answer to "is this
  actually Adobe, or something wearing its name?"
- **Script it runs** — when the executable is only a wrapper (`/bin/sh`,
  `/usr/bin/env python3`, …) the script holding the real logic is resolved and
  shown inline. Without this, half the jobs on the machine would just read
  `/bin/sh` and tell you nothing.

Source is capped at 240 lines / 12 KB per file, and the panel scrolls at 330px
rather than growing the page. When a cap bites, the panel says exactly what it cut
(`showing first 240 of 500 lines`) — a truncation you cannot see would read as
"this is the whole script", which is worse than showing nothing.

**The page reports on the job that writes it**, so no row can honestly show its
own staleness. Instead the page checks its own age against your browser's clock
and raises a banner if it is more than ~36 hours old — if `jobs.html` looks
stale, the schedule behind it has stopped. `node scripts/jobs.smoke.js` exercises
that logic (and the rendering) against a stub DOM.

## Two things this gets right

**Deduplication.** Claude Code replays conversation history into a new transcript
whenever a session is forked or resumed, so the same assistant message appears in
several `.jsonl` files. In this dataset 18,738 of 34,613 usage records were
replays — 54%. Every record is deduplicated on `message.id`, without which totals
roughly double.

**Cache-aware pricing.** Cached input is not billed at the input rate. Cache reads
cost 0.1×, 5-minute cache writes 1.25×, and 1-hour writes 2×. These are tracked as
separate columns, so the dashboard can price them properly — and can show what
caching saved you (about 84% off the uncached price).

## Caveats

- **Costs are hypothetical.** They are what these tokens would cost on the public
  Anthropic and OpenAI APIs. Claude Code and Codex subscriptions are billed
  differently. Treat this as a measure of workload, not an invoice.
- **`claude-opus-5` has an assumed rate.** It is not in the published pricing
  table this was built against, so it is billed at Opus-tier ($5/$25) and flagged
  with an "est. rate" pill. Correct it in `config/pricing.json` when the real rate
  is known; it is ~32% of the total here, so the number moves.
- **History only goes as far back as your transcripts.** Claude Code rotates old
  ones away, and past that window `data/monthly/*.json` is the only surviving
  record — so back that directory up somewhere. Not here: it is gitignored,
  because those files name real projects and carry their cost. Everything under
  `data/`, plus the two generated pages, stays out of the repo; a clone starts
  empty and fills from its own transcripts.
- Days are bucketed in your **local timezone**; the raw timestamps are UTC.

## Palette

Colors come from the `dataviz` skill's reference palette and were checked with its
validator against both surfaces — all categorical checks pass in dark mode; light
mode passes with a contrast warning on three hues, which is why every chart also
ships visible labels and a table view.
