#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
install_hooks.py —— 把 wechat-notify 挂到 AI 助手的 hook 上（WorkBuddy / Claude Code）

做三件事：
  1. 备份 ~/.workbuddy/settings.json
  2. 在 hooks 里合并加入 Stop / Notification / PermissionRequest / UserPromptSubmit 四条
     （已存在则覆盖，不影响别人的 hook）
  3. 校验 JSON 能解析

为什么需要 UserPromptSubmit：hook payload 里没有"会话标题"字段，所以靠它把每个会话的
第一句提问记下来当标题，之后每条通知都能告诉你是哪个会话。

用法：
  python install_hooks.py --print        # 只打印将要写入的片段，不改文件
  python install_hooks.py                # 真正安装
  python install_hooks.py --uninstall    # 卸载（只删自己加的那几条）
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK_PY = HERE / "hook.py"

SETTINGS_CANDIDATES = [
    Path.home() / ".workbuddy" / "settings.json",
    Path.home() / ".codebuddy" / "settings.json",
    Path.home() / ".claude" / "settings.json",
]

PYTHON_CANDIDATES = [
    Path("C:/Python313/python.exe"),
    Path("C:/Python312/python.exe"),
    Path("C:/Program Files/Python313/python.exe"),
    Path("C:/Program Files/Python312/python.exe"),
    Path("/usr/bin/python3"),
    Path("/usr/local/bin/python3"),
]


def win_path(p: Path) -> str:
    """hook 命令由 bash 执行 —— 必须用正斜杠，反斜杠会被当转义符吃掉。"""
    return str(p).replace("\\", "/")


def pick_python(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    for c in PYTHON_CANDIDATES:
        if c.is_file():
            return c
    return Path(sys.executable)


def pick_settings(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    for c in SETTINGS_CANDIDATES:
        if c.is_file():
            return c
    return SETTINGS_CANDIDATES[0]


def build_entries(python: Path) -> dict:
    py = f'"{win_path(python)}"'
    hp = f'"{win_path(HOOK_PY)}"'

    def cmd(event: str, extra: str = "") -> str:
        return f"{py} {hp} --hook {event}{extra}"

    return {
        "Stop": [{"hooks": [{"type": "command", "command": cmd("Stop")}]}],
        "Notification": [{"hooks": [{"type": "command", "command": cmd("Notification")}]}],
        "PermissionRequest": [{"hooks": [{"type": "command", "command": cmd("PermissionRequest")}]}],
        # 只用来记"会话标题"（每个会话的第一句提问），永不推送
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": cmd("UserPromptSubmit")}]}],
    }


def is_ours(entry) -> bool:
    """判断这条 hook 是不是我们自己装的。"""
    try:
        for h in entry.get("hooks", []):
            if "hook.py" in str(h.get("command", "")) and "wechat-notify" in str(h.get("command", "")):
                return True
    except Exception:
        pass
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="把 wechat-notify 挂到 AI 助手的 hook 上")
    ap.add_argument("--settings", default=None, help="settings.json 路径")
    ap.add_argument("--python", default=None, help="用哪个 python 解释器执行 hook")
    ap.add_argument("--print", dest="print_only", action="store_true", help="只打印，不写文件")
    ap.add_argument("--uninstall", action="store_true", help="卸载（只删自己加的）")
    args = ap.parse_args()

    settings = pick_settings(args.settings)
    python = pick_python(args.python)
    entries = build_entries(python)

    print(f"配置文件: {settings}")
    print(f"解释器  : {python}")
    print()

    data: dict = {}
    if settings.is_file():
        try:
            data = json.loads(settings.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"[!] 现有 settings.json 解析失败，为安全起见中止：{e}")
            return 1
    else:
        print(f"[i] {settings} 不存在，将新建。")

    hooks = data.setdefault("hooks", {})

    if args.uninstall:
        removed = 0
        for event in list(hooks.keys()):
            kept = [e for e in hooks[event] if not is_ours(e)]
            removed += len(hooks[event]) - len(kept)
            if kept:
                hooks[event] = kept
            else:
                hooks.pop(event)
        if not hooks:
            data.pop("hooks", None)
    else:
        for event, group in entries.items():
            existing = [e for e in hooks.get(event, []) if not is_ours(e)]
            hooks[event] = existing + group

    snippet = json.dumps({"hooks": entries}, ensure_ascii=False, indent=2)
    print("将要写入的片段：")
    print(snippet)
    print()

    if args.print_only:
        print("[i] --print 模式：没有修改任何文件。")
        return 0

    if settings.is_file():
        backup = settings.with_name(
            f"{settings.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(settings, backup)
        print(f"[✓] 已备份原配置 -> {backup}")

    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")

    # 写回后立刻自检
    try:
        check = json.loads(settings.read_text(encoding="utf-8"))
        events = list((check.get("hooks") or {}).keys())
    except Exception as e:  # noqa: BLE001
        print(f"[!] 写入后校验失败：{e}")
        return 1

    print(f"[✓] 已{'卸载' if args.uninstall else '安装'}完成，当前 hooks 事件：{events}")
    if not args.uninstall:
        print()
        print("提示：hook 事件在下次对话时生效。想先验证效果，可以直接跑：")
        print(f'  "{python}" "{HOOK_PY}" --test-stop')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
