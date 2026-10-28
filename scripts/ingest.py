#!/usr/bin/env python3
"""
Aggregate Claude Code and Codex usage from local JSONL transcripts into
repo-local JSON files. No database: everything lands in data/.

  python3 scripts/ingest.py                    # rescan every month found
  python3 scripts/ingest.py --month 2026-07    # rescan one month only
  python3 scripts/ingest.py --last-month       # rescan the previous calendar month
  python3 scripts/ingest.py --current-month    # rescan the month in progress
  python3 scripts/ingest.py --rebuild-payload  # no rescan; re-render from data/monthly
  python3 scripts/ingest.py --month 2026-07 --replace   # discard, don't merge

Three things this script exists to get right:

1. Deduplication. Transcripts replay history when a session is forked or
   resumed, so the same assistant message appears in several files. Every
   record is deduplicated on `message.id`; without it, totals roughly double.

2. Cache-aware cost. Cached input is not billed at the input rate --
   5-minute writes cost 1.25x, 1-hour writes 2x, and reads 0.1x. Those are
   tracked as separate columns so the dashboard can price them correctly.

3. Never shrinking. Claude Code deletes transcripts after about 30 days, so a
   rescan sees strictly less history than it did last week. Monthly files are
   therefore merged, not overwritten: a rebuild can add rows and correct
   undercounts, but cannot drop what an earlier run captured. Once a transcript
   is gone the monthly file is the only surviving record of it. `--replace`
   opts out, for when a parser or pricing fix makes the old rows wrong.

Monthly files are the source of truth on disk; data/usage.js is the
regenerated payload the dashboard reads.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"
MONTHLY = DATA / "monthly"
PRICING_PATH = REPO / "config" / "pricing.json"
TEMPLATE = REPO / "scripts" / "dashboard.template.html"
DASHBOARD = REPO / "dashboard.html"
SOURCE = Path(os.environ.get("CLAUDE_PROJECTS_DIR", Path.home() / ".claude" / "projects"))
CODEX_SOURCE = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))

COLUMNS = ["date", "project", "model", "input", "output",
           "cache_write_5m", "cache_write_1h", "cache_read", "messages", "agent"]
TOKEN_FIELDS = ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read")
SIDE_FIELDS = ("tools_daily", "hours_daily", "sessions_daily", "user_messages_daily")


def zeros() -> dict:
    return {f: 0 for f in TOKEN_FIELDS} | {"messages": 0}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def load_pricing() -> dict:
    return json.loads(PRICING_PATH.read_text())


def canonical_model(raw: str, pricing: dict) -> str | None:
    if not raw or raw == "<synthetic>":
        return None
    return pricing.get("aliases", {}).get(raw, raw)


def project_name(cwd: str) -> str:
    """Turn a working directory into a stable, human-readable project label."""
    if not cwd:
        return "unknown"
    p = Path(cwd)
    parts = p.parts

    # git worktrees: .../worktrees/<repo>/<branch-dir> -> <repo>
    if "worktrees" in parts:
        i = parts.index("worktrees")
        # Codex uses ~/.codex/worktrees/<id>/<repo>.
        if i > 0 and parts[i - 1] == ".codex" and i + 2 < len(parts):
            return parts[i + 2]
        if i + 1 < len(parts):
            return parts[i + 1]

    home = str(Path.home())
    s = str(p)
    for root in (f"{home}/_code/", f"{home}/code/"):
        if s.startswith(root):
            rel = s[len(root):].split("/")
            return "/".join(rel[:2]) if len(rel) >= 2 else rel[0]

    if s.startswith(home):
        rel = s[len(home):].lstrip("/")
        return rel.split("/")[0] if rel else "~"
    return p.name or "unknown"


def parse_ts_local(ts: str) -> datetime | None:
    """ISO-8601 UTC timestamp -> local time, so day boundaries match your calendar."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()


