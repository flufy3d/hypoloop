"""三个角色共用的提示词零件。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

SKIP_PARTS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist",
              "build", ".hypoloop", ".idea", ".vscode"}
MAX_TREE = 120


def project_tree(root: Path, limit: int = MAX_TREE) -> str:
    """给个文件清单当起点。agent 自己有读文件的工具，这里只负责让它别乱找。"""
    root = Path(root)
    rows: List[str] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in SKIP_PARTS for part in rel.parts):
            continue
        if not path.is_file():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        rows.append("  {0}  ({1:,} B)".format(str(rel).replace("\\", "/"), size))
        if len(rows) >= limit:
            rows.append("  …（还有更多，自己去看）")
            break
    return "\n".join(rows)


def as_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def readonly_oath() -> str:
    """只读角色的纪律。注意：这段话是**提醒**，真正的强制在 guard.py。"""
    return (
        "【硬性约束】你是只读角色。**一个字节都不许写进工作区** —— 不许改文件、不许"
        "新建文件、不许 git 操作、不许跑任何会改变仓库状态的命令。读、搜、跑只读命令"
        "都随意。\n"
        "（这条约束由外部机制强制：调用方在你跑之前和跑之后各给工作区拍了一次指纹，"
        "你只要动了任何东西，这一轮会被判定失败并中止。所以别试。）"
    )


def falsifiable_rule() -> str:
    return (
        "【可证伪】每条假设都必须带一个 predicted_observable：**如果这条假设成立，"
        "哪个可以被测量的量会怎么变**。要具体到能拿数字或明确的是/否来对照，比如"
        "「首屏截图的 RMS 对比度会上升」「console 不再报 X」「某个断言会通过」。"
        "写「画面会更好看」这种没法测的，等于没写。"
    )


def history_briefing(rounds: List[Dict[str, Any]]) -> str:
    """把前几轮压缩成一段 —— 只留结论和证据，不留过程。"""
    if not rounds:
        return ""
    out = ["【前几轮发生了什么】"]
    for r in rounds:
        out.append("── 第 {0} 轮 ──".format(r.get("round")))
        v = r.get("verify") or {}
        if v.get("summary"):
            out.append("验证者结论：" + str(v["summary"]))
        for e in (v.get("evidence") or []):
            out.append("  [{0}] {1} —— 方法：{2}；前：{3}；后：{4}".format(
                e.get("verdict"), e.get("hypothesis_id"), e.get("method"),
                e.get("before"), e.get("after")))
        if v.get("kept"):
            out.append("  已实证并保留：" + ", ".join(map(str, v["kept"])))
        if v.get("reverted"):
            out.append("  已证伪并回滚：" + ", ".join(map(str, v["reverted"])))
        for q in (v.get("open_questions") or []):
            out.append("  未决问题：" + str(q))
    out.append("\n**不要重复已经被证伪的方向**，也不要把已经保留下来的改动再做一遍。")
    return "\n".join(out)
