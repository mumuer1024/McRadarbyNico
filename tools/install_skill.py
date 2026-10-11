#!/usr/bin/env python3
"""把工作区里的 McRadar 源码安装 / 同步到 WorkBuddy 用户级 Skill 目录。

关系说明（避免两份代码不一致）：
    E:\\test\\mcd-skill-challenge\\        ← **唯一真源**（开发、提交 Git 都在这里）
        SKILL.md
        scripts/
    ~/.workbuddy/skills/mcd-radar/       ← 安装副本（WorkBuddy 从这里加载）
        SKILL.md
        scripts/
        data/                            ← 运行期状态（不回传）

方向永远是「工作区 → 用户目录」单向同步，不反向覆盖。

用法：
    python tools/install_skill.py              # 安装或更新
    python tools/install_skill.py --check      # 只检查是否一致
    python tools/install_skill.py --remove     # 卸载（保留工作区源码）
"""

from __future__ import annotations

import argparse
import filecmp
import hashlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGET = Path.home() / ".workbuddy" / "skills" / "mcd-radar"

SYNC_FILES = ["SKILL.md"]
SYNC_DIRS = ["scripts"]


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _plan(src: Path, dst: Path) -> list[tuple[Path, Path, str]]:
    """返回 [(src_file, dst_file, status)]，status ∈ add/update/same"""
    out: list[tuple[Path, Path, str]] = []
    for name in SYNC_FILES:
        s, d = src / name, dst / name
        if not s.exists():
            continue
        if not d.exists():
            out.append((s, d, "add"))
        elif _sha(s) != _sha(d):
            out.append((s, d, "update"))
        else:
            out.append((s, d, "same"))
    for name in SYNC_DIRS:
        sdir, ddir = src / name, dst / name
        for s in sorted(sdir.rglob("*")):
            if s.is_dir() or "__pycache__" in s.parts or s.suffix == ".pyc":
                continue
            rel = s.relative_to(src)
            d = dst / rel
            if not d.exists():
                out.append((s, d, "add"))
            elif _sha(s) != _sha(d):
                out.append((s, d, "update"))
            else:
                out.append((s, d, "same"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=str(DEFAULT_TARGET))
    ap.add_argument("--check", action="store_true", help="只检查一致性")
    ap.add_argument("--remove", action="store_true", help="卸载安装副本")
    a = ap.parse_args()

    src, dst = ROOT, Path(a.target)

    if a.remove:
        if dst.exists():
            shutil.rmtree(dst)
            print(f"已卸载：{dst}")
        else:
            print("未安装，无需卸载")
        return 0

    if not (src / "SKILL.md").exists():
        print("工作区缺少 SKILL.md", file=sys.stderr)
        return 1

    plan = _plan(src, dst)
    diff = [x for x in plan if x[2] != "same"]

    if a.check:
        if diff:
            print(f"❌ 不一致，{len(diff)} 个文件需要同步：")
            for s, d, st in diff:
                print(f"   {st:<6} {s.relative_to(src)}")
            return 1
        print(f"✅ 已安装且与工作区一致：{dst}")
        return 0

    dst.mkdir(parents=True, exist_ok=True)
    (dst / "data").mkdir(exist_ok=True)

    for s, d, st in plan:
        if st == "same":
            continue
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)
        print(f"{st:<6} {s.relative_to(src)}")

    # 清理目标里已不存在于源的文件（防止残留旧模块）
    keep = {d for _, d, _ in plan}
    for name in SYNC_DIRS:
        ddir = dst / name
        if not ddir.exists():
            continue
        for d in sorted(ddir.rglob("*")):
            if d.is_dir() or "__pycache__" in d.parts or d.suffix == ".pyc":
                continue
            if d not in keep:
                d.unlink()
                print(f"remove {d.relative_to(dst)}")

    print(f"\n✅ 安装完成：{dst}")
    print("   工作区是唯一真源；改完代码请重新运行本脚本同步。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