def new_side() -> dict:
    return {
        "tools": defaultdict(lambda: defaultdict(int)),   # date -> tool -> n
        "hours": defaultdict(lambda: [0] * 24),           # date -> hour -> tokens
        "sessions": defaultdict(set),                     # date -> {session_id}
        "user_messages": defaultdict(int),                # date -> n
    }


# --------------------------------------------------------------------------- #
# scan
# --------------------------------------------------------------------------- #

def scan(files, wanted_month: str | None, pricing: dict):
    """One pass over the transcripts. Returns (facts, side_stats, diagnostics)."""
    seen: dict[str, tuple] = {}
    facts: dict[tuple, dict] = defaultdict(zeros)
    side: dict[str, dict] = defaultdict(new_side)
    unknown: set[str] = set()
    dupes = 0

    for path in files:
        try:
            fh = open(path, "r", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                dt = parse_ts_local(rec.get("timestamp", ""))
                if dt is None:
                    continue
                date = dt.strftime("%Y-%m-%d")
                month = date[:7]
                if wanted_month and month != wanted_month:
                    continue
                s = side[month]

                if rec.get("sessionId"):
                    s["sessions"][date].add(rec["sessionId"])
                if rec.get("type") == "user" and not rec.get("isSidechain"):
                    s["user_messages"][date] += 1

                msg = rec.get("message")
                if not isinstance(msg, dict):
                    continue

                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            s["tools"][date][block.get("name") or "unknown"] += 1

                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue

                mid = msg.get("id") or f"{path}:{rec.get('uuid')}"
                out = int(usage.get("output_tokens") or 0)
                if mid in seen:
                    # A streamed message is written once per content block, and
                    # only the last copy carries its final output count. Input
                    # and cache figures are identical in every copy.
                    dupes += 1
                    b, hours, counted = seen[mid]
                    if out > counted:
                        b["output"] += out - counted
                        hours[0][hours[1]] += out - counted
                        seen[mid] = (b, hours, out)
                    continue

                model = canonical_model(msg.get("model") or "", pricing)
                if model is None:
                    continue
                if model not in pricing["models"]:
                    unknown.add(model)

                cc = usage.get("cache_creation") or {}
                w1h = int(cc.get("ephemeral_1h_input_tokens") or 0)
                w5m = int(cc.get("ephemeral_5m_input_tokens") or 0)
                total_write = int(usage.get("cache_creation_input_tokens") or 0)
                # If the breakdown is missing or inconsistent, bill the remainder at 5m.
                if w1h + w5m != total_write:
                    w5m = max(0, total_write - w1h)

                inp = int(usage.get("input_tokens") or 0)
                read = int(usage.get("cache_read_input_tokens") or 0)

                b = facts[(date, project_name(rec.get("cwd", "")), model)]
                b["input"] += inp
                b["output"] += out
                b["cache_write_5m"] += w5m
                b["cache_write_1h"] += w1h
                b["cache_read"] += read
                b["messages"] += 1

                s["hours"][date][dt.hour] += inp + out + w5m + w1h + read
                seen[mid] = (b, (s["hours"][date], dt.hour), out)

    return facts, side, {"unique": len(seen), "dupes": dupes, "unknown": unknown}


def scan_codex(files, wanted_month: str | None, pricing: dict):
    """Read response usage and older cumulative updates without counting both."""
    facts = defaultdict(zeros)
    side = defaultdict(new_side)
    seen, side_seen, unknown = set(), set(), set()
    dupes = 0
    fields = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
              "output_tokens", "reasoning_output_tokens", "total_tokens")

    def counts(value):
        return {k: max(0, int(value.get(k) or 0)) for k in fields}

    for path in files:
        # A compaction can reset token_count totals while response records keep
        # lifetime totals. Prefer response records for an entire turn, even if
        # its first token_count was written before its first response record.
        modern_turns = set()
        line_count = 0
        session, turn = str(path), ""
        try:
            with path.open(errors="replace") as fh:
                for line in fh:
                    line_count += 1
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(rec, dict) or not isinstance(rec.get("payload"), dict):
                        continue
                    p = rec["payload"]
                    if rec.get("type") == "session_meta":
                        session = p.get("id") or session
                    elif rec.get("type") == "turn_context":
                        turn = p.get("turn_id") or ""
                    elif rec.get("type") == "token_usage_record" and isinstance(p.get("usage"), dict):
                        modern_turns.add((session, turn))
        except OSError:
            continue
        session, cwd, model, turn = str(path), "", "codex-unknown", ""
        previous = None
        try:
            fh = path.open(errors="replace")
        except OSError:
            continue
        with fh:
            for line_number, line in enumerate(fh):
                # An active session may append a new turn between the two passes.
                # Leave those new lines for the next refresh.
                if line_number >= line_count:
                    break
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                p = rec.get("payload")
                if not isinstance(p, dict):
                    continue
                kind = rec.get("type")
                if kind == "session_meta":
                    session = p.get("id") or session
                    cwd = p.get("cwd") or cwd
                    continue
                if kind == "turn_context":
                    cwd = p.get("cwd") or cwd
                    model = canonical_model(p.get("model") or "codex-unknown", pricing) or "codex-unknown"
                    turn = p.get("turn_id") or ""
                    continue

                # Update counters even outside the requested month, so its first
                # event cannot bring in tokens from the previous month.
                usage, key = None, None
                if kind == "token_usage_record" and isinstance(p.get("usage"), dict):
                    usage = counts(p["usage"])
                    key = ("response", p["response_id"]) if p.get("response_id") else (
                        "record", session, rec.get("timestamp"), tuple(usage.values()))
                elif kind == "event_msg" and p.get("type") == "token_count":
                    info = p.get("info") or {}
                    if not isinstance(info, dict):
                        continue
                    total = info.get("total_token_usage")
                    if not isinstance(total, dict):
                        continue
                    total = counts(total)
                    if (session, turn) in modern_turns:
                        previous = total
                        dupes += 1
                        continue
                    if total == previous:
                        dupes += 1
                        continue
                    # Last usage survives cumulative counter resets on compaction.
                    if isinstance(info.get("last_token_usage"), dict):
                        usage = counts(info["last_token_usage"])
                    else:
                        usage = {k: max(0, v - (previous or {}).get(k, 0))
                                 for k, v in total.items()}
                    previous = total
                    # Forks copy timestamps and turn IDs with their history.
                    key = ("legacy", turn, rec.get("timestamp"), tuple(total.values()))

                dt = parse_ts_local(rec.get("timestamp", ""))
                if dt is None:
                    continue
                date = dt.strftime("%Y-%m-%d")
                if wanted_month and date[:7] != wanted_month:
                    continue
                s = side[date[:7]]
                tool = None
                user = kind == "event_msg" and p.get("type") == "user_message"
                if kind == "response_item" and p.get("type") in (
                        "function_call", "custom_tool_call", "web_search_call", "local_shell_call"):
                    tool = p.get("name") or p["type"]
                if tool or user:
                    sid = ("tool", p.get("call_id") or p.get("id")) if tool and (
                        p.get("call_id") or p.get("id")) else (
                        "event", turn, rec.get("timestamp"), json.dumps(p, sort_keys=True))
                    if sid not in side_seen:
                        side_seen.add(sid)
                        s["sessions"][date].add(session)
                        if tool:
                            s["tools"][date][tool] += 1
                        else:
                            s["user_messages"][date] += 1
                if usage is None:
                    continue
                if key in seen:
                    dupes += 1
                    continue
                seen.add(key)
                if not usage["input_tokens"] and not usage["output_tokens"]:
                    continue
                if model not in pricing["models"]:
                    unknown.add(model)
                # Cached input and reasoning output are subsets, not extra tokens.
                inp = usage["input_tokens"]
                read = min(inp, usage["cached_input_tokens"])
                write = min(inp - read, usage["cache_write_input_tokens"])
                b = facts[(date, project_name(cwd), model)]
                b["input"] += inp - read - write
                b["cache_read"] += read
                b["cache_write_5m"] += write
                b["output"] += usage["output_tokens"]
                b["messages"] += 1
                s["sessions"][date].add(session)
                s["hours"][date][dt.hour] += inp + usage["output_tokens"]
    return facts, side, {"unique": len(seen), "dupes": dupes, "unknown": unknown}


