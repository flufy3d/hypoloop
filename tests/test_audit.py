"""自查的测试 —— 防的是「真实工作被静默丢弃」这一整类 bug。

`AgyCall.degraded` 修掉了具体那一处判断。但同样的错误还能从别的地方再犯：
提前 return、schema 名改了导致产出落到别的文件名下、异常路径上漏了一次写盘。
自查不去猜将来会怎么错，只比对磁盘：raw 里有可用产出、却没有对应的已采纳
产出，就是丢了。这个判据不依赖任何一处具体逻辑。
"""

import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from hypoloop import audit

# 真实那次被丢掉的调用（截取），不是手写的
REAL_DROPPED = {
    "status": "ERROR",
    "error": "The stream was interrupted. Please continue the task you were working on.",
    "returncode": 0,
    "conversation_id": "69d497f8-a47c-40b3-b976-dd529bd09760",
    "usage": {"total_tokens": 1005704},
    "structured_output": {
        "changes": [], "kept": [], "reverted": ["h1-bloom-pass-mips-bandwidth"],
        "evidence": [{"verdict": "refuted", "hypothesis_id": "h1-bloom-pass-mips-bandwidth"}],
        "summary": "干完了",
    },
}


def _write(d: Path, name: str, blob) -> None:
    with io.open(d / name, "w", encoding="utf-8") as fh:
        json.dump(blob, fh, ensure_ascii=False)


class TestDroppedPayloads(unittest.TestCase):
    def test_catches_the_real_case(self):
        with TemporaryDirectory() as t:
            d = Path(t)
            _write(d, "round2-verification.raw.json", REAL_DROPPED)
            # 注意：没有 round2-verification.json —— 产出没被采纳
            rows = audit.dropped_payloads(d)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["step"], "round2-verification")
            self.assertEqual(rows[0]["status"], "ERROR")
            self.assertEqual(rows[0]["tokens"], "1,005,704")

            text = audit.render(d)
            self.assertIn("round2-verification", text)
            self.assertIn("1,005,704", text)
            self.assertIn("stream was interrupted", text)

    def test_accepted_payload_is_not_flagged(self):
        with TemporaryDirectory() as t:
            d = Path(t)
            _write(d, "round2-verification.raw.json", REAL_DROPPED)
            _write(d, "round2-verification.json", REAL_DROPPED["structured_output"])
            self.assertEqual(audit.dropped_payloads(d), [])
            self.assertEqual(audit.render(d), "")

    def test_genuine_failure_is_not_flagged(self):
        # 真的什么都没产出，不算「被丢掉」—— 否则自查会天天喊狼来了
        with TemporaryDirectory() as t:
            d = Path(t)
            _write(d, "round1-verification.raw.json",
                   {"status": "TIMEOUT", "error": "超时", "usage": {}})
            self.assertEqual(audit.dropped_payloads(d), [])

    def test_success_path_stays_quiet(self):
        with TemporaryDirectory() as t:
            d = Path(t)
            for stem in ("round1-hypotheses", "round1-critique", "round1-verification"):
                _write(d, stem + ".raw.json",
                       {"status": "SUCCESS", "structured_output": {"a": 1}})
                _write(d, stem + ".json", {"a": 1})
            self.assertEqual(audit.render(d), "")

    def test_broken_raw_file_does_not_crash(self):
        with TemporaryDirectory() as t:
            d = Path(t)
            (d / "round1-verification.raw.json").write_text("{ 这不是 JSON",
                                                            encoding="utf-8")
            self.assertEqual(audit.dropped_payloads(d), [])

    def test_missing_dir_does_not_crash(self):
        self.assertEqual(audit.dropped_payloads(Path("这个目录不存在")), [])
        self.assertEqual(audit.render(Path("这个目录不存在")), "")

    def test_reports_every_dropped_step_not_just_the_first(self):
        with TemporaryDirectory() as t:
            d = Path(t)
            _write(d, "round1-verification.raw.json", REAL_DROPPED)
            _write(d, "round2-verification.raw.json", REAL_DROPPED)
            self.assertEqual(len(audit.dropped_payloads(d)), 2)


class TestWiredIntoRun(unittest.TestCase):
    def test_loop_runs_the_audit_before_committing(self):
        """顺序有意义：真丢了东西，人得在「已提交」那句之前先看到。"""
        import inspect

        from hypoloop import loop

        src = inspect.getsource(loop.run)
        self.assertIn("audit.render", src)
        self.assertLess(src.index("audit.render"), src.index("_commit_run"))


if __name__ == "__main__":
    unittest.main()
