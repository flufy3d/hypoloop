"""hypoloop 的命令行入口。

    hypoloop "让游戏画面更好更精致一些"      # 在目标项目目录里直接跑
    hypoloop backends                         # 看本机装了哪几家 CLI
    hypoloop quota                            # 看额度
    hypoloop history                          # 看历史账
"""

from __future__ import annotations

import argparse
import io
import os
import sys
from pathlib import Path
from typing import Optional

from . import __version__, backends, config
from .backends import CliNotFound
from .ledger import history
from .loop import LoopError, load_manifest, run
from .quota import format_quota

EPILOG = """\
例子：
  cd E:/Projects/my-game
  hypoloop "让游戏画面更好更精致一些" --rounds 2 --commit
  hypoloop "找出并修掉首屏白屏的根因" --evidence-hint "用无头浏览器截图对比首屏"
  hypoloop "把必死组合修掉" --rounds 5 --until "全速度段必死率为 0 且难度常数未改动"
  hypoloop "同一个任务换一家跑" --backend codex
  hypoloop --resume ~/.hypoloop/runs/20260903-174503-xxx -   # 验证者超时后续跑
  hypoloop backends
  hypoloop quota

三个角色：假设者提出可证伪的假设 → 质疑者攻击它们 → 验证者动手实测。
**只有验证者能改文件**，这一条由工作区指纹强制，不是靠提示词请求。

hypoloop 自己不调模型，它调本机装着的 CLI agent。装了哪几家用 `hypoloop backends`
看；不指定 --backend 就用装了的第一家，并在开跑时说清楚用的是谁。
"""


def _stdout_utf8() -> None:
    """Windows 控制台默认不是 UTF-8，中文和 ✅ 会直接抛 UnicodeEncodeError。"""
    for stream in ("stdout", "stderr"):
        s = getattr(sys, stream)
        if hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
        elif hasattr(s, "buffer"):
            setattr(sys, stream, io.TextIOWrapper(
                s.buffer, encoding="utf-8", errors="replace", line_buffering=True))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hypoloop",
        description="假设者 / 质疑者 / 验证者 —— 三角色实证循环，"
                    "跑在你本机装着的 CLI agent 上。",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("task", nargs="?",
                   help="要交给这套系统的任务，一句话说清就行。"
                        "配合 --resume 时可以写 - ，表示沿用上次的任务描述。")
    p.add_argument("-C", "--target", default=".", help="目标项目目录（默认当前目录）")
    p.add_argument("--backend", choices=backends.names(),
                   help="用哪家 CLI agent 跑（默认：本机装了的第一家）。"
                        "`hypoloop backends` 看本机情况。")
    p.add_argument("--rounds", type=int, help="跑几轮（默认 2）")
    p.add_argument("--hypotheses", type=int, help="每轮提几条假设（默认 3）")
    p.add_argument("--model", help="假设者/质疑者用的模型（默认用后端自己的中档）")
    p.add_argument("--verifier-model", dest="verifier_model",
                   help="验证者用的模型（默认用后端自己的高档）")
    p.add_argument("--evidence-hint", dest="evidence_hint",
                   help="给验证者的取证建议，比如「用无头浏览器截图对比」。"
                        "不填它自己看着办。")
    p.add_argument("--until", dest="stop_when", metavar="条件",
                   help="结束条件。达成了就收工，跳过剩下的轮次 —— --rounds 开大了"
                        "又提前收敛时，能省掉整轮的 token。"
                        "条件要写成**可判定**的，比如「必死率降到 0 且难度常数未改动」；"
                        "写「变好了」这种没法判的等于没写。"
                        "判定由验证者拿读数给出，判不准一律算没达成、继续跑。")
    p.add_argument("--commit", action="store_true",
                   help="整个 run 的改动收成一个提交，放在 hypoloop/<run> 分支上")
    p.add_argument("--allow-dirty", action="store_true",
                   help="目标不是 git 仓库、或工作区不干净时也硬跑")
    p.add_argument("--resume", metavar="RUN_DIR",
                   help="续跑一个已有的运行目录：已经产出过的步骤直接复用，只补跑"
                        "缺的那些。验证者超时后不用把前两个角色重跑一遍。")
    p.add_argument("--dry-run", action="store_true",
                   help="只打印将要发出去的提示词，一个 token 都不花")
    p.add_argument("--save-config", action="store_true",
                   help="把本次的模型/轮数/取证提示存成这个项目的默认值")
    p.add_argument("--version", action="version",
                   version="hypoloop {0}".format(__version__))
    return p


