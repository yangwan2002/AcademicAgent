"""Layer 3 逃生舱（C6，Week 5）。

当 Layer 1/2 覆盖不了某个格式需求时，可在**用户明确同意**下用 python 脚本操作
working 的**副本**，成功且通过校验才采纳。

诚实的安全边界（重要）：
- 这不是一个强隔离沙箱。它只做到：在临时目录里对副本执行、限制超时、与原
  working 物理隔离（失败不影响原文件）。
- 不联网限制、系统调用限制在跨平台下难以严格保证，因此默认关闭，必须显式确认。
- 脚本通过环境变量 ``DOCX_PATH`` 拿到副本路径，应就地修改该文件。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Callable

Confirm = Callable[[str], bool]


@dataclass
class SandboxResult:
    ok: bool
    output_path: str = ""
    message: str = ""
    stdout: str = ""
    stderr: str = ""


def run_python_script(
    script: str,
    working_path: str,
    confirm: Confirm,
    *,
    timeout: int = 30,
) -> SandboxResult:
    """在副本上执行脚本；需用户确认。返回改动后的副本路径（未替换原文件）。"""
    if not confirm(
        "这个需求超出确定性工具范围。我可以用 python 脚本在**副本**上尝试，"
        "但不保证正确、也非强隔离沙箱。是否继续？"
    ):
        return SandboxResult(ok=False, message="用户取消，未执行脚本。")

    tmpdir = tempfile.mkdtemp(prefix="paper_sandbox_")
    copy_path = os.path.join(tmpdir, os.path.basename(working_path))
    shutil.copyfile(working_path, copy_path)
    script_path = os.path.join(tmpdir, "_op.py")
    with open(script_path, "w", encoding="utf-8") as fh:
        fh.write(script)

    env = {"DOCX_PATH": copy_path, "PATH": os.environ.get("PATH", "")}
    try:
        proc = subprocess.run(
            ["python", script_path],
            cwd=tmpdir, env=env, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return SandboxResult(ok=False, message=f"脚本超时（>{timeout}s）。")
    except (OSError, subprocess.SubprocessError) as exc:
        return SandboxResult(ok=False, message=f"脚本执行失败：{exc}")

    if proc.returncode != 0:
        return SandboxResult(
            ok=False, message="脚本返回非零退出码。",
            stdout=proc.stdout, stderr=proc.stderr,
        )
    return SandboxResult(
        ok=True, output_path=copy_path, message="脚本在副本上执行成功。",
        stdout=proc.stdout, stderr=proc.stderr,
    )


__all__ = ["SandboxResult", "run_python_script"]
