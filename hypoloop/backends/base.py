"""后端（CLI agent）的公共契约。

hypoloop 自己不调模型，它调**别人的 CLI agent**。哪家都行，只要能满足四件事：

  1. 非交互跑一段提示词，提示词走 stdin（不走 argv —— Windows 的命令行总长上限是
     32767 字符，带着代码上下文的提示词迟早会在某个大项目上炸，而且炸出来的错误
     「参数过长」跟提示词毫无关系，极难定位）
  2. 能按给定的 JSON Schema 吐**结构化输出** —— 三角色循环靠 schema 咬合，
     从自由文本里抠 JSON 是不可接受的
  3. 有**只读模式** —— 假设者和质疑者不许改文件
  4. 能报**本次用量**，最好还能报**剩余额度**

新增一家（比如 opencode）就是写一个 `Backend` 子类放进 `hypoloop/backends/`，
在 `backends/__init__.py` 里登记一行。**循环本身、角色提示词、guard、账本一个字都不用改。**

## 只读是怎么保证的

不是靠提示词里写「请不要改文件」，是两道机制：

  - 后端自己的只读模式（agy `--mode plan` / codex `-s read-only` /
    claude `--permission-mode plan`）
  - `guard.py` 的工作区指纹：角色跑之前算一次，跑完再算一次，不一致就中止

第二道是**兜底**，而且是可执行的 —— 就算某天某家的只读模式漏了，这条也拦得住。
所以新后端即使只读模式不够硬，接进来也是安全的。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..quota import Quota, unavailable

MODE_READONLY = "readonly"
MODE_WRITE = "write"
MODE_LABEL = {MODE_READONLY: "只读", MODE_WRITE: "可写"}


class CliNotFound(RuntimeError):
    pass


class Call(dict):
    """一次后端调用的结果。字段可缺 —— 拿到什么记什么。

    各家 CLI 的返回长得完全不一样，backend 负责把它们归一成这几个键：
    `status` / `structured_output` / `usage` / `session_key` / `response`。
    """

    @property
    def ok(self) -> bool:
        return bool(self.get("status") == "SUCCESS")

    @property
    def data(self) -> Optional[Dict[str, Any]]:
        out = self.get("structured_output")
        return out if isinstance(out, dict) else None

    @property
    def degraded(self) -> bool:
        """CLI 报了错，但结构化产出是完整的。

        实际撞到过：agy 返回 `status: ERROR` +「The stream was interrupted」，然而
        `returncode` 是 0、`structured_output` 完整、`response` 里连收尾总结都写完了
        —— agent 真把活干完了，中断的只是中途某一段流，它自己接上跑完了，但信封上的
        status 没改回来。

        原先只看 status，于是这份完整产出被整个丢掉，还倒贴一次续接调用的额度，
        提交信息里还写了一句假的「第 2 轮：中止」。**有产出就认，别信信封。**
        """
        return (not self.ok) and self.data is not None

    @property
    def usable(self) -> bool:
        return self.data is not None

    @property
    def total_tokens(self) -> int:
        return int((self.get("usage") or {}).get("total_tokens") or 0)

    @property
    def session_key(self) -> Optional[str]:
        """续接这段会话要用的 id。各家叫法不同，backend 填进来时统一成这个。"""
        for key in ("session_key", "conversation_id", "session_id", "thread_id"):
            val = self.get(key)
            if val:
                return str(val)
        return None


# 续接抢救时说的话。**不许再干活**，只把已经做完的要回来。
SALVAGE_PROMPT = (
    "不要再做任何新工作，不要再改任何文件，不要再跑任何命令。"
    "就把你到目前为止**已经完成的工作**和**已经真实测到的读数**，按给定的 JSON "
    "schema 输出出来。没有测到的一律填 inconclusive，**绝对不许编造任何读数**。"
)


def find_binary(names: List[str], env_var: str,
                fallbacks: Optional[List[Path]] = None) -> str:
    """按「环境变量 → PATH → 已知安装位置」找可执行文件。

    `shutil.which` 在 Windows 上会带上 PATHEXT，所以 npm 装出来的 `foo.cmd` 也找得到
    —— 直接把 "foo" 交给 `subprocess` 是找不到 `.cmd` 的（CreateProcess 只补 .exe），
    这个坑实测踩过。
    """
    override = os.environ.get(env_var)
    if override:
        if not Path(override).exists():
            raise CliNotFound("{0} 指向的文件不存在：{1}".format(env_var, override))
        return override
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    for cand in fallbacks or []:
        if cand.exists():
            return str(cand)
    raise CliNotFound(
        "找不到 {0}。装了的话确认它在 PATH 上，或者设环境变量 {1}=<完整路径>。".format(
            names[0], env_var))


def json_tail(stdout: str) -> Optional[Dict[str, Any]]:
    """从 stdout 里取出那个 JSON 对象。

    名义上只吐一个 JSON，但实测前面可能夹着 "Fetching available models..." 这类进度行，
    所以从后往前找第一行能解析成对象的 —— 结果 JSON 总是最后打印的。
    """
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        blob = json.loads(text)
        if isinstance(blob, dict):
            return blob
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            blob = json.loads(line)
        except ValueError:
            continue
        if isinstance(blob, dict):
            return blob
    return None


class Backend:
    """一家 CLI agent 的接入。子类至少要实现 `run` 和 `binary`。"""

    name = ""            # 命令行上写的名字，如 "agy"
    label = ""           # 给人看的名字，如 "Antigravity CLI（Gemini）"
    env_var = ""         # 覆盖可执行文件路径的环境变量
    default_model = ""           # 假设者 / 质疑者
    default_verifier_model = ""  # 验证者（要动手改代码，给强一点的）

    # ---- 能力探测 ----

    def binary(self) -> str:
        raise NotImplementedError

    def installed(self) -> bool:
        try:
            return bool(self.binary())
        except CliNotFound:
            return False
        except OSError:
            return False

    def version(self) -> str:
        """给 `hypoloop backends` 显示用。拿不到就空字符串，不要报错。"""
        return ""

    # ---- 干活 ----

    def run(self, prompt: str, *, cwd: Path, model: str, mode: str,
            schema_path: Optional[Path] = None, timeout_sec: int = 1800,
            resume_key: Optional[str] = None) -> Call:
        """跑一次。**从不抛**（除了找不到 CLI）—— 失败要变成一个带 status 的 Call。"""
        raise NotImplementedError

    def salvage(self, call: Call, *, cwd: Path, model: str, mode: str,
                schema_path: Optional[Path] = None, timeout_sec: int = 300,
                on_attempt: Optional[Callable[[Call], None]] = None
                ) -> Optional[Call]:
        """一步超时/出错后，续接它的会话，只把结果要回来。

        这不是可有可无的优化。实测过一次：验证者跑满 45 分钟、烧掉 12.6 万 token、
        做了 36 次输出，CLI 到点就把这些**全部丢弃**，只回一个空壳错误。但那段会话
        还在，接上去就能把结论捞回来。

        拿不到就返回 None —— 抢救失败不该再制造一个新问题。但**失败的抢救也是真花了
        额度的**，所以不管成不成都先把这次调用交给 `on_attempt` 记一笔，否则账对不上
        用户看到的额度跌幅。
        """
        key = call.session_key
        if not key:
            return None
        out = self.run(SALVAGE_PROMPT, cwd=cwd, model=model, mode=mode,
                       schema_path=schema_path, timeout_sec=timeout_sec,
                       resume_key=key)
        out["salvaged_from"] = key
        if on_attempt is not None:
            on_attempt(out)
        return out if out.usable else None

    # ---- 额度 ----

    def quota(self, model: str = "") -> Quota:
        """读一次剩余额度。**从不抛**，拿不到就 unavailable(原因)。"""
        return unavailable("{0} 没有实现额度探针".format(self.name))

    # ---- 环境事实 ----

    def env_notes(self) -> List[str]:
        """这台机器上、跟这个后端有关、会影响取证的事实。见 environ.py 的分层说明。"""
        return []
