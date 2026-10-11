"""本地状态持久化（原子写、短时效、最小敏感信息）。

P0-C 要求：
- 订单指纹状态记录必须包含：请求指纹、提交时间、提交状态、已知订单 ID、核对所需最小关联信息
- 绝不保存 Token、支付凭证、完整地址或其他不必要敏感信息

所有文件以 UTF-8 写入，使用 tmp + os.replace 保证原子性。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

__all__ = ["StateStore", "default_data_dir", "now_ts", "iso_ts"]

DEFAULT_DIRNAME = "data"


def default_data_dir() -> Path:
    env = os.environ.get("MCD_RADAR_DATA")
    if env:
        return Path(env)
    # scripts/mcdradar/state.py -> 项目根
    return Path(__file__).resolve().parents[2] / DEFAULT_DIRNAME


def now_ts() -> float:
    return time.time()


def iso_ts(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts or now_ts()))


class StateStore:
    """极简 JSON 状态仓库。每个 key 一个文件。"""

    def __init__(self, data_dir: Path | str | None = None):
        self.dir = Path(data_dir) if data_dir else default_data_dir()
        self.dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, key: str) -> Path:
        assert "/" not in key and "\\" not in key, "key 不得含路径分隔符"
        return self.dir / f"{key}.json"

    def read(self, key: str, default: Any = None) -> Any:
        p = self.path_for(key)
        if not p.exists():
            return default
        try:
            with p.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default

    def write(self, key: str, value: Any) -> Path:
        p = self.path_for(key)
        tmp_fd, tmp_name = tempfile.mkstemp(dir=str(self.dir), suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(value, f, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp_name, p)
        except Exception:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise
        return p

    def update(self, key: str, fn) -> Any:
        cur = self.read(key)
        new = fn(cur)
        self.write(key, new)
        return new

    def delete(self, key: str) -> None:
        p = self.path_for(key)
        if p.exists():
            p.unlink()


# ---- 敏感信息红线 ----------------------------------------------------------

SENSITIVE_KEY_HINTS = (
    "token", "authorization", "cookie", "password", "payid", "pay_id",
    "mobile", "phone", "address", "nickname", "member", "idcard",
)


def scrub(obj: Any) -> Any:
    """递归剔除疑似敏感字段。落盘前统一过一遍。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if any(h in lk for h in SENSITIVE_KEY_HINTS):
                continue
            out[k] = scrub(v)
        return out
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    return obj
