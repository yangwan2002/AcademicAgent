"""FileBackedSession（A1，Week 2）。

职责：
- 绑定一个源文件；把内容复制到 working copy（``<workspace>/<sid>/current.<ext>``）。
- **源文件永不被修改**，直到用户明确 save。
- 提供 working 读写与 undo。

历史/undo 机制（Week 2 补 git）：
- git 可用时，在会话目录初始化一个仓库，每次写入自动 commit，``undo`` 走
  ``git reset``（可跨进程恢复、可追溯）。
- git 不可用（或任一 git 调用失败）时，优雅降级为内存快照栈。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field


def _git_available() -> bool:
    return shutil.which("git") is not None


@dataclass
class FileBackedSession:
    source_path: str
    workspace_root: str = ".paper_workspace"
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    use_git: bool = True

    working_path: str = field(init=False)
    session_dir: str = field(init=False)
    ext: str = field(init=False)
    _snapshots: list[str] = field(default_factory=list, init=False)
    _git_ok: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not os.path.isfile(self.source_path):
            raise FileNotFoundError(f"源文件不存在：{self.source_path}")
        self.ext = os.path.splitext(self.source_path)[1].lstrip(".").lower()
        self.session_dir = os.path.join(self.workspace_root, self.session_id)
        os.makedirs(self.session_dir, exist_ok=True)
        self.working_path = os.path.join(self.session_dir, f"current.{self.ext}")
        shutil.copyfile(self.source_path, self.working_path)  # 源自此不再被触碰
        if self.use_git and _git_available():
            self._git_ok = self._git_init()

    # ---- working 读写 ----
    def read_working(self) -> str:
        with open(self.working_path, "r", encoding="utf-8") as fh:
            return fh.read()

    def write_working(self, text: str, message: str = "edit") -> None:
        """写入 working；git 可用则 commit，否则压入快照栈。"""
        if not self._git_ok:
            self._snapshots.append(self.read_working())
        with open(self.working_path, "w", encoding="utf-8") as fh:
            fh.write(text)
        if self._git_ok:
            self._git_commit(message)

    def undo(self) -> bool:
        """回退到上一个版本。无历史返回 False。"""
        if self._git_ok:
            return self._git_undo()
        if not self._snapshots:
            return False
        prev = self._snapshots.pop()
        with open(self.working_path, "w", encoding="utf-8") as fh:
            fh.write(prev)
        return True

    # ---- 二进制/直接文件操作的 git 原语（format_docx 用）----
    @property
    def git_enabled(self) -> bool:
        return self._git_ok

    def commit(self, message: str) -> bool:
        """把当前 working（含直接对文件的改动）提交为一个版本。"""
        if not self._git_ok:
            return False
        self._git_commit(message)
        return True

    def discard_changes(self) -> bool:
        """丢弃自上次 commit 以来对 working 的所有改动，恢复到 HEAD。"""
        if not self._git_ok:
            return False
        try:
            return self._git("reset", "--hard", "HEAD").returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    # ---- 明确落盘到源文件（用户 save 时才调用）----
    def save_to_source(self) -> None:
        shutil.copyfile(self.working_path, self.source_path)

    def read_source(self) -> str:
        with open(self.source_path, "r", encoding="utf-8") as fh:
            return fh.read()

    # ---- git 内部实现（全部优雅降级）----
    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", self.session_dir, *args],
            capture_output=True, text=True, timeout=30,
        )

    def _git_init(self) -> bool:
        try:
            if self._git("init").returncode != 0:
                return False
            self._git("add", "-A")
            self._git(
                "-c", "user.email=agent@local", "-c", "user.name=paper-agent",
                "commit", "-m", "init", "--allow-empty",
            )
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    def _git_commit(self, message: str) -> None:
        try:
            self._git("add", "-A")
            self._git(
                "-c", "user.email=agent@local", "-c", "user.name=paper-agent",
                "commit", "-m", message, "--allow-empty",
            )
        except (OSError, subprocess.SubprocessError):
            pass

    def _git_undo(self) -> bool:
        try:
            # 至少要有 2 个提交才能回退
            log = self._git("rev-list", "--count", "HEAD")
            if log.returncode != 0 or int((log.stdout or "0").strip()) < 2:
                return False
            return self._git("reset", "--hard", "HEAD~1").returncode == 0
        except (OSError, subprocess.SubprocessError, ValueError):
            return False


__all__ = ["FileBackedSession"]
