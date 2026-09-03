"""三角色循环的编排。

一轮 = 假设者（只读）→ 质疑者（只读）→ 验证者（唯一能写）。
下一轮把上一轮的**证据和裁决**喂回去，所以循环是收敛的，不是三个人各说各话。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import config, report
from .agy import MODE_READONLY, MODE_WRITE, AgyCall, run_agy
from .guard import ReadOnlyGuard, WorktreeChanged, is_git_repo, run_git
from .ledger import Ledger
from .quota import Quota, consumed, format_quota, probe
from .roles import challenger, hypothesizer, verifier


class LoopError(RuntimeError):
    pass


def _default_log(msg: str) -> None:
    print(msg, flush=True)


def _role_failed(role: str, call: AgyCall) -> str:
    bits = [call.get("error"), call.get("stderr"), call.get("response")]
    detail = next((str(b)[:800] for b in bits if b), "（没有更多信息）")
    return "{0} 这一步没成（status={1}）：{2}".format(role, call.get("status"), detail)


def _dirty(root: Path) -> str:
    code, out = run_git(root, "status", "--porcelain")
    return out.strip() if code == 0 else ""


def run(task: str, target: Path, cfg: Dict[str, Any], *,
        dry_run: bool = False, commit: bool = False, allow_dirty: bool = False,
        log: Callable[[str], None] = _default_log) -> Dict[str, Any]:
    """跑完整个循环。返回一个包含运行目录、各轮结果、账本的 dict。"""
    target = Path(target).resolve()
    if not target.is_dir():
        raise LoopError("目标目录不存在：{0}".format(target))

    git = is_git_repo(target)
    if not git and not allow_dirty:
        raise LoopError(
            "{0} 不是 git 仓库。验证者会直接改文件，没有 git 就没有后悔药 —— "
            "先 `git init` 并提交一次，或者明知故犯加 --allow-dirty。".format(target))
    if git and not allow_dirty:
        dirty = _dirty(target)
        if dirty:
            raise LoopError(
                "目标仓库有未提交的改动，先处理掉，否则分不清哪些是 hypoloop 改的：\n"
                "{0}\n（真要在脏工作区上跑就加 --allow-dirty）".format(dirty))

    run_dir = config.new_run_dir(task)
    ledger = Ledger()
    rounds: List[Dict[str, Any]] = []

    log("运行目录：{0}".format(run_dir))
    log("目标项目：{0}{1}".format(target, "" if git else "（非 git）"))
    log("")

    quota_before = probe(model=cfg["model"])
    log(format_quota(quota_before, "开跑前额度"))
    log("")

    branch = None
    if commit and git:
        branch = "hypoloop/{0}".format(run_dir.name)
        code, out = run_git(target, "checkout", "-b", branch)
        if code != 0:
            log("建分支失败，就在当前分支上提交了：{0}".format(out.strip()))
            branch = None
        else:
            log("已切到分支 {0}".format(branch))
            log("")

    total_rounds = int(cfg["rounds"])
    try:
        for i in range(1, total_rounds + 1):
            rounds.append(_one_round(
                i, total_rounds, task, target, cfg, rounds, run_dir, ledger,
                dry_run=dry_run, log=log))
            if commit and git and not dry_run:
                _commit_round(target, i, task, log)
    except WorktreeChanged as e:
        log("")
        log("!! 不变量被破坏，循环中止：\n{0}".format(e))
        rounds.append({"round": len(rounds) + 1, "error": str(e)})
    except LoopError as e:
        log("")
        log("!! {0}".format(e))
        rounds.append({"round": len(rounds) + 1, "error": str(e)})

    log("")
    quota_after = probe(model=cfg["model"])
    log(format_quota(quota_after, "跑完后额度"))
    spent = consumed(quota_before, quota_after)
    for key, label in (("five_hour", "5 小时窗口"), ("weekly", "周窗口")):
        if spent.get(key) is not None:
            log("  本次吃掉 {0} 的 {1:.3f} 个百分点".format(label, spent[key]))
    log("")
    log(ledger.render())

    git_summary = None
    if git:
        _, status = run_git(target, "status", "--porcelain")
        _, logs = run_git(target, "log", "--oneline", "-5")
        git_summary = "git status --porcelain:\n{0}\ngit log -5:\n{1}".format(
            status.strip() or "（干净）", logs.strip())

    report_path = report.write_report(
        run_dir, task=task, target=target, rounds=rounds,
        ledger_text=ledger.render(), quota_before=quota_before,
        quota_after=quota_after, consumed=spent, git_summary=git_summary)
    ledger.record(task=task, target=str(target), run_dir=str(run_dir),
                  quota_before=dict(quota_before), quota_after=dict(quota_after),
                  consumed=spent)

    log("")
    log("报告：{0}".format(report_path))
    return {
        "run_dir": run_dir, "report": report_path, "rounds": rounds,
        "ledger": ledger, "quota_before": quota_before,
        "quota_after": quota_after, "consumed": spent, "branch": branch,
    }


def _one_round(i: int, total: int, task: str, target: Path, cfg: Dict[str, Any],
               history: List[Dict[str, Any]], run_dir: Path, ledger: Ledger,
               *, dry_run: bool, log: Callable[[str], None]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"round": i}
    log("=" * 60)
    log("第 {0}/{1} 轮".format(i, total))
    log("=" * 60)

    # ---- 假设者（只读）----
    prompt = hypothesizer.build_prompt(task, target, cfg, history)
    out["hypothesize"] = _step(
        "假设者", prompt, target, cfg, run_dir, ledger, i,
        model=cfg["model"], mode=MODE_READONLY, schema_name="hypotheses",
        timeout=cfg["readonly_timeout_sec"], readonly=True,
        dry_run=dry_run, log=log)
    if dry_run:
        out["challenge"] = _step(
            "质疑者", challenger.build_prompt(task, target, cfg, {}, history),
            target, cfg, run_dir, ledger, i, model=cfg["model"],
            mode=MODE_READONLY, schema_name="critique",
            timeout=cfg["readonly_timeout_sec"], readonly=True,
            dry_run=True, log=log)
        out["verify"] = _step(
            "验证者", verifier.build_prompt(task, target, cfg, {}, {}, history),
            target, cfg, run_dir, ledger, i, model=cfg["verifier_model"],
            mode=MODE_WRITE, schema_name="verification",
            timeout=cfg["verifier_timeout_sec"], readonly=False,
            dry_run=True, log=log)
        return out

    n = len(out["hypothesize"].get("hypotheses") or [])
    log("  → 提了 {0} 条假设".format(n))
    if not n:
        raise LoopError("假设者一条假设都没提，这一轮没法继续")

    # ---- 质疑者（只读）----
    prompt = challenger.build_prompt(task, target, cfg, out["hypothesize"], history)
    out["challenge"] = _step(
        "质疑者", prompt, target, cfg, run_dir, ledger, i,
        model=cfg["model"], mode=MODE_READONLY, schema_name="critique",
        timeout=cfg["readonly_timeout_sec"], readonly=True,
        dry_run=False, log=log)
    crits = out["challenge"].get("critiques") or []
    rejected = sum(1 for c in crits if c.get("verdict") == "reject")
    revised = sum(1 for c in crits if c.get("verdict") == "revise")
    log("  → {0} 条意见（驳回 {1}、要求修正 {2}），另提 {3} 条新假设".format(
        len(crits), rejected, revised,
        len(out["challenge"].get("counter_hypotheses") or [])))

    # ---- 验证者（唯一能写）----
    prompt = verifier.build_prompt(
        task, target, cfg, out["hypothesize"], out["challenge"], history)
    out["verify"] = _step(
        "验证者", prompt, target, cfg, run_dir, ledger, i,
        model=cfg["verifier_model"], mode=MODE_WRITE, schema_name="verification",
        timeout=cfg["verifier_timeout_sec"], readonly=False,
        dry_run=False, log=log)
    ev = out["verify"].get("evidence") or []
    tally = {k: sum(1 for e in ev if e.get("verdict") == k)
             for k in ("supported", "refuted", "inconclusive")}
    log("  → 实证 {supported}、证伪 {refuted}、未决 {inconclusive}；改了 {n} 个文件"
        .format(n=len(out["verify"].get("changes") or []), **tally))
    return out


def _step(role: str, prompt: str, target: Path, cfg: Dict[str, Any],
          run_dir: Path, ledger: Ledger, round_no: int, *, model: str, mode: str,
          schema_name: str, timeout: int, readonly: bool, dry_run: bool,
          log: Callable[[str], None]) -> Dict[str, Any]:
    stem = "round{0}-{1}".format(round_no, schema_name)
    (run_dir / (stem + ".prompt.md")).write_text(prompt, encoding="utf-8")

    if dry_run:
        log("")
        log("--- {0}（{1}，{2}）的提示词 ---".format(role, model, mode))
        log(prompt)
        return {}

    log("  {0} 跑起来了（{1}，{2}）…".format(role, model, mode))
    t0 = time.time()
    guard = ReadOnlyGuard(target, role, enabled=readonly)
    with guard:
        call = run_agy(
            prompt, cwd=target, model=model, mode=mode,
            schema_path=config.schema(schema_name), timeout_sec=int(timeout))
    ledger.add(role, round_no, call)
    report.dump_json(run_dir / (stem + ".raw.json"), dict(call))

    if not call.ok or call.data is None:
        raise LoopError(_role_failed(role, call))
    log("    用时 {0:.0f}s，{1:,} tokens".format(time.time() - t0,
                                                 call.total_tokens))
    report.dump_json(run_dir / (stem + ".json"), call.data)
    return call.data


def _commit_round(target: Path, i: int, task: str,
                  log: Callable[[str], None]) -> None:
    code, out = run_git(target, "status", "--porcelain")
    if code != 0 or not out.strip():
        log("  （这一轮没有改动，不提交）")
        return
    run_git(target, "add", "-A")
    msg = "hypoloop 第 {0} 轮：{1}".format(i, task)
    code, out = run_git(target, "commit", "-m", msg)
    if code != 0:
        log("  提交失败：{0}".format(out.strip()[:400]))
    else:
        log("  已提交第 {0} 轮的改动".format(i))
