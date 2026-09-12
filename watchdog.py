#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
watchdog.py —— 电脑上的任务「干完了」或「停下来等你操作/确认」时，自动发消息到你微信

它盯两类信号
------------
  ① 进程退出                        -> ✅ 完成 / ❌ 失败（含退出码、起止时间、耗时、日志尾部）
  ② 进程还活着，但停下来等你了       -> ⏸ 在等你操作
       · 静默等待：连续 N 分钟既没日志、没输出、CPU 也几乎不动
         （典型：脚本在 input() 等输入、弹了个确认框、AI 在等你回话）
       · 弹窗等待：进程冒出可见窗口（--detect-dialogs 打开时）

三种用法
--------
1) 包着命令跑（最推荐，能拿到退出码和准确耗时）：
     python watchdog.py --label "训练A" --log train.log -- python train.py
2) 附加到已经在跑的进程：
     python watchdog.py --label "训练A" --log train.log --pid 12345
3) 按进程名/命令行关键字自动找到并盯住：
     python watchdog.py --label "训练A" --log train.log --name python.exe --cmdline train.py
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from notify import send as notify_send  # noqa: E402
from notify import _stdout_utf8  # noqa: E402

DEFAULT_DIALOG_KEYWORDS = ("确认", "是否", "继续", "允许", "权限", "警告", "安装", "错误",
                           "失败", "用户帐户控制", "UAC", "Confirm", "Warning", "Error",
                           "OK", "Cancel", "Yes", "No", "Retry")


# =========================================================================== #
# 进程探测后端：优先 psutil，没有则用 Windows 原生 API（psapi/kernel32）
# =========================================================================== #
class ProcInfo:
    __slots__ = ("pid", "name", "cmdline", "create_time", "cpu_time")

    def __init__(self, pid, name="", cmdline="", create_time=0.0, cpu_time=0.0):
        self.pid = pid
        self.name = name
        self.cmdline = cmdline
        self.create_time = create_time
        self.cpu_time = cpu_time


class PsutilBackend:
    name = "psutil"

    def __init__(self):
        import psutil  # noqa
        self._ps = psutil

    def list_all(self):
        out = []
        for p in self._ps.process_iter(["pid", "name", "cmdline", "create_time", "cpu_times"]):
            try:
                i = p.info
                ct = i.get("cpu_times")
                out.append(ProcInfo(
                    pid=i["pid"],
                    name=i.get("name") or "",
                    cmdline=" ".join(i.get("cmdline") or []),
                    create_time=float(i.get("create_time") or 0),
                    cpu_time=float(ct.user + ct.system) if ct else 0.0,
                ))
            except Exception:
                continue
        return out

    def info(self, pid):
        try:
            p = self._ps.Process(pid)
            if not p.is_running() or p.status() in ("zombie", "dead"):
                return None
            ct = p.cpu_times()
            return ProcInfo(pid=pid, name=p.name(),
                            cmdline=" ".join(p.cmdline() or []),
                            create_time=p.create_time(),
                            cpu_time=ct.user + ct.system)
        except Exception:
            return None


