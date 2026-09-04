"""额度读数的公共类型和格式化。

**每家 CLI 的额度口子完全不同，所以这里不做统一探测，只做统一的「读数」类型。**
真正的探针在各自的 backend 里（`hypoloop/backends/<name>.py` 的 `quota()`）：

  agy    —— `agy -v=3 models` 把 HTTP 响应体打进日志，再读出来（不烧额度）
  codex  —— `codex app-server` 的 `account/rateLimits/read`（官方协议方法，不烧额度）
  claude —— `GET /api/oauth/usage`，凭据蹭 Claude Code 自己维护的那份（不烧额度）

三条路都**不消耗额度**，所以「跑之前报一次、跑完再报一次」这件事本身是免费的。

铁律：**探针失败绝不能影响主流程。** 拿不到额度是小事，把三角色循环搞崩是大事。
所以所有 `quota()` 都要求「从不抛」，拿不到就返回 `unavailable(原因)`。

口径统一成「**还剩百分之几**」：agy 原生就给 remainingFraction，codex 和 claude 给的
是 used_percent，在各自 backend 里换算完再进来。这样 `consumed()` 对三家都成立。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

SRC_PROBE = "probe"      # 厂商自己记的真额度
SRC_DERIVED = "derived"  # 我们从本地用量日志推算的
SRC_NONE = "none"        # 没拿到 —— 如实说，不编


class Quota(dict):
    """一次额度读数。available=False 时 note 说明为什么。"""

    @property
    def available(self) -> bool:
        return bool(self.get("available"))


def iso_local(value: Any) -> Optional[str]:
    """重置时间 → 本地时间字符串。

    三家给的格式不一样：agy 和 claude 给 ISO-8601 字符串，codex 给 unix 秒。
    都收，认不出来就返回 None（宁可不显示，也不显示一个错的时间）。
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def window(name: str, remaining_pct: Optional[float], reset_at: Any = None,
           description: Optional[str] = None) -> Dict[str, Any]:
    """一个额度窗口的读数。remaining_pct 是 None 表示这个窗口没读到。"""
    pct = None if remaining_pct is None else round(float(remaining_pct), 3)
    return {
        "window": name,
        "remaining_pct": pct,
        "remaining_fraction": None if pct is None else pct / 100.0,
        "reset_at": iso_local(reset_at),
        "description": description,
    }


def from_used_pct(name: str, used_pct: Any, reset_at: Any = None,
                  description: Optional[str] = None) -> Dict[str, Any]:
    """codex / claude 报的是「已用百分之几」，换算成本库统一的「还剩」。"""
    if used_pct is None:
        return window(name, None)
    try:
        return window(name, 100.0 - float(used_pct), reset_at, description)
    except (TypeError, ValueError):
        return window(name, None)


def unavailable(note: str, **extra: Any) -> Quota:
    return Quota(available=False, source=SRC_NONE, note=note, **extra)


def make(*, source: str, five_hour: Dict[str, Any], weekly: Dict[str, Any],
         note: str = "", **extra: Any) -> Quota:
    """组装一次读数。两个窗口至少有一个读到才算 available。"""
    return Quota(
        available=(five_hour.get("remaining_pct") is not None
                   or weekly.get("remaining_pct") is not None),
        source=source, five_hour=five_hour, weekly=weekly, note=note, **extra)


def format_quota(q: Quota, title: str = "额度") -> str:
    """给终端看的一段。拿不到就如实说拿不到，不编数字。"""
    if not q.available:
        return "{0}：读不到 —— {1}".format(title, q.get("note") or "未知原因")
    bits = [b for b in (q.get("group"),
                        "档位 " + str(q["tier"]) if q.get("tier") else None) if b]
    head = "{0}{1}：".format(title, "（{0}）".format("，".join(bits)) if bits else "")
    lines = [head]
    for key, label in (("five_hour", "5 小时窗口"), ("weekly", "周窗口  ")):
        b = q.get(key) or {}
        if b.get("remaining_pct") is None:
            continue
        lines.append("  {0}  剩余 {1:.3f}%{2}".format(
            label, b["remaining_pct"],
            "   重置于 " + b["reset_at"] if b.get("reset_at") else ""))
    if q.get("stale"):
        lines.append("  （注意：这是上一次调用留下的旧读数）")
    if q.get("coarse"):
        lines.append("  （注意：{0} 只给整数百分比，所以小额消耗可能显示为 0）".format(
            q.get("coarse")))
    return "\n".join(lines)


def consumed(before: Quota, after: Quota) -> Dict[str, Optional[float]]:
    """两次读数之间吃掉了多少额度 —— 这是最诚实的「这轮花了多少」。"""
    out: Dict[str, Optional[float]] = {}
    for key in ("five_hour", "weekly"):
        b, a = (before.get(key) or {}), (after.get(key) or {})
        if b.get("remaining_pct") is None or a.get("remaining_pct") is None:
            out[key] = None
        else:
            out[key] = round(b["remaining_pct"] - a["remaining_pct"], 3)
    return out
