"""hypoloop 的命令行入口。

    hypoloop "让游戏画面更好更精致一些"      # 在目标项目目录里直接跑
    hypoloop quota                            # 只看 agy 额度
    hypoloop history                          # 看历史账
"""

from __future__ import annotations

import argparse
import io
import os
import sys
from pathlib import Path

from . import __version__, config
from .agy import AgyNotFound
from .ledger import history
from .loop import LoopError, load_manifest, run
from .quota import format_quota, probe

EPILOG = """\
例子：
  cd E:/Projects/my-game
  hypoloop "让游戏画面更好更精致一些" --rounds 2 --commit
  hypoloop "找出并修掉首屏白屏的根因" --evidence-hint "用无头浏览器截图对比首屏"
  hypoloop "把必死组合修掉" --rounds 5 --until "全速度段必死率为 0 且难度常数未改动"
  hypoloop --resume ~/.hypoloop/runs/20260903-174503-xxx -   # 验证者超时后续跑
  hypoloop quota

三个角色：假设者提出可证伪的假设 → 质疑者攻击它们 → 验证者动手实测。
**只有验证者能改文件**，这一条由工作区指纹强制，不是靠提示词请求。
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
        description="假设者 / 质疑者 / 验证者 —— 三角色实证循环，跑在 agy 上。",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("task", nargs="?",
                   help="要交给这套系统的任务，一句话说清就行。"
                        "配合 --resume 时可以写 - ，表示沿用上次的任务描述。")
    p.add_argument("-C", "--target", default=".", help="目标项目目录（默认当前目录）")
    p.add_argument("--rounds", type=int, help="跑几轮（默认 2）")
    p.add_argument("--hypotheses", type=int, help="每轮提几条假设（默认 3）")
    p.add_argument("--model", help="假设者/质疑者用的模型")
    p.add_argument("--verifier-model", dest="verifier_model", help="验证者用的模型")
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


def cmd_quota() -> int:
    q = probe()
    print(format_quota(q, "agy 额度"))
    if not q.available:
        print("\n（额度是从 `agy -v=3 models` 打进 "
              "~/.gemini/antigravity-cli/log/ 的 HTTP 响应体里读的。"
              "agy 升级后这个未公开的 glog 标志可能失效。）")
        return 1
    return 0


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

    if args.task in ("quota", "history") and args.target == ".":
        return cmd_quota() if args.task == "quota" else cmd_history()
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
        "rounds": args.rounds,
        "hypotheses": args.hypotheses,
        "model": args.model,
        "verifier_model": args.verifier_model,
        "evidence_hint": args.evidence_hint,
        "stop_when": args.stop_when,
    }
    manifest_cfg = manifest.get("config") if isinstance(manifest.get("config"), dict) else None
    cfg = config.load(target, overrides, base=manifest_cfg)
    if args.save_config:
        path = config.save_project(
            target, {k: v for k, v in overrides.items() if v is not None})
        print("这个项目的默认配置已存到 {0}".format(path))

    try:
        run(task, target, cfg, dry_run=args.dry_run, commit=args.commit,
            allow_dirty=args.allow_dirty, resume_dir=resume_dir)
    except AgyNotFound as e:
        print("找不到 agy：{0}".format(e), file=sys.stderr)
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
