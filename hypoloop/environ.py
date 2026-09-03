"""探测这台机器上、会影响验证者取证的**环境事实**。

为什么要有这个模块：

`--evidence-hint` 是留给使用者写「这个项目该怎么取证」的。但有一类事实既不属于
使用者、也不属于项目 —— 它属于**机器**：某个工具坏了、某个依赖已经装好了。
让人每次跑都手打一遍这些，既啰嗦又容易过期。更糟的是，人在手打的时候很容易顺手把
**任务结论**也塞进去（「这个项目的 X 是 Y 驱动的」），那是假设者的活，一旦塞进去
这一轮就没法说明系统能自己发现问题了。

所以这里划一条线：

    环境事实  → 本模块自动探测，自动注入验证者提示     （机器的属性）
    取证提示  → --evidence-hint，默认空                （人的可选叮嘱）
    任务结论  → 谁都不许预先写，那是三个角色自己的活

本模块只报**这台机器上确实观测到的**东西，不联网、不花额度、不猜。探测不出来就
什么都不说 —— 说错比不说更坏。
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path
from typing import List

HOME = Path(os.path.expanduser("~"))

AGY_LOG_DIR = Path(".gemini") / "antigravity-cli" / "log"
LOG_SCAN = 8            # 只翻最近这几份日志，够用且不至于翻半天

# agy 内置的 browser 工具装不上驱动时，Go 那边打出来的原话。
# 故意不锚 browser_context.go 的行号 —— agy 一升级行号就变（quota.py 踩过同样的坑）。
_PW_FAIL = re.compile(r"failed to install playwright: (.+)$")

# 上游 issue。这两条是实际查证过的，不是编的：agy 把 playwright-go 钉在 driver
# 1.57.0，而微软已经不在 /builds/driver/ 这个路径上托管 driver zip 了，新旧 CDN
# 全 404，Linux 上一样。
_AGY_BROWSER_ISSUES = (
    "https://github.com/google-antigravity/antigravity-cli/issues/638",
    "https://github.com/google-antigravity/antigravity-cli/issues/629",
)

_PW_CACHES = (
    Path(os.environ.get("LOCALAPPDATA") or (HOME / "AppData" / "Local")) / "ms-playwright",
    HOME / "Library" / "Caches" / "ms-playwright",
    HOME / ".cache" / "ms-playwright",
)


def _newest_first(paths: List[Path]) -> List[Path]:
    def key(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0
    return sorted(paths, key=key, reverse=True)


def agy_browser_broken(home: Path = HOME) -> str:
    """agy 自带的 browser 工具在这台机器上是不是装不上驱动。

    只在**日志里真的出现过这个错**的时候才返回原因 —— 也就是说这是观测，不是预测。
    没跑过 browser 工具的机器上返回 ""，我们就不提这茬。
    """
    try:
        logs = _newest_first(list((home / AGY_LOG_DIR).glob("cli-*.log")))
    except OSError:
        return ""
    for path in logs[:LOG_SCAN]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            m = _PW_FAIL.search(line)
            if m:
                return m.group(1).strip()
    return ""


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


def notes(home: Path = HOME) -> List[str]:
    """返回若干条环境事实。每条都短，都是这台机器上观测到的。"""
    out: List[str] = []

    reason = agy_browser_broken(home)
    if reason:
        out.append(
            "**agy 自带的 browser 工具在这台机器上用不了**，别在它身上浪费时间。\n"
            "  日志原话：failed to install playwright: {0}\n"
            "  这是 agy 的上游 bug（把 playwright-go 钉在了一个微软已经不再托管的 "
            "driver 版本上），不是你的问题，也没法靠重试绕过：\n"
            "    {1}\n"
            "  **但这不等于「不能用浏览器取证」** —— 坏的只是 agy 内置的那个 Go 驱动。"
            "npm 上的 playwright 包是另一套东西，正常可用，你自己装就行。".format(
                reason, "\n    ".join(_AGY_BROWSER_ISSUES)))

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


def block(home: Path = HOME) -> str:
    """拼成能直接塞进提示词的一段。没探到任何东西就返回空串。"""
    items = notes(home)
    if not items:
        return ""
    body = "\n".join("- " + n for n in items)
    return ("【这台机器的情况（hypoloop 自动探测，不是针对本项目的建议）】\n"
            + body +
            "\n以上只是环境事实。**这个项目该怎么取证、该改什么，一个字都没告诉你，"
            "那是你自己的判断。**")
