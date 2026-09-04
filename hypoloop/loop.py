"""三角色循环的编排。

一轮 = 假设者（只读）→ 质疑者（只读）→ 验证者（唯一能写）。
下一轮把上一轮的**证据和裁决**喂回去，所以循环是收敛的，不是三个人各说各话。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import audit, config, report
from .agy import MODE_READONLY, MODE_WRITE, AgyCall, run_agy, salvage
from .guard import ReadOnlyGuard, WorktreeChanged, is_git_repo, run_git
from .ledger import Ledger
from .quota import Quota, consumed, format_quota, probe
from .roles import challenger, hypothesizer, verifier


class LoopError(RuntimeError):
    pass


# 续接抢救只是把结论要回来，不该再干活，所以给个短超时。
SALVAGE_TIMEOUT_SEC = 420

# 取证过程留下的东西，绝不能跟着源码改动一起提交进目标仓库。验证者被要求自己清理，
# 但那是提示词里的一句请求；这里是机制：提交前把它们挑出来，只 add 正当的源码改动，
# 剩下的原样留在磁盘上并大声报出来，由人来处置。
JUNK_DIRS = {
    "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
    ".playwright", "playwright-report", "test-results", ".hypoloop",
    ".cache", "coverage", ".nyc_output",
}
JUNK_SUFFIXES = (".log", ".tmp", ".zip", ".7z", ".pyc")
JUNK_NAME_HINTS = ("screenshot", "shot-", "before-", "after-", "baseline-",
                   "hypoloop-", "-probe", "tmp_")


def _is_junk(rel: str) -> bool:
    parts = rel.replace("\\", "/").split("/")
    if any(p in JUNK_DIRS for p in parts):
        return True
    name = parts[-1].lower()
    if name.endswith(JUNK_SUFFIXES):
        return True
    return any(hint in name for hint in JUNK_NAME_HINTS)


def _changed_paths(root: Path) -> List[str]:
    code, out = run_git(root, "status", "--porcelain")
    if code != 0:
        return []
    paths = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        rel = line[3:].strip().strip('"')
        # 重命名是 "old -> new"，要的是新名字
        if " -> " in rel:
            rel = rel.split(" -> ", 1)[1]
        if rel:
            paths.append(rel)
    return paths


def _write_manifest(run_dir: Path, task: str, target: Path,
                    cfg: Dict[str, Any]) -> None:
    """把任务和目标记在运行目录里，`--resume` 才知道自己在续什么。"""
    try:
        (run_dir / "run.json").write_text(json.dumps(
            {"task": task, "target": str(target), "config": cfg},
            ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def load_manifest(run_dir: Path) -> Dict[str, Any]:
    try:
        return json.loads((Path(run_dir) / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


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
        resume_dir: Optional[Path] = None,
        log: Callable[[str], None] = _default_log) -> Dict[str, Any]:
    """跑完整个循环。返回一个包含运行目录、各轮结果、账本的 dict。

    `resume_dir` 指向一个已有的运行目录时，已经产出过结果的步骤直接复用，只补跑
    缺的那些 —— 验证者超时不该逼着把假设者和质疑者重跑一遍（那是几十万 token 的
    纯浪费，而且会把上一轮那份已经很扎实的质疑意见丢掉）。
    """
    resuming = resume_dir is not None
    target = Path(target).resolve()
    if not target.is_dir():
        raise LoopError("目标目录不存在：{0}".format(target))

    git = is_git_repo(target)
    if not git and not allow_dirty:
        raise LoopError(
            "{0} 不是 git 仓库。验证者会直接改文件，没有 git 就没有后悔药 —— "
            "先 `git init` 并提交一次，或者明知故犯加 --allow-dirty。".format(target))
    if git and not allow_dirty and not resuming:
        dirty = _dirty(target)
        if dirty:
            raise LoopError(
                "目标仓库有未提交的改动，先处理掉，否则分不清哪些是 hypoloop 改的：\n"
                "{0}\n（真要在脏工作区上跑就加 --allow-dirty）".format(dirty))

    run_dir = Path(resume_dir) if resuming else config.new_run_dir(task)
    if not run_dir.is_dir():
        raise LoopError("要续跑的运行目录不存在：{0}".format(run_dir))
    _write_manifest(run_dir, task, target, cfg)
    ledger = Ledger()
    rounds: List[Dict[str, Any]] = []

    log("运行目录：{0}{1}".format(run_dir, "（续跑）" if resuming else ""))
    log("目标项目：{0}{1}".format(target, "" if git else "（非 git）"))
    log("")

    quota_before = probe(model=cfg["model"])
    log(format_quota(quota_before, "开跑前额度"))
    log("")

    branch = None
    if commit and git:
        branch = "hypoloop/{0}".format(run_dir.name)
        _, cur = run_git(target, "rev-parse", "--abbrev-ref", "HEAD")
        code, out = (0, "") if cur.strip() == branch else run_git(
            target, "checkout", "-b", branch)
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
                dry_run=dry_run, resume=resuming, log=log))
            if not dry_run and _goal_met(cfg, rounds[-1], log):
                skipped = total_rounds - i
                if skipped > 0:
                    log("  结束条件已达成，跳过剩下的 {0} 轮。".format(skipped))
                break
    except WorktreeChanged as e:
        log("")
        log("!! 不变量被破坏，循环中止：\n{0}".format(e))
        rounds.append({"round": len(rounds) + 1, "error": str(e)})
    except LoopError as e:
        log("")
        log("!! {0}".format(e))
        rounds.append({"round": len(rounds) + 1, "error": str(e)})

    # 自查：有没有哪一步 agy 给了完整产出、我们却没采纳。放在提交之前 —— 万一真丢了
    # 东西，人得在那句「已提交」之前先看到，而不是事后从日志里翻。详见 audit.py。
    dropped = audit.render(run_dir) if not dry_run else ""
    if dropped:
        log("")
        log(dropped)

    # 跑几轮是这套系统内部的事，不该泄进目标仓库的历史 —— 整个 run 收一个提交。
    # 放在 except 外面：中途崩了也要把已经改出来的东西落下来，别让它悬着。
    if commit and git and not dry_run:
        log("")
        _commit_run(target, task, rounds, log)

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
               *, dry_run: bool, resume: bool = False,
               log: Callable[[str], None]) -> Dict[str, Any]:
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
        dry_run=dry_run, resume=resume, log=log)
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
        dry_run=False, resume=resume, log=log)
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
        dry_run=False, resume=resume, log=log)
    ev = out["verify"].get("evidence") or []
    tally = {k: sum(1 for e in ev if e.get("verdict") == k)
             for k in ("supported", "refuted", "inconclusive")}
    log("  → 实证 {supported}、证伪 {refuted}、未决 {inconclusive}；改了 {n} 个文件"
        .format(n=len(out["verify"].get("changes") or []), **tally))
    for line in _correction_report(out["challenge"], out["verify"]):
        log(line)
    return out


def _goal_met(cfg: Dict[str, Any], rnd: Dict[str, Any],
              log: Callable[[str], None]) -> bool:
    """`--until` 的结束条件达成了没有。

    `--rounds` 开大又提前收敛时，后面每一轮都是白烧一百多万 token；而且没东西可改时
    硬凑三条假设，会逼着它去动不该动的地方。所以给个出口。

    **这个判断的失败方向必须是「多跑一轮」，绝不能是「错误地提前停」。** 所以：
      - 没设 --until   → 不停
      - 那一轮出错了   → 不停（结论本身就不可信）
      - goal 字段缺失   → 不停（schema 里它是可选的，缺失就是没判）
      - met 不是 True  → 不停
      - evidence 空的  → 不停，并且**明确说是因为没给证据才不认**
    只有「明确 met=true 且带着证据」才收工，而且把证据打出来让人当场能质疑。
    """
    if not (cfg.get("stop_when") or "").strip():
        return False
    if rnd.get("error"):
        return False
    goal = (rnd.get("verify") or {}).get("goal")
    if not isinstance(goal, dict) or goal.get("met") is not True:
        return False
    evidence = str(goal.get("evidence") or "").strip()
    if not evidence:
        log("  验证者说结束条件达成了，但**没给证据** —— 不认，继续跑。")
        return False
    log("  → 验证者判定结束条件已达成，它给的证据：")
    for line in evidence.splitlines() or [evidence]:
        log("      " + line.strip())
    return True


def _correction_report(critique: Dict[str, Any],
                       verify: Dict[str, Any]) -> List[str]:
    """核对质疑者的订正有没有被逐条交代 —— 机械核对，不是看它自己怎么说。

    起因是一次真实事故：质疑者正确指出假设者把某个位移算成了碰撞体全宽（3.70m，
    几何错了），验证者采纳了 `revise` 这个**裁决**，却把 3.70 原样写进代码，
    留下一个必死率 9% 的窟窿，下一轮才被抓出来。裁决是结构化字段，订正当时只在
    自由文本里 —— 所以能被静默跳过。

    现在订正是一等字段，这里比对 id 覆盖：漏了就点名，rejected 的也列出来让人过目。
    只报告不中止 —— 和 audit.py 一样，是照妖镜不是熔断器。
    """
    wanted = {c["id"]: c for c in verifier.corrections(critique)}
    if not wanted:
        return []
    got = {}
    for a in (verify or {}).get("corrections_addressed") or []:
        if isinstance(a, dict) and a.get("correction_id"):
            got[str(a["correction_id"])] = a

    missed = [i for i in wanted if i not in got]
    rejected = [i for i, a in got.items()
                if a.get("action") == "rejected" and i in wanted]
    lines = ["  → 质疑者给了 {0} 条订正，交代了 {1} 条".format(
        len(wanted), len(set(got) & set(wanted)))]
    if missed:
        lines.append("    !! 有 {0} 条订正**没被交代**，这正是上次留下 9% 窟窿的"
                     "路径，自己看一眼：".format(len(missed)))
        for i in missed:
            w = wanted[i]
            lines.append("       [{0}] {1}：应为 {2}（假设者用的是 {3}）".format(
                i, w.get("what"), w.get("correct"), w.get("wrong")))
    for i in rejected:
        lines.append("    · [{0}] 验证者反驳了这条订正，理由：{1}".format(
            i, str(got[i].get("evidence") or "")[:160]))
    return lines


def _step(role: str, prompt: str, target: Path, cfg: Dict[str, Any],
          run_dir: Path, ledger: Ledger, round_no: int, *, model: str, mode: str,
          schema_name: str, timeout: int, readonly: bool, dry_run: bool,
          log: Callable[[str], None], resume: bool = False) -> Dict[str, Any]:
    stem = "round{0}-{1}".format(round_no, schema_name)
    cached = run_dir / (stem + ".json")
    if resume and cached.exists():
        try:
            data = json.loads(cached.read_text(encoding="utf-8"))
            log("  {0} 复用上次的产出（{1}）".format(role, cached.name))
            return data
        except (OSError, ValueError):
            log("  {0} 上次的产出读不出来，重跑".format(role))
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

    if call.degraded:
        # 有完整产出就认，别信信封上的 status。详见 AgyCall.degraded 的注释。
        log("    agy 报了 {0}，但结构化产出是完整的，直接用（不重跑、不续接）。".format(
            call.get("status")))
        log("      agy 的原话：{0}".format(str(call.get("error") or "").strip()[:200]))

    if not call.usable:
        # 超时/出错不等于白干。agy 到点就丢弃 agent 已完成的工作，但那段会话还在，
        # 接上去只把结果要回来 —— 实测能从一次跑满 45 分钟的超时里捞回完整结论。
        log("    这一步没拿到结果（{0}），试着续接会话把已做的工作要回来…".format(
            call.get("status")))
        with ReadOnlyGuard(target, role + "(续接)", enabled=readonly):
            rescued = salvage(call, cwd=target, model=model, mode=mode,
                              schema_path=config.schema(schema_name),
                              timeout_sec=SALVAGE_TIMEOUT_SEC,
                              on_attempt=lambda c: ledger.add(
                                  role + "(续接)", round_no, c))
        if rescued is None:
            raise LoopError(_role_failed(role, call))
        report.dump_json(run_dir / (stem + ".salvage.json"), dict(rescued))
        log("    捞回来了，多花 {0:,} tokens".format(rescued.total_tokens))
        call = rescued
    log("    用时 {0:.0f}s，{1:,} tokens".format(time.time() - t0,
                                                 call.total_tokens))
    report.dump_json(run_dir / (stem + ".json"), call.data)
    return call.data


def _round_notes(rounds: List[Dict[str, Any]]) -> List[str]:
    """把每轮的裁决压成几行，放进提交信息的正文。

    对外只该有一个提交 —— 跑了几轮是这套系统自己的事，不该泄进目标仓库的历史。
    但「哪条被实证、哪条被证伪回滚了」是有价值的，塞进 body 留个交代。
    """
    lines: List[str] = []
    for r in rounds:
        v = r.get("verify") or {}
        if not v and not r.get("error"):
            continue
        bits = []
        if v.get("kept"):
            bits.append("实证保留 " + ", ".join(map(str, v["kept"])))
        if v.get("reverted"):
            bits.append("证伪回滚 " + ", ".join(map(str, v["reverted"])))
        if r.get("error"):
            bits.append("中止：" + str(r["error"])[:120])
        head = "第 {0} 轮".format(r.get("round"))
        lines.append("{0}：{1}".format(head, "；".join(bits) if bits else "无结论"))
        if v.get("summary"):
            lines.append("  " + str(v["summary"]).strip().replace("\n", " ")[:300])
    return lines


def _commit_run(target: Path, task: str, rounds: List[Dict[str, Any]],
                log: Callable[[str], None]) -> None:
    """整个 run 收一个提交。只提交源码改动。

    取证残渣挑出来、留在磁盘上、大声报出来，不塞进历史。
    """
    paths = _changed_paths(target)
    if not paths:
        log("这次没有任何改动，不提交。")
        return
    junk = [p for p in paths if _is_junk(p)]
    good = [p for p in paths if not _is_junk(p)]

    if junk:
        log("!! 验证者在仓库里留下了取证残渣，**这些没有被提交**，还在磁盘上：")
        for p in junk[:20]:
            log("     " + p)
        if len(junk) > 20:
            log("     …另有 {0} 个".format(len(junk) - 20))
        log("   （自己看一眼要不要删。取证工具不该留在目标仓库里。）")

    if not good:
        log("除了残渣没有源码改动，不提交。")
        return

    run_git(target, "add", "--", *good)
    body = _round_notes(rounds)
    msg = task if not body else task + "\n\n" + "\n".join(body)
    code, out = run_git(target, "commit", "-m", msg)
    if code != 0:
        log("提交失败：{0}".format(out.strip()[:400]))
    else:
        log("已提交 {0} 个文件（整个 run 一个提交）。".format(len(good)))
