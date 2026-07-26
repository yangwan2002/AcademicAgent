"""intent-planning Task 7 验收：Session Baseline（可回滚点）。

- git / 非 git 两路径建立、恢复、释放
- 快照放 .baselines/（session git 仓库之外）→ 不被 git 提交
- restore 只恢复 working copy、源文件不受影响
- restore 后仍可继续写；release 幂等
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_agent.session.file_session import FileBackedSession, SessionBaseline
from paper_agent.tasking.contracts import SessionBaseline as ContractsBaseline


SAMPLE = "line1\nline2\n"


def _session(tmp_path, use_git):
    src = tmp_path / "p.tex"
    src.write_text(SAMPLE, encoding="utf-8")
    return src, FileBackedSession(
        str(src), workspace_root=str(tmp_path / "ws"), use_git=use_git
    )


def test_contracts_reexports_session_baseline():
    # 同一个类（下沉到 session、contracts re-export）
    assert SessionBaseline is ContractsBaseline


# --- 非 git 路径（确定性）---

def test_nongit_create_restore_release(tmp_path):
    src, session = _session(tmp_path, use_git=False)
    base = session.create_baseline("plan")
    assert base.git_commit is None
    assert base.fallback_snapshot_path and os.path.isfile(base.fallback_snapshot_path)

    session.write_working("完全改写的新内容\n")
    assert session.read_working() == "完全改写的新内容\n"

    assert session.restore_baseline(base) is True
    assert session.read_working() == SAMPLE          # 回滚成功
    assert src.read_text(encoding="utf-8") == SAMPLE  # 源文件始终未变

    # restore 后仍可继续写
    session.write_working("再次修改\n")
    assert session.read_working() == "再次修改\n"

    # release 清理快照 + 幂等
    session.release_baseline(base)
    assert not os.path.isfile(base.fallback_snapshot_path)
    session.release_baseline(base)  # 再次调用不报错


def test_snapshot_lives_outside_session_git_dir(tmp_path):
    src, session = _session(tmp_path, use_git=False)
    base = session.create_baseline("plan")
    # 快照在 .baselines 下，且不在 session_dir（git 仓库目录）内
    assert ".baselines" in base.fallback_snapshot_path
    assert not base.fallback_snapshot_path.startswith(session.session_dir)


def test_restore_only_affects_working_not_source(tmp_path):
    src, session = _session(tmp_path, use_git=False)
    base = session.create_baseline()
    session.write_working("changed\n")
    session.restore_baseline(base)
    assert session.read_working() == SAMPLE
    assert src.read_text(encoding="utf-8") == SAMPLE


# --- git 路径（存在 git 时）---

def test_git_baseline_uses_commit(tmp_path):
    src, session = _session(tmp_path, use_git=True)
    if not session.git_enabled:
        return  # 环境无 git，跳过
    base = session.create_baseline("plan")
    assert base.git_commit is not None          # git 路径记 commit
    assert base.fallback_snapshot_path is None   # 不落快照

    session.write_working("git 改动\n")
    assert "git 改动" in session.read_working()
    assert session.restore_baseline(base) is True
    assert session.read_working() == SAMPLE
    # git 基线 release 无快照可清、无副作用
    session.release_baseline(base)


def test_restore_missing_snapshot_returns_false(tmp_path):
    src, session = _session(tmp_path, use_git=False)
    bogus = SessionBaseline(baseline_id="x", fallback_snapshot_path=str(tmp_path / "nope"))
    assert session.restore_baseline(bogus) is False
