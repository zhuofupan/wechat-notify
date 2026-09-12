#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo_task.py —— 假任务，用来验证 watchdog 是否真的能识别"完成 / 卡住 / 失败"

  python demo_task.py --mode slow   --log demo.log     # 正常跑完
  python demo_task.py --mode wait   --log demo.log     # 中途静默一段时间（模拟"停下来等你操作"）
  python demo_task.py --mode crash  --log demo.log     # 跑一半报错退出
"""

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path


def log_line(path: Path, msg: str) -> None:
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    print(line, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["slow", "wait", "hang", "crash"], default="slow")
    ap.add_argument("--log", default="demo.log")
    ap.add_argument("--steps", type=int, default=8)
    ap.add_argument("--hang", type=float, default=20.0, help="wait 模式下静默多少秒")
    ap.add_argument("--interval", type=float, default=2.0)
    args = ap.parse_args()
    if args.mode == "hang":
        args.mode = "wait"

    path = Path(args.log) if args.log else None
    log_line(path, f"=== 任务开始（mode={args.mode}） ===")

    if args.mode == "crash":
        for i in range(1, 3):
            log_line(path, f"第 {i} 步完成")
            time.sleep(args.interval)
        log_line(path, "ERROR: 第 3 步失败，模拟异常退出")
        return 3

    if args.mode == "wait":
        log_line(path, "第 1 步完成")
        time.sleep(args.interval)
        log_line(path, "第 2 步完成，接下来停下来等你确认…")
        time.sleep(args.hang)                      # ← 这期间看门狗应该报"在等你操作"
        log_line(path, "你确认了，继续处理")
        time.sleep(args.interval)
        log_line(path, "=== 任务正常结束 ===")
        return 0

    for i in range(1, args.steps + 1):
        time.sleep(args.interval)
        log_line(path, f"第 {i}/{args.steps} 步完成")
    log_line(path, "=== 任务正常结束 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
