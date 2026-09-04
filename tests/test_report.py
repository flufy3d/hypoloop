import tempfile
from pathlib import Path
import unittest

from hypoloop.quota import Quota
from hypoloop.report import write_report


class TestReportQuotaFormatting(unittest.TestCase):
    def test_consumed_quota_sign_and_formatting(self):
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            consumed = {"five_hour": 1.234, "weekly": 0.567}
            qb = Quota({"source": "none", "note": "before"})
            qa = Quota({"source": "none", "note": "after"})

            rep_path = write_report(
                run_dir,
                task="test task",
                target=run_dir,
                rounds=[],
                ledger_text="ledger",
                quota_before=qb,
                quota_after=qa,
                consumed=consumed,
            )
            content = rep_path.read_text(encoding="utf-8")

            self.assertIn("- 5 小时窗口消耗：1.234 个百分点", content)
            self.assertIn("- 周窗口消耗：0.567 个百分点", content)
            self.assertNotIn("-1.234", content)
            self.assertNotIn("+1.234", content)
            self.assertNotIn("-0.567", content)
            self.assertNotIn("+0.567", content)

    def test_consumed_quota_none_shows_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            consumed = {"five_hour": None, "weekly": None}
            qb = Quota({"source": "none", "note": "before"})
            qa = Quota({"source": "none", "note": "after"})

            rep_path = write_report(
                run_dir,
                task="test task",
                target=run_dir,
                rounds=[],
                ledger_text="ledger",
                quota_before=qb,
                quota_after=qa,
                consumed=consumed,
            )
            content = rep_path.read_text(encoding="utf-8")

            self.assertIn("- 5 小时窗口消耗：读不到", content)
            self.assertIn("- 周窗口消耗：读不到", content)
