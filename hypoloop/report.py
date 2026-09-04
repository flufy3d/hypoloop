"""把一次运行写成人能读的报告。

产物全部落在 ~/.hypoloop/runs/<时间戳>-<任务名>/，**目标项目里不留任何东西** ——
只有验证者对源码的正当修改会留在那边。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .quota import format_quota

VERDICT_MARK = {"supported": "✅ 实证", "refuted": "❌ 证伪",
                "inconclusive": "⬜ 未决"}
CRIT_MARK = {"keep": "保留", "revise": "修正", "reject": "驳回"}


def dump_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def _hypotheses_section(data: Dict[str, Any]) -> List[str]:
    out = []
    if data.get("understanding"):
        out += ["> " + str(data["understanding"]), ""]
    for h in data.get("hypotheses") or []:
        out += [
            "**{0}** — {1}".format(h.get("id"), h.get("claim")),
            "",
            "- 依据：{0}".format(h.get("rationale")),
            "- 可观测预测：{0}".format(h.get("predicted_observable")),
            "- 涉及文件：{0}".format(", ".join(h.get("files") or []) or "—"),
            "- 打算怎么改：{0}".format(h.get("approach")),
            "- 风险：{0}".format(h.get("risk") or "—"),
            "- 自评置信度：{0}".format(h.get("confidence")),
            "",
        ]
    return out


def _critique_section(data: Dict[str, Any]) -> List[str]:
    out = []
    for c in data.get("critiques") or []:
        out.append("- **{0}** → {1}（{2}）：{3}{4}".format(
            c.get("hypothesis_id"),
            CRIT_MARK.get(c.get("verdict"), c.get("verdict")),
            c.get("severity"), c.get("flaw"),
            "\n  - 改成：" + str(c["revision"]) if c.get("revision") else ""))
    counters = data.get("counter_hypotheses") or []
    if counters:
        out += ["", "**质疑者另提的假设：**", ""]
        for h in counters:
            out += ["- **{0}** — {1}".format(h.get("id"), h.get("claim")),
                    "  - 依据：{0}".format(h.get("rationale")),
                    "  - 可观测预测：{0}".format(h.get("predicted_observable"))]
    if data.get("priority_order"):
        out += ["", "建议验证顺序：" + " → ".join(map(str, data["priority_order"]))]
    return out


def _verify_section(data: Dict[str, Any]) -> List[str]:
    out = ["> " + str(data.get("summary") or ""), ""]
    ev = data.get("evidence") or []
    if ev:
        out += ["| 假设 | 裁决 | 方法 | 改动前 | 改动后 |",
                "|---|---|---|---|---|"]
        for e in ev:
            out.append("| {0} | {1} | {2} | {3} | {4} |".format(
                e.get("hypothesis_id"),
                VERDICT_MARK.get(e.get("verdict"), e.get("verdict")),
                _cell(e.get("method")), _cell(e.get("before")),
                _cell(e.get("after"))))
        out.append("")
        for e in ev:
            if e.get("reasoning"):
                out.append("- **{0}**：{1}".format(
                    e.get("hypothesis_id"), e["reasoning"]))
        out.append("")
    changes = data.get("changes") or []
    if changes:
        out += ["**改了这些文件：**", ""]
        for c in changes:
            out.append("- `{0}`（{1}）：{2}".format(
                c.get("file"), c.get("hypothesis_id"), c.get("summary")))
        out.append("")
    if data.get("kept"):
        out.append("保留：" + ", ".join(map(str, data["kept"])))
    if data.get("reverted"):
        out.append("已证伪并回滚：" + ", ".join(map(str, data["reverted"])))
    for q in data.get("open_questions") or []:
        out.append("- 未决：" + str(q))
    return out


def _cell(text: Any) -> str:
    """塞进 markdown 表格的单元格：换行和竖线都得处理掉。"""
    s = str(text if text is not None else "")
    s = s.replace("|", "\\|").replace("\n", " ")
    return s if len(s) <= 160 else s[:157] + "…"


def write_report(run_dir: Path, *, task: str, target: Path,
                 rounds: List[Dict[str, Any]], ledger_text: str,
                 quota_before: Any, quota_after: Any,
                 consumed: Dict[str, Optional[float]],
                 git_summary: Optional[str] = None,
                 backend: Optional[str] = None) -> Path:
    lines = [
        "# hypoloop 运行报告",
        "",
        "**任务**：{0}".format(task),
        "",
        "**目标项目**：`{0}`".format(target),
        "",
        # 报告要能自证是谁跑的：同一个任务换一家再跑一遍时，两份报告摆在一起
        # 必须一眼看得出区别，否则读数就没法归因。
        "**后端**：{0}".format(backend or "未记录"),
        "",
        "## 额度",
        "",
        "```",
        format_quota(quota_before, "开跑前"),
        "",
        format_quota(quota_after, "跑完后"),
        "```",
        "",
    ]
    deltas = [
        "- 5 小时窗口消耗：{0}".format(
            "{0:.3f} 个百分点".format(consumed["five_hour"])
            if consumed.get("five_hour") is not None else "读不到"),
        "- 周窗口消耗：{0}".format(
            "{0:.3f} 个百分点".format(consumed["weekly"])
            if consumed.get("weekly") is not None else "读不到"),
    ]
    lines += deltas + ["", "```", ledger_text, "```", ""]

    for r in rounds:
        lines += ["---", "", "## 第 {0} 轮".format(r.get("round")), ""]
        if r.get("hypothesize"):
            lines += ["### 假设者", ""] + _hypotheses_section(r["hypothesize"])
        if r.get("challenge"):
            lines += ["### 质疑者", ""] + _critique_section(r["challenge"]) + [""]
        if r.get("verify"):
            lines += ["### 验证者", ""] + _verify_section(r["verify"]) + [""]
        if r.get("error"):
            lines += ["> ⚠️ 这一轮出错：{0}".format(r["error"]), ""]

    if git_summary:
        lines += ["---", "", "## 目标仓库最终状态", "", "```", git_summary, "```", ""]

    path = run_dir / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
