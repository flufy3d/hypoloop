"""agy(Antigravity CLI)子进程封装。

**提示词一律走 stdin,不走 argv。** agy 的 help 里写着「Prompts are read only from
-p/--print, -i/--prompt-interactive, or stdin」,而 Windows 的命令行总长上限是 32767
字符 —— 把带着代码上下文的提示词塞进 argv 迟早会在某个大项目上炸,而且炸出来的错误
(参数过长)跟提示词毫无关系,极难定位。stdin 没有这个限制。

结构化输出用 `--json-schema`:agy 会在返回的 JSON 里多给一个**已经解析好**的
`structured_output` 字段,不用再去从 `response` 的自由文本里抠 JSON。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# agy 装完默认**不在 PATH 上**(得手动跑 `agy install`),所以自己找一遍。
_AGY_FALLBACKS = [
    Path(os.path.expanduser("~")) / "AppData" / "Local" / "agy" / "bin" / "agy.exe",
    Path(os.path.expanduser("~")) / ".local" / "bin" / "agy",
    Path("/usr/local/bin/agy"),
]

MODE_READONLY = "plan"
MODE_WRITE = "accept-edits"


class AgyNotFound(RuntimeError):
    pass


def agy_binary() -> str:
    """找到 agy 可执行文件。HYPOLOOP_AGY 环境变量优先。"""
    override = os.environ.get("HYPOLOOP_AGY")
    if override:
        if not Path(override).exists():
            raise AgyNotFound(f"HYPOLOOP_AGY 指向的文件不存在:{override}")
        return override
    found = shutil.which("agy")
    if found:
        return found
    for cand in _AGY_FALLBACKS:
        if cand.exists():
            return str(cand)
    raise AgyNotFound(
        "找不到 agy。装了的话跑一次 `agy install` 把它加进 PATH,"
        "或者设环境变量 HYPOLOOP_AGY=<agy 的完整路径>。"
    )


class AgyCall(dict):
    """一次 agy 调用的结果。字段可缺 —— 拿到什么记什么。"""

    @property
    def ok(self) -> bool:
        return bool(self.get("status") == "SUCCESS")

    @property
    def data(self) -> Optional[Dict[str, Any]]:
        out = self.get("structured_output")
        return out if isinstance(out, dict) else None

    @property
    def degraded(self) -> bool:
        """agy 报了错，但结构化产出是完整的。

        实际撞到过：agy 返回 `status: ERROR` + `"The stream was interrupted.
        Please continue the task you were working on."`，然而 `returncode` 是 0、
        `structured_output` 完整、`response` 里连收尾总结都写完了 —— 也就是说
        agent 真的把活干完了，中断的只是中途某一段流，agy 自己接上继续跑完了，
        但信封上的 status 没改回来。

        原先只看 status，于是这份完整产出被整个丢掉，还倒贴一次续接调用的额度。
        有产出就认，别信信封。
        """
        return (not self.ok) and self.data is not None

    @property
    def usable(self) -> bool:
        return self.data is not None

    @property
    def total_tokens(self) -> int:
        return int((self.get("usage") or {}).get("total_tokens") or 0)


def build_argv(
    binary: str,
    *,
    model: str,
    mode: str,
    workspace: Optional[Path] = None,
    schema_path: Optional[Path] = None,
    timeout_sec: int = 1800,
    extra_dirs: Optional[List[str]] = None,
    conversation_id: Optional[str] = None,
) -> List[str]:
    """拼 agy 的命令行。

    三个都是实测踩出来的坑，别想当然改：

    **`--add-dir <目标目录>` 是必须的。agy 不认子进程的 cwd。** 不给它，agent 会在
    自己的临时工作区 `~/.gemini/antigravity-cli/scratch` 里干活 —— 而且**它不会
    报错**：你让它建个文件，它高高兴兴回报「已创建」，路径却在 scratch 底下；你让它
    读代码，它看到的是一个空目录。整件事从头到尾 status=SUCCESS，静默地全错。

    **`--dangerously-skip-permissions` 是必须的，只读角色也一样。** 在 headless 的
    print 模式下 agy 没法弹权限确认框,任何需要授权的工具(包括 `read_file`)会被
    **自动拒绝**,agent 直接交白卷:「a tool required the "read_file" permission
    that headless mode cannot prompt for, so it was auto-denied」。所以「只读」不能
    靠不给这个标志来实现 —— 那样只读角色连代码都读不了。真正管住写的是
    `--mode plan` 加上 guard.py 的工作区指纹。

    **不要加 `--disable-slash-commands`。** 它会连带把 `--mode` 废掉,agy 会警告
    「--mode plan has no effect while slash command expansion is disabled」——
    于是只读角色悄悄变成了可写角色,而命令行看上去一切正常。
    """
    argv = [
        binary,
        "--model", model,
        "--mode", mode,
        "--output-format", "json",
        "--print-timeout", "{0}s".format(int(timeout_sec)),
        "--dangerously-skip-permissions",
    ]
    if conversation_id:
        argv += ["--conversation", conversation_id]
    if workspace is not None:
        argv += ["--add-dir", str(workspace)]
    if schema_path is not None:
        argv += ["--json-schema", str(schema_path)]
    for d in extra_dirs or []:
        argv += ["--add-dir", d]
    return argv


def run_agy(
    prompt: str,
    *,
    cwd: Path,
    model: str,
    mode: str = MODE_READONLY,
    schema_path: Optional[Path] = None,
    timeout_sec: int = 1800,
    binary: Optional[str] = None,
    conversation_id: Optional[str] = None,
) -> AgyCall:
    """跑一次 agy,返回解析后的结果。**从不抛**(除了找不到 agy)。

    给 `conversation_id` 就是续接一段已有的会话(见 `salvage`)。
    """
    bin_path = binary or agy_binary()
    argv = build_argv(
        bin_path, model=model, mode=mode, workspace=Path(cwd),
        schema_path=schema_path, timeout_sec=timeout_sec,
        conversation_id=conversation_id,
    )
    try:
        proc = subprocess.run(
            argv,
            input=prompt,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            # 子进程超时比 agy 自己的 --print-timeout 多留 60s,让 agy 有机会
            # 自己优雅收尾并把 usage 打出来 —— 我们杀了它就什么账都没有了。
            timeout=timeout_sec + 60,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return AgyCall(status="TIMEOUT", error=f"agy 超过 {timeout_sec + 60}s 没返回",
                       usage={}, argv=argv)
    except OSError as e:
        return AgyCall(status="SPAWN_ERROR", error=f"{type(e).__name__}: {e}",
                       usage={}, argv=argv)

    parsed = parse_output(proc.stdout)
    if parsed is None:
        return AgyCall(
            status="PARSE_ERROR",
            error="agy 的输出里没有 JSON",
            stdout=proc.stdout[-4000:],
            stderr=proc.stderr[-4000:],
            returncode=proc.returncode,
            usage={},
            argv=argv,
        )
    parsed.setdefault("usage", {})
    parsed["returncode"] = proc.returncode
    parsed["argv"] = argv
    if proc.stderr.strip():
        parsed["stderr"] = proc.stderr[-4000:]
    return AgyCall(parsed)


def parse_output(stdout: str) -> Optional[AgyCall]:
    """从 agy 的 stdout 里取出那个 JSON。

    `--output-format json` 名义上只吐一个 JSON 对象,但实测前面可能夹着
    "Fetching available models..." 这类进度行,所以从后往前找第一行能解析成
    对象的 —— 结果 JSON 总是最后打印的。
    """
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        blob = json.loads(text)
        if isinstance(blob, dict):
            return AgyCall(blob)
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
            return AgyCall(blob)
    return None


# 让超时的那一步不至于血本无归。agy 的 print 模式有自己的 --print-timeout,到点就
# 直接 status=ERROR / "timeout waiting for response",**它已经做完的工作全部丢弃**。
# 实测过一次:验证者跑满 45 分钟、烧掉 12.6 万 token、做了 36 次输出,最后拿到的是
# 一个空壳错误。但那段会话还在,`--conversation <id>` 能接上去。
SALVAGE_PROMPT = (
    "不要再做任何新工作,不要再改任何文件,不要再跑任何命令。"
    "就把你到目前为止**已经完成的工作**和**已经真实测到的读数**,按给定的 JSON "
    "schema 输出出来。没有测到的一律填 inconclusive,**绝对不许编造任何读数**。"
)


def salvage(call: AgyCall, *, cwd: Path, model: str, mode: str,
            schema_path: Optional[Path] = None, timeout_sec: int = 300,
            binary: Optional[str] = None,
            on_attempt: Optional[Callable[[AgyCall], None]] = None
            ) -> Optional[AgyCall]:
    """一步超时/出错后,续接它的会话,只把结果要回来。

    拿不回来就返回 None —— 抢救失败不该再制造一个新问题。

    但**失败的抢救也是真花了额度的**。只把成功的记进账本，账就对不上用户看到的
    额度跌幅。所以不管成不成，都先把这次调用交给 `on_attempt` 记一笔。
    """
    conv = call.get("conversation_id")
    if not conv:
        return None
    out = run_agy(
        SALVAGE_PROMPT, cwd=cwd, model=model, mode=mode,
        schema_path=schema_path, timeout_sec=timeout_sec, binary=binary,
        conversation_id=str(conv),
    )
    out["salvaged_from"] = conv
    if on_attempt is not None:
        on_attempt(out)
    return out if out.usable else None
