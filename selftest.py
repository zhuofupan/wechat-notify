#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自检：危险命令判定 / 会话标题 / 重复推送抑制 / 消息内容。"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hook  # noqa: E402

BS = chr(92)   # 反斜杠，避免转义地狱


def test_dangerous():
    print("=== 1) 危险命令判定（该静默的必须静默） ===")
    cases = [
        ('rm -rf "/tmp/demo/_cprofile"', False),
        ("rm -f watchdog.py", False),
        ("rm -rf ./build", False),
        ("rm -rf /tmp/xxx", False),
        ("rm -rf /usr/local/lib/foo", True),
        ("rm -rf /", True),
        ("rm -rf ~", True),
        ("rm -rf *", True),
        ("rm -rf ..", True),
        ("rm -rf C:" + BS, True),
        ("rm -rf C:/Windows/System32", True),
        ("del /s /q C:" + BS, True),
        ("diskpart", True),
        ("format c:", True),
        ("shutdown /s /t 0", True),
        ("git push --force origin main", True),
        ("git reset --hard HEAD~3", True),
        ("DROP TABLE users;", True),
        ("pip install psutil", False),
        ("git status", False),
        ("mkdir -p a/b/c", False),
    ]
    bad = 0
    for cmd, expect in cases:
        hit = hook.dangerous_command(cmd)
        ok = bool(hit) == expect
        bad += 0 if ok else 1
        print(f"  {'OK ' if ok else 'BAD'}  {str(bool(hit)):5} (期望 {expect})  {cmd[:50]}")
    print(f"  -> 误判 {bad} 条\n")
    return bad


def test_title_and_dedup():
    print("=== 2) 会话标题 + 重复推送抑制 ===")
    tmp = Path(tempfile.mkdtemp())
    state = {}
    titles = {"titles": state}

    p1 = {"hook_event_name": "UserPromptSubmit", "session_id": "SID-A",
          "cwd": "C:/proj/我的项目",
          "prompt": "帮我写一个批量重命名图片的小脚本"}
    t1 = hook.remember_title(p1, titles)
    print(f"  记下标题: {t1!r}")

    # 后续 prompt 不能覆盖标题
    hook.remember_title({"session_id": "SID-A", "prompt": "再改一下"}, titles)
    print(f"  第二条 prompt 后仍是: {titles['titles']['SID-A']!r}")

    class A:
        summary_chars = 60
        stop_summary_chars = 40      # 与 hook.py 的 --stop-summary-chars 默认值保持一致
        all_permissions = False

    payload = {"hook_event_name": "PermissionRequest", "session_id": "SID-A",
               "cwd": "C:/proj/我的项目", "tool_name": "Bash",
               "tool_input": {"command": "rm -rf /", "description": "危险演示"}}

    m_perm = hook.build_message("PermissionRequest", payload, A(), titles)
    print(f"\n  [PermissionRequest] 标题: {m_perm[0]}")
    print(f"                      节流键: {m_perm[2]}")
    for line in m_perm[1].splitlines():
        print(f"                      | {line}")

    n_payload = {"hook_event_name": "Notification", "session_id": "SID-A",
                 "cwd": "C:/proj/我的项目", "notification_type": "permission_request"}
    m_note = hook.build_message("Notification", n_payload, A(), titles)
    print(f"\n  [Notification]      标题: {m_note[0]}")
    print(f"                      节流键: {m_note[2]}")
    print(f"  -> 两个事件共用节流键? {m_perm[2] == m_note[2]}  (True 表示只会推一条)")

    stop_payload = {"hook_event_name": "Stop", "session_id": "SID-A",
                    "cwd": "C:/proj/我的项目",
                    "last_assistant_message": "全部做完了，四个脚本都写好了。"}
    m_stop = hook.build_message("Stop", stop_payload, A(), titles)
    print(f"\n  [Stop] 标题: {m_stop[0]}")
    for line in m_stop[1].splitlines():
        print(f"        | {line}")

    idle = hook.build_message("Notification", {**n_payload, "notification_type": "idle_prompt"}, A(), titles)
    print(f"\n  [idle_prompt] 标题: {idle[0]}  节流键: {idle[2]}")
    print(f"  -> 和 attention 键不同? {idle[2] != m_perm[2]}")
    return 0


def test_throttle_scope():
    """守住"节流计时器按会话独立 + 完成类有静默窗"这两条 2026-09-26 修的规则。

    背景：原实现节流键只有事件组（全局共享），同一会话每轮 Stop 间隔超过 10s
    就重复推一条 —— 用户报的"同一个会话提醒很多次"。
    """
    print("=== 3) 节流作用域（回归守卫） ===")
    bad = 0

    # 3a) 完成类窗口必须够长（能盖住一轮里多次 Stop 触发）
    for name, val, low in (("STOP_QUIET_WINDOW", hook.STOP_QUIET_WINDOW, 120.0),
                           ("GLOBAL_MIN_GAP", hook.GLOBAL_MIN_GAP, 1.0)):
        ok = val >= low
        bad += 0 if ok else 1
        print(f"  {'OK ' if ok else 'BAD'}  {name} = {val:g}s (应 >= {low:g}s)")

    # 3b) 完成类不能再出现在事件组窗口表里（否则会和静默窗语义打架）
    ok = "stop" not in hook.THROTTLE_WINDOWS
    bad += 0 if ok else 1
    print(f"  {'OK ' if ok else 'BAD'}  stop 不在共享窗口表中: {ok}")

    # 3c) 节流键必须带会话号 —— 用源码文本判定，避免依赖运行态
    src = (HERE / "hook.py").read_text(encoding="utf-8")
    ok = 'f"last_{group}_{sid_key}"' in src
    bad += 0 if ok else 1
    print(f"  {'OK ' if ok else 'BAD'}  节流键含会话维度 (last_<组>_<sid>): {ok}")

    print(f"  -> 不合规 {bad} 条\n")
    return bad


if __name__ == "__main__":
    n = test_dangerous()
    test_title_and_dedup()
    n += test_throttle_scope()
    raise SystemExit(0 if n == 0 else 1)
