"""Run with python3 -m unittest discover -s scripts -p 'test_*.py'."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ingest
import publish


class DayRecordTests(unittest.TestCase):
    def test_each_day_holds_its_rows_and_side_stats_per_agent(self):
        month = {"rows": [["2026-10-27", "example", "claude-opus-5-5", 1, 2, 3, 4, 5, 6, "claude"],
                          ["2026-10-27", "example", "gpt-6.1-sol", 1, 2, 0, 0, 5, 1, "codex"],
                          ["2026-10-28", "other", "claude-opus-5-5", 1, 1, 0, 0, 0, 1]],
                 "agents": {"claude": {"sessions_daily": {"2026-10-27": 2, "2026-10-28": 1},
                                       "hours_daily": {"2026-10-27": [1] * 24}},
                            "codex": {"sessions_daily": {"2026-10-27": 1, "2026-09-30": 4}}}}
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "2026-10.json").write_text(json.dumps(month))
            with patch.object(ingest, "MONTHLY", Path(temp)):
                days = publish.day_records("agent-jasper")
        self.assertEqual(sorted(days), ["2026-10-27", "2026-10-28"])
        day = days["2026-10-27"]
        self.assertEqual((day["machine"], day["columns"]), ("agent-jasper", ingest.COLUMNS[1:]))
        self.assertEqual([r[-1] for r in day["rows"]], ["claude", "codex"])
        self.assertEqual(day["agents"], {"claude": {"sessions_daily": 2, "hours_daily": [1] * 24},
                                         "codex": {"sessions_daily": 1}})
        self.assertEqual(days["2026-10-28"]["rows"][0][-1], "claude")
        self.assertEqual(publish.digest(day), publish.digest(json.loads(json.dumps(day))))

    def test_machine_name_prefers_the_override_and_lowercases(self):
        self.assertEqual(publish.machine_name("Agent-Jasper"), "agent-jasper")
        with patch.dict("os.environ", {"USAGE_MACHINE": "MacBook-Pro"}):
            self.assertEqual(publish.machine_name(None), "macbook-pro")


if __name__ == "__main__":
    unittest.main()
