"""假设者：读代码，对任务提出**可证伪**的假设。不动手。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from ._shared import falsifiable_rule, history_briefing, project_tree, readonly_oath

NAME = "假设者"


def build_prompt(task: str, root: Path, cfg: Dict[str, Any],
                 history: List[Dict[str, Any]]) -> str:
    n = int(cfg.get("hypotheses", 3))
    blocks = [
        "你是一个三角色协作系统里的**假设者**。另外两个角色是质疑者（专门挑你毛病）"
        "和验证者（唯一能改代码的人，会去实测你的假设）。",
        "",
        "【任务】\n" + task,
        "",
        "【项目】{0}\n{1}".format(root, project_tree(root)),
        "",
        readonly_oath(),
        "",
        "【你要做的】",
        "1. 先真的去读代码 —— 把和任务相关的文件读明白，别凭文件名猜。",
        "2. 提出 {0} 条假设。每条都是一个**关于「为什么现在不够好」以及「改什么会让它"
        "变好」的具体断言**，不是一句待办。".format(n),
        "3. rationale 里必须引用具体文件和具体位置（函数名、变量名、行号），"
        "让质疑者能顺着你的话去核对。",
        "4. approach 写清楚打算怎么改；risk 写清楚可能的副作用。",
        "",
        falsifiable_rule(),
        "",
        "【质量要求】",
        "- 假设之间要**互相独立**，不要三条其实是同一件事的三种说法。",
        "- 优先提那些**改动小、效果可测、风险低**的；不要提「重写整个渲染管线」"
        "这种没法在一轮里验证的。",
        "- 宁可提三条扎实的，也不要凑数。confidence 老实填，别都写 0.9。",
    ]
    hist = history_briefing(history)
    if hist:
        blocks += ["", hist]
    blocks += ["", "把结果按给定的 JSON schema 输出。"]
    return "\n".join(blocks)
