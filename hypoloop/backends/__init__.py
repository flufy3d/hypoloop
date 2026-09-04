"""后端注册表。

**hypoloop 不绑定任何一家 CLI。** 本机装了哪几家就提供哪几家，一家都不强制依赖。

加一家新的（opencode、aider、随便什么）只要两步：

  1. 在本目录下写一个模块，实现 `base.Backend` 的 `binary` / `run`，
     能报额度就再实现 `quota`
  2. 在下面的 `_MODULES` 里加一行

循环、角色提示词、guard、账本、报告**一个字都不用改** —— 它们只跟 `Backend` 这个
契约打交道。契约里的四件事和为什么是这四件，见 `base.py` 的模块注释。

`_MODULES` 里的顺序就是**自动挑选的优先级**：没指定 `--backend`、项目也没存过配置
时，用装了的第一家，并且**在日志里大声说是哪一家、还有哪几家可选**。不静默挑、也不
因为装了多家就把人拦在门外 —— 前者会让人搞不清刚才那一百万 token 花在谁身上，
后者会让第一次跑的人卡住。
"""

from __future__ import annotations

import importlib
from typing import Dict, List, Optional

from .base import (MODE_LABEL, MODE_READONLY, MODE_WRITE, Backend, Call,
                   CliNotFound)

# 模块名 → 类名。顺序 = 自动挑选的优先级。
_MODULES = [
    ("agy", "AgyBackend"),
    ("codex", "CodexBackend"),
    ("claude", "ClaudeBackend"),
]

_CACHE: Dict[str, Backend] = {}


def names() -> List[str]:
    return [name for name, _ in _MODULES]


def get(name: str) -> Backend:
    """按名字拿一个后端实例。名字不认识就报错，**不猜**。"""
    key = (name or "").strip().lower()
    if key in _CACHE:
        return _CACHE[key]
    for mod_name, cls_name in _MODULES:
        if mod_name != key:
            continue
        mod = importlib.import_module("{0}.{1}".format(__name__, mod_name))
        _CACHE[key] = getattr(mod, cls_name)()
        return _CACHE[key]
    raise CliNotFound("不认识的后端「{0}」。现在支持：{1}".format(
        name, "、".join(names())))


def all_backends() -> List[Backend]:
    out = []
    for name in names():
        try:
            out.append(get(name))
        except Exception:       # noqa: BLE001 —— 一家坏了不该连累别家
            continue
    return out


def installed() -> List[Backend]:
    """这台机器上真的装了的那几家。"""
    return [b for b in all_backends() if b.installed()]


def choose(preferred: Optional[str] = None) -> Backend:
    """挑一个后端用。

    指定了就用指定的（**没装也照样返回**，让它在真正调用时报一个说得清楚的错，
    而不是在这里被悄悄换成另一家）。没指定就用装了的第一家。
    """
    if preferred:
        return get(preferred)
    found = installed()
    if not found:
        raise CliNotFound(
            "本机一家 CLI agent 都没找到。hypoloop 自己不调模型，它调别人的 CLI，"
            "所以至少得装一个：{0}。\n"
            "装好后用 `hypoloop backends` 确认能被认出来。".format("、".join(names())))
    return found[0]


__all__ = ["Backend", "Call", "CliNotFound", "MODE_READONLY", "MODE_WRITE",
           "MODE_LABEL", "names", "get", "all_backends", "installed", "choose"]
