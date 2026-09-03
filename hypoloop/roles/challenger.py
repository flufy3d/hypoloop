"""质疑者：攻击假设，给出修正案或全新假设。不动手。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from ._shared import (as_json, falsifiable_rule, history_briefing, project_tree,
                      readonly_oath)

NAME = "质疑者"


def build_prompt(task: str, root: Path, cfg: Dict[str, Any],
                 hypotheses: Dict[str, Any],
                 history: List[Dict[str, Any]]) -> str:
    blocks = [
        "你是一个三角色协作系统里的**质疑者**。假设者刚提了一批假设，你的活是"
        "**尽力把它们打掉**。打不掉的才值得验证者花时间去实测。",
        "",
        "【任务】\n" + task,
        "",
        "【项目】{0}\n{1}".format(root, project_tree(root)),
        "",
        "【假设者提交的内容】\n" + as_json(hypotheses),
        "",
        readonly_oath(),
        "",
        "【你要做的】",
        "1. **去代码里核对**假设者的每一条 rationale。它引用的文件/函数/行号真的是"
        "那样吗？这一步不许省 —— 假设者最常见的错就是凭印象编一个不存在的调用。",
        "2. 对每条假设给出 critique：",
        "   - flaw 要指名道姓，落到具体缺陷类型上：事实错误 / 不可证伪 / 现有证据"
        "并不支持 / 有更简单的解释 / 副作用被低估 / 和另一条假设其实是同一件事。",
        "   - verdict：keep（站得住，直接验）、revise（方向对但要改，"
        "**必须在 revision 里写清改成什么**）、reject（站不住，说清为什么）。",
        "3. 如果你看出了假设者**整个没想到**的方向，写进 counter_hypotheses —— "
        "格式和假设者的一样，同样要可证伪。没想到就交空数组，别硬凑。",
        "4. priority_order：按「最该先被实证」排个序，把 id 列出来。判断标准是"
        "**信息量**：先验最不确定、但一测就能定生死的排前面。",
        "",
        falsifiable_rule(),
        "",
        "【态度】",
        "- 你的价值在于**说不**。全部 keep 等于你没干活。",
        "- 但也别为反对而反对：挑不出毛病的就老实 keep，并说清它为什么站得住。",
        "- 你不是在评审文风，是在找**会让验证者白干一场**的错。",
    ]
    hist = history_briefing(history)
    if hist:
        blocks += ["", hist]
    blocks += ["", "把结果按给定的 JSON schema 输出。"]
    return "\n".join(blocks)
