"""agy 额度探针。

agy **没有**任何 quota/usage/status 子命令，`~/.gemini` 下也没有额度文件。真额度只有
一条路能拿到：

  `-v=3` 是 glog 的 verbosity 标志（`agy --help` 里没列），打开后 agy 会把完整的
  HTTP 请求/响应体写进 ~/.gemini/antigravity-cli/log/cli-*.log。跑一次
  `agy -v=3 models` —— 它只列模型、不生成内容，**不烧额度** —— 日志里就有
  v1internal:retrieveUserQuotaSummary 的响应体：

      groups[].buckets[]{bucketId, window: "5h"|"weekly", resetTime, remainingFraction}

  额度**按模型组分**（Gemini 一组，Claude+GPT 另一组），各有 5 小时窗口和周窗口。
  同一份日志里的 v1internal:loadCodeAssist 给订阅档位。

这套办法的出处和踩过的坑来自 flufy3d/taiji 的 hub/service/quota.py，这里按 agy 1.1.25
重新实测过（2026-09-03 有效）。glog 的行号从 taiji 记录的 :256 变成了 :324，所以正则
**不锚行号**。

铁律：**探针失败绝不能影响主流程。** 拿不到额度是小事，把三角色循环搞崩是大事。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .agy import AgyNotFound, agy_binary

HOME = Path(os.path.expanduser("~"))
AGY_LOG_DIR = Path(".gemini") / "antigravity-cli" / "log"

QUOTA_MARK = "v1internal:retrieveUserQuotaSummary"
TIER_MARK = "v1internal:loadCodeAssist"

# 行尾那个冒号是实测出来的，不是装饰：
#   http_helpers.go:324] Response from https://…/v1internal:retrieveUserQuotaSummary:
# 而且必须是 "Response from" 而不是 "Response Headers from"（那后面跟的是响应头）。
# 故意不锚 go 文件的行号 —— agy 一升级行号就变。
_RESP_LINE = re.compile(r"Response from \S*?(v1internal:[A-Za-z]+):?\s*$")

# 一次 agy 调用会写**多份**日志（主进程 + 干活的子进程），HTTP 体只落在其中一份里，
# 而且不保证是 mtime 最新的那份。所以要扫最近几份，别只看最新那一个。
LOG_SCAN = 4
PROBE_TIMEOUT_SEC = 90.0

SRC_PROBE = "probe"    # 厂商自己记的真额度
SRC_NONE = "none"      # 没拿到 —— 如实说，不编


class Quota(dict):
    """一次额度读数。available=False 时 note 说明为什么。"""

    @property
    def available(self) -> bool:
        return bool(self.get("available"))


def _iso(value: Any) -> Optional[str]:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M").strip()


def _newest_first(paths: List[Path]) -> List[Path]:
    files = [p for p in paths if p.is_file()]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def _brace_delta(line: str) -> int:
    """这一行让花括号深度变化多少（字符串里的括号不算）。"""
    depth, in_str, escaped = 0, False, False
    for ch in line:
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == '"':
            in_str = not in_str
        elif not in_str:
            depth += (ch == "{") - (ch == "}")
    return depth


def log_bodies(text: str) -> Dict[str, Any]:
    """把日志里所有「Response from …v1internal:X」后面的 JSON 体抠出来。

    这份日志不是 jsonl，是 glog 文本行里夹着 pretty-print 的多行 JSON。按
    「标记行 → 从下一行起累到花括号配平」切。同一个方法出现多次时**后来的覆盖
    先前的** —— 最新那次才是当前额度。
    """
    out: Dict[str, Any] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        hit = _RESP_LINE.search(lines[i])
        if not hit:
            i += 1
            continue
        method, buf, depth, i = hit.group(1), [], 0, i + 1
        while i < len(lines):
            if not buf and "{" not in lines[i]:
                i += 1      # 响应体前的空行：还没开始数，别让 depth 立刻归零
                continue
            buf.append(lines[i])
            depth += _brace_delta(lines[i])
            i += 1
            if depth <= 0:
                break
        try:
            out[method] = json.loads("\n".join(buf))
        except ValueError:
            pass            # 日志被轮转/截断过 —— 跳过这一段，别让整份日志报废
    return out


def pick_group(groups: List[Dict[str, Any]], model: str) -> Optional[Dict[str, Any]]:
    """按模型选对应的额度组，否则会拿 Claude 那组的数字冒充 Gemini 的。"""
    if not groups:
        return None
    family = (model or "").split("-")[0].lower()
    if family:
        for group in groups:
            blurb = "{0} {1}".format(
                group.get("displayName", ""), group.get("description", "")).lower()
            if family in blurb:
                return group
    return groups[0]


def refresh_logs(home: Path = HOME, run: bool = True) -> Tuple[List[Path], float]:
    """跑一次 `agy -v=3 models`，返回（最近几份日志, 本次开跑的时间戳）。

    选 models 子命令是因为它只列模型、不生成内容 —— **这次调用不消耗额度**。
    """
    started = datetime.now(timezone.utc).timestamp()
    if run:
        try:
            subprocess.run(
                [agy_binary(), "-v=3", "models"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                stdin=subprocess.DEVNULL, timeout=PROBE_TIMEOUT_SEC, check=False,
            )
        except (OSError, subprocess.SubprocessError, AgyNotFound):
            pass    # 跑不起来就退回读现存日志，总比什么都没有强
    logs = _newest_first(list((home / AGY_LOG_DIR).glob("cli-*.log")))
    return logs[:LOG_SCAN], started


def _bucket(buckets: Dict[str, Any], window: str) -> Dict[str, Any]:
    b = buckets.get(window) or {}
    frac = b.get("remainingFraction")
    return {
        "window": window,
        "remaining_fraction": float(frac) if frac is not None else None,
        "remaining_pct": round(100.0 * float(frac), 3) if frac is not None else None,
        "reset_at": _iso(b.get("resetTime")),
        "description": b.get("description"),
    }


def _tier(load_code_assist: Dict[str, Any]) -> Optional[str]:
    for key in ("paidTier", "currentTier"):
        tier = load_code_assist.get(key) or {}
        if not isinstance(tier, dict):
            continue
        name = tier.get("name")
        if name:
            ident = tier.get("id")
            return "{0} ({1})".format(name, ident) if ident else str(name)
    return None


def probe(home: Path = HOME, model: str = "gemini", run: bool = True) -> Quota:
    """读一次真额度。任何失败都返回 available=False + note，**不抛**。"""
    try:
        return _probe(home, model, run)
    except Exception as e:      # noqa: BLE001 —— 探针绝不能拖垮主流程
        return Quota(available=False, source=SRC_NONE,
                     note="额度探针异常（{0}: {1}）".format(type(e).__name__, e))


def _probe(home: Path, model: str, run: bool) -> Quota:
    logs, started = refresh_logs(home, run=run)
    if not logs:
        return Quota(available=False, source=SRC_NONE,
                     note="找不到 ~/{0}/cli-*.log".format(AGY_LOG_DIR.as_posix()))

    tier = None
    for log in logs:
        try:
            bodies = log_bodies(log.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        tier = tier or _tier(bodies.get(TIER_MARK) or {})
        summary = bodies.get(QUOTA_MARK)
        if not isinstance(summary, dict):
            continue

        groups = [g for g in (summary.get("groups") or []) if isinstance(g, dict)]
        group = pick_group(groups, model) or {}
        buckets = {b.get("window"): b for b in group.get("buckets", [])
                   if isinstance(b, dict)}
        five, weekly = _bucket(buckets, "5h"), _bucket(buckets, "weekly")
        # 本次调用之前就存在的日志 ⇒ 读到的是上一次留下的旧读数，得说清楚
        stale = log.stat().st_mtime < started - 5
        return Quota(
            available=(five["remaining_fraction"] is not None
                       or weekly["remaining_fraction"] is not None),
            source=SRC_PROBE,
            tier=tier,
            group=group.get("displayName") or "未识别",
            five_hour=five,
            weekly=weekly,
            stale=stale,
            log=log.name,
            note="读自 {0} 的 {1}{2}".format(
                log.name, QUOTA_MARK,
                "，**是上一次调用留下的旧读数**" if stale else ""),
        )

    return Quota(
        available=False, source=SRC_NONE, tier=tier,
        note="最近 {0} 份 agy 日志里都没有 {1} 的响应体 —— agy 换版本后 "
             "-v=3 可能不再打印 HTTP 体了".format(len(logs), QUOTA_MARK))


def format_quota(q: Quota, title: str = "agy 额度") -> str:
    """给终端看的一段。拿不到就如实说拿不到，不编数字。"""
    if not q.available:
        return "{0}：读不到 —— {1}".format(title, q.get("note") or "未知原因")
    head = "{0}（{1}{2}）：".format(
        title, q.get("group") or "?",
        "，档位 " + q["tier"] if q.get("tier") else "")
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
