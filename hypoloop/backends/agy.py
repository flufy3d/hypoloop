"""agy（Antigravity CLI，Gemini）后端。

**提示词一律走 stdin，不走 argv。** agy 的 help 里写着「Prompts are read only from
-p/--print, -i/--prompt-interactive, or stdin」，而 Windows 的命令行总长上限是 32767
字符 —— 把带着代码上下文的提示词塞进 argv 迟早会在某个大项目上炸，而且炸出来的错误
（参数过长）跟提示词毫无关系，极难定位。stdin 没有这个限制。

结构化输出用 `--json-schema`：agy 会在返回的 JSON 里多给一个**已经解析好**的
`structured_output` 字段，不用再去从 `response` 的自由文本里抠 JSON。

额度：agy **没有**任何 quota/usage 子命令，`~/.gemini` 下也没有额度文件。真额度只有
一条路能拿到 —— `-v=3` 是 glog 的 verbosity 标志（`agy --help` 里没列），打开后 agy 会
把完整的 HTTP 响应体写进 ~/.gemini/antigravity-cli/log/cli-*.log。跑一次
`agy -v=3 models`（只列模型、不生成内容 ⇒ **不烧额度**）日志里就有
v1internal:retrieveUserQuotaSummary 的响应体。办法的出处是 flufy3d/taiji 的
hub/service/quota.py，这里按 agy 1.1.25 重新实测过。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import quota as q
from ..quota import SRC_PROBE, Quota
from .base import (MODE_READONLY as ABSTRACT_READONLY, MODE_WRITE as
                   ABSTRACT_WRITE, Backend, Call, CliNotFound, find_binary,
                   json_tail)

HOME = Path(os.path.expanduser("~"))

# agy 装完默认**不在 PATH 上**（得手动跑 `agy install`），所以自己找一遍。
_AGY_FALLBACKS = [
    HOME / "AppData" / "Local" / "agy" / "bin" / "agy.exe",
    HOME / ".local" / "bin" / "agy",
    Path("/usr/local/bin/agy"),
]

# agy 自己的模式名。抽象层用的是 "readonly"/"write"，这里做映射。
MODE_READONLY = "plan"
MODE_WRITE = "accept-edits"
_MODE_MAP = {ABSTRACT_READONLY: MODE_READONLY, ABSTRACT_WRITE: MODE_WRITE}


class AgyNotFound(CliNotFound):
    pass


#: 旧名字。Call 才是本体，这个留着是为了让「agy 的返回」在本模块里读起来顺。
AgyCall = Call


def agy_binary() -> str:
    """找到 agy 可执行文件。HYPOLOOP_AGY 环境变量优先。"""
    try:
        return find_binary(["agy"], "HYPOLOOP_AGY", _AGY_FALLBACKS)
    except CliNotFound as e:
        raise AgyNotFound(
            "{0}\n（装了的话跑一次 `agy install` 把它加进 PATH。）".format(e))


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
    print 模式下 agy 没法弹权限确认框，任何需要授权的工具（包括 `read_file`）会被
    **自动拒绝**，agent 直接交白卷：「a tool required the "read_file" permission
    that headless mode cannot prompt for, so it was auto-denied」。所以「只读」不能
    靠不给这个标志来实现 —— 那样只读角色连代码都读不了。真正管住写的是
    `--mode plan` 加上 guard.py 的工作区指纹。

    **不要加 `--disable-slash-commands`。** 它会连带把 `--mode` 废掉，agy 会警告
    「--mode plan has no effect while slash command expansion is disabled」——
    于是只读角色悄悄变成了可写角色，而命令行看上去一切正常。
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
) -> Call:
    """跑一次 agy，返回解析后的结果。**从不抛**（除了找不到 agy）。

    给 `conversation_id` 就是续接一段已有的会话。
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
            # 子进程超时比 agy 自己的 --print-timeout 多留 60s，让 agy 有机会
            # 自己优雅收尾并把 usage 打出来 —— 我们杀了它就什么账都没有了。
            timeout=timeout_sec + 60,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Call(status="TIMEOUT", error="agy 超过 {0}s 没返回".format(
            timeout_sec + 60), usage={}, argv=argv)
    except OSError as e:
        return Call(status="SPAWN_ERROR", error="{0}: {1}".format(
            type(e).__name__, e), usage={}, argv=argv)

    parsed = parse_output(proc.stdout)
    if parsed is None:
        return Call(
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
    return parsed


def parse_output(stdout: str) -> Optional[Call]:
    """从 agy 的 stdout 里取出那个 JSON。"""
    blob = json_tail(stdout)
    return None if blob is None else Call(blob)


# ---------------------------------------------------------------- 额度探针

AGY_LOG_DIR = Path(".gemini") / "antigravity-cli" / "log"
QUOTA_MARK = "v1internal:retrieveUserQuotaSummary"
TIER_MARK = "v1internal:loadCodeAssist"

# 行尾那个冒号是实测出来的，不是装饰：
#   http_helpers.go:324] Response from https://…/v1internal:retrieveUserQuotaSummary:
# 而且必须是 "Response from" 而不是 "Response Headers from"（那后面跟的是响应头）。
# 故意不锚 go 文件的行号 —— agy 一升级行号就变。
_RESP_LINE = re.compile(r"Response from \S*?(v1internal:[A-Za-z]+):?\s*$")

# 一次 agy 调用会写**多份**日志（主进程 + 干活的子进程），HTTP 体只落在其中一份里，
# 而且不保证是 mtime 最新的那份。所以要扫最近几份，别只看最新那一个。
LOG_SCAN = 4
PROBE_TIMEOUT_SEC = 90.0


def _newest_first(paths: List[Path]) -> List[Path]:
    files = [p for p in paths if p.is_file()]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def _brace_delta(line: str) -> int:
    """这一行让花括号深度变化多少（字符串里的括号不算）。"""
    depth, in_str, escaped = 0, False, False
    for ch in line:
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == '"':
            in_str = not in_str
        elif not in_str:
            depth += (ch == "{") - (ch == "}")
    return depth


def log_bodies(text: str) -> Dict[str, Any]:
    """把日志里所有「Response from …v1internal:X」后面的 JSON 体抠出来。

    这份日志不是 jsonl，是 glog 文本行里夹着 pretty-print 的多行 JSON。按
    「标记行 → 从下一行起累到花括号配平」切。同一个方法出现多次时**后来的覆盖
    先前的** —— 最新那次才是当前额度。
    """
    out: Dict[str, Any] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        hit = _RESP_LINE.search(lines[i])
        if not hit:
            i += 1
            continue
        method, buf, depth, i = hit.group(1), [], 0, i + 1
        while i < len(lines):
            if not buf and "{" not in lines[i]:
                i += 1      # 响应体前的空行：还没开始数，别让 depth 立刻归零
                continue
            buf.append(lines[i])
            depth += _brace_delta(lines[i])
            i += 1
            if depth <= 0:
                break
        try:
            out[method] = json.loads("\n".join(buf))
        except ValueError:
            pass            # 日志被轮转/截断过 —— 跳过这一段，别让整份日志报废
    return out


def pick_group(groups: List[Dict[str, Any]], model: str) -> Optional[Dict[str, Any]]:
    """按模型选对应的额度组，否则会拿 Claude 那组的数字冒充 Gemini 的。"""
    if not groups:
        return None
    family = (model or "").split("-")[0].lower()
    if family:
        for group in groups:
            blurb = "{0} {1}".format(
                group.get("displayName", ""), group.get("description", "")).lower()
            if family in blurb:
                return group
    return groups[0]


def refresh_logs(home: Path = HOME, run: bool = True) -> Tuple[List[Path], float]:
    """跑一次 `agy -v=3 models`，返回（最近几份日志, 本次开跑的时间戳）。

    选 models 子命令是因为它只列模型、不生成内容 —— **这次调用不消耗额度**。
    """
    started = datetime.now(timezone.utc).timestamp()
    if run:
        try:
            subprocess.run(
                [agy_binary(), "-v=3", "models"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                stdin=subprocess.DEVNULL, timeout=PROBE_TIMEOUT_SEC, check=False,
            )
        except (OSError, subprocess.SubprocessError, CliNotFound):
            pass    # 跑不起来就退回读现存日志，总比什么都没有强
    logs = _newest_first(list((home / AGY_LOG_DIR).glob("cli-*.log")))
    return logs[:LOG_SCAN], started


def _bucket(buckets: Dict[str, Any], name: str) -> Dict[str, Any]:
    b = buckets.get(name) or {}
    frac = b.get("remainingFraction")
    return q.window(name, None if frac is None else 100.0 * float(frac),
                    b.get("resetTime"), b.get("description"))


def _tier(load_code_assist: Dict[str, Any]) -> Optional[str]:
    for key in ("paidTier", "currentTier"):
        tier = load_code_assist.get(key) or {}
        if not isinstance(tier, dict):
            continue
        name = tier.get("name")
        if name:
            ident = tier.get("id")
            return "{0} ({1})".format(name, ident) if ident else str(name)
    return None


def probe(home: Path = HOME, model: str = "gemini", run: bool = True) -> Quota:
    """读一次真额度。任何失败都返回 available=False + note，**不抛**。"""
    try:
        return _probe(home, model, run)
    except Exception as e:      # noqa: BLE001 —— 探针绝不能拖垮主流程
        return q.unavailable("额度探针异常（{0}: {1}）".format(type(e).__name__, e))


def _probe(home: Path, model: str, run: bool) -> Quota:
    logs, started = refresh_logs(home, run=run)
    if not logs:
        return q.unavailable("找不到 ~/{0}/cli-*.log".format(AGY_LOG_DIR.as_posix()))

    tier = None
    for log in logs:
        try:
            bodies = log_bodies(log.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        tier = tier or _tier(bodies.get(TIER_MARK) or {})
        summary = bodies.get(QUOTA_MARK)
        if not isinstance(summary, dict):
            continue

        groups = [g for g in (summary.get("groups") or []) if isinstance(g, dict)]
        group = pick_group(groups, model) or {}
        buckets = {b.get("window"): b for b in group.get("buckets", [])
                   if isinstance(b, dict)}
        # 本次调用之前就存在的日志 ⇒ 读到的是上一次留下的旧读数，得说清楚
        stale = log.stat().st_mtime < started - 5
        return q.make(
            source=SRC_PROBE,
            five_hour=_bucket(buckets, "5h"),
            weekly=_bucket(buckets, "weekly"),
            tier=tier,
            group=group.get("displayName") or "未识别",
            stale=stale,
            log=log.name,
            note="读自 {0} 的 {1}{2}".format(
                log.name, QUOTA_MARK,
                "，**是上一次调用留下的旧读数**" if stale else ""))

    return q.unavailable(
        "最近 {0} 份 agy 日志里都没有 {1} 的响应体 —— agy 换版本后 -v=3 可能不再"
        "打印 HTTP 体了".format(len(logs), QUOTA_MARK), tier=tier)


# ------------------------------------------------------------ 环境事实探测

# agy 内置的 browser 工具装不上驱动时，Go 那边打出来的原话。
# 故意不锚 browser_context.go 的行号 —— agy 一升级行号就变（额度探针踩过同样的坑）。
_PW_FAIL = re.compile(r"failed to install playwright: (.+)$")

# 上游 issue。这两条是实际查证过的，不是编的：agy 把 playwright-go 钉在 driver
# 1.57.0，而微软已经不在 /builds/driver/ 这个路径上托管 driver zip 了，新旧 CDN
# 全 404，Linux 上一样。
_BROWSER_ISSUES = (
    "https://github.com/google-antigravity/antigravity-cli/issues/638",
    "https://github.com/google-antigravity/antigravity-cli/issues/629",
)

ENV_LOG_SCAN = 8


def browser_broken(home: Path = HOME) -> str:
    """agy 自带的 browser 工具在这台机器上是不是装不上驱动。

    只在**日志里真的出现过这个错**的时候才返回原因 —— 也就是说这是观测，不是预测。
    没跑过 browser 工具的机器上返回 ""，我们就不提这茬。
    """
    try:
        logs = _newest_first(list((home / AGY_LOG_DIR).glob("cli-*.log")))
    except OSError:
        return ""
    for path in logs[:ENV_LOG_SCAN]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            m = _PW_FAIL.search(line)
            if m:
                return m.group(1).strip()
    return ""


class AgyBackend(Backend):
    name = "agy"
    label = "Antigravity CLI（Gemini）"
    env_var = "HYPOLOOP_AGY"
    default_model = "gemini-3.8-flash-medium"
    default_verifier_model = "gemini-3.8-flash-high"

    def binary(self) -> str:
        return agy_binary()

    def version(self) -> str:
        try:
            out = subprocess.run([self.binary(), "--version"], capture_output=True,
                                 text=True, encoding="utf-8", errors="replace",
                                 timeout=20, check=False)
        except (OSError, subprocess.SubprocessError, CliNotFound):
            return ""
        return (out.stdout or out.stderr).strip().splitlines()[0][:40] \
            if (out.stdout or out.stderr).strip() else ""

    def run(self, prompt: str, *, cwd: Path, model: str, mode: str,
            schema_path: Optional[Path] = None, timeout_sec: int = 1800,
            resume_key: Optional[str] = None) -> Call:
        return run_agy(prompt, cwd=cwd, model=model,
                       mode=_MODE_MAP.get(mode, mode), schema_path=schema_path,
                       timeout_sec=timeout_sec, conversation_id=resume_key)

    def quota(self, model: str = "") -> Quota:
        return probe(model=model or self.default_model)

    def env_notes(self) -> List[str]:
        reason = browser_broken()
        if not reason:
            return []
        return ["**agy 自带的 browser 工具在这台机器上用不了**，别在它身上浪费时间。\n"
                "  日志原话：failed to install playwright: {0}\n"
                "  这是 agy 的上游 bug（把 playwright-go 钉在了一个微软已经不再托管的 "
                "driver 版本上），不是你的问题，也没法靠重试绕过：\n"
                "    {1}\n"
                "  **但这不等于「不能用浏览器取证」** —— 坏的只是 agy 内置的那个 Go "
                "驱动。npm 上的 playwright 包是另一套东西，正常可用，你自己装就行。"
                .format(reason, "\n    ".join(_BROWSER_ISSUES))]