def side_payload(s: dict) -> dict:
    return {
        "tools_daily": {d: dict(t) for d, t in sorted(s["tools"].items())},
        "hours_daily": dict(sorted(s["hours"].items())),
        "sessions_daily": {d: len(v) for d, v in sorted(s["sessions"].items())},
        "user_messages_daily": dict(sorted(s["user_messages"].items())),
    }


def agent_stats(month: dict) -> dict:
    """Old monthly files contain only Claude data."""
    return month.get("agents") or {"claude": {k: month.get(k, {}) for k in SIDE_FIELDS}}


def combined_stats(agents: dict) -> dict:
    out = {k: {} for k in SIDE_FIELDS}
    for stats in agents.values():
        for field in SIDE_FIELDS:
            for day, value in stats.get(field, {}).items():
                if field == "tools_daily":
                    target = out[field].setdefault(day, {})
                    for tool, n in value.items():
                        target[tool] = target.get(tool, 0) + n
                elif field == "hours_daily":
                    target = out[field].setdefault(day, [0] * 24)
                    for hour, n in enumerate(value):
                        target[hour] += n
                else:
                    out[field][day] = out[field].get(day, 0) + value
    return out


def build(wanted_month: str | None, pricing: dict):
    sources = {
        "claude": sorted(SOURCE.rglob("*.jsonl")),
        "codex": sorted({p for directory in ("sessions", "archived_sessions")
                         for p in (CODEX_SOURCE / directory).rglob("*.jsonl")}),
    }
    if not any(sources.values()):
        sys.exit(f"No transcripts found under {SOURCE} or {CODEX_SOURCE}")

    # A file last touched before the month began cannot hold that month's data.
    if wanted_month:
        start = datetime.strptime(wanted_month + "-01", "%Y-%m-%d")
        cutoff = (start - timedelta(days=2)).timestamp()
        sources = {agent: [f for f in files if f.stat().st_mtime >= cutoff]
                   for agent, files in sources.items()}

    months: dict[str, dict] = {}
    diag = {"unique": 0, "dupes": 0, "unknown": set()}
    for agent, files in sources.items():
        facts, side, result = (scan if agent == "claude" else scan_codex)(
            files, wanted_month, pricing)
        diag["unique"] += result["unique"]
        diag["dupes"] += result["dupes"]
        diag["unknown"].update(result["unknown"])
        for (date, project, model), v in sorted(facts.items()):
            months.setdefault(date[:7], {"rows": [], "agents": {}})["rows"].append(
                [date, project, model, *(v[f] for f in TOKEN_FIELDS), v["messages"], agent])
        for month, s in side.items():
            months.setdefault(month, {"rows": [], "agents": {}})["agents"][agent] = side_payload(s)

    now = datetime.now().astimezone().isoformat(timespec="seconds")
    for month, payload in months.items():
        payload.update({
            "month": month,
            "columns": COLUMNS,
            "generated_at": now,
            **combined_stats(payload["agents"]),
        })
    return months, diag


