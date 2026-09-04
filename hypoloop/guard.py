"""「只有验证者能改文件」这条不变量的**执行机制**。

提示词里写「你不许改文件」是一句请求，不是一个保证。这里给的是保证：假设者和质疑者
跑之前给工作区拍一个指纹，跑完再拍一次，不一样就中止整轮并如实报出改了什么。

双保险的另一半在调用侧：这两个角色跑在各家后端自己的只读档（agy `--mode plan`、
codex `-s read-only`、claude `--permission-mode plan`）。三家都实测挡得住写。但那是
**它们的**实现细节，会变，而且新接一家后端时谁也不知道那家的只读到底有多硬；
指纹是我们自己的，对谁都一样成立。

指纹要覆盖**内容**而不只是文件名：`git status --porcelain` 只列出未跟踪文件的路径，
一个未跟踪文件的内容被改了它一个字都不会变。所以未跟踪文件的内容也要单独哈希。
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

# 指纹要跳过的目录：它们要么是版本库自己的，要么是任何人都不该盯着的体积黑洞。
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".idea", ".vscode", "dist", "build",
    ".hypoloop",
}
MAX_WALK_FILES = 20000


class WorktreeChanged(RuntimeError):
    """只读角色动了工作区 —— 不变量被破坏，这一轮必须停。"""


def run_git(root: Path, *args: str) -> Tuple[int, str]:
    """跑一条 git，返回 (returncode, stdout)。**从不抛** —— 没装 git 也只是返回非零。"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root)] + list(args),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, timeout=120, check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "{0}: {1}".format(type(e).__name__, e)
    return proc.returncode, proc.stdout


_git = run_git


def is_git_repo(root: Path) -> bool:
    code, out = _git(root, "rev-parse", "--is-inside-work-tree")
    return code == 0 and out.strip() == "true"


def _untracked(root: Path) -> List[str]:
    code, out = _git(root, "ls-files", "--others", "--exclude-standard")
    if code != 0:
        return []
    return [line for line in out.splitlines() if line.strip()]


def _hash_untracked(root: Path, paths: List[str]) -> str:
    """未跟踪文件的内容哈希。一次子进程搞定所有文件，别一文件一进程。"""
    if not paths:
        return ""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "hash-object", "--stdin-paths"],
            input="\n".join(paths) + "\n",
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "hash-object-failed"
    return proc.stdout


def _walk_fingerprint(root: Path) -> str:
    """非 git 目录的兜底：按 相对路径 + 大小 + mtime_ns 做指纹。"""
    h = hashlib.sha256()
    count = 0
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        count += 1
        if count > MAX_WALK_FILES:
            h.update(b"__truncated__")
            break
        try:
            st = path.stat()
        except OSError:
            continue
        h.update(str(path.relative_to(root)).replace("\\", "/").encode("utf-8"))
        h.update("{0}:{1}".format(st.st_size, st.st_mtime_ns).encode("utf-8"))
    return h.hexdigest()


def fingerprint(root: Path) -> str:
    """给工作区拍一个指纹。git 仓库走 git，否则走文件遍历。"""
    root = Path(root)
    if not is_git_repo(root):
        return "walk:" + _walk_fingerprint(root)
    parts = []
    for args in (("diff", "HEAD"), ("status", "--porcelain")):
        _, out = _git(root, *args)
        parts.append(out)
    untracked = _untracked(root)
    parts.append("\n".join(untracked))
    parts.append(_hash_untracked(root, untracked))
    h = hashlib.sha256("\x00".join(parts).encode("utf-8", "replace"))
    return "git:" + h.hexdigest()


def describe_changes(root: Path) -> str:
    """出事时给人看的：到底改了什么。"""
    root = Path(root)
    if not is_git_repo(root):
        return "（非 git 目录，只知道文件的大小/时间戳变了，说不出具体是哪个）"
    _, status = _git(root, "status", "--porcelain")
    _, stat = _git(root, "diff", "--stat", "HEAD")
    body = (status.strip() + "\n" + stat.strip()).strip()
    return body or "（git 说没变，但指纹变了 —— 可能是被忽略的文件动了）"


class ReadOnlyGuard:
    """with 块里的代码不许改工作区，改了就抛 WorktreeChanged。

        with ReadOnlyGuard(root, "假设者"):
            backend.run(...)
    """

    def __init__(self, root: Path, who: str, enabled: bool = True) -> None:
        self.root = Path(root)
        self.who = who
        self.enabled = enabled
        self.before: Optional[str] = None

    def __enter__(self) -> "ReadOnlyGuard":
        if self.enabled:
            self.before = fingerprint(self.root)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if not self.enabled or exc_type is not None:
            return False        # 本来就在抛别的异常，别盖掉它
        after = fingerprint(self.root)
        if after != self.before:
            raise WorktreeChanged(
                "{0}（只读角色）动了工作区 —— 这违反了「只有验证者能改文件」。\n"
                "改动如下：\n{1}".format(self.who, describe_changes(self.root)))
        return False