def cmd_backends() -> int:
    """本机装了哪几家。**一家都不强制依赖，装了哪家就能用哪家。**"""
    found = backends.installed()
    default = found[0].name if found else None
    print("{0:<8}{1:<26}{2:<10}{3}".format("名字", "是什么", "装了没", "默认模型"))
    for b in backends.all_backends():
        ok = b.installed()
        print("{0:<8}{1:<26}{2:<10}{3}".format(
            b.name + ("*" if b.name == default else ""),
            b.label, "是" if ok else "—",
            "{0} / {1}".format(b.default_model, b.default_verifier_model)))
    if not found:
        print("\n一家都没找到。hypoloop 自己不调模型，至少得装一个上面列的 CLI。")
        return 1
    print("\n带 * 的是不指定 --backend 时会用的那家。")
    return 0


def cmd_quota(name: Optional[str] = None) -> int:
    """报额度。不指定就把装了的几家全报一遍 —— 换着用的时候这个最有用。"""
    targets = [backends.get(name)] if name else backends.installed()
    if not targets:
        print("本机没找到任何 CLI agent，无从谈额度。跑 `hypoloop backends` 看看。")
        return 1
    any_ok = False
    for i, b in enumerate(targets):
        if i:
            print("")
        q = b.quota()
        any_ok = any_ok or q.available
        print(format_quota(q, "{0} 额度".format(b.name)))
    return 0 if any_ok else 1


def cmd_history() -> int:
    rows = history()
    if not rows:
        print("还没有任何运行记录（账本在 {0}）。".format(config.LEDGER))
        return 0
    print("{0:<20}{1:>10}{2:>14}  {3}".format("时间", "调用数", "tokens", "任务"))
    for r in rows:
        print("{0:<20}{1:>10}{2:>14,}  {3}".format(
            str(r.get("at", ""))[:19], r.get("calls", 0),
            (r.get("tokens") or {}).get("total_tokens", 0),
            str(r.get("task", ""))[:50]))
    total = sum((r.get("tokens") or {}).get("total_tokens", 0) for r in rows)
    print("\n最近 {0} 次合计 {1:,} tokens".format(len(rows), total))
    return 0


def main(argv=None) -> int:
    _stdout_utf8()
    args = build_parser().parse_args(argv)

    if args.task in ("quota", "history", "backends") and args.target == ".":
        if args.task == "backends":
            return cmd_backends()
        if args.task == "quota":
            return cmd_quota(args.backend)
        return cmd_history()
    if not args.task:
        build_parser().print_help()
        return 2

    resume_dir = Path(args.resume).resolve() if args.resume else None
    manifest = load_manifest(resume_dir) if resume_dir else {}
    if manifest.get("target") and args.target == ".":
        args.target = manifest["target"]
    task = args.task if args.task != "-" else manifest.get("task", args.task)

    target = Path(args.target).resolve()
    overrides = {
        "backend": args.backend,
        "rounds": args.rounds,
        "hypotheses": args.hypotheses,
        "model": args.model,
        "verifier_model": args.verifier_model,
        "evidence_hint": args.evidence_hint,
        "stop_when": args.stop_when,
    }
    manifest_cfg = manifest.get("config") if isinstance(manifest.get("config"), dict) else None
    # 后端要先定下来，项目配置里「哪家用哪个模型」那一层才知道该叠哪一段。
    try:
        backend = backends.choose(
            args.backend or (manifest_cfg or {}).get("backend")
            or config.load(target).get("backend"))
    except CliNotFound as e:
        print(e, file=sys.stderr)
        return 3
    cfg = config.load(target, overrides, base=manifest_cfg, backend=backend.name)
    if args.save_config:
        path = config.save_project(
            target, {k: v for k, v in overrides.items() if v is not None},
            backend=backend.name)
        print("这个项目的默认配置已存到 {0}".format(path))
    if not args.backend and not args.dry_run:
        others = [b.name for b in backends.installed() if b.name != backend.name]
        if others:
            print("本机还装了 {0}；这次用的是 {1}（--backend 可以换）。".format(
                "、".join(others), backend.name))

    try:
        run(task, target, cfg, backend=backend, dry_run=args.dry_run,
            commit=args.commit, allow_dirty=args.allow_dirty,
            resume_dir=resume_dir)
    except CliNotFound as e:
        print("找不到 {0}：{1}".format(backend.name, e), file=sys.stderr)
        return 3
    except LoopError as e:
        print("跑不下去了：{0}".format(e), file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print("\n中断了。已经产生的改动还在目标仓库里，自己看一眼 git status。",
              file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
