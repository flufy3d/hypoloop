"""claude（Claude Code CLI）后端。

跟 agy 的对应关系（都是实测出来的）：

  提示词走 stdin      `claude -p`，不给 prompt 参数就从 stdin 读
  结构化输出          `--json-schema '<JSON 字面量>'`，返回的 JSON 里有一个**已经解析
                      好**的 `structured_output` 字段 —— 这一点跟 agy 一模一样。
                      **注意它要的是 schema 本身，不是 schema 的文件路径**，给路径会
                      直接报「--json-schema is not valid JSON」。
  只读                `--permission-mode plan`。实测有效：让它读文件正常，让它建文件
                      被挡下来了（它自己说「Plan Mode 禁止写入」），磁盘上确实没有。
  可写                `--permission-mode acceptEdits --dangerously-skip-permissions`
  用量                返回 JSON 里的 `usage`
  续接                `--resume <session_id>`
  额度                `GET /api/oauth/usage`，见 `probe()`

只读模式**不加** `--dangerously-skip-permissions`：加了就是 bypassPermissions，会把
plan 模式那道门拆掉。agy 那边必须加是因为它 headless 下不加连 read_file 都被自动拒绝；
claude 没有这个毛病，plan 模式下读工具本来就放行。
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import quota as q
from ..quota import SRC_PROBE, Quota
from .base import (MODE_READONLY, MODE_WRITE, Backend, Call, CliNotFound,
                   find_binary, json_tail)

HOME = Path(os.path.expanduser("~"))

_FALLBACKS = [
    HOME / ".local" / "bin" / "claude",
    HOME / "AppData" / "Local" / "Programs" / "claude" / "claude.exe",
    Path("/usr/local/bin/claude"),
]

PERMISSION_MODE = {MODE_READONLY: "plan", MODE_WRITE: "acceptEdits"}

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
HTTP_TIMEOUT_SEC = 20.0
FIVE_HOUR_MIN = 5 * 60
SEVEN_DAY_MIN = 7 * 24 * 60


def claude_binary() -> str:
    return find_binary(["claude"], "HYPOLOOP_CLAUDE", _FALLBACKS)


def build_argv(binary: str, *, model: str, mode: str,
               schema_text: Optional[str] = None,
               cwd: Optional[Path] = None,
               resume_key: Optional[str] = None) -> List[str]:
    argv = [binary, "-p", "--output-format", "json"]
    if model:
        argv += ["--model", model]
    argv += ["--permission-mode", PERMISSION_MODE.get(mode, "plan")]
    if mode == MODE_WRITE:
        argv += ["--dangerously-skip-permissions"]
    if cwd is not None:
        argv += ["--add-dir", str(cwd)]
    if schema_text:
        argv += ["--json-schema", schema_text]
    if resume_key:
        argv += ["--resume", str(resume_key)]
    return argv


def run_claude(prompt: str, *, cwd: Path, model: str, mode: str,
               schema_path: Optional[Path] = None, timeout_sec: int = 1800,
               binary: Optional[str] = None,
               resume_key: Optional[str] = None) -> Call:
    """跑一次 claude -p。**从不抛**（除了找不到 claude）。"""
    bin_path = binary or claude_binary()
    schema_text = None
    if schema_path is not None:
        try:
            schema_text = Path(schema_path).read_text(encoding="utf-8")
        except OSError as e:
            return Call(status="SPAWN_ERROR", usage={},
                        error="读不出 schema 文件 {0}：{1}".format(schema_path, e))
    argv = build_argv(bin_path, model=model, mode=mode, schema_text=schema_text,
                      cwd=Path(cwd), resume_key=resume_key)
    try:
        proc = subprocess.run(
            argv, input=prompt, cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout_sec, check=False)
    except subprocess.TimeoutExpired:
        return Call(status="TIMEOUT", usage={}, argv=argv,
                    error="claude 超过 {0}s 没返回".format(timeout_sec))
    except OSError as e:
        return Call(status="SPAWN_ERROR", usage={}, argv=argv,
                    error="{0}: {1}".format(type(e).__name__, e))

    blob = json_tail(proc.stdout)
    if blob is None:
        return Call(status="PARSE_ERROR", usage={}, argv=argv,
                    error="claude 的输出里没有 JSON",
                    stdout=(proc.stdout or "")[-4000:],
                    stderr=(proc.stderr or "")[-4000:],
                    returncode=proc.returncode)

    call = Call(blob)
    call["usage"] = _usage(blob.get("usage") or {})
    call["session_key"] = blob.get("session_id")
    call["argv"] = argv
    call["returncode"] = proc.returncode
    # claude 用 is_error/subtype 表达成败，归一成本库统一的 status。
    call["status"] = "ERROR" if (blob.get("is_error")
                                 or blob.get("subtype") != "success") else "SUCCESS"
    if call["status"] == "ERROR" and not call.get("error"):
        call["error"] = str(blob.get("result")
                            or blob.get("api_error_status") or "")[:800]
    if (proc.stderr or "").strip():
        call["stderr"] = proc.stderr[-4000:]
    return call


def _usage(usage: Dict[str, Any]) -> Dict[str, int]:
    """claude 的 usage → 账本认识的字段。

    **cache_read 不进合计。** 它是另一个计费档（约为新输入的十分之一），长会话里会
    压倒性地大，混进 total 会让「这次花了多少」完全失真 —— taiji 在这上面栽过。
    """
    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    details = usage.get("output_tokens_details") or {}
    return {
        "input_tokens": inp + int(usage.get("cache_creation_input_tokens") or 0),
        "output_tokens": out,
        "thinking_tokens": int(details.get("thinking_tokens") or 0),
        "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
        "total_tokens": inp + int(usage.get("cache_creation_input_tokens") or 0) + out,
    }


# ---------------------------------------------------------------- 额度探针

def oauth_token(home: Path = HOME) -> str:
    """Claude Code 自己维护的 OAuth 令牌。

    **只读**：不落库、不进日志、不回写这个文件，过期了也**不去刷新** —— Anthropic 的
    refresh token 会轮换，我们抢着刷会把 Claude Code 自己那份挤掉。人只要还在用
    Claude Code，它每次启动都会续，我们蹭到的就是新鲜的；真过期就如实说读不到。
    """
    try:
        blob = json.loads((home / ".claude" / ".credentials.json").read_text(
            encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    oauth = blob.get("claudeAiOauth") or blob
    return str(oauth.get("accessToken") or "")


def _http_json(url: str, headers: Dict[str, str]):
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8")), ""
    except urllib.error.HTTPError as e:
        return e.code, None, "HTTP {0}".format(e.code)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return 0, None, "{0}: {1}".format(type(e).__name__, e)


def probe(home: Path = HOME, model: str = "") -> Quota:
    """打 /api/oauth/usage —— 跟在 claude 里敲 `/usage` 看到的是同一个数。

    这是 Claude Code 自己在打的接口，只读账户状态，**不生成内容、不烧额度**。
    办法出自 flufy3d/taiji 的 hub/service/quota.py。**从不抛。**
    """
    try:
        return _probe(home)
    except Exception as e:      # noqa: BLE001 —— 探针绝不能拖垮主流程
        return q.unavailable("额度探针异常（{0}: {1}）".format(type(e).__name__, e))


def _probe(home: Path) -> Quota:
    token = oauth_token(home)
    if not token:
        return q.unavailable(
            "读不到 ~/.claude/.credentials.json 里的 OAuth 令牌 —— "
            "用 API key 而不是订阅登录的话本来就没有这个文件")
    status, payload, err = _http_json(USAGE_URL, {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "anthropic-beta": "oauth-2025-04-20",
    })
    if status != 200 or not isinstance(payload, dict):
        return q.unavailable(
            "/api/oauth/usage 没给出结果（{0}）—— 令牌可能过期了，"
            "等 Claude Code 自己续上就好".format(err or status))
    return from_usage(payload)


def from_usage(payload: Dict[str, Any]) -> Quota:
    five = payload.get("five_hour") or {}
    week = payload.get("seven_day") or {}
    return q.make(
        source=SRC_PROBE,
        five_hour=q.from_used_pct("5h", five.get("utilization"),
                                  five.get("resets_at")),
        weekly=q.from_used_pct("weekly", week.get("utilization"),
                               week.get("resets_at")),
        group="Claude Code",
        note="读自 /api/oauth/usage（跟 claude 里的 /usage 同源）")


class ClaudeBackend(Backend):
    name = "claude"
    label = "Claude Code CLI"
    env_var = "HYPOLOOP_CLAUDE"
    # claude 没有「推理档位」这个旋钮，强弱就是换模型。
    default_model = "sonnet"
    default_verifier_model = "opus"

    def binary(self) -> str:
        return claude_binary()

    def version(self) -> str:
        try:
            out = subprocess.run([self.binary(), "--version"], capture_output=True,
                                 text=True, encoding="utf-8", errors="replace",
                                 timeout=30, check=False)
        except (OSError, subprocess.SubprocessError, CliNotFound):
            return ""
        return (out.stdout or "").strip()[:40]

    def run(self, prompt: str, *, cwd: Path, model: str, mode: str,
            schema_path: Optional[Path] = None, timeout_sec: int = 1800,
            resume_key: Optional[str] = None) -> Call:
        return run_claude(prompt, cwd=cwd, model=model, mode=mode,
                          schema_path=schema_path, timeout_sec=timeout_sec,
                          resume_key=resume_key)

    def quota(self, model: str = "") -> Quota:
        return probe()
