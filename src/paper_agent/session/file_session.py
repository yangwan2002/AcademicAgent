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
from paper_agent.session.docx_format_probe import is_docx
from paper_agent.session.docx_document import apply_text, project_text


@dataclass(frozen=True)
class SessionBaseline:
    """计划级可回滚基线：git commit（优先）或文件/二进制快照副本。

    定义在 session 层（可回滚点是会话能力）；`tasking.contracts` 从此 re-export，
    保持依赖方向 tasking → session。
    """

    baseline_id: str
    git_commit: str | None = None
    fallback_snapshot_path: str | None = None
    # 正文之外、须与正文一起回滚的状态（当前用于账本数字别名快照）。
    # 计划级回滚同样不能只回正文：顺序编码把「正文 `[n]` 指哪条」记在别名里。
    extra_state: object = None


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
    # 非空表示「本会话源文件是 docx」。working copy **本身就是 docx 副本**：
    # 排版直接改它，正文按段落就地写回，二者不互斥。
    # （早前版本把 working 变成 markdown、保存时用 pandoc 重建，导致 run 级排版
    #   被静默丢弃 —— 纯排版请求会「报告成功但结果错误」。已弃用该路径。）
    docx_source: str = field(default="", init=False)
    # 载入时把源文档里已有的 REF 交叉引用域摊平成纯文本后的报告（见 __post_init__）。
    # 非 None 且 flattened>0 表示「用户提交的就是交叉引用版」—— 导出时要升回域。
    crossref_import: object = field(default=None, init=False)
    # `:save` 是否把 docx 存成交叉引用域版（默认是 —— 用户要的是能自动更新编号的
    # 成品；working copy 仍保持纯文本，升域只作用于写出的那一份）。
    save_crossrefs: bool = True
    # 升域时 `[n]` 连方括号一起上标（GB/T 7714 顺序编码制的常见排法）。
    crossref_superscript: bool = True
    # 最近一次 `:save` 的升域报告与错误（由 `_write_crossref_copy` 填，上层负责提示）。
    last_save_crossref: object = field(default=None, init=False)
    last_save_crossref_error: str = field(default="", init=False)
    _snapshots: list[str] = field(default_factory=list, init=False)
    # 与每次 write_working 一一对应的「正文之外的待回退状态」（见 write_working）。
    # git 可用时 _snapshots 为空，这个栈仍照常压 —— 两条 undo 路径都要回放它。
    _undo_hooks: list[object] = field(default_factory=list, init=False)
    _pending_undo_state: object = field(default=None, init=False)
    _crossref_error: str = field(default="", init=False)
    _git_ok: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not os.path.isfile(self.source_path):
            raise FileNotFoundError(f"源文件不存在：{self.source_path}")
        raw_ext = os.path.splitext(self.source_path)[1].lstrip(".").lower()
        self.session_dir = os.path.join(self.workspace_root, self.session_id)
        os.makedirs(self.session_dir, exist_ok=True)

        # docx：working copy 就是 docx 副本本身，全程不离开二进制。
        # 意图层看到的是它的**只读文本投影**（read_working），写回时按段落 diff
        # 就地落盘（write_working）—— 排版因此一点不动。详见 docx_document 模块文档。
        self.docx_source = ""
        self.ext = raw_ext
        if is_docx(raw_ext):
            self.docx_source = self.source_path
        self.working_path = os.path.join(self.session_dir, f"current.{self.ext}")
        shutil.copyfile(self.source_path, self.working_path)  # 源自此不再被触碰
        if self.docx_source:
            self._flatten_crossrefs()
        if self.use_git and _git_available():
            self._git_ok = self._git_init()

    def _flatten_crossrefs(self) -> None:
        """把 working copy 里已有的 REF 域摊平成纯文本（只动副本，源文件不碰）。

        用户提交的 docx 引用若已是交叉引用域，投影层看不见域内文字，后果是**静默
        数据损坏**：模型看到空的 `[]` 会自己补引用，写回时域元素还会漂移到段尾
        （实测 `[1]...[2]` → `[]...[12]`）。摊平后整条流水线按纯文本正常工作，
        `:export` 时再升回域并复用原书签。

        摊平失败不阻塞会话 —— 退化成「和以前一样」，但报告里留下警告。
        """
        try:
            from paper_agent.tools.docx_crossref import import_crossrefs
            self.crossref_import = import_crossrefs(self.working_path)
        except Exception as exc:  # noqa: BLE001 —— 载入不能因此失败
            self.crossref_import = None
            self._crossref_error = f"{type(exc).__name__}: {exc}"

    # ---- working 读写 ----
    def read_working(self) -> str:
        """返回 working 的文本。

        docx 会话返回**投影**（一段一行，标题带 `#` 前缀），使意图层与章节索引
        无需改动即可工作；docx 二进制本身仍是真相。
        """
        if self.docx_source:
            return project_text(self.working_path)
        with open(self.working_path, "r", encoding="utf-8") as fh:
            return fh.read()

    def write_working(self, text: str, message: str = "edit", *, bus=None,
                      undo_hook=None) -> None:
        """写入 working（原子）；git 可用则 commit，否则压入快照栈。

        可选 `bus`（按方法传、不长存）：写 working 后发 `WORKING_COPY_UPDATE`，
        git 快照后发 `WORKSPACE_COMMIT`。不传时行为与既有一致。

        可选 `undo_hook`：无参可调用，返回值在本次写入的**同一时刻**入栈，`undo()`
        成功后回放。给顺序编码用 —— 它把「正文 `[n]` 指哪条」记在账本别名里，正文
        回退而别名不回退，旧号就解析到别的文献。挂在写入这一步而不是由调用方另存一
        个栈：另存的栈会在「有轮次不写正文」时与正文快照错位，回放就串档了。
        """
        # 与正文快照**同一时刻**取，保证 undo 时两者成对回放。
        self._undo_hooks.append(undo_hook() if undo_hook is not None else None)
        if self.docx_source:
            # docx：无 git 时的快照必须存**二进制**副本，不能存投影文本 ——
            # 存文本的话 undo 会把排版一并丢掉（投影里没有排版信息）。
            if not self._git_ok:
                self._snapshots.append(self._binary_snapshot())
            apply_text(self.working_path, text)
        else:
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

    def _binary_snapshot(self) -> str:
        """把当前 working docx 复制到会话目录，返回快照路径（无 git 时的 undo 用）。

        放在 `snapshots/` 子目录：会话 git 仓库若可用不会走到这里，而该目录也不该
        被当成 working 的一部分。
        """
        snap_dir = os.path.join(self.session_dir, "snapshots")
        os.makedirs(snap_dir, exist_ok=True)
        path = os.path.join(snap_dir, f"{uuid.uuid4().hex[:8]}.docx")
        shutil.copyfile(self.working_path, path)
        return path

    def undo(self) -> bool:
        """回退到上一个版本。无历史返回 False。

        成功时同步弹出配对的 `undo_hook` 载荷，供 `take_undo_state()` 取用；
        失败不弹 —— 正文没退，配对状态也不能退，否则两边永久错位。
        """
        ok = self._undo_working()
        if ok and self._undo_hooks:
            self._pending_undo_state = self._undo_hooks.pop()
        return ok

    def take_undo_state(self):
        """取走上一次成功 `undo()` 配对的 hook 载荷（取后清空）。无则 None。"""
        state, self._pending_undo_state = self._pending_undo_state, None
        return state

    def _undo_working(self) -> bool:
        if self._git_ok:
            return self._git_undo()
        if not self._snapshots:
            return False
        prev = self._snapshots.pop()
        if self.docx_source:
            # docx 快照是**文件路径**（二进制），不是文本内容。
            if not os.path.isfile(prev):
                return False
            tmp = f"{self.working_path}.tmp.{uuid.uuid4().hex[:8]}"
            shutil.copyfile(prev, tmp)
            os.replace(tmp, self.working_path)   # 原子替换
            return True
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
        # git 模式下 undo 按「提交数」回退，故每个提交都要占一个 hook 位，
        # 否则这次提交会错配到上一次 write_working 的载荷上（排版提交撤销时
        # 会连带把别名回退掉）。这条路径没有正文之外的状态，占位 None。
        self._undo_hooks.append(None)
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
    def create_baseline(self, label: str = "plan",
                        *, extra_state=None) -> "SessionBaseline":
        """建立当前 working copy 的可回滚基线。

        git 可用 → 记录当前 HEAD commit（回滚走 reset --hard）；
        否则 → 把 working copy 快照到 `<workspace_root>/.baselines/<sid>/<bid>/`
        （在 session git 仓库**之外**，不会被 `git add -A` 误提交）。二进制安全。

        `extra_state` 随基线一起保存，`restore_baseline` 成功后由调用方经
        `take_undo_state()` 取回回放 —— 正文之外的状态（账本数字别名）也要跟着回滚。
        """
        baseline_id = f"{label}_{uuid.uuid4().hex[:8]}"
        if self._git_ok:
            head = self._git_head()
            if head:
                return SessionBaseline(baseline_id=baseline_id, git_commit=head,
                                       extra_state=extra_state)
        # 降级：文件/二进制快照副本。
        snap_dir = os.path.join(
            self.workspace_root, ".baselines", self.session_id, baseline_id
        )
        os.makedirs(snap_dir, exist_ok=True)
        snap_path = os.path.join(snap_dir, os.path.basename(self.working_path))
        shutil.copyfile(self.working_path, snap_path)
        return SessionBaseline(
            baseline_id=baseline_id, fallback_snapshot_path=snap_path,
            extra_state=extra_state,
        )

    def restore_baseline(self, baseline: "SessionBaseline") -> bool:
        """回滚 working copy 到基线；只影响 working copy，**不动源文件**。

        成功时把 `baseline.extra_state` 交给 `take_undo_state()`（与 `undo()` 同口径）；
        失败不交 —— 正文没回滚，配对状态也不能回滚。
        """
        ok = self._restore_working(baseline)
        if ok and baseline.extra_state is not None:
            self._pending_undo_state = baseline.extra_state
        return ok

    def _restore_working(self, baseline: "SessionBaseline") -> bool:
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
    def save_to_source(self, *, bus=None, crossrefs: bool = False) -> str:
        """把 working 原子写回源文件（二进制安全）；返回实际写入的路径。

        docx 与其他格式走**同一条路**：working 本身就是同格式副本，直接拷回即可，
        不再有格式转换、也就没有保真损失。

        `crossrefs=True`（docx 专用）先把正文 `[n]` 升成 REF 交叉引用域再写回，
        于是保存产物**本身**就是交叉引用版，不必额外记一条 `:export`。

        以前这条路只能存纯文本，因为带域的 docx 重新载入会让投影层看不见引用。
        `import_crossrefs` 消除了那个前提（载入即摊平），所以限制不再成立。
        working copy 仍保持纯文本 —— 升域只发生在写出的那一份上。
        """
        # 先拷到源文件同目录的 tmp 再 os.replace，保证原子且不破坏二进制。
        dest = self.source_path
        tmp = f"{dest}.tmp.{uuid.uuid4().hex[:8]}"
        if crossrefs and self.docx_source:
            self._write_crossref_copy(tmp)
        else:
            shutil.copyfile(self.working_path, tmp)
        os.replace(tmp, dest)
        if bus is not None:
            bus.publish(
                EventKind.SOURCE_SAVE,
                path=os.path.basename(dest),
                status="saved",
            )
        return dest

    def _write_crossref_copy(self, dest_tmp: str) -> None:
        """把 working 的交叉引用版写到 `dest_tmp`。转换失败则退回纯文本副本。

        保存**不能因为升域失败而丢内容** —— 那是用户的正文。转换出错时照常写纯文本，
        并把原因记在 `last_save_crossref_error` 里由上层提示。
        """
        from paper_agent.tools.docx_crossref import export_crossrefs

        self.last_save_crossref = None
        self.last_save_crossref_error = ""
        staging = f"{dest_tmp}.xref.docx"
        try:
            bmap = dict(getattr(self.crossref_import, "bookmark_map", {}) or {})
            rep = export_crossrefs(
                self.working_path, staging,
                superscript=self.crossref_superscript, bookmark_map=bmap,
            )
            self.last_save_crossref = rep
            shutil.copyfile(staging, dest_tmp)
        except Exception as exc:  # noqa: BLE001 —— 宁可存纯文本也不能丢正文
            self.last_save_crossref_error = f"{type(exc).__name__}: {exc}"
            shutil.copyfile(self.working_path, dest_tmp)
        finally:
            if os.path.exists(staging):
                try:
                    os.remove(staging)
                except OSError:
                    pass

    def read_source(self) -> str:
        """返回**源文件**的文本。

        docx 与 `read_working` 同口径走投影：docx 是 zip 二进制，按 UTF-8 文本
        直读必然 `UnicodeDecodeError`（真实症状：带 .docx 参数启动 chat 直接崩在
        启动期的事实提取）。
        """
        if self.docx_source:
            return project_text(self.source_path)
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
