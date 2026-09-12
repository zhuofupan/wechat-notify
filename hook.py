#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hook.py —— 挂在 AI 助手（WorkBuddy / Claude Code 等）的 hook 上，
          **任务完成** 或 **需要你操作/确认** 时，自动发消息到你微信。

为什么用 hook：AI 助手自己最清楚"我停了"和"我在等你"这两件事，
比从外面猜进程状态准得多。

支持的事件
----------
  Stop               每次对话回合结束（= 干完活了）        -> ✅ 任务完成
  Notification       WorkBuddy 的提醒事件，其中：
                       idle_prompt       空闲等你回复       -> ⏸ 在等你回复
                       permission_prompt 要你点确认         -> 🔐 需要你确认
                       auth_success      登录完成           -> 默认忽略
  PermissionRequest  工具要权限（写文件/跑命令等）          -> 🔐 需要你确认（默认只推高风险操作）

用法（由 hook 自动调用，一般不需要手敲）
----------------------------------------
  python hook.py --hook Stop            < payload.json
  python hook.py --hook Notification    < payload.json
  python hook.py --hook PermissionRequest
  python hook.py --test-stop            # 不接 hook，手动看效果

设计注意
--------
* 默认**不向 stdout 输出任何内容**，避免影响 hook 的 JSON 约定；
  调试用 --verbose，日志写在脚本同目录的 hook.log。
* 有节流（默认 Stop 类 60 秒内只推一条），避免连续回合刷屏。
* 任何异常都吞掉并返回 0，绝不阻塞 AI 助手。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from notify import send as notify_send, _stdout_utf8  # noqa: E402

STATE_FILE = HERE / ".hook_state.json"
LOG_FILE = HERE / "hook.log"
LOG_MAX_BYTES = 512 * 1024


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def log(msg: str, verbose: bool = False) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n"
    try:
        line = line.encode("utf-8", "replace").decode("utf-8")   # 防 surrogate 炸日志
    except Exception:
        pass
    try:
        if LOG_FILE.is_file() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            LOG_FILE.write_text(line, encoding="utf-8")
        else:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass
    if verbose:
        try:
            sys.stderr.write(line)
        except Exception:
            pass