# --------------------------------------------------------------------------- #
# merge
# --------------------------------------------------------------------------- #

def _rank(row: list) -> tuple:
    """Completeness of a fact row: message count first, then total tokens."""
    return (row[8], sum(row[3:8]))


def normalized_row(row: list) -> list:
    return row if len(row) == len(COLUMNS) else row + ["claude"]


def row_key(row: list) -> tuple:
    return (row[0], row[1], row[2], row[9])


def merge_month(old: dict | None, new: dict) -> tuple[dict, int]:
    """Union a freshly scanned month with the one already on disk.

    Transcripts rotate away after ~30 days, so the scan behind `new` may be
    missing days the file on disk still records. Every (date, project, model,
    agent) key resolves to whichever version looks more complete, which makes monthly
    files effectively append-only. Returns (merged, rows_preserved).
    """
    if not old:
        return new, 0

    best: dict[tuple, list] = {}
    for row in (old.get("rows") or []) + new["rows"]:
        row = normalized_row(row)
        key = row_key(row)
        if key not in best or _rank(row) > _rank(best[key]):
            best[key] = row

    fresh = {row_key(normalized_row(r)) for r in new["rows"]}
    preserved = sum(1 for k, r in best.items() if k not in fresh or r not in new["rows"])

    merged = dict(new)
    merged["rows"] = [best[k] for k in sorted(best)]

    # Same rule for the per-day side stats: keep the fuller of the two.
    weight = {"tools_daily": lambda v: sum(v.values()),
              "hours_daily": sum,
              "sessions_daily": lambda v: v,
              "user_messages_daily": lambda v: v}
    old_agents, new_agents = agent_stats(old), agent_stats(new)
    merged["agents"] = {}
    for agent in sorted(old_agents.keys() | new_agents.keys()):
        stats = {}
        for field, size in weight.items():
            out = dict(old_agents.get(agent, {}).get(field) or {})
            for day, val in (new_agents.get(agent, {}).get(field) or {}).items():
                if day not in out or size(val) >= size(out[day]):
                    out[day] = val
            stats[field] = {d: out[d] for d in sorted(out)}
        merged["agents"][agent] = stats
    merged.update(combined_stats(merged["agents"]))

    return merged, preserved


