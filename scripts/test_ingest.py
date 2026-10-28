"""Run with python3 -m unittest discover -s scripts -p 'test_*.py'."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ingest


def event(kind, payload, timestamp="2026-08-15T12:00:00Z"):
    return {"type": kind, "payload": payload, "timestamp": timestamp}


def usage(inp=100, cached=60, out=20):
    return {"input_tokens": inp, "cached_input_tokens": cached,
            "output_tokens": out, "reasoning_output_tokens": 10,
            "total_tokens": inp + out}


def token(total, last=None, timestamp="2026-08-15T12:00:00Z"):
    info = {"total_token_usage": total}
    if last is not None:
        info["last_token_usage"] = last
    return event("event_msg", {"type": "token_count", "info": info}, timestamp)


class CodexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pricing = ingest.load_pricing()
        self.meta = event("session_meta", {"id": "s1", "cwd": "/projects/example"})
        self.context = event("turn_context", {"model": "gpt-5.3-codex", "turn_id": "t1"})

    def file(self, name, events):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(json.dumps(e) for e in events) + '\n{"partial":')
        return p

    def scan(self, events, month=None):
        return ingest.scan_codex([self.file("session.jsonl", [self.meta, self.context] + events)],
                                 month, self.pricing)

    def test_cache_and_reasoning_are_subsets_and_repeated_totals_are_ignored(self):
        facts, side, diag = self.scan([token(usage(), usage()), token(usage(), usage()),
                                      token(usage(250, 150, 50), usage(150, 90, 30))])
        row = next(iter(facts.values()))
        self.assertEqual(row, {"input": 100, "cache_read": 150, "output": 50,
                               "cache_write_5m": 0, "cache_write_1h": 0, "messages": 2})
        self.assertEqual(sum(side["2026-08"]["hours"]["2026-08-15"]), 300)
        self.assertEqual(diag["dupes"], 1)

    def test_response_records_and_token_events_count_once_across_copies(self):
        record = event("token_usage_record", {"response_id": "resp1", "usage": usage(),
                                              "thread_token_usage": usage()})
        records = [self.meta, self.context, record, token(usage(), usage())]
        files = [self.file("active.jsonl", records), self.file("archive.jsonl", records)]
        facts, _, _ = ingest.scan_codex(files, None, self.pricing)
        self.assertEqual(next(iter(facts.values()))["messages"], 1)

    def test_legacy_fork_history_and_tool_calls_are_deduplicated(self):
        records = [self.meta, self.context,
                   event("response_item", {"type": "function_call", "name": "exec", "call_id": "c1"}),
                   event("event_msg", {"type": "user_message", "message": "hello"}),
                   token(usage(), usage())]
        files = [self.file("original.jsonl", records), self.file("fork.jsonl", records + [
            event("session_meta", {"id": "s2", "forked_from_id": "s1"}),
            token(usage(200, 120, 40), usage(), "2026-08-15T13:00:00Z")])]
        facts, side, _ = ingest.scan_codex(files, None, self.pricing)
        self.assertEqual(next(iter(facts.values()))["messages"], 2)
        self.assertEqual(side["2026-08"]["tools"]["2026-08-15"]["exec"], 1)
        self.assertEqual(side["2026-08"]["user_messages"]["2026-08-15"], 1)
        self.assertEqual(len(side["2026-08"]["sessions"]["2026-08-15"]), 2)

    def test_response_records_win_when_compaction_resets_event_counters(self):
        facts, _, _ = self.scan([
            token(usage(100, 60, 20), usage()),
            event("token_usage_record", {"response_id": "resp1", "usage": usage(),
                "thread_token_usage": usage(10000, 6000, 2000)}),
            token(usage(40, 10, 10), usage(40, 10, 10)),
            event("token_usage_record", {"response_id": "resp2", "usage": usage(40, 10, 10),
                "thread_token_usage": usage(10040, 6010, 2010)})])
        row = next(iter(facts.values()))
        self.assertEqual(row["messages"], 2)
        self.assertEqual(sum(row[k] for k in ingest.TOKEN_FIELDS), 170)

    def test_month_filter_keeps_previous_counter_and_model_context(self):
        facts, _, _ = self.scan([
            token(usage(), timestamp="2026-07-31T12:00:00Z"),
            token(usage(150, 80, 30), timestamp="2026-08-01T12:00:00Z")], "2026-08")
        row = next(iter(facts.values()))
        self.assertEqual(row["input"] + row["cache_read"], 50)
        self.assertEqual(row["output"], 10)

    def test_counter_reset_uses_last_usage_and_model_switch(self):
        facts, _, _ = self.scan([token(usage(), usage()),
            event("turn_context", {"model": "gpt-5.4", "turn_id": "t2"}),
            token(usage(40, 10, 10), usage(40, 10, 10))])
        self.assertEqual(len(facts), 2)
        self.assertEqual(facts[("2026-08-15", "example", "gpt-5.4")]["input"], 30)

    def test_cache_writes_are_not_added_twice(self):
        u = usage()
        u["cache_write_input_tokens"] = 15
        facts, _, _ = self.scan([token(u, u)])
        row = next(iter(facts.values()))
        self.assertEqual(row["input"], 25)
        self.assertEqual(sum(row[k] for k in ingest.TOKEN_FIELDS), 120)

    def test_null_info_and_non_objects_do_not_break_scan(self):
        facts, _, _ = self.scan([None, [], event("event_msg", {"type": "token_count", "info": None}),
                                 token(usage(), usage())])
        self.assertEqual(len(facts), 1)

    def test_codex_only_installation_reads_archives(self):
        self.file("codex/archived_sessions/session.jsonl", [self.meta, self.context, token(usage(), usage())])
        with patch.object(ingest, "SOURCE", self.root / "no-claude"), patch.object(
                ingest, "CODEX_SOURCE", self.root / "codex"):
            months, _ = ingest.build(None, self.pricing)
        self.assertEqual(months["2026-08"]["rows"][0][9], "codex")
        self.assertEqual(months["2026-08"]["sessions_daily"]["2026-08-15"], 1)

    def test_model_specific_cache_rate_and_unknown_model(self):
        row = ingest.zeros() | {"input": 40, "cache_read": 60, "output": 20, "agent": "codex"}
        self.pricing["models"]["test"] = {"input": 2, "cached_input": 0.5, "output": 10}
        self.assertAlmostEqual(ingest.cost_of(row, "test", self.pricing), 0.00031)
        self.assertEqual(ingest.cost_of(row, "private-model", self.pricing), 0)
        self.assertTrue(ingest.rate_of("private-model", "codex", self.pricing)["unpriced"])

    def test_codex_worktree_uses_repository_name(self):
        self.assertEqual(ingest.project_name("/home/me/.codex/worktrees/abcd/example"), "example")
        self.assertEqual(ingest.project_name("/home/me/worktrees/example/feature"), "example")

    def test_legacy_month_migration_preserves_claude_and_separates_agents(self):
        date = "2026-08-15"
        row = [date, "example", "shared-model", 100, 20, 0, 0, 0, 1]
        old = {"rows": [row], "hours_daily": {date: [10] * 24}, "sessions_daily": {date: 2}}
        new = {"columns": ingest.COLUMNS, "rows": [row + ["codex"]], "agents": {
            "codex": {"hours_daily": {date: [5] * 24}, "sessions_daily": {date: 1}}}}
        merged, _ = ingest.merge_month(old, new)
        self.assertEqual({r[9] for r in merged["rows"]}, {"claude", "codex"})
        self.assertEqual(merged["hours_daily"][date], [15] * 24)
        self.assertEqual(merged["sessions_daily"][date], 3)
        again, _ = ingest.merge_month(merged, new)
        self.assertEqual(again, merged)
        self.file("monthly/2026-08.json", []).write_text(json.dumps(merged))
        with patch.object(ingest, "MONTHLY", self.root / "monthly"):
            payload = ingest.payload_from_disk(self.pricing)
        self.assertEqual(len(payload["rows"]), 2)
        self.assertEqual(payload["agents"]["claude"]["sessions_daily"][date], 2)


if __name__ == "__main__":
    unittest.main()
