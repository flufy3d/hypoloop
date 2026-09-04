"""探测这台机器上、会影响验证者取证的**环境事实**。

为什么要有这个模块：

`--evidence-hint` 是留给使用者写「这个项目该怎么取证」的。但有一类事实既不属于
使用者、也不属于项目 —— 它属于**机器**：某个工具坏了、某个依赖已经装好了。
让人每次跑都手打一遍这些，既啰嗦又容易过期。更糟的是，人在手打的时候很容易顺手把
**任务结论**也塞进去（「这个项目的 X 是 Y 驱动的」），那是假设者的活，一旦塞进去
这一轮就没法说明系统能自己发现问题了。

所以这里划一条线：

    后端事实  → Backend.env_notes()，只对那一家成立    （这家 CLI 的属性）
    机器事实  → 本模块自动探测                          （这台机器的属性）
    取证提示  → --evidence-hint，默认空                 （人的可选叮嘱）
    任务结论  → 谁都不许预先写，那是三个角色自己的活

头两层拼在一起注入验证者提示。分开是因为「agy 自带的 browser 工具装不上驱动」这种事
只对 agy 成立 —— 换 codex 跑的时候再说一遍是纯噪音，还会误导它去绕一个不存在的坑。

本模块只报**这台机器上确实观测到的**东西，不联网、不花额度、不猜。探测不出来就
什么都不说 —— 说错比不说更坏。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import List, Optional

HOME = Path(os.path.expanduser("~"))

_PW_CACHES = (
    Path(os.environ.get("LOCALAPPDATA") or (HOME / "AppData" / "Local")) / "ms-playwright",
    HOME / "Library" / "Caches" / "ms-playwright",
    HOME / ".cache" / "ms-playwright",
)


def playwright_cached() -> Path | None:
    """ms-playwright 浏览器缓存在不在。在的话 `npm i playwright` 是秒完成的。"""
    for cache in _PW_CACHES:
        try:
            if cache.is_dir() and any(cache.iterdir()):
                return cache
        except OSError:
            continue
    return None


def _tools() -> List[str]:
    found = []
    for name in ("node", "npm", "python3", "python", "git", "docker"):
        if shutil.which(name):
            found.append(name)
    # Windows 上 python3 常常不存在，只有 python；别把这个当成缺失来报
    if "python" in found and "python3" in found:
        found.remove("python3")
    return found


def notes(home: Path = HOME, extra: Optional[List[str]] = None) -> List[str]:
    """返回若干条环境事实。每条都短，都是这台机器上观测到的。

    `extra` 是**当前后端**自己探到的事实（`Backend.env_notes()`）—— 比如 agy 内置的
    browser 工具在这台机器上装不上驱动。这类事实只对那一家成立，所以不写在这里，
    由后端自己报，这里只负责跟机器通用的事实拼在一起。
    """
    out: List[str] = list(extra or [])

    cache = playwright_cached()
    if cache:
        out.append(
            "playwright 的浏览器二进制已经缓存在 {0}，"
            "所以 `npm i playwright && npx playwright install chromium` 走缓存，"
            "不会再下一遍上百 MB。".format(cache))

    tools = _tools()
    if tools:
        out.append("机器上现成可用的：{0}（python 是 {1}）。".format(
            "、".join(tools), sys.version.split()[0]))

    return out


def block(home: Path = HOME, extra: Optional[List[str]] = None) -> str:
    """拼成能直接塞进提示词的一段。没探到任何东西就返回空串。"""
    items = notes(home, extra)
    if not items:
        return ""
    body = "\n".join("- " + n for n in items)
    return ("【这台机器的情况（hypoloop 自动探测，不是针对本项目的建议）】\n"
            + body +
            "\n以上只是环境事实。**这个项目该怎么取证、该改什么，一个字都没告诉你，"
            "那是你自己的判断。**")
