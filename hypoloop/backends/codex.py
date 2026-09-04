"""codex（OpenAI Codex CLI）后端。

跟 agy 的对应关系（都是实测出来的，不是照着文档抄的）：

  提示词走 stdin      `codex exec … -`（PROMPT 位置写 `-` 就从 stdin 读）
  结构化输出          `--output-schema <file>` + `-o <file>` 收最后一条消息，
                      那条消息就是符合 schema 的 JSON
  只读                `-s read-only` —— 这是**操作系统级的沙箱**，比 agy 的
                      `--mode plan` 硬；agent 仍然能跑 rg/cat 之类的只读命令
  可写                `-s workspace-write` + 打开网络（取证经常要 npm i / 起本地服务）
  用量                `--json` 事件流里最后一个 `turn.completed` 的 usage
  续接                `codex exec resume <thread_id> -`
  额度                app-server 的 `account/rateLimits/read`，见 `probe()`

**stdout 落盘再解析，不用 capture_output。** 因为超时的时候我们会杀掉进程，
`subprocess.run(capture_output=True)` 那条路上拿到的是空的 —— 连 `thread_id` 都没有，
于是「续接会话把已做的工作要回来」这个救命功能直接失效。落盘就还能从已经写出来的
事件里把 thread_id 抠出来。这一点 agy 那边是靠它自己的 `--print-timeout` 兜的，
codex 没有对应的东西，只能自己兜。
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import quota as q
from ..quota import SRC_PROBE, Quota
from .base import (MODE_READONLY, MODE_WRITE, Backend, Call, CliNotFound,
                   find_binary)

HOME = Path(os.path.expanduser("~"))

_FALLBACKS = [
    HOME / ".local" / "bin" / "codex",
    Path("/usr/local/bin/codex"),
]

# codex 的沙箱档位。只读那档是真沙箱，不是提示词请求。
SANDBOX = {MODE_READONLY: "read-only", MODE_WRITE: "workspace-write"}

PROBE_TIMEOUT_SEC = 30.0


def codex_binary() -> str:
    return find_binary(["codex"], "HYPOLOOP_CODEX", _FALLBACKS)


def split_model(model: str) -> Tuple[str, str]:
    """`gpt-5.6-terra:high` → (模型, 推理档位)。

    codex 的「强弱」不是靠换模型，是靠 `model_reasoning_effort`。hypoloop 的
    「假设者用中档、验证者用高档」在 agy 那边体现为 flash-medium / flash-high，
    在这里就体现为同一个模型的两个 effort，所以用冒号把它们写在一个字符串里，
    命令行上 `--model gpt-5.6-sol:xhigh` 就能直接指定。
    """
    if ":" in (model or ""):
        name, _, effort = model.partition(":")
        return name.strip(), effort.strip()
    return (model or "").strip(), ""


def build_argv(binary: str, *, model: str, mode: str,
               schema_path: Optional[Path] = None,
               last_message_path: Optional[Path] = None,
               cwd: Optional[Path] = None,
               resume_key: Optional[str] = None,
               full_access: bool = False) -> List[str]:
    name, effort = split_model(model)
    argv = [binary, "exec"]
    if resume_key:
        argv += ["resume", str(resume_key)]
    argv += ["--json", "--skip-git-repo-check"]
    if name:
        argv += ["-m", name]
    if effort:
        argv += ["-c", "model_reasoning_effort={0}".format(effort)]

    # `codex exec resume` **不收** `-s` 和 `-C`（`codex exec resume --help` 里没有
    # 这两项），沙箱档位和工作目录都从原会话继承 —— 那正是我们要的：只读角色续接
    # 出来还是只读。给了会直接报参数错误，抢救当场失败，而错误信息
    # 「to pass '-s' as a value, use '-- -s'」跟真正的原因毫无关系。
    # 实测撞过：agy 和 claude 的续接都通，唯独 codex 这条静默地废着。
    if not resume_key:
        if cwd is not None:
            argv += ["-C", str(cwd)]
        if full_access and mode == MODE_WRITE:
            # 明知故犯的档位：跟 agy 的 --dangerously-skip-permissions 等价。
            argv += ["--dangerously-bypass-approvals-and-sandbox"]
        else:
            argv += ["-s", SANDBOX.get(mode, "read-only")]
            if mode == MODE_WRITE:
                # 取证要装包、要起本地服务器。不开网络，验证者只能空手套白狼。
                argv += ["-c", "sandbox_workspace_write.network_access=true"]
    elif full_access and mode == MODE_WRITE:
        argv += ["--dangerously-bypass-approvals-and-sandbox"]
    if schema_path is not None:
        argv += ["--output-schema", str(schema_path)]
    if last_message_path is not None:
        argv += ["-o", str(last_message_path)]
    argv += ["-"]           # 提示词从 stdin 读
    return argv


def strictify(node: Any) -> Any:
    """把 hypoloop 的 schema 改写成 OpenAI 严格结构化输出认的样子。

    codex 走的是 OpenAI 的 strict schema，两条硬要求：**每个对象都得写
    `additionalProperties: false`，而且每个字段都得列进 `required`。** 不满足就是
    HTTP 400 `invalid_json_schema`，一个 token 都没花就整步失败。

    但 hypoloop 的 schema 里「可选」是有意义的，不是随手省的：

      - `verification.goal` 故意可选 —— 缺失 = **没判过结束条件**，绝不能被当成
        「判了，结果是没达成」以外的任何东西，更不能因此提前收工
      - `critique.revision` / `hypotheses.risk` 是「没有就别硬编」

    所以不能去改那三份 schema 迁就 codex —— 那等于让一家后端的序列化限制反过来
    改写整套系统的语义。这里做的是**机械改写**：把原本可选的字段一律变成
    「必填但可以是 null」，语义一一对应（null = 当初的「缺失」），而且以后加新字段
    自动跟着走，不用记得回来改这里。

    对应的读取端本来就认 null：`loop._goal_met` 拿到非 dict 一律「继续跑」。
    """
    if isinstance(node, list):
        return [strictify(x) for x in node]
    if not isinstance(node, dict):
        return node

    out = {k: strictify(v) for k, v in node.items()}
    props = out.get("properties")
    if isinstance(props, dict):
        required = set(out.get("required") or [])
        for name, sub in props.items():
            if name not in required and isinstance(sub, dict):
                props[name] = _nullable(sub)
        out["required"] = list(props)
        out["additionalProperties"] = False
    return out


def _nullable(sub: Dict[str, Any]) -> Dict[str, Any]:
    """「可选」→「必填但可以是 null」。"""
    t = sub.get("type")
    if isinstance(t, str) and t != "null":
        sub["type"] = [t, "null"]
    elif isinstance(t, list) and "null" not in t:
        sub["type"] = list(t) + ["null"]
    elif t is None:
        sub = {"anyOf": [sub, {"type": "null"}]}
    if isinstance(sub.get("enum"), list) and None not in sub["enum"]:
        sub["enum"] = list(sub["enum"]) + [None]
    return sub


def strict_schema_file(schema_path: Path, out_dir: Path) -> Optional[Path]:
    """读原 schema、改写成严格版、落到临时目录。改写不了就退回原文件。"""
    try:
        raw = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Path(schema_path)
    out = out_dir / (Path(schema_path).stem + ".strict.json")
    try:
        out.write_text(json.dumps(strictify(raw), ensure_ascii=False, indent=2),
                       encoding="utf-8")
    except OSError:
        return Path(schema_path)
    return out


def _iter_events(text: str) -> List[Dict[str, Any]]:
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            blob = json.loads(line)
        except ValueError:
            continue        # 被杀掉时最后一行可能是半截，跳过就是了
        if isinstance(blob, dict):
            out.append(blob)
    return out


def _usage(events: List[Dict[str, Any]]) -> Dict[str, int]:
    """把 turn.completed 的 usage 归一成账本认识的字段。

    codex **不给 total**，得自己加。注意 `cached_input_tokens` 是 `input_tokens`
    的子集（不是另一笔），所以合计只能是 input + output，不能三个一起加。
    """
    last: Dict[str, Any] = {}
    for ev in events:
        if ev.get("type") == "turn.completed" and isinstance(ev.get("usage"), dict):
            last = ev["usage"]
    if not last:
        return {}
    inp = int(last.get("input_tokens") or 0)
    out = int(last.get("output_tokens") or 0)
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "thinking_tokens": int(last.get("reasoning_output_tokens") or 0),
        "cache_read_tokens": int(last.get("cached_input_tokens") or 0),
        "total_tokens": inp + out,
    }


def _event_error(ev: Dict[str, Any]) -> str:
    """把 error / turn.failed 事件里那句人话挖出来。

    codex 会把上游的 HTTP 错误整个 JSON 塞进 `message` 字符串里，直接打出来是一坨
    转义。挖到最里层那句 `message` 才是能照着改的东西（比如
    「'additionalProperties' is required to be supplied and to be false」）。
    """
    raw = ev.get("message") or ev.get("error") or ev
    if isinstance(raw, dict):
        raw = raw.get("message") or json.dumps(raw, ensure_ascii=False)
    text = str(raw)
    try:
        blob = json.loads(text)
    except ValueError:
        return text[:500]
    while isinstance(blob, dict):
        inner = blob.get("error") if isinstance(blob.get("error"), dict) else None
        if inner is None:
            return str(blob.get("message") or text)[:500]
        blob = inner
    return text[:500]


def _thread_id(events: List[Dict[str, Any]]) -> Optional[str]:
    for ev in events:
        if ev.get("type") == "thread.started" and ev.get("thread_id"):
            return str(ev["thread_id"])
    return None


def _last_agent_message(events: List[Dict[str, Any]]) -> str:
    text = ""
    for ev in events:
        item = ev.get("item") or {}
        if ev.get("type") == "item.completed" and item.get("type") == "agent_message":
            text = str(item.get("text") or "")
    return text


def _structured(last_message_path: Path, events: List[Dict[str, Any]]
                ) -> Optional[Dict[str, Any]]:
    """结构化产出：先看 `-o` 落的那个文件，再退回事件流里最后一条 agent 消息。

    两条路都留着是因为超时被杀的时候 `-o` 不会落盘，但事件流里可能已经有一条完整的
    结构化消息了 —— 那份产出不该白白丢掉（`Call.degraded` 讲的就是这件事）。
    """
    for raw in (_read(last_message_path), _last_agent_message(events)):
        raw = (raw or "").strip()
        if not raw:
            continue
        if raw.startswith("```"):       # 偶尔会包一层围栏
            raw = raw.strip("`")
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        try:
            blob = json.loads(raw)
        except ValueError:
            continue
        if isinstance(blob, dict):
            return blob
    return None


def _read(path: Optional[Path]) -> str:
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def run_codex(prompt: str, *, cwd: Path, model: str, mode: str,
              schema_path: Optional[Path] = None, timeout_sec: int = 1800,
              binary: Optional[str] = None, resume_key: Optional[str] = None,
              full_access: bool = False) -> Call:
    """跑一次 codex exec。**从不抛**（除了找不到 codex）。"""
    bin_path = binary or codex_binary()
    tmp = Path(tempfile.mkdtemp(prefix="hypoloop-codex-"))
    events_path, last_path = tmp / "events.jsonl", tmp / "last.json"
    if schema_path is not None:
        schema_path = strict_schema_file(schema_path, tmp)
    argv = build_argv(bin_path, model=model, mode=mode, schema_path=schema_path,
                      last_message_path=last_path, cwd=Path(cwd),
                      resume_key=resume_key, full_access=full_access)

    status, error, returncode = "SUCCESS", None, None
    try:
        with events_path.open("w", encoding="utf-8") as fh:
            proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=fh, stderr=subprocess.PIPE,
                cwd=str(cwd), text=True, encoding="utf-8", errors="replace")
            try:
                _, stderr = proc.communicate(input=prompt, timeout=timeout_sec)
            except subprocess.TimeoutExpired:
                proc.kill()
                _, stderr = proc.communicate()
                status, error = "TIMEOUT", "codex 超过 {0}s 没返回".format(timeout_sec)
            returncode = proc.returncode
    except OSError as e:
        return Call(status="SPAWN_ERROR", error="{0}: {1}".format(
            type(e).__name__, e), usage={}, argv=argv)

    events = _iter_events(_read(events_path))
    data = _structured(last_path, events)
    # 先看事件流里怎么说，**再**退回「退出码非 0」这种废话。顺序反了的话，
    # 「退出码 1」会盖住真正的原因（实测撞过一次：真话是 schema 被 API 拒了，
    # 报出来的却是一个跟 schema 毫无关系的退出码）。
    for ev in events:
        if ev.get("type") in ("error", "turn.failed"):
            status = "ERROR"
            error = _event_error(ev) or error
    if status == "SUCCESS" and returncode not in (0, None):
        status, error = "ERROR", "codex 退出码 {0}".format(returncode)

    call = Call(status=status, usage=_usage(events), argv=argv,
                returncode=returncode, session_key=_thread_id(events))
    if data is not None:
        call["structured_output"] = data
    if error:
        call["error"] = error
    if data is None:
        # 没产出的时候才需要现场证据，有产出就别拿几十 KB 的事件流撑爆 raw.json
        call["response"] = _last_agent_message(events)[:4000]
        call["stderr"] = (stderr or "")[-4000:]
        # 出了事就把整份事件流留在磁盘上并把路径写进来 —— 那是唯一能看清
        # codex 到底怎么死的东西。成功的时候没必要留，不然跑几十次攒一堆垃圾。
        call["events"] = str(events_path)
    else:
        _rmtree(tmp)
    return call


def _rmtree(path: Path) -> None:
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


# ---------------------------------------------------------------- 额度探针

_RPC_INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"clientInfo": {"name": "hypoloop", "version": "1"}}}
_RPC_READY = {"jsonrpc": "2.0", "method": "initialized", "params": {}}
_RPC_LIMITS = {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read",
               "params": {}}


def rate_limits(binary: Optional[str] = None,
                timeout_sec: float = PROBE_TIMEOUT_SEC) -> Optional[Dict[str, Any]]:
    """跟 `codex app-server` 说三句 JSON-RPC，把额度快照要回来。

    `account/rateLimits/read` 是 app-server 协议里的**正式方法**（可以用
    `codex app-server generate-json-schema --out <dir>` 把整份协议导出来看），
    不是什么未公开的调试开关 —— 这一点比 agy 那边的 `-v=3` 稳。它只读账户状态，
    **不生成内容，所以不烧额度**。

    拿不到就返回 None。探针绝不能拖垮主流程。
    """
    bin_path = binary or codex_binary()
    try:
        proc = subprocess.Popen(
            [bin_path, "app-server"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
    except OSError:
        return None

    inbox: "queue.Queue[str]" = queue.Queue()

    def pump() -> None:
        try:
            for line in proc.stdout:        # type: ignore[union-attr]
                inbox.put(line)
        except (OSError, ValueError):
            pass
        finally:
            inbox.put("")                   # 进程没了也要叫醒等的人

    threading.Thread(target=pump, daemon=True).start()
    try:
        assert proc.stdin is not None
        for msg in (_RPC_INIT, _RPC_READY, _RPC_LIMITS):
            proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            try:
                line = inbox.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                break
            if not line:
                break
            try:
                blob = json.loads(line)
            except ValueError:
                continue
            if blob.get("id") == 2:
                return blob.get("result") if isinstance(
                    blob.get("result"), dict) else None
        return None
    except OSError:
        return None
    finally:
        try:
            proc.kill()
        except OSError:
            pass


def probe(binary: Optional[str] = None, model: str = "") -> Quota:
    """读一次真额度。**从不抛。**"""
    try:
        result = rate_limits(binary)
    except Exception as e:      # noqa: BLE001 —— 探针绝不能拖垮主流程
        return q.unavailable("额度探针异常（{0}: {1}）".format(type(e).__name__, e))
    if not result:
        return q.unavailable(
            "`codex app-server` 的 account/rateLimits/read 没给出结果 —— "
            "可能没登录（跑一次 `codex login`），也可能协议改了")
    return from_snapshot(result.get("rateLimits") or {})


def from_snapshot(snap: Dict[str, Any]) -> Quota:
    """RateLimitSnapshot → 本库的 Quota。

    codex 报的是「已用百分之几」，而且 schema 上是 int32 —— **小额消耗可能显示成 0**，
    所以打上 coarse 标记，让报告里如实说明这个精度，别让人以为「跑了两轮没花额度」。
    """
    primary = snap.get("primary") or {}
    secondary = snap.get("secondary") or {}
    if not primary and not secondary:
        return q.unavailable("codex 给的额度快照里没有 primary/secondary 窗口")
    return q.make(
        source=SRC_PROBE,
        five_hour=q.from_used_pct(
            "5h", primary.get("usedPercent"), primary.get("resetsAt"),
            _window_desc(primary)),
        weekly=q.from_used_pct(
            "weekly", secondary.get("usedPercent"), secondary.get("resetsAt"),
            _window_desc(secondary)),
        group="Codex",
        tier=snap.get("planType"),
        coarse="codex",
        note="读自 codex app-server 的 account/rateLimits/read")


def _window_desc(w: Dict[str, Any]) -> Optional[str]:
    mins = w.get("windowDurationMins")
    return None if mins is None else "{0} 分钟窗口".format(mins)


class CodexBackend(Backend):
    name = "codex"
    label = "OpenAI Codex CLI"
    env_var = "HYPOLOOP_CODEX"
    # 同一个模型的两个推理档位，对应 agy 那边的 flash-medium / flash-high。
    default_model = "gpt-5.6-terra:medium"
    default_verifier_model = "gpt-5.6-terra:high"

    def __init__(self, full_access: bool = False) -> None:
        self.full_access = full_access

    def binary(self) -> str:
        return codex_binary()

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
        return run_codex(prompt, cwd=cwd, model=model, mode=mode,
                         schema_path=schema_path, timeout_sec=timeout_sec,
                         resume_key=resume_key, full_access=self.full_access)

    def quota(self, model: str = "") -> Quota:
        return probe()
