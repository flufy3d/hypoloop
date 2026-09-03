"""跑完之后自查：有没有哪一步，agy 明明给了完整产出，我们却没采纳。

为什么需要这个东西：

hypoloop 整套设计的立论是「别信 agent 的声明，看它测出来的读数」。结果它自己
栽在同一个坑里 —— 只看 agy 返回信封上的 `status` 字段，没看信封里装了什么。
agy 返回 `status: ERROR` + "The stream was interrupted."，但 `returncode` 是 0、
`structured_output` 完整、连收尾总结都写完了；hypoloop 把整份产出丢掉，判这一轮
中止，还在目标仓库的提交信息里写下一句假话。

`AgyCall.degraded` 修掉了**那一个**判断。但同样的错误还能从别的地方再犯一次：
以后谁在 `_step` 里加个提前 return、谁改了 schema 名导致产出落到别的文件名下、
谁在异常路径上漏了一次写盘 —— 都会重演「真实工作被静默丢弃」。

所以这里不去猜将来会怎么错，只做一件事：**比对磁盘**。
每一步都会把 agy 的原始返回写成 `<stem>.raw.json`，采纳后的产出写成 `<stem>.json`。
凡是「raw 里有可用产出、却没有对应的 .json」的，就是被丢掉的工作。这个检查
不依赖任何一处具体的判断逻辑，将来换了实现它照样成立。

自查只报告，不抛异常 —— 它是照妖镜，不是熔断器。
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, Dict, List

from .agy import AgyCall

RAW_SUFFIX = ".raw.json"


def _load(path: Path) -> Dict[str, Any]:
    try:
        with io.open(path, encoding="utf-8") as fh:
            blob = json.load(fh)
    except (OSError, ValueError):
        return {}
    return blob if isinstance(blob, dict) else {}


def dropped_payloads(run_dir: Path) -> List[Dict[str, str]]:
    """列出「agy 给了完整产出、hypoloop 却没采纳」的步骤。

    判据只有磁盘上的两个文件，不碰任何业务逻辑：
      <stem>.raw.json 里有可用的 structured_output，而 <stem>.json 不存在。
    """
    out: List[Dict[str, str]] = []
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        return out
    for raw in sorted(run_dir.glob("*" + RAW_SUFFIX)):
        stem = raw.name[: -len(RAW_SUFFIX)]
        if (run_dir / (stem + ".json")).exists():
            continue                      # 采纳了，正常
        call = AgyCall(_load(raw))
        if not call.usable:
            continue                      # 真的没产出，不算丢
        out.append({
            "step": stem,
            "status": str(call.get("status") or "?"),
            "error": str(call.get("error") or "").strip()[:200],
            "tokens": "{0:,}".format(call.total_tokens),
            "file": raw.name,
        })
    return out


def render(run_dir: Path) -> str:
    """自查结论。没问题就返回空串 —— 一切正常时不该刷屏。"""
    rows = dropped_payloads(run_dir)
    if not rows:
        return ""
    lines = [
        "!! 自查发现 {0} 步的产出被丢掉了 —— agy 给了完整结果，hypoloop 没采纳。".format(
            len(rows)),
        "   这是一个 bug，不是 agy 的问题。产出还在磁盘上，别让它白跑：",
    ]
    for r in rows:
        lines.append("     {0}（status={1}，{2} tokens）→ {3}".format(
            r["step"], r["status"], r["tokens"], r["file"]))
        if r["error"]:
            lines.append("       agy 的原话：" + r["error"])
    lines.append("   用 `hypoloop --resume <运行目录> -` 可以在不重跑的前提下接着走。")
    return "\n".join(lines)