# --------------------------------------------------------------------------- #
# cost + payload
# --------------------------------------------------------------------------- #

def rate_of(model: str, agent: str, pricing: dict) -> dict:
    fallback = pricing.get("codex_fallback", {
        "input": 0, "output": 0, "unpriced": True,
        "note": "No configured API rate; excluded from cost totals."})
    return pricing["models"].get(model) or (fallback if agent == "codex" else pricing["fallback"])


def cost_of(row: dict, model: str, pricing: dict) -> float:
    spec = rate_of(model, row.get("agent", "claude"), pricing)
    mult = pricing["cache_multipliers"]
    inr, outr = spec["input"], spec["output"]
    return (row["input"] * inr
            + row["output"] * outr
            + row["cache_write_5m"] * inr * mult["write_5m"]
            + row["cache_write_1h"] * inr * mult["write_1h"]
            + row["cache_read"] * spec.get("cached_input", inr * mult["read"])) / 1_000_000


def payload_from_disk(pricing: dict) -> dict:
    """Read every monthly file and produce the dashboard payload.

    Ships raw fact rows: the dashboard aggregates client-side so one date
    filter can scope every chart consistently.
    """
    months = sorted(p.stem for p in MONTHLY.glob("*.json"))
    rows: list[list] = []
    tools_daily: dict[str, dict] = {}
    hours_daily: dict[str, list] = {}
    sessions_daily: dict[str, int] = {}
    user_messages_daily: dict[str, int] = {}
    agents: dict[str, dict] = {}

    for month in months:
        m = json.loads((MONTHLY / f"{month}.json").read_text())
        rows.extend(normalized_row(r) for r in m["rows"])
        for agent, stats in agent_stats(m).items():
            target = agents.setdefault(agent, {k: {} for k in SIDE_FIELDS})
            for field in SIDE_FIELDS:
                target[field].update(stats.get(field) or {})
        tools_daily.update(m.get("tools_daily") or {})
        hours_daily.update(m.get("hours_daily") or {})
        sessions_daily.update(m.get("sessions_daily") or {})
        user_messages_daily.update(m.get("user_messages_daily") or {})

    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    dates = sorted({r[0] for r in rows})

    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "months": months,
        "columns": COLUMNS,
        "rows": rows,
        "range": {"start": dates[0] if dates else None, "end": dates[-1] if dates else None},
        "tools_daily": tools_daily,
        "hours_daily": hours_daily,
        "sessions_daily": sessions_daily,
        "user_messages_daily": user_messages_daily,
        "agents": agents,
        "model_labels": {k: v.get("label", k) for k, v in pricing["models"].items()},
        "pricing": {r[2]: rate_of(r[2], r[9], pricing) for r in rows},
        "fallback": pricing["fallback"],
        "cache_multipliers": pricing["cache_multipliers"],
    }


