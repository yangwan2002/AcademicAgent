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

from paper_agent.observability.events import EventKind


@dataclass(frozen=True)
class SessionBaseline:
    """计划级可回滚基线：git commit（优先）或文件/二进制快照副本。

    定义在 session 层（可回滚点是会话能力）；`tasking.contracts` 从此 re-export，
    保持依赖方向 tasking → session。
    """

    baseline_id: str
    git_commit: str | None = None
    fallback_snapshot_path: str | None = None


def _git_available() -> bool:
    return shutil.which("git") is not None


def _atomic_write(path: str, text: str) -> None:
    """原子写文本：tmp + os.replace，保证「全有或全无」（Property 5）。"""
    tmp = f"{path}.tmp.{uuid.uuid4().hex[:8]}"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


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

    def write_working(self, text: str, message: str = "edit", *, bus=None) -> None:
        """写入 working（原子）；git 可用则 commit，否则压入快照栈。

        可选 `bus`（按方法传、不长存）：写 working 后发 `WORKING_COPY_UPDATE`，
        git 快照后发 `WORKSPACE_COMMIT`。不传时行为与既有一致。
        """
        if not self._git_ok:
            self._snapshots.append(self.read_working())
        _atomic_write(self.working_path, text)
        if bus is not None:
            bus.publish(
                EventKind.WORKING_COPY_UPDATE,
                path=os.path.basename(self.working_path),
                status="updated",
            )
        if self._git_ok:
            self._git_commit(message)
            if bus is not None:
                bus.publish(
                    EventKind.WORKSPACE_COMMIT,
                    path=os.path.basename(self.session_dir),
                    status="committed",
                )

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

    # ---- 计划级基线（intent-planning Task 7）：可回滚点 ----
    def create_baseline(self, label: str = "plan") -> "SessionBaseline":
        """建立当前 working copy 的可回滚基线。

        git 可用 → 记录当前 HEAD commit（回滚走 reset --hard）；
        否则 → 把 working copy 快照到 `<workspace_root>/.baselines/<sid>/<bid>/`
        （在 session git 仓库**之外**，不会被 `git add -A` 误提交）。二进制安全。
        """
        baseline_id = f"{label}_{uuid.uuid4().hex[:8]}"
        if self._git_ok:
            head = self._git_head()
            if head:
                return SessionBaseline(baseline_id=baseline_id, git_commit=head)
        # 降级：文件/二进制快照副本。
        snap_dir = os.path.join(
            self.workspace_root, ".baselines", self.session_id, baseline_id
        )
        os.makedirs(snap_dir, exist_ok=True)
        snap_path = os.path.join(snap_dir, os.path.basename(self.working_path))
        shutil.copyfile(self.working_path, snap_path)
        return SessionBaseline(
            baseline_id=baseline_id, fallback_snapshot_path=snap_path
        )

    def restore_baseline(self, baseline: "SessionBaseline") -> bool:
        """回滚 working copy 到基线；只影响 working copy，**不动源文件**。"""
        if baseline.git_commit and self._git_ok:
            try:
                return self._git(
                    "reset", "--hard", baseline.git_commit
                ).returncode == 0
            except (OSError, subprocess.SubprocessError):
                return False
        if baseline.fallback_snapshot_path and os.path.isfile(
            baseline.fallback_snapshot_path
        ):
            tmp = f"{self.working_path}.tmp.{uuid.uuid4().hex[:8]}"
            shutil.copyfile(baseline.fallback_snapshot_path, tmp)
            os.replace(tmp, self.working_path)  # 原子替换
            return True
        return False

    def release_baseline(self, baseline: "SessionBaseline") -> None:
        """成功完成/用户明确保留后清理 fallback 快照，避免工作区堆积。幂等。"""
        path = baseline.fallback_snapshot_path
        if not path:
            return  # git 基线无需清理
        snap_dir = os.path.dirname(path)
        try:
            if os.path.isfile(path):
                os.remove(path)
            if os.path.isdir(snap_dir) and not os.listdir(snap_dir):
                os.rmdir(snap_dir)
        except OSError:
            pass

    def _git_head(self) -> str | None:
        try:
            res = self._git("rev-parse", "HEAD")
            if res.returncode == 0:
                return (res.stdout or "").strip() or None
        except (OSError, subprocess.SubprocessError):
            pass
        return None

    # ---- 明确落盘到源文件（用户 save 时才调用）----
    def save_to_source(self, *, bus=None) -> None:
        """把 working 原子写回源文件（二进制安全）；可选 `bus` 发 `SOURCE_SAVE`。"""
        # 先拷到源文件同目录的 tmp 再 os.replace，保证原子且不破坏二进制（docx）。
        tmp = f"{self.source_path}.tmp.{uuid.uuid4().hex[:8]}"
        shutil.copyfile(self.working_path, tmp)
        os.replace(tmp, self.source_path)
        if bus is not None:
            bus.publish(
                EventKind.SOURCE_SAVE,
                path=os.path.basename(self.source_path),
                status="saved",
            )

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
