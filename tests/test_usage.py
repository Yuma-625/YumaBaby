import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from app.utils import llm, usage


class UsageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "usage.jsonl"
        self._patches = [
            patch.object(settings, "USAGE_LOG", str(self.path)),
            patch.object(settings, "LLM_PRICE_INPUT_PER_M", 2.0),
            patch.object(settings, "LLM_PRICE_OUTPUT_PER_M", 8.0),
            patch.object(settings, "DAILY_BUDGET_SOFT", 10.0),
            patch.object(settings, "DAILY_BUDGET_HARD", 30.0),
        ]
        for p in self._patches:
            p.start()
        usage.reset_state()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        usage.reset_state()
        self.tmp.cleanup()

    def test_estimate_cost(self):
        self.assertAlmostEqual(usage.estimate_cost(1_000_000, 0), 2.0)
        self.assertAlmostEqual(usage.estimate_cost(0, 1_000_000), 8.0)
        self.assertAlmostEqual(usage.estimate_cost(500_000, 250_000), 3.0)

    def test_record_and_summary(self):
        usage.record_usage("deepseek-chat", 1000, 200)
        usage.record_usage("deepseek-chat", 2000, 100, kind="analysis")

        summary = usage.today_summary()
        self.assertEqual(summary["calls"], 2)
        self.assertEqual(summary["prompt_tokens"], 3000)
        self.assertEqual(summary["completion_tokens"], 300)
        self.assertAlmostEqual(summary["cost_cny"], 3000 / 1e6 * 2 + 300 / 1e6 * 8, places=6)
        self.assertEqual(summary["by_kind"]["analysis"]["calls"], 1)
        self.assertEqual(summary["level"], "ok")

    def test_survives_restart(self):
        usage.record_usage("m", 1000, 100)
        usage.reset_state()  # 模拟进程重启，重新从文件扫描
        self.assertEqual(usage.today_summary()["calls"], 1)

    def test_budget_levels(self):
        usage.record_usage("m", 5_000_000, 0)      # ¥10
        self.assertEqual(usage.budget_level(), "soft")
        usage.record_usage("m", 10_000_000, 0)     # 累计 ¥30
        self.assertEqual(usage.budget_level(), "hard")
        self.assertTrue(usage.should_block_call())

    def test_zero_limit_disables_budget(self):
        with patch.object(settings, "DAILY_BUDGET_SOFT", 0), \
             patch.object(settings, "DAILY_BUDGET_HARD", 0):
            usage.record_usage("m", 100_000_000, 0)
            self.assertEqual(usage.budget_level(), "ok")
            self.assertFalse(usage.should_block_call())

    def test_corrupt_line_is_ignored(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json}\n", encoding="utf-8")
        self.assertEqual(usage.today_summary()["calls"], 0)

    def test_chat_skips_call_at_hard_limit(self):
        with patch.object(llm, "get_llm", return_value=object()), \
             patch.object(usage, "should_block_call", return_value=True):
            self.assertIsNone(asyncio.run(llm.chat("hi")))


if __name__ == "__main__":
    unittest.main()