class Win32Backend:
    """不依赖第三方库：psapi.EnumProcesses + kernel32.GetProcessTimes。"""

    name = "win32"

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._psapi = ctypes.WinDLL("psapi", use_last_error=True)
        self._PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        self._STILL_ACTIVE = 259
        self._epoch_delta = 11644473600.0

        k32 = self._k32
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
        ]
        k32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
        ]
        self._psapi.EnumProcesses.argtypes = [
            ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)
        ]

    @staticmethod
    def _ft(ft):
        return (ft.dwHighDateTime << 32 | ft.dwLowDateTime) / 1e7

    def info(self, pid):
        ctypes = self._ctypes
        from ctypes import wintypes
        h = self._k32.OpenProcess(self._PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return None
        try:
            code = wintypes.DWORD()
            if not self._k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return None
            if code.value != self._STILL_ACTIVE:
                return None
            c, e, k, u = (wintypes.FILETIME() for _ in range(4))
            if not self._k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                                             ctypes.byref(k), ctypes.byref(u)):
                return None
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            name = ""
            if self._k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value)
            return ProcInfo(pid=int(pid), name=name, cmdline="",
                            create_time=self._ft(c) - self._epoch_delta,
                            cpu_time=self._ft(k) + self._ft(u))
        finally:
            self._k32.CloseHandle(h)

    def list_all(self):
        ctypes = self._ctypes
        from ctypes import wintypes
        count = 4096
        arr = (wintypes.DWORD * count)()
        needed = wintypes.DWORD()
        if not self._psapi.EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(needed)):
            return []
        out = []
        for i in range(needed.value // ctypes.sizeof(wintypes.DWORD)):
            pid = arr[i]
            if not pid:
                continue
            info = self.info(pid)
            if info:
                out.append(info)
        return out


def make_backend():
    if os.environ.get("WATCHDOG_FORCE_WIN32") == "1":
        return Win32Backend()
    try:
        return PsutilBackend()
    except Exception:
        if os.name == "nt":
            return Win32Backend()
        raise SystemExit("需要 psutil：请执行  pip install psutil")


# --------------------------------------------------------------------------- #
# 可见窗口探测（判断"是不是弹了个框在等你"）
# --------------------------------------------------------------------------- #
def visible_windows(pid: int, keywords=None) -> list[str]:
    if os.name != "nt" or not pid:
        return []
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        found: list[str] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def cb(hwnd, _lparam):
            wpid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if wpid.value != pid:
                return True
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.strip()
            if not title:
                return True
            if keywords and not any(k.lower() in title.lower() for k in keywords):
                return True
            found.append(title)
            return True

        user32.EnumWindows(cb, 0)
        return found
    except Exception:
        return []


# =========================================================================== #
# 小工具
# =========================================================================== #
def fmt_dur(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} 小时 {m} 分 {s} 秒"
    if m:
        return f"{m} 分 {s} 秒"
    return f"{s} 秒"


def fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def tail_file(path: Path, lines: int = 15, max_bytes: int = 3000) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 64 * 1024))
            data = f.read()
        text = data.decode("utf-8", "replace")
        parts = [ln for ln in text.splitlines() if ln.strip()]
        out = "\n".join(parts[-lines:])
        if len(out.encode("utf-8")) > max_bytes:
            raw = out.encode("utf-8")[-max_bytes:]
            out = "…" + raw.decode("utf-8", "ignore")
        return out
    except Exception as e:  # noqa: BLE001
        return f"(读取日志失败: {e})"


def stat_sig(path: Path):
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


