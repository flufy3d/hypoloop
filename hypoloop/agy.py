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
from typing import Any, Dict, List, Optional

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
    def total_tokens(self) -> int:
        return int((self.get("usage") or {}).get("total_tokens") or 0)


def build_argv(
    binary: str,
    *,
    model: str,
    mode: str,
    schema_path: Optional[Path] = None,
    allow_edits: bool = False,
    timeout_sec: int = 1800,
    extra_dirs: Optional[List[str]] = None,
) -> List[str]:
    argv = [
        binary,
        "--model", model,
        "--mode", mode,
        "--output-format", "json",
        "--print-timeout", f"{int(timeout_sec)}s",
        # 提示词里的 /xxx 会被当成 slash command 展开,这里是纯数据,关掉。
        "--disable-slash-commands",
    ]
    if schema_path is not None:
        argv += ["--json-schema", str(schema_path)]
    if allow_edits:
        argv += ["--dangerously-skip-permissions"]
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
    allow_edits: bool = False,
    timeout_sec: int = 1800,
    binary: Optional[str] = None,
) -> AgyCall:
    """跑一次 agy,返回解析后的结果。**从不抛**(除了找不到 agy)。"""
    bin_path = binary or agy_binary()
    argv = build_argv(
        bin_path, model=model, mode=mode, schema_path=schema_path,
        allow_edits=allow_edits, timeout_sec=timeout_sec,
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
