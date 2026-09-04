"""默认值、路径、以及每个项目的可选覆盖配置。

**目标项目里不落任何东西。** 每项目的配置按目标目录的绝对路径哈希，存在
~/.hypoloop/projects/<hash>.json —— 这样 hypoloop 用在别人的仓库上也不会往人家的
git status 里塞一个 .hypoloop.json 出来。

hypoloop 本身**零第三方依赖**，只用 Python 标准库。怎么取证是验证者自己的事，它
有完整的 shell，需要什么工具自己装 —— 这套系统不替它做主，也就不用背它的依赖。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

HOME = Path(os.path.expanduser("~"))
STATE_DIR = Path(os.environ.get("HYPOLOOP_HOME") or (HOME / ".hypoloop"))
RUNS_DIR = STATE_DIR / "runs"
PROJECTS_DIR = STATE_DIR / "projects"
LEDGER = STATE_DIR / "ledger.jsonl"

PKG_DIR = Path(__file__).resolve().parent
SCHEMA_DIR = PKG_DIR / "schemas"

#: 模型名留空 = 用当前后端自己的默认值（见 backends/<name>.py 的
#: `default_model` / `default_verifier_model`）。**不能在这里写死一个 gemini 模型名**
#: —— 那样换到 codex 或 claude 上会把一个它们不认识的模型名递过去。
DEFAULTS: Dict[str, Any] = {
    # 用哪家 CLI。空 = 自动挑本机装了的第一家，并在开跑时说清楚挑了谁。
    "backend": "",
    "model": "",
    "verifier_model": "",
    "rounds": 2,
    "hypotheses": 3,
    # 只读角色几分钟就够；验证者要真动手改代码 + 自己搭取证手段，给足时间。
    "readonly_timeout_sec": 900,
    "verifier_timeout_sec": 2700,
    # 给验证者的取证提示。空着它就自己看着办；填了就是「这个项目该这么取证」。
    "evidence_hint": "",
    # 结束条件。填了就是「达成这个就收工，剩下的轮次别跑了」。
    # 空着就老老实实跑满 rounds。判定必须由验证者拿读数给出，见 loop._goal_met。
    "stop_when": "",
}


def ensure_dirs() -> None:
    for d in (STATE_DIR, RUNS_DIR, PROJECTS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def project_key(root: Path) -> str:
    raw = str(Path(root).resolve()).lower().replace("\\", "/")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def project_config_path(root: Path) -> Path:
    return PROJECTS_DIR / "{0}.json".format(project_key(root))


def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


#: 这些键的取值只对某一家后端有意义（模型名尤其如此），所以在项目配置里按后端分开存。
PER_BACKEND_KEYS = ("model", "verifier_model")


def load(root: Path, overrides: Optional[Dict[str, Any]] = None,
         base: Optional[Dict[str, Any]] = None,
         backend: str = "") -> Dict[str, Any]:
    """默认值 ← 每项目配置 ← 该后端的每项目配置 ← 底层配置（续跑的 manifest）← 命令行。

    命令行里的 None 一律忽略。

    多出来的那一层「该后端的每项目配置」是必须的：`--model` 存下来的是
    `gemini-3.8-flash-high` 这种**只对一家成立**的名字，下次换 `--backend codex` 跑
    同一个项目时再把它递过去，就是拿 gemini 的模型名去问 codex —— 所以按后端分开存。
    """
    cfg = dict(DEFAULTS)
    path = project_config_path(root)
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            cfg = _deep_merge(cfg, saved)
            per = (saved.get("backends") or {}).get(backend) if backend else None
            if isinstance(per, dict):
                cfg = _deep_merge(cfg, per)
        except (OSError, ValueError):
            pass    # 配置坏了就用默认值跑，别因为一个配置文件把任务卡死
    if base:
        cfg = _deep_merge(cfg, base)
    clean = {k: v for k, v in (overrides or {}).items() if v is not None}
    return _deep_merge(cfg, clean)


def save_project(root: Path, patch: Dict[str, Any],
                 backend: str = "") -> Path:
    ensure_dirs()
    path = project_config_path(root)
    current: Dict[str, Any] = {}
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = {}
    patch = dict(patch)
    if backend:
        per = {k: patch.pop(k) for k in PER_BACKEND_KEYS if k in patch}
        if per:
            current = _deep_merge(current, {"backends": {backend: per}})
    current = _deep_merge(current, patch)
    current["_target"] = str(Path(root).resolve())
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def slug(text: str, limit: int = 32) -> str:
    """任务描述 → 能当目录名的短串。中文原样留着，Windows 也接受。"""
    s = re.sub(r'[\\/:*?"<>|\s]+', "-", (text or "task").strip())
    s = re.sub(r"-{2,}", "-", s).strip("-.")
    return (s[:limit] or "task")


def new_run_dir(task: str) -> Path:
    ensure_dirs()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    d = RUNS_DIR / "{0}-{1}".format(stamp, slug(task))
    d.mkdir(parents=True, exist_ok=True)
    return d


def schema(name: str) -> Path:
    return SCHEMA_DIR / "{0}.schema.json".format(name)