# =========================================================================== #
# 监控器
# =========================================================================== #
class Watchdog:
    def __init__(self, args):
        self.args = args
        self.backend = make_backend()
        self.proc: subprocess.Popen | None = None
        self.pid: int | None = args.pid
        self.label = args.label or "未命名任务"
        self.start_ts = time.time()
        self.stdout_bytes = 0
        self._stdout_lock = threading.Lock()
        self.quiet = args.quiet

    # ---------- 输出 ----------
    def log(self, msg: str) -> None:
        if not self.quiet:
            print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)

    def push(self, title: str, content: str) -> None:
        if self.args.dry_run:
            print("\n┌── [dry-run] 将要推送 ────────────────────────", flush=True)
            print(f"│ {title}", flush=True)
            for ln in content.splitlines():
                print(f"│ {ln}", flush=True)
            print("└──────────────────────────────────────────────\n", flush=True)
            return
        try:
            notify_send(title, content, config_path=self.args.config, verbose=not self.quiet)
        except Exception as e:  # noqa: BLE001
            self.log(f"推送失败: {type(e).__name__}: {e}")

    # ---------- 启动 / 找到目标 ----------
    def start(self) -> None:
        args = self.args
        if args.cmd:
            self.log(f"启动并监控：{' '.join(args.cmd)}")
            env = os.environ.copy()
            env.setdefault("PYTHONUNBUFFERED", "1")
            self.proc = subprocess.Popen(
                args.cmd,
                stdout=subprocess.PIPE if args.capture_output else None,
                stderr=subprocess.STDOUT if args.capture_output else None,
                bufsize=0,
                cwd=args.cwd or None,
                env=env,
            )
            self.pid = self.proc.pid
            self.start_ts = time.time()
            if self.proc.stdout is not None:
                threading.Thread(target=self._pump_stdout, args=(self.proc.stdout,),
                                 daemon=True).start()
        elif args.pid:
            info = self.backend.info(args.pid)
            if info is None:
                raise SystemExit(f"PID {args.pid} 不存在或已结束")
            self.start_ts = info.create_time or time.time()
            self.log(f"附加到已运行的进程 PID={info.pid} {info.name}"
                     f"（已运行 {fmt_dur(time.time() - self.start_ts)}）")
        else:
            raise SystemExit("必须指定 --pid 或 --name，或用 `-- 命令` 方式启动")

        self.log(f"开始监控「{self.label}」 PID={self.pid}（进程探测后端：{self.backend.name}）")
        self.log(f"  轮询：每 {args.poll} 秒")
        if args.log:
            self.log(f"  信号：日志 {args.log}")
        if args.heartbeat:
            self.log(f"  信号：心跳 {args.heartbeat}")
        if self.proc is not None and args.capture_output:
            self.log("  信号：子进程输出")
        if args.detect_dialogs:
            self.log(f"  信号：可见弹窗（关键词：{'/'.join(args.dialog_keywords)}）")
        else:
            self.log("  弹窗检测：未开启（加 --detect-dialogs 打开）")
        if not args.no_wait_check:
            self.log(f"  等待判定：连续 {args.wait_after:g} 分钟没动静就提醒你"
                     f"（最多 {args.wait_max} 次，间隔 {args.wait_repeat:g} 分钟）")
        if args.notify_on_start:
            self.push(f"▶️ 开始监控：{self.label}",
                      f"PID: {self.pid}\n开始时间: {datetime.now():%Y-%m-%d %H:%M:%S}\n"
                      f"命令: {' '.join(args.cmd) if args.cmd else '(附加监控)'}")

    def _pump_stdout(self, pipe) -> None:
        """透传子进程输出到终端，同时统计字节数作为"有动静"的信号。"""
        out = sys.stdout.buffer if hasattr(sys.stdout, "buffer") else None
        while True:
            chunk = pipe.read(4096)
            if not chunk:
                break
            with self._stdout_lock:
                self.stdout_bytes += len(chunk)
            if out:
                try:
                    out.write(chunk)
                    out.flush()
                except Exception:
                    out = None

    def _find_by_name(self) -> int | None:
        args = self.args
        key = (args.name or "").lower()
        needle = (args.cmdline or "").lower()
        if not key:
            return None
        matches = []
        for info in self.backend.list_all():
            if key not in info.name.lower():
                continue
            if needle and needle not in info.cmdline.lower():
                continue
            if info.pid == os.getpid():
                continue
            matches.append(info)
        if not matches:
            return None
        if len(matches) > 1:
            self.log(f"匹配到 {len(matches)} 个进程，取启动时间最新的一个；"
                     f"可用 --cmdline 缩小范围")
        matches.sort(key=lambda i: i.create_time, reverse=True)
        info = matches[0]
        self.start_ts = info.create_time or time.time()
        self.log(f"匹配到 PID={info.pid} {info.name} {info.cmdline[:120]}")
        return info.pid

    def wait_for_target(self) -> None:
        if self.args.pid or self.args.cmd:
            return
        deadline = time.time() + self.args.find_timeout
        while True:
            pid = self._find_by_name()
            if pid:
                self.pid = pid
                self.log(f"附加成功，已运行 {fmt_dur(time.time() - self.start_ts)}")
                return
            if time.time() > deadline:
                raise SystemExit(
                    f"{self.args.find_timeout:g} 秒内没有找到匹配「{self.args.name}」的进程，退出。")
            time.sleep(max(1.0, self.args.poll))

    # ---------- 信号 ----------
    def snapshot(self, info: ProcInfo | None) -> dict:
        snap: dict = {}
        if self.args.log:
            snap["log"] = stat_sig(Path(self.args.log))
        if self.args.heartbeat:
            snap["heartbeat"] = stat_sig(Path(self.args.heartbeat))
        if self.proc is not None and self.args.capture_output:
            with self._stdout_lock:
                snap["stdout"] = self.stdout_bytes
        snap["cpu"] = info.cpu_time if info else None
        return snap

    def check_progress(self, prev: dict, cur: dict) -> tuple[bool, str]:
        reasons = []
        if prev.get("log") is not None and cur.get("log") is not None:
            if cur["log"] != prev["log"]:
                reasons.append("日志更新")
        if prev.get("heartbeat") is not None and cur.get("heartbeat") is not None:
            if cur["heartbeat"] != prev["heartbeat"]:
                reasons.append("心跳更新")
        if "stdout" in prev and "stdout" in cur and cur["stdout"] > prev["stdout"]:
            reasons.append(f"有输出 +{cur['stdout'] - prev['stdout']}B")
        pc, cc = prev.get("cpu"), cur.get("cpu")
        if pc is not None and cc is not None:
            delta = cc - pc
            if delta >= self.args.cpu_epsilon:
                reasons.append(f"CPU +{delta:.1f}s")
        return (bool(reasons), "、".join(reasons) if reasons else "无任何变化")

    # ---------- 主循环 ----------
    def run(self) -> int:
        args = self.args
        self.start()
        self.wait_for_target()

        prev = self.snapshot(self.backend.info(self.pid) if self.pid else None)
        last_progress_ts = time.time()
        last_reason = "启动"
        wait_alerts = 0
        next_wait_alert = last_progress_ts + args.wait_after * 60
        alerted = False
        dialog_since = None
        dialog_alerts = 0
        next_dialog_alert = 0.0
        exit_code = None
        gone_since = None

        while True:
            time.sleep(max(0.2, args.poll))
            now = time.time()
            info = self.backend.info(self.pid)

            # ---- 进程是否还活着 ----
            if self.proc is not None:
                rc = self.proc.poll()
                if rc is not None:
                    exit_code = rc
                    break
                info = self.backend.info(self.pid) or info
            elif info is None:
                if gone_since is None:
                    gone_since = now
                    if args.end_grace:
                        self.log("目标进程暂时探测不到，等待确认…")
                elif now - gone_since >= args.end_grace:
                    break

            # ---- 信号 1：可见弹窗（在等你点确认） ----
            if args.detect_dialogs:
                wins = visible_windows(self.pid, args.dialog_keywords)
                if wins:
                    if dialog_since is None:
                        dialog_since = now
                        next_dialog_alert = now + args.dialog_grace
                        self.log(f"检测到窗口：「{wins[0]}」")
                    elif now >= next_dialog_alert and dialog_alerts < args.wait_max:
                        dialog_alerts += 1
                        next_dialog_alert = now + args.wait_repeat * 60
                        self.push(*self.build_dialog_message(info, wins, dialog_alerts))
                        self.log(f"已推送弹窗提醒（第 {dialog_alerts} 次）")
                else:
                    if dialog_alerts:
                        self.push(f"▶️ 已继续：{self.label}",
                                  f"窗口已关掉，任务继续跑起来了。\n"
                                  f"时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
                    dialog_since = None
                    dialog_alerts = 0

            # ---- 有动静吗 ----
            cur = self.snapshot(info)
            progressed, reason = self.check_progress(prev, cur)
            prev = cur
            if progressed:
                last_progress_ts = now
                last_reason = reason
                next_wait_alert = now + args.wait_after * 60
                if alerted:
                    self.push(f"▶️ 已继续：{self.label}",
                              f"任务重新有动静了（{reason}）。\n"
                              f"时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
                    alerted = False
                    wait_alerts = 0

            # ---- 信号 2：长时间静默（在等你输入/确认） ----
            if not args.no_wait_check and now >= next_wait_alert:
                if dialog_since is not None:
                    pass  # 已经报了"窗口在等你"，不重复报静默
                elif wait_alerts < args.wait_max:
                    wait_alerts += 1
                    alerted = True
                    next_wait_alert = now + args.wait_repeat * 60
                    self.push(*self.build_wait_message(info, now - last_progress_ts,
                                                       last_progress_ts, wait_alerts))
                    self.log(f"已推送等待提醒（第 {wait_alerts} 次，"
                             f"静默 {fmt_dur(now - last_progress_ts)}）")
                else:
                    next_wait_alert = now + 3600
            elif args.verbose_loop:
                self.log(f"运行中 · 已运行 {fmt_dur(now - self.start_ts)} · "
                         f"最近动静 {fmt_dur(now - last_progress_ts)} 前（{last_reason}）")

        return self.finish(exit_code)

    # ---------- 消息内容 ----------
    def _head_lines(self, info: ProcInfo | None) -> list[str]:
        return [f"任务: {self.label}",
                f"PID: {self.pid} {info.name if info else ''}".rstrip()]

    def build_dialog_message(self, info, wins: list[str], nth: int) -> tuple[str, str]:
        a = self.args
        lines = self._head_lines(info)
        more = f"（还有 {len(wins) - 1} 个窗口）" if len(wins) > 1 else ""
        lines += [f"状态: 🖱 弹了个窗口在等你操作（第 {nth} 次提醒）",
                  f"窗口: {wins[0]}{more}",
                  f"已运行: {fmt_dur(time.time() - self.start_ts)}",
                  f"时间: {datetime.now():%Y-%m-%d %H:%M:%S}"]
        if a.log:
            lines += ["", f"日志尾部（{a.log}）:", tail_file(Path(a.log), a.tail)]
        return f"🖱 需要你点一下：{self.label}", "\n".join(lines)

    def build_wait_message(self, info, idle: float, last_progress_ts: float,
                           nth: int) -> tuple[str, str]:
        a = self.args
        lines = self._head_lines(info)
        lines += [
            f"状态: ⏸ 停下来等你了（第 {nth} 次提醒）",
            f"已经静默: {fmt_dur(idle)}（超过 {a.wait_after:g} 分钟的阈值）",
            f"最后一次动静: {fmt_ts(last_progress_ts)}",
            f"已运行: {fmt_dur(time.time() - self.start_ts)}",
        ]
        if info and info.cpu_time:
            lines.append(f"累计 CPU 时间: {info.cpu_time:.1f} 秒（几乎不涨 = 八成在等人）")
        lines.append(f"时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
        lines += ["", "可能是：在等你输入 / 弹了确认框 / 网络卡住 / 真的挂了"]
        if a.log:
            lines += ["", f"日志尾部（{a.log}）:", tail_file(Path(a.log), a.tail)]
        return f"⏸ 在等你操作：{self.label}", "\n".join(lines)

    def finish(self, exit_code) -> int:
        a = self.args
        end = time.time()
        dur = end - self.start_ts
        if exit_code is None:
            head, status = "✅ 已结束", "进程已退出（未拿到退出码）"
        elif exit_code == 0:
            head, status = "✅ 完成", "正常结束（退出码 0）"
        elif exit_code < 0 or exit_code > 128:
            head, status = "❌ 异常退出", f"退出码 {exit_code}（可能被强制结束）"
        else:
            head, status = "❌ 失败", f"退出码 {exit_code}"

        lines = [f"任务: {self.label}",
                 f"状态: {status}",
                 f"开始: {fmt_ts(self.start_ts)}",
                 f"结束: {fmt_ts(end)}",
                 f"耗时: {fmt_dur(dur)}"]
        if a.cmd:
            lines.append(f"命令: {' '.join(a.cmd)}")
        if a.log:
            lines += ["", f"日志尾部（{a.log}）:", tail_file(Path(a.log), a.tail)]
        self.push(f"{head}：{self.label}", "\n".join(lines))
        self.log(f"{head} · 耗时 {fmt_dur(dur)}")
        return int(exit_code or 0)

    def abort(self) -> None:
        self.log("收到中断，正在结束监控…")
        if self.proc is not None and self.proc.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                                   capture_output=True, check=False)
                else:
                    self.proc.terminate()
            except Exception:
                pass
        self.push(f"⏹ 已手动中止：{self.label}",
                  f"监控被 Ctrl+C 中断，任务已一并结束。\n"
                  f"已运行: {fmt_dur(time.time() - self.start_ts)}\n"
                  f"时间: {datetime.now():%Y-%m-%d %H:%M:%S}")


# =========================================================================== #
# CLI
# =========================================================================== #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="watchdog.py",
        description="任务干完了 / 停下来等你操作时，自动发消息到你微信",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  # 1. 包着命令跑：10 分钟没动静就提醒我
  python watchdog.py --label "训练A" --log train.log -- python train.py

  # 2. 盯住已经在跑的进程
  python watchdog.py --label "下载" --log dl.log --pid 12345

  # 3. 按进程名自动找（中途才发现要监控时）
  python watchdog.py --label "训练A" --log train.log --name python.exe --cmdline train.py

  # 4. 顺带检测"弹窗等你点确认"
  python watchdog.py --detect-dialogs --label "安装" -- installer.exe

  # 5. 只要"跑完通知我"
  python watchdog.py --no-wait-check -- python build.py

  # 6. 先看效果不真发消息
  python watchdog.py --dry-run -- python demo_task.py --mode wait
""")
    p.add_argument("cmd", nargs="*", help="要运行的命令（写在 -- 之后）")
    p.add_argument("--label", default="", help="任务名，会出现在消息里")
    p.add_argument("--log", default=None, help="日志文件路径，作为“有动静”的信号（推荐）")
    p.add_argument("--heartbeat", default=None, help="心跳文件路径，作为“有动静”的信号")
    p.add_argument("--pid", type=int, default=None, help="附加到指定 PID")
    p.add_argument("--name", default=None, help="按进程名匹配（如 python.exe）")
    p.add_argument("--cmdline", default=None, help="进一步用命令行关键字筛选匹配的进程")
    p.add_argument("--cwd", default=None, help="运行子进程的工作目录")

    g = p.add_argument_group("“在等你操作”检测")
    g.add_argument("--wait-after", "--stall-after", dest="wait_after", type=float, default=10.0,
                   help="连续多少分钟没动静就提醒你（默认 10，可写小数如 0.5）")
    g.add_argument("--wait-repeat", "--stall-repeat", dest="wait_repeat", type=float, default=10.0,
                   help="之后每隔多少分钟重复提醒（默认 10）")
    g.add_argument("--wait-max", "--stall-max", dest="wait_max", type=int, default=3,
                   help="最多提醒几次（默认 3）")
    g.add_argument("--cpu-epsilon", type=float, default=0.5,
                   help="单次轮询 CPU 时间增长超过多少秒算“有动静”（默认 0.5）")
    g.add_argument("--no-wait-check", "--no-stall", dest="no_wait_check", action="store_true",
                   help="关掉等待检测，只在结束时通知")
    g.add_argument("--detect-dialogs", action="store_true",
                   help="检测目标进程是否弹出了可见窗口（默认关闭）")
    g.add_argument("--dialog-keywords", nargs="*", default=list(DEFAULT_DIALOG_KEYWORDS),
                   help="弹窗标题里包含这些词才算“在等你操作”")
    g.add_argument("--dialog-grace", type=float, default=20.0,
                   help="窗口出现后保持多少秒才提醒（默认 20）")
    g.add_argument("--end-grace", type=float, default=30.0,
                   help="附加模式下探测不到进程后，再等多少秒判定结束（默认 30）")

    m = p.add_argument_group("其他")
    m.add_argument("--poll", type=float, default=5.0, help="轮询间隔秒数（默认 5）")
    m.add_argument("--tail", type=int, default=15, help="消息里附带的日志尾部行数（默认 15）")
    m.add_argument("--find-timeout", type=float, default=300.0,
                   help="按 --name 查找进程的最长等待秒数（默认 300）")
    m.add_argument("--notify-on-start", action="store_true", help="开始时也发一条消息")
    m.add_argument("--capture-output", action="store_true", default=True,
                   help="捕获子进程输出作为“有动静”的信号（默认开启）")
    m.add_argument("--no-capture-output", dest="capture_output", action="store_false",
                   help="不捕获子进程输出")
    m.add_argument("--config", default=None, help="推送配置文件路径")
    m.add_argument("--dry-run", action="store_true", help="不真正发送，只打印")
    m.add_argument("--quiet", action="store_true", help="不打印监控过程")
    m.add_argument("--verbose-loop", action="store_true", help="每轮打印一次运行状态")
    return p


def main(argv=None) -> int:
    _stdout_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd and not args.pid and not args.name:
        parser.print_help()
        return 2
    wd = Watchdog(args)
    try:
        return wd.run()
    except KeyboardInterrupt:
        wd.abort()
        return 130
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"[watchdog] 出错了: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
