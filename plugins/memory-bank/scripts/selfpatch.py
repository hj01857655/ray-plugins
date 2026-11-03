#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selfpatch —— 自改代码沙箱管线（进化闭环⑤）。

变更以「变异脚本」提交（mutate(src: str) -> str），四道闸门全绿才原子落盘：
  G1 静态门：变异脚本自身禁 I/O（禁 open/os./subprocess/eval/exec/__import__）——
     变更逻辑必须是纯字符串变换，杜绝补丁里夹带任意写盘
  G2 编译门：产物 py_compile 通过
  G3 导入门：产物 import 无异常
  G4 冒烟门：产物调用冒烟函数（--smoke 指定，默认不变）返回 truthy
落盘：目标加时间戳 .bak → 原子替换 → 账本 evolve/selfpatch.jsonl。
任何一道失败：目标文件一个字节都不动。

用法:
  python selfpatch.py --file <目标.py> --patch <变异脚本.py> [--smoke <模块名>]
"""
import argparse
import datetime
import importlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

FORBIDDEN = re.compile(
    r"\bopen\s*\(|\bos\.|\bsubprocess\b|\beval\s*\(|\bexec\s*\(|\b__import__\b|\bcompile\s*\(")


def g1_static(patch_src: str) -> None:
    hit = FORBIDDEN.search(patch_src)
    if hit:
        raise ValueError("G1 静态门：变异脚本含禁用调用 " + repr(hit.group(0)))


def g2_compile(new_src: str, name: str) -> None:
    tmp = Path(tempfile.gettempdir()) / ("selfpatch-g2-" + name)
    tmp.write_text(new_src, encoding="utf-8")
    try:
        compile(new_src, str(tmp), "exec")
    except SyntaxError as e:
        raise ValueError(f"G2 编译门：{e}")
    finally:
        tmp.unlink(missing_ok=True)


def g34_import_smoke(new_src: str, name: str, smoke: str) -> None:
    tmpdir = Path(tempfile.mkdtemp(prefix="selfpatch-g34-"))
    try:
        (tmpdir / name).write_text(new_src, encoding="utf-8")
        (tmpdir / "__init__.py").write_text("", encoding="utf-8")
        sys.path.insert(0, str(tmpdir))
        mod = importlib.import_module(name[:-3] if name.endswith(".py") else name)
        if smoke and not getattr(mod, smoke, lambda: False)():
            raise ValueError("G4 冒烟门：" + smoke + " 返回 falsy")
    except ValueError:
        raise
    except Exception as e:
        raise ValueError("G3 导入门：" + repr(e))
    finally:
        sys.path[:] = [p for p in sys.path if not p.startswith(str(tmpdir))]
        shutil.rmtree(tmpdir, ignore_errors=True)


def ledger(action: str, detail: dict) -> None:
    d = Path.home() / ".zcode" / "cli" / "memories" / "global" / "memory" / "evolve"
    d.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds"), "action": action} | detail
    with (d / "selfpatch.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="目标 .py（会被修改，自动备份）")
    ap.add_argument("--patch", required=True, help="变异脚本：def mutate(src: str) -> str")
    ap.add_argument("--smoke", default="", help="产物内冒烟函数名（返回 truthy 才放行）")
    a = ap.parse_args()

    target = Path(a.file).resolve()
    patch_p = Path(a.patch).resolve()
    if not target.exists() or not patch_p.exists():
        print("目标或补丁不存在"); return 1
    name = target.name

    patch_src = patch_p.read_text(encoding="utf-8")
    src = target.read_text(encoding="utf-8")

    try:
        g1_static(patch_src)
        ns = {}
        exec(compile(patch_src, str(patch_p), "exec"), ns)   # G1 已过滤；执行仅取 mutate
        mut = ns.get("mutate")
        if not callable(mut):
            raise ValueError("G1 静态门：补丁缺 mutate(src) -> str")
        new_src = mut(src)
        if not isinstance(new_src, str) or not new_src.strip():
            raise ValueError("G1 静态门：mutate 返回空")
        if abs(len(new_src) - len(src)) > len(src) * 0.2:
            raise ValueError("G1 静态门：产物体积变化超 ±20%，疑似误操作")
        if new_src == src:
            print("  [noop] 产物与原文件一致，无事发生"); return 0
        g2_compile(new_src, name)
        g34_import_smoke(new_src, name, a.smoke)
    except ValueError as e:
        print("  ** REJECTED ** " + str(e))
        ledger("rejected", {"target": str(target), "patch": str(patch_p), "reason": str(e)})
        return 1

    bak = target.with_suffix(".pre-selfpatch-" + datetime.datetime.now().strftime("%Y%m%dT%H%M%S") +
                             target.suffix)
    shutil.copy2(target, bak)
    tmp = target.with_suffix(".selfpatch-tmp")
    tmp.write_text(new_src, encoding="utf-8")
    os.replace(tmp, target)
    ledger("applied", {"target": str(target), "patch": str(patch_p), "backup": str(bak),
                       "delta": len(new_src) - len(src)})
    print(f"  [applied] {target}（+{len(new_src)-len(src)} B），备份 {bak.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
