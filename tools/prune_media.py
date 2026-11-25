#!/usr/bin/env python3
"""清理仓库里的旧报修附件（图片/视频），防止仓库无限膨胀。

## 背景

仓库里**只有**两类业务数据会增长：
  1. `media/<单号>/*`          —— 报修图片/视频（每单几百 KB 到几 MB）
  2. `signed_state.json`       —— 维修单签字状态（很小）

钉钉 AI 表格里的单据数据和本机 `D:/lmy/维修申报` 都有备份，所以超过保留期的
附件可以从仓库删掉，页面上那几单不再显示图片（不影响单据本身）。

## 用法

    python tools/prune_media.py --days 60 --dry   # 只看要删什么
    python tools/prune_media.py --days 60         # 真删（改 manifest 并删目录）

单号形如 `202611071022000420198`，前 8 位就是日期（YYYYMMDD），据此判断账龄。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

DATE_PREFIX = re.compile(r"^(\d{8})")
NOISE = {"_说明"}


def no_date(key: str) -> date | None:
    m = DATE_PREFIX.match(str(key).strip())
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def prune_manifest(path: Path, cutoff: date, dry: bool) -> tuple[list[str], int]:
    """返回 (被删单号, 删掉的条目数)。"""
    if not path.exists():
        return [], 0
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except ValueError:
        return [], 0
    if not isinstance(data, dict):
        return [], 0

    dropped: list[str] = []
    removed = 0
    for key in list(data.keys()):
        d = no_date(key)
        if d is None or d >= cutoff:
            continue
        entries = data.get(key) or []
        removed += len(entries) if isinstance(entries, list) else 0
        dropped.append(str(key))
        if not dry:
            del data[key]

    if dropped and not dry:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return dropped, removed


def prune_dirs(media_root: Path, dropped: list[str], dry: bool) -> int:
    n = 0
    for no in dropped:
        d = media_root / no
        if d.is_dir():
            n += 1
            if not dry:
                shutil.rmtree(d, ignore_errors=True)
    return n


def prune_signed(path: Path, cutoff: date, dry: bool) -> int:
    """signed_state.json 的键也是审批编号，同样按日期清理。"""
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except ValueError:
        return 0
    if not isinstance(data, dict):
        return 0
    gone = [k for k in data if (no_date(k) or date.max) < cutoff]
    if gone and not dry:
        for k in gone:
            del data[k]
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return len(gone)


def main() -> int:
    ap = argparse.ArgumentParser(description="清理仓库旧附件")
    ap.add_argument("--root", default=".", help="仓库根目录，默认当前目录")
    ap.add_argument("--days", type=int, default=60, help="保留多少天，默认 60")
    ap.add_argument("--dry", action="store_true", help="只打印，不删除")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    cutoff = date.today() - timedelta(days=args.days)
    print(f"保留 {args.days} 天，早于 {cutoff} 的单据将被清理")

    dropped, removed = prune_manifest(root / "media" / "manifest.json", cutoff, args.dry)
    dirs = prune_dirs(root / "media", dropped, args.dry)
    signed = prune_signed(root / "signed_state.json", cutoff, args.dry)

    for no in dropped[:20]:
        print(f"  - {no}")
    if len(dropped) > 20:
        print(f"  ...共 {len(dropped)} 单")
    print(f"{'[dry] ' if args.dry else ''}清理单据 {len(dropped)} 个、"
          f"目录 {dirs} 个、附件条目 {removed} 条、签字记录 {signed} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
