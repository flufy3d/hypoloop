"""验证者：唯一能改代码的角色。自己搭取证手段，把结论推向被实证的方向。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from .. import environ
from ._shared import as_json, history_briefing, project_tree

NAME = "验证者"


def corrections(critique: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把质疑者散在各条意见里的订正项摊平成一张清单。"""
    out: List[Dict[str, Any]] = []
    for c in (critique or {}).get("critiques") or []:
        for fix in c.get("corrections") or []:
            if isinstance(fix, dict) and fix.get("id"):
                item = dict(fix)
                item["hypothesis_id"] = c.get("hypothesis_id")
                out.append(item)
    return out


def corrections_checklist(critique: Dict[str, Any]) -> str:
    """订正清单。**必须逐条签收**，这是机制而不是客气话。

    为什么单独摆出来而不是让它自己从 critique 的 JSON 里翻：质疑者的裁决
    （keep/revise/reject）是结构化的，具体数值订正原来只在 flaw 的自由文本里。
    实际发生过一次：验证者接受了 revise 这个裁决，却把被点名算错的那个常数原样
    写进代码，留下一个必死率 9% 的窟窿，下一轮才被抓出来。所以摊平、编号、
    要求逐条交代，调用方还会机械核对 id 覆盖情况。
    """
    items = corrections(critique)
    if not items:
        return "【订正清单】质疑者这轮没给出需要订正的具体数值。"
    lines = ["【订正清单 —— 每一条都必须在 corrections_addressed 里交代，"
             "一条不许漏。调用方会机械核对 id，漏了会被点名】"]
    for it in items:
        lines.append("  [{0}] （针对 {1}）{2}".format(
            it.get("id"), it.get("hypothesis_id"), it.get("what")))
        lines.append("       假设者用的错值：{0}".format(it.get("wrong")))
        lines.append("       正确值：{0}".format(it.get("correct")))
        lines.append("       依据：{0}".format(it.get("basis")))
    lines.append("**注意：采纳 revise 这个裁决，不等于采纳了订正的数值。** "
                 "要么按 correct 改并给出读数（applied），要么拿读数证明质疑者也"
                 "错了（rejected），要么说清这条为什么落空（not_applicable）。"
                 "「我觉得不用」不是理由。")
    return "\n".join(lines)


def build_prompt(task: str, root: Path, cfg: Dict[str, Any],
                 hypotheses: Dict[str, Any], critique: Dict[str, Any],
                 history: List[Dict[str, Any]]) -> str:
    hint = (cfg.get("evidence_hint") or "").strip()
    blocks = [
        "你是一个三角色协作系统里的**验证者**。假设者提了假设，质疑者挑了毛病，"
        "现在轮到你。**你是这三个人里唯一能改文件的**，所以这一轮最后留在仓库里的"
        "东西全部由你负责。",
        "",
        "【任务】\n" + task,
        "",
        "【项目】{0}\n{1}".format(root, project_tree(root)),
        "",
        "【假设者提交的内容】\n" + as_json(hypotheses),
        "",
        "【质疑者的意见】\n" + as_json(critique),
        "",
        corrections_checklist(critique),
        "",
        "【最重要的一条：往被实证的方向走】",
        "你的目标不是「把假设实现掉」，是**搞清楚哪条是真的**。做法：",
        "  1. **先采基线。** 动手改任何东西之前，先把「现在是什么样」测下来、记下来。"
        "改完再测一次同样的东西。没有基线的对比不算证据。",
        "  2. 一次只动一条假设能覆盖的范围，改完立刻测，别攒着一起测 —— 攒着测就分不"
        "清是哪一条起的作用。",
        "  3. 每条假设给一个 verdict：supported（前后读数明确支持它）、"
        "refuted（明确不支持，或者引入了新问题）、inconclusive（测不出来）。",
        "  4. **被证伪的改动自己回滚掉**（`git checkout -- <file>` 或手工改回去），"
        "并把 id 写进 reverted。留着一堆没有证据支撑的改动是这一轮最坏的结果。",
        "  5. inconclusive 不丢人 —— 把「为什么测不出来」写进 reasoning 和 "
        "open_questions，下一轮好接着办。**编一个没测过的读数才是最坏的**。",
        "",
        "【怎么取证由你自己定】",
        "你有完整的 shell。用什么手段取证是你的判断：跑现成的测试、写个一次性脚本、"
        "起本地服务、装个无头浏览器截图对比、量个数值指标 …… 都行。",
        "只有两条边界：",
        "  - **不许把取证用的工具、依赖、临时产物留在这个仓库里。** 要装东西就装到"
        "系统级或临时目录（比如 %TEMP% / ~ 下自己建个目录），跑完清理干净。"
        "这一轮结束时，`git status` 里只应该有你**对源码的正当修改**，不能有 "
        "node_modules、截图、日志、脚本之类的残渣。",
        "  - 装东西前先看看有没有现成的；能用标准库/系统自带的就别装。",
    ]
    stop_when = (cfg.get("stop_when") or "").strip()
    if stop_when:
        blocks += [
            "",
            "【结束条件】\n" + stop_when,
            "调用方设了这个条件：一旦达成，**剩下的轮次全部跳过**。所以你要在 goal "
            "字段里给出判断和证据。",
            "  - met=true 时，evidence 必须**逐项**对上条件里的每一个要求，"
            "每项都给出你这一轮真的测出来的读数。",
            "  - 判不准、证据不足、只验了一部分、条件里有一半没测 —— **一律 false**，"
            "并在 evidence 里写清还差什么。",
            "  - **别为了让任务显得完成而填 true。** 多跑一轮只是多花点 token；"
            "错误地提前收工，会把没验完的改动留在别人的仓库里。这个代价大得多。",
        ]

    env = environ.block()
    if env:
        blocks += ["", env]
    if hint:
        blocks += ["", "【调用方给的取证提示】\n" + hint +
                   "\n（这是建议不是命令 —— 你有更好的办法就用你的，但要在 method "
                   "里说清楚你用了什么。）"]
    blocks += [
        "",
        "【报告纪律】",
        "- evidence 里的 before/after 必须是**你真的测出来的具体读数**"
        "（数字、报错原文、通过/失败）。写「变好了」「更流畅」这种没有出处的话，"
        "等于这条假设根本没被验证。",
        "- method 要写清你是怎么测的，让人能照着重跑一遍。",
        "- changes 要和 evidence 对得上：每个改动都要能指回一条假设。",
        "- summary 里如果这一轮没能实证任何东西，就老实说没有。",
    ]
    hist = history_briefing(history)
    if hist:
        blocks += ["", hist]
    blocks += ["", "干完活后，把结果按给定的 JSON schema 输出。"]
    return "\n".join(blocks)