def write_dashboard(payload: dict) -> None:
    """Render the template with the data inlined.

    Browsers treat every file:// document as its own opaque origin, so Safari
    and Firefox refuse to load a sibling <script src>. Inlining the payload is
    what makes the dashboard work by double-clicking it, anywhere, offline.
    """
    if not TEMPLATE.exists():
        sys.exit(f"Missing template: {TEMPLATE}")
    tpl = TEMPLATE.read_text()
    marker = "/*__USAGE_DATA__*/"
    if marker not in tpl:
        sys.exit(f"Template has no {marker} placeholder")
    # "</" would otherwise let a project name close the <script> element early.
    blob = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    html = tpl.replace(marker, "window.USAGE_DATA=" + blob + ";")
    # Drop the template's own "edit the template" banner from the output.
    html = html.replace("<!-- SOURCE TEMPLATE. Rendered to ../dashboard.html by scripts/ingest.py.\n"
                        "     Edit this file; dashboard.html is generated. -->\n", "")
    DASHBOARD.write_text(html)


# --------------------------------------------------------------------------- #

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--month", help="rebuild a single month, format YYYY-MM")
    g.add_argument("--last-month", action="store_true")
    g.add_argument("--current-month", action="store_true")
    g.add_argument("--rebuild-payload", action="store_true",
                   help="re-render data/ and dashboard.html from the monthly files "
                        "already on disk, without rescanning transcripts")
    ap.add_argument("--replace", action="store_true",
                    help="overwrite the targeted months instead of merging. Destructive: "
                         "drops rows whose transcripts have since been deleted")
    args = ap.parse_args()

    today = datetime.now().astimezone()
    if args.last_month:
        args.month = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    elif args.current_month:
        args.month = today.strftime("%Y-%m")

    pricing = load_pricing()
    MONTHLY.mkdir(parents=True, exist_ok=True)

    diag = None
    if not args.rebuild_payload:
        months, diag = build(args.month, pricing)
        if args.month and args.month not in months:
            print(f"  no usage found for {args.month}")
        for month, m in sorted(months.items()):
            path = MONTHLY / f"{month}.json"
            preserved = 0
            if not args.replace:
                old = json.loads(path.read_text()) if path.exists() else None
                m, preserved = merge_month(old, m)
            path.write_text(json.dumps(m, indent=1))
            note = f", {preserved} kept from earlier runs" if preserved else ""
            print(f"  wrote data/monthly/{month}.json  ({len(m['rows'])} rows{note})")

    payload = payload_from_disk(pricing)
    (DATA / "usage.json").write_text(json.dumps(payload, indent=1))
    (DATA / "usage.js").write_text(
        "// Generated by scripts/ingest.py -- do not edit by hand.\n"
        "window.USAGE_DATA = " + json.dumps(payload, separators=(",", ":")) + ";\n")
    write_dashboard(payload)
    print(f"  wrote dashboard.html  ({DASHBOARD.stat().st_size // 1024} KB, self-contained)")

    total = sum(cost_of(dict(zip(COLUMNS, r)), r[2], pricing) for r in payload["rows"])
    projects = {r[1] for r in payload["rows"]}
    print()
    if diag:
        print(f"  {diag['unique']:,} unique API messages "
              f"({diag['dupes']:,} repeated or replayed usage records ignored)")
    print(f"  {payload['range']['start']} -> {payload['range']['end']}  |  "
          f"{len(projects)} projects  |  ${total:,.2f} at API rates")
    if diag and diag["unknown"]:
        print(f"  ! not in config/pricing.json (Claude uses fallback; Codex cost excluded): "
              f"{', '.join(sorted(diag['unknown']))}")


if __name__ == "__main__":
    main()
