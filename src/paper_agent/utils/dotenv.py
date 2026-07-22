"""极简 .env 加载器（零依赖）。

从项目根目录的 .env 读取 KEY=VALUE 写入环境变量，使本地运行无需每次手动
配置环境变量。已存在的环境变量优先（不覆盖），便于 CI / 命令行临时覆盖。

支持：
- 注释行（以 # 开头）与空行
- 可选的 `export ` 前缀
- 值两侧的单/双引号会被去除
"""

from __future__ import annotations

import os
from pathlib import Path


def find_dotenv(start: str | None = None, filename: str = ".env") -> str | None:
    """从 start 目录起向上查找 .env，返回首个命中路径。"""
    current = Path(start or os.getcwd()).resolve()
    for directory in (current, *current.parents):
        candidate = directory / filename
        if candidate.is_file():
            return str(candidate)
    return None


def load_dotenv(path: str | None = None, override: bool = False) -> bool:
    """加载 .env 到环境变量。返回是否成功加载了某个文件。"""
    dotenv_path = path or find_dotenv()
    if not dotenv_path or not os.path.isfile(dotenv_path):
        return False

    with open(dotenv_path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if not key:
                continue
            if override or key not in os.environ:
                os.environ[key] = value
    return True