def load_state() -> dict:
    try:
        if STATE_FILE.is_file():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def clip_text(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " …"


def sanitize_text(value) -> str:
    """去掉 surrogate 与控制字符。

    Windows 上 hook 的 stdin 走 bash 管道时，Python 可能用 GBK 解码 UTF-8 字节，
    解不出的字节会变成 U+DC80–U+DCFF 的孤立 surrogate；这种字符串一旦
    json.dumps().encode() 就抛 UnicodeEncodeError。这里做兜底清洗。
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    if any("\ud800" <= ch <= "\udfff" for ch in text):
        try:
            # surrogateescape 产物的原始字节可以还原；还原不出来就退到 replace
            text = text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
        except Exception:
            text = text.encode("utf-8", "replace").decode("utf-8", "replace")
    return "".join(ch for ch in text if ch >= " " or ch in "\t\n\r")


def sanitize_obj(obj):
    """递归清洗 dict / list 里的所有字符串。"""
    if isinstance(obj, str):
        return sanitize_text(obj)
    if isinstance(obj, dict):
        return {k: sanitize_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_obj(v) for v in obj]
    return obj


def decode_bytes(data: bytes) -> str:
    """hook 传的是 UTF-8 JSON；优先按 UTF-8 解，失败再退到系统编码。"""
    if not data:
        return ""
    for enc in ("utf-8", "utf-8-sig"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    try:
        import locale
        return data.decode(locale.getpreferredencoding(False), errors="replace")
    except Exception:
        return data.decode("utf-8", errors="replace")


DUMP_FILE = Path(__file__).resolve().parent / "hook_payload.jsonl"


def dump_payload(event: str, payload: dict) -> None:
    """把 hook payload 的字段结构记下来（长字符串截断到 80 字），
    方便排查"某个字段从哪来"，也方便将来发现更好用的字段。"""
    try:
        if DUMP_FILE.is_file() and DUMP_FILE.stat().st_size > 128_000:
            DUMP_FILE.write_text("", encoding="utf-8")

        def brief(o, depth=0):
            if depth > 4:
                return "…"
            if isinstance(o, str):
                return o if len(o) <= 80 else o[:80] + f"…<len={len(o)}>"
            if isinstance(o, dict):
                return {k: brief(v, depth + 1) for k, v in o.items()}
            if isinstance(o, list):
                return [brief(v, depth + 1) for v in o[:5]]
            return o

        rec = {"ts": f"{datetime.now():%Y-%m-%d %H:%M:%S}", "event": event,
               "payload": brief(payload)}
        with open(DUMP_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_stdin_payload() -> dict:
    """读 hook 的 stdin payload。

    关键：必须走 sys.stdin.buffer 拿原始字节再自己按 UTF-8 解码。
    直接 sys.stdin.read() 会跟随 Windows 的 GBK 编码，把中文 payload 弄坏。
    """
    try:
        if sys.stdin is None or sys.stdin.closed:
            return {}
        buf = getattr(sys.stdin, "buffer", None)
        if buf is not None:
            raw = decode_bytes(buf.read())
        else:
            raw = sys.stdin.read()
    except Exception:
        return {}
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return sanitize_obj(data) if isinstance(data, dict) else {}
    except Exception:
        return {"_raw": sanitize_text(raw)}


def pick(d: dict, *names, default=None):
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]
    return default


# --------------------------------------------------------------------------- #
# 高风险判断（PermissionRequest 用）
# --------------------------------------------------------------------------- #
COMMAND_TOOLS = {"Bash", "Shell", "Terminal", "run_command", "execute_command", "PowerShell"}

# 无论目标是什么，本身就该警惕的命令
ALWAYS_DANGEROUS_RE = re.compile(
    r"(format\s+[a-z]:|diskpart|mkfs(\.\w+)?\s|dd\s+if=|shutdown\b|Stop-Computer|"
    r":\(\)\s*\{|--no-preserve-root|git\s+push\s+(--force|-f)\b|git\s+reset\s+--hard|"
    r"drop\s+(table|database)|truncate\s+table|taskkill\s+/f)",
    re.IGNORECASE,
)
DELETE_CMD_RE = re.compile(r"\b(rm|rmdir|del|erase|Remove-Item)\b([^\n|;&]*)", re.IGNORECASE)
DELETE_FLAG_RE = re.compile(r"(-[a-zA-Z]*[rf][a-zA-Z]*\b|/s\b|/q\b|-Recurse\b|-Force\b)")
# 删到这些地方才是"灾难"；删项目里的临时目录是日常操作，不吵人
_DEADLY_TARGETS = {"*", "/*", "**", ".", "..", "~", "/", "\\", "$HOME", "%USERPROFILE%"}
_DEADLY_PREFIXES = ("c:/windows", "c:/program files", "c:/programdata",
                    "/etc", "/usr", "/bin", "/sbin", "/var", "/system", "/library")

SENSITIVE_PATH_RE = re.compile(
    r"(^|[\\/])(Windows|Program Files( \(x86\))?|ProgramData|System32)([\\/]|$)"
    r"|^[A-Za-z]:[\\/](Users[\\/][^\\/]+[\\/](Desktop|Documents|Downloads))?$",
    re.IGNORECASE,
)


def _looks_deadly_target(target: str) -> bool:
    t = target.strip().strip("\"'").strip()
    if not t:
        return False
    bare = t.rstrip("\\/") or t
    if bare in _DEADLY_TARGETS:
        return True
    if re.fullmatch(r"[A-Za-z]:", bare):          # 只写了 C: 或 C:\
        return True
    return t.replace("\\", "/").lower().startswith(_DEADLY_PREFIXES)


def dangerous_command(cmd: str) -> str | None:
    """挑出真正值得把你叫醒的命令。

    `rm -rf <项目里的临时目录>` 是 AI 的日常清理动作，不该报警 ——
    只有目标是根目录/家目录/上一级/通配符/系统目录，或命令本身灾难性时才算。
    """
    if not cmd:
        return None
    m = ALWAYS_DANGEROUS_RE.search(cmd)
    if m:
        return m.group(0).strip()

    for m in DELETE_CMD_RE.finditer(cmd):
        flags = m.group(2) or ""
        if not DELETE_FLAG_RE.search(flags):
            continue
        for arg in flags.split():
            if arg.startswith("-"):
                continue
            if _looks_deadly_target(arg):
                return f"{m.group(1)} {' '.join(flags.split())}".strip()[:100]
    return None


def is_high_risk(tool_name: str, tool_input) -> tuple[bool, str]:
    """判断这次权限请求值不值得把你从手机那头叫醒。"""
    name = tool_name or ""
    raw = ""
    if isinstance(tool_input, dict):
        raw = " ".join(str(v) for v in tool_input.values() if isinstance(v, (str, int, float)))
    else:
        raw = str(tool_input or "")

    if name in COMMAND_TOOLS:
        cmd = str(pick(tool_input, "command", "cmd", default="")) if isinstance(tool_input, dict) else ""
        hit = dangerous_command(cmd or raw)
        if hit:
            return True, f"危险命令：{hit}"
        return False, ""

    # 结构化的写操作：只看目标路径，避免把文档正文误判成危险内容
    if isinstance(tool_input, dict):
        target = str(pick(tool_input, "file_path", "path", "notebook_path", "target", default=""))
        if target and SENSITIVE_PATH_RE.search(target):
            return True, f"写入敏感位置：{target}"
        if pick(tool_input, "delete", default=False) or pick(tool_input, "recursive", default=False):
            return True, "带删除/递归标记的操作"

    return False, ""


# --------------------------------------------------------------------------- #
# 会话标题来源（按优先级）：
#   1) WorkBuddy 的 SQLite 数据库 ~/.workbuddy/workbuddy.db 的 sessions 表
#      → custom_title（你在 UI 里手动重命名的名字） > title（自动生成的标题）。
#      这是"改名后自动跟随"的关键：hook payload 本身没有标题字段，但数据库有。
#   2) 回退：UserPromptSubmit 时记下的"本会话第一句提问"（存在 .hook_state.json）。
#   3) 再回退：项目文件夹名。
# --------------------------------------------------------------------------- #
TITLE_MAX = 60
TITLE_KEEP = 60

# 各分组的节流窗口（秒）。
# stop 窗口故意设得很短（10s）：用户明确要"每次任务完成都收到提醒"，
# 窗口过长会把一来一回的真实完成通知吞掉（实测 60s 太容易误杀）。
# 10s 只挡同一瞬间的重复触发（比如 hook 同回合被触发两次）。
THROTTLE_WINDOWS = {"stop": 10.0, "idle": 60.0, "attention": 60.0, "notice": 60.0}


def _short_title(text: str) -> str:
    t = " ".join(sanitize_text(text).split())
    t = t.strip(" 　")
    if not t:
        return ""
    return t if len(t) <= TITLE_MAX else t[:TITLE_MAX].rstrip() + "…"


def remember_title(payload: dict, state: dict) -> str:
    sid = str(pick(payload, "session_id", default="")).strip()
    if not sid:
        return ""
    titles = state.setdefault("titles", {})
    if titles.get(sid):
        return str(titles[sid])
    title = _short_title(str(pick(payload, "prompt", "user_prompt", "message", default="")))
    if not title:
        return ""
    titles[sid] = title
    if len(titles) > TITLE_KEEP:
        for k in list(titles)[: len(titles) - TITLE_KEEP]:
            titles.pop(k, None)
    return title


def session_title(state: dict, sid: str) -> str:
    """会话标题：优先读 WorkBuddy 数据库（含你在 UI 里改的名），
    退回自动记录的首句提问，再退回项目文件夹名。"""
    t = db_session_title(sid)
    if t:
        return t
    return str((state.get("titles") or {}).get(sid, "") or "")


# WorkBuddy 把会话标题存在这个 SQLite 里（sessions 表）。
# 关键字段：custom_title = 你在 UI 里手动重命名的名字；title = 自动生成的标题。
# 读它就能自动跟随改名，不必再手动 --set-title。
DB_PATH = HERE.parents[1] / "workbuddy.db"


def db_session_title(sid: str) -> str:
    """从 WorkBuddy 的 sessions 表读当前会话标题。

    优先级：custom_title（你改的名） > title（自动生成的名）。
    只读打开（mode=ro，不阻塞正在运行的 App），任何异常都吞掉返回 ""。
    """
    sid = (sid or "").strip()
    if not sid:
        return ""
    con = None
    try:
        import sqlite3
        if not DB_PATH.is_file():
            return ""
        con = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True, timeout=2)
        row = con.execute(
            "SELECT COALESCE(NULLIF(custom_title,''), title) FROM sessions WHERE id=?",
            (sid,),
        ).fetchone()
        return _short_title(row[0]) if row and row[0] else ""
    except Exception:
        return ""
    finally:
        try:
            if con is not None:
                con.close()
        except Exception:
            pass


def _folder_line(payload: dict) -> str:
    """精简消息用：只取项目文件夹名一行。"""
    cwd = str(pick(payload, "cwd", default="") or "")
    folder = Path(cwd).name if cwd else ""
    return f"📁 {folder}" if folder else ""


# --------------------------------------------------------------------------- #
# 设备名：同一账号可能多台设备登录，消息里先标明"从哪台机器来的"。
# WorkBuddy 心跳文件（~/.workbuddy/sessions/*.json）里的 hostname 与 OS 主机名
# 同源（实测一致），直接取 platform.node() 即可，无需查库；跨平台稳定。
# --------------------------------------------------------------------------- #
_DEVICE: dict = {}


def device_name() -> str:
    if "name" in _DEVICE:
        return _DEVICE["name"]
    name = ""
    try:
        import platform
        name = (platform.node() or "").strip()
    except Exception:
        name = ""
    if not name:
        try:
            import socket
            name = (socket.gethostname() or "").strip()
        except Exception:
            name = ""
    _DEVICE["name"] = name
    return name


def _device_line() -> str:
    """设备行：🖥 主机名（多设备同账号时区分消息来自哪台机器）。"""
    dev = device_name()
    return f"🖥 {dev}" if dev else ""


def _title_line(payload: dict, state: dict, sid: str, fallback: str = "当前会话") -> str:
    """会话行：💬 会话名（WorkBuddy 改名自动跟随）。"""
    title = session_title(state, sid)
    if not title:
        cwd = str(pick(payload, "cwd", default="") or "")
        title = (Path(cwd).name if cwd else "") or fallback
    return f"💬 {title}"


# --------------------------------------------------------------------------- #
# 概要提取：把助手的最后一条消息压成一行"干货"。
# 去掉 Markdown 符号、客套话、空行/符号行，挑信息量最高的一句，再极简裁剪。
# --------------------------------------------------------------------------- #
_FILLER_LEAD = re.compile(
    r"^(好的[的呀吧]?[，,。！!]?|明白了?[。！!]?|收到[。！!]?|完成[。！!]?|搞定[。！!]?|"
    r"当然[，,。!！。]?|没问题[。！!]?|说实在的[，,]?|说实话[，,]?|直接说结论[：:]?|"
    r"嗯+|哦+|让我|我来|我先|接下来|另外[，,]?|首先[，,]?|其次[，,]?|总之[，,]?|"
    r"注意[：:]?|补充[一下]*[：:]?)\s*",
)
_RESULT_WORDS = re.compile(
    r"(完成|修复|修好|解决|新增|添加|删除|移除|成功|通过|失败|报错|错误|异常|部署|上线|"
    r"更新|升级|创建|生成|写好|跑通|验证|优化|重构|支持|兼容|迁移|配置|安装|卸载|启用|"
    r"停用|发布|推送|同步|清理|重建|重启|导出|导入|提交|合并|发布版|已实现|已支持)"
)
_CODEY_LINE = re.compile(r"^[\s\{\}\[\]\(\);=<>+\-*/|`\\~^\"']*$")
_HAS_URL = re.compile(r"https?://|www\.")
_TAIL_PUNCT = " ，,。.、；;：:！!？?"


def _strip_md(text: str) -> str:
    """去掉常见 Markdown 装饰符号，保留文字内容。"""
    t = re.sub(r"```[a-zA-Z0-9+-]*", " ", text)              # 代码围栏
    t = re.sub(r"!?\[([^\]]*)\]\([^)]{0,300}\)", r"\1", t)   # [文字](链接) → 文字
    t = t.replace("**", "").replace("__", "")
    t = re.sub(r"[*_~`#>|]+", " ", t)                        # 强调/标题/表格线
    t = re.sub(r"(?m)^\s*[-=]{2,}[-=\s]*$", " ", t)          # 水平分隔线整行删除
    t = re.sub(r"^\s{0,12}[-*+]\s+", "", t)                  # 无序列表前缀
    t = re.sub(r"^\s{0,12}\d{1,2}[.、)]\s+", "", t)          # 有序列表前缀
    return t


def _pick_summary_line(text: str) -> str:
    """从消息里挑一句最有信息量的：优先首句结论，首句是客套话则向下找结果句。"""
    fallback = ""
    for idx, raw in enumerate(text.splitlines()[:40]):
        line = " ".join(_strip_md(raw).split())
        if not line or _CODEY_LINE.match(line):
            continue
        for _ in range(2):  # 客套话最多剥两层（"好的。明白了。xxx"）
            stripped = _FILLER_LEAD.sub("", line).strip(_TAIL_PUNCT)
            if stripped == line or len(stripped) < 6:
                break            # 没剥动 / 剥后太短：保留原句
            line = stripped
        core = line.strip(_TAIL_PUNCT)
        if len(core) < 6 or len(core) > 120 or _CODEY_LINE.match(core):
            continue
        if idx == 0:
            return core                       # 首行有干货，直接用
        if not fallback:
            fallback = core                   # 先记一个候选
        if _RESULT_WORDS.search(core):
            return core                       # 结果句优先于普通后文
    return fallback


def summarize(text: str, limit: int) -> str:
    """概要入口：去无用字符 + 挑干货句 + 极简裁剪。"""
    if not text:
        return ""
    core = _pick_summary_line(text[:4000])
    if not core:
        core = " ".join(_strip_md(text[:4000]).split())   # 兜底：整条压平
    core = core.strip(_TAIL_PUNCT)
    out = clip_text(core, limit).rstrip(_TAIL_PUNCT)
    return out


# --------------------------------------------------------------------------- #
# 事件 -> (通知标题, 正文, 节流分组键)
# 节流键让 PermissionRequest 和 Notification(权限) 共用一组，
# 避免同一件事推两条，白白消耗微信配额。
# --------------------------------------------------------------------------- #
def build_message(event: str, payload: dict, args, state: dict) -> tuple[str, str, str] | None:
    sid = str(pick(payload, "session_id", default="")).strip()
    # 防 hook 自循环：Stop hook 触发的续跑会被标记
    if event == "Stop" and payload.get("stop_hook_active"):
        return None

    # 竖排样式：每行一个 Emoji 开头。公共头两行 = 设备行 + 会话行
    lines = [ln for ln in (_device_line(), _title_line(payload, state, sid)) if ln]
    folder = _folder_line(payload)

    if event == "Stop":
        # 本轮完成概要：去 Markdown 符号/客套话，挑信息量最高的一句，极简裁剪
        summary = str(pick(payload, "last_assistant_message", "lastAssistantMessage",
                           "result", "summary", default="")).strip()
        if summary:
            brief = summarize(summary, args.stop_summary_chars)
            if brief:
                lines.append(f"📝 {brief}")
        return "✅ 任务完成", "\n".join(lines), "stop"

    if event == "Notification":
        ntype = str(pick(payload, "notification_type", "type", "notificationType", default=""))
        message = str(pick(payload, "message", "notification_message", default=""))
        low = ntype.lower()
        if "auth_success" in low:
            return None
        if folder:
            lines.append(folder)
        if "idle" in low or "wait" in low or "空闲" in ntype:
            if message:
                lines.append(clip_text(message, args.summary_chars))
            return "⏸️ 等你回复", "\n".join(lines), "idle"
        if "permission" in low or ("auth" in low and "success" not in low):
            if message:
                lines.append(clip_text(message, args.summary_chars))
            return "🔐 需确认", "\n".join(lines), "attention"
        if message:
            lines.append(clip_text(message, args.summary_chars))
        return "👀 需要你看一眼", "\n".join(lines), "notice"

    if event == "PermissionRequest":
        tool = str(pick(payload, "tool_name", "toolName", default=""))
        tool_input = pick(payload, "tool_input", "toolInput", default={})
        risky, why = is_high_risk(tool, tool_input)
        if not risky and not args.all_permissions:
            return None
        if folder:
            lines.append(folder)
        lines.append(f"🛠 工具：{tool or '-'}")
        if why:
            lines.append(f"⚠️ 原因：{why}")
        if tool in COMMAND_TOOLS and isinstance(tool_input, dict):
            cmd = str(pick(tool_input, "command", "cmd", default=""))
            if cmd:
                lines += ["", f"💻 命令：{clip_text(cmd, 300)}"]
        elif isinstance(tool_input, dict):
            target = pick(tool_input, "file_path", "path", default="")
            if target:
                lines += ["", f"🎯 目标：{target}"]
        return "🔐 需确认", "\n".join(lines), "attention"

    return None


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    _stdout_utf8()
    ap = argparse.ArgumentParser(description="把 AI 助手的 hook 事件推送到微信")
    ap.add_argument("--hook", default=None,
                    choices=["Stop", "Notification", "PermissionRequest",
                             "UserPromptSubmit", "SubagentStop"],
                    help="hook 事件名")
    ap.add_argument("--event-file", default=None, help="从文件读 payload（调试用）")
    ap.add_argument("--payload", default=None, help="直接给一段 JSON（调试用）")
    ap.add_argument("--test-stop", action="store_true", help="模拟一次 Stop 事件")
    ap.add_argument("--test-notify", action="store_true", help="模拟一次'等你回复'事件")
    ap.add_argument("--config", default=None, help="推送配置文件路径")
    ap.add_argument("--summary-chars", type=int, default=200, help="通知摘要最多取多少字（默认 200）")
    ap.add_argument("--stop-summary-chars", type=int, default=40,
                    help="Stop 消息里'本轮完成概要'最多取多少字（默认 40，极简）")
    ap.add_argument("--min-interval", type=float, default=60.0,
                    help="同类事件最短间隔秒数，防止刷屏（默认 60）")
    ap.add_argument("--timeout", type=int, default=6,
                    help="单次推送的网络超时秒数（默认 6，避免拖慢 AI 助手）")
    ap.add_argument("--all-permissions", action="store_true",
                    help="所有权限请求都推送（默认只推高风险操作）")
    ap.add_argument("--set-title", default=None,
                    help="设定当前会话的显示名（覆盖自动标题，用于同步 UI 改名）")
    ap.add_argument("--dry-run", action="store_true", help="不真正发送")
    ap.add_argument("--verbose", action="store_true", help="打印调试信息到 stderr")
    args = ap.parse_args(argv)

    # 手动设定当前会话的显示名（用于同步你在 UI 里改过的会话名）
    if args.set_title:
        st = load_state()
        sid = st.get("last_sid")
        if not sid:
            print("尚无会话记录，请先让 hook 在目标会话里跑过一次再设定显示名。")
            return 0
        st.setdefault("titles", {})[sid] = _short_title(args.set_title)
        save_state(st)
        print(f"✅ 已为会话 {sid[:8]} 设定显示名：{args.set_title}")
        return 0

    event = args.hook
    payload: dict = {}

    if args.payload:
        try:
            payload = sanitize_obj(json.loads(args.payload))
        except Exception:
            payload = {"_raw": sanitize_text(args.payload)}
    elif args.event_file:
        try:
            payload = sanitize_obj(json.loads(Path(args.event_file).read_text(encoding="utf-8")))
        except Exception as e:  # noqa: BLE001
            log(f"读取 payload 失败: {e}", args.verbose)
    elif args.test_stop:
        event = event or "Stop"
        payload = {"hook_event_name": "Stop", "cwd": os.getcwd(), "session_id": "test1234",
                   "last_assistant_message": "（这是测试消息）任务已经跑完了，一切正常。"}
    elif args.test_notify:
        event = event or "Notification"
        payload = {"hook_event_name": "Notification", "cwd": os.getcwd(),
                   "notification_type": "idle_prompt", "session_id": "test1234"}
    else:
        payload = read_stdin_payload()

    if not event:
        event = str(pick(payload, "hook_event_name", "event", "hook", default="Stop"))

    dump_payload(event, payload)

    # stdin 没读到 payload 时绝不能瞎猜事件，否则会误报一条"任务完成"
    if not payload:
        log(f"{event}: stdin 无 payload，跳过", args.verbose)
        return 0

    state = load_state()

    # 记录最近一次会话 sid，供 --set-title 使用（改名后手动同步显示名）
    sid0 = str(pick(payload, "session_id", default="")).strip()
    if sid0:
        state["last_sid"] = sid0
        save_state(state)

    # UserPromptSubmit 只用来记会话标题（拿每个会话的第一句提问当标题），永不推送
    if event == "UserPromptSubmit":
        title = remember_title(payload, state)
        if title:
            save_state(state)
        log(f"UserPromptSubmit: 记录会话标题 <{title}>" if title
            else "UserPromptSubmit: 已有标题，跳过", args.verbose)
        return 0

    built = build_message(event, payload, args, state)
    if built is None:
        log(f"{event}: 跳过（无需通知）", args.verbose)
        return 0
    title, body, throttle_key = (sanitize_text(built[0]), sanitize_text(built[1]), built[2])

    # 判断是否为手动测试（--test-stop / --test-notify）
    # 测试事件：不检查节流、不写入节流状态，避免"刚测完真 Stop 就被吞"
    is_test = bool(args.test_stop or args.test_notify)

    # 节流：按"分组键"而不是事件名，PermissionRequest 与 Notification(权限)
    # 共用 attention 组 —— 同一件事只推一条，不重复消耗微信配额。
    # 各分组窗口不同（stop 只 10s，其余 60s），可用 --min-interval 覆盖默认。
    # 测试事件完全绕过节流（测试不应影响真实通知的节流状态）
    key = f"last_{throttle_key}"
    now = time.time()
    window = THROTTLE_WINDOWS.get(throttle_key, args.min_interval)
    if not is_test and window > 0 and now - float(state.get(key, 0)) < window:
        log(f"{event}: 节流跳过（{throttle_key} 组 {window:g}s 内已推过）", args.verbose)
        return 0

    if args.dry_run:
        log(f"{event}: [dry-run] {title}\n{body}", True)
        return 0

    # 硬超时保护：无论网络多慢，hook 都必须在几秒内返回，不能拖慢 AI 助手
    box: dict = {}

    def worker():
        try:
            box["results"] = notify_send(title, body, config_path=args.config,
                                         verbose=args.verbose, timeout=args.timeout)
        except Exception as e:  # noqa: BLE001
            box["error"] = f"{type(e).__name__}: {e}"

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    th.join(args.timeout + 2)
    if th.is_alive():
        log(f"{event}: 推送超过 {args.timeout + 2}s 未返回，放弃等待（不阻塞）", args.verbose)
        return 0

    results = box.get("results") or []
    if box.get("error"):
        log(f"{event}: 推送异常 {box['error']}", args.verbose)
        return 0

    # 多通道时只要有一条成功就算"通知到了"：否则一条通道挂掉（比如 clawbot 被限流）
    # 会让节流状态永不更新，下次重复推送，反而变成刷屏。
    # 注意：测试事件不写节流状态，避免污染真实通知的计时器
    attempted = [r for r in results if r.get("channel") != "-"]
    ok = any(r.get("ok") for r in attempted)
    if ok and not is_test:
        state[key] = now
        save_state(state)
    detail = "; ".join(f"{r.get('channel')}={r.get('detail')}" for r in results)
    n_ok = sum(1 for r in attempted if r.get("ok"))
    status = "已推送" if ok else "推送失败"
    if ok and n_ok < len(attempted):
        status = f"已推送({n_ok}/{len(attempted)} 通道成功，其余见详情)"
    log(f"{event}: {status} | {title} | {detail}", args.verbose)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001  —— hook 里绝不能抛异常
        try:
            log(f"未捕获异常: {type(e).__name__}: {e}")
        except Exception:
            pass
        raise SystemExit(0)
