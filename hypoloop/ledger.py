"""token 账本。

agy 每次调用的 JSON 里都带 usage，这里按角色累计，再把每次运行的总账追加到
~/.hypoloop/ledger.jsonl。额度百分比是厂商给的真数字（见 quota.py），token 数是
这里自己数的 —— 两者互相印证，任务结束时一起报。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from .config import LEDGER, ensure_dirs

FIELDS = ("input_tokens", "output_tokens", "thinking_tokens",
          "cache_read_tokens", "total_tokens")


class Ledger:
    """一次运行里所有 agy 调用的账。"""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def add(self, role: str, round_no: int, call: Dict[str, Any]) -> None:
        usage = call.get("usage") or {}
        self.calls.append({
            "role": role,
            "round": round_no,
            "status": call.get("status"),
            "conversation_id": call.get("conversation_id"),
            "duration_seconds": call.get("duration_seconds"),
            "usage": {k: int(usage.get(k) or 0) for k in FIELDS},
        })

    @property
    def total(self) -> int:
        return sum(c["usage"]["total_tokens"] for c in self.calls)

    def by_role(self) -> Dict[str, Dict[str, int]]:
        out: Dict[str, Dict[str, int]] = {}
        for c in self.calls:
            slot = out.setdefault(c["role"], {"calls": 0, "total_tokens": 0})
            slot["calls"] += 1
            slot["total_tokens"] += c["usage"]["total_tokens"]
        return out

    def totals(self) -> Dict[str, int]:
        return {k: sum(c["usage"][k] for c in self.calls) for k in FIELDS}

    def render(self) -> str:
        if not self.calls:
            return "本次没有产生任何 agy 调用。"
        rows = ["本次 token 消耗（来自 agy 每次调用返回的 usage）：",
                "  {0:<8}{1:>8}{2:>14}".format("角色", "调用数", "total_tokens")]
        for role, slot in self.by_role().items():
            rows.append("  {0:<8}{1:>8}{2:>14,}".format(
                role, slot["calls"], slot["total_tokens"]))
        t = self.totals()
        rows.append("  {0:<8}{1:>8}{2:>14,}".format("合计", len(self.calls),
                                                    t["total_tokens"]))
        rows.append("  （其中 输入 {0:,} / 输出 {1:,} / 思考 {2:,} / 缓存读 {3:,}）"
                    .format(t["input_tokens"], t["output_tokens"],
                            t["thinking_tokens"], t["cache_read_tokens"]))
        return "\n".join(rows)

    def record(self, *, task: str, target: str, run_dir: str,
               quota_before: Optional[Dict[str, Any]] = None,
               quota_after: Optional[Dict[str, Any]] = None,
               consumed: Optional[Dict[str, Any]] = None) -> None:
        """追加一条到 ~/.hypoloop/ledger.jsonl。写失败不影响任务结果。"""
        entry = {
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "task": task,
            "target": target,
            "run_dir": run_dir,
            "calls": len(self.calls),
            "tokens": self.totals(),
            "by_role": self.by_role(),
            "quota_before": quota_before,
            "quota_after": quota_after,
            "quota_consumed_pct": consumed,
        }
        try:
            ensure_dirs()
            with LEDGER.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass


def history(limit: int = 20) -> List[Dict[str, Any]]:
    if not LEDGER.exists():
        return []
    out = []
    try:
        for line in LEDGER.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        return []
    return out[-limit:]
