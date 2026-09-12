#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
notify.py —— 一条命令把消息推送到你的微信（多通道，纯标准库实现）

支持的通道（在 config.json 里启用任意一个或多个，会同时发送）：
  wecom_app      企业微信自建应用消息   -> 直推个人微信（走"微信插件"）
  wecom_webhook  企业微信群"消息推送"   -> 推到企业微信群
  serverchan     Server酱（免费 5 条/天）
  wxpusher       WxPusher 极简推送（SPT）
  pushplus       PushPlus（免费 200 条/天）
  dingtalk       钉钉群机器人
  feishu         飞书群机器人
  webhook        任意自定义 Webhook（POST 一段 JSON）

命令行用法：
  python notify.py -t "标题" -d "正文"
  python notify.py -t "标题" -d "正文" --dry-run      # 只打印，不发送
  python notify.py --test                             # 发一条测试消息
  python notify.py -t "标题" -d "正文" --only serverchan

作为库调用：
  from notify import send
  send("标题", "正文")

配置文件查找顺序：
  --config 参数 > 环境变量 WECHAT_NOTIFY_CONFIG > 脚本同目录 config.json > ~/.wechat-notify/config.json
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

__version__ = "1.0.0"

DEFAULT_TIMEOUT = 10
TOKEN_CACHE_NAME = ".token_cache.json"

# 视为"还没填"的占位符，避免拿着模板值去请求接口
PLACEHOLDERS = ("你的", "YOUR_", "your_", "XXXX", "xxxx", "填写", "填这里", "TODO")


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def _stdout_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def sanitize(text) -> str:
    """去掉孤立 surrogate（Windows 管道按 GBK 解码 UTF-8 时会产生），
    否则 json.dumps(...).encode() 会抛 UnicodeEncodeError。"""
    if text is None:
        return ""
    s = text if isinstance(text, str) else str(text)
    if any("\ud800" <= ch <= "\udfff" for ch in s):
        try:
            s = s.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
        except Exception:
            s = s.encode("utf-8", "replace").decode("utf-8", "replace")
    return s


def clip(text: str, max_bytes: int) -> str:
    """按 UTF-8 字节数截断，避免微信侧直接报错。"""
    text = sanitize(text)
    raw = (text or "").encode("utf-8")
    if len(raw) <= max_bytes:
        return text or ""
    return raw[: max_bytes - 15].decode("utf-8", "ignore") + "\n…（内容已截断）"


def is_blank(value) -> bool:
    """配置项为空 / 仍是占位符 => 跳过该通道。"""
    if value is None:
        return True
    s = str(value).strip()
    if not s:
        return True
    return any(p in s for p in PLACEHOLDERS)


def _mask(secret: str, keep: int = 6) -> str:
    s = str(secret or "")
    if len(s) <= keep:
        return "***"
    return s[:keep] + "***" + s[-2:]


def find_config(explicit: str | None = None) -> Path | None:
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env = os.environ.get("WECHAT_NOTIFY_CONFIG")
    if env:
        candidates.append(Path(env).expanduser())
    candidates.append(Path(__file__).resolve().parent / "config.json")
    candidates.append(Path.home() / ".wechat-notify" / "config.json")
    for c in candidates:
        if c.is_file():
            return c
    return None


def load_config(explicit: str | None = None) -> tuple[dict, Path | None]:
    path = find_config(explicit)
    if path is None:
        return {}, None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f), path


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _opener(ignore_env_proxy: bool = True) -> urllib.request.OpenerDirector:
    handlers = []
    if ignore_env_proxy:
        # 不使用 http_proxy/https_proxy 环境变量（Clash TUN 模式等场景直连更稳）
        handlers.append(urllib.request.ProxyHandler({}))
    ctx = ssl.create_default_context()
    handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


def http_post_json(url: str, payload: dict, timeout: int = DEFAULT_TIMEOUT,
                   ignore_env_proxy: bool = True) -> tuple[bool, str]:
    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        # 兜底：payload 里混进了孤立 surrogate
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8", "replace")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    return _do(req, timeout, ignore_env_proxy)


def http_post_form(url: str, fields: dict, timeout: int = DEFAULT_TIMEOUT,
                   ignore_env_proxy: bool = True) -> tuple[bool, str]:
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
    )
    return _do(req, timeout, ignore_env_proxy)


def http_get(url: str, timeout: int = DEFAULT_TIMEOUT,
             ignore_env_proxy: bool = True) -> tuple[bool, str]:
    return _do(urllib.request.Request(url, method="GET"), timeout, ignore_env_proxy)


def _do(req: urllib.request.Request, timeout: int, ignore_env_proxy: bool) -> tuple[bool, str]:
    try:
        with _opener(ignore_env_proxy).open(req, timeout=timeout) as resp:
            return True, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        return False, f"HTTP {e.code}: {body[:300]}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


# --------------------------------------------------------------------------- #
# 各通道实现：返回 (是否成功, 说明)
# --------------------------------------------------------------------------- #
def _send_wecom_app(cfg: dict, title: str, content: str, opt: dict) -> tuple[bool, str]:
    corpid, secret, agentid = cfg.get("corpid"), cfg.get("corpsecret"), cfg.get("agentid")
    if is_blank(corpid) or is_blank(secret) or is_blank(agentid):
        return False, "缺少 corpid / corpsecret / agentid"
    if not cfg.get("_token") or cfg.get("_token_expire", 0) < time.time():
        ok, body = http_get(
            "https://qyapi.weixin.qq.com/cgi-bin/gettoken?"
            + urllib.parse.urlencode({"corpid": corpid, "corpsecret": secret}),
            opt["timeout"], opt["ignore_env_proxy"],
        )
        if not ok:
            return False, f"获取 access_token 失败: {body}"
        try:
            js = json.loads(body)
        except Exception:
            return False, f"access_token 返回异常: {body[:200]}"
        if js.get("errcode") not in (0, None) or not js.get("access_token"):
            return False, f"access_token errcode={js.get('errcode')} {js.get('errmsg')}"
        cfg["_token"] = js["access_token"]
        cfg["_token_expire"] = time.time() + int(js.get("expires_in", 7200)) - 300

    url = ("https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token="
           + urllib.parse.quote(cfg["_token"], safe=""))
    payload = {
        "touser": str(cfg.get("touser") or "@all"),
        "msgtype": "text",
        "agentid": int(agentid),
        "text": {"content": clip(f"{title}\n{content}".strip(), 2000)},
        "enable_duplicate_check": 0,
    }
    ok, body = http_post_json(url, payload, opt["timeout"], opt["ignore_env_proxy"])
    if not ok:
        return False, body
    try:
        js = json.loads(body)
    except Exception:
        return True, body[:120]
    if js.get("errcode") == 0:
        return True, "ok"
    if js.get("errcode") == 42001:  # token 过期，下次自动重新获取
        cfg["_token_expire"] = 0
    return False, f"errcode={js.get('errcode')} {js.get('errmsg')}"


def _send_wecom_webhook(cfg: dict, title: str, content: str, opt: dict) -> tuple[bool, str]:
    url = cfg.get("webhook")
    if is_blank(url):
        return False, "缺少 webhook"
    payload = {"msgtype": "text",
               "text": {"content": clip(f"{title}\n{content}".strip(), 2000)}}
    ok, body = http_post_json(url, payload, opt["timeout"], opt["ignore_env_proxy"])
    if not ok:
        return False, body
    try:
        js = json.loads(body)
        if js.get("errcode") == 0:
            return True, "ok"
        return False, f"errcode={js.get('errcode')} {js.get('errmsg')}"
    except Exception:
        return True, body[:120]


def _send_serverchan(cfg: dict, title: str, content: str, opt: dict) -> tuple[bool, str]:
    key = str(cfg.get("sendkey", "")).strip()
    if is_blank(key):
        return False, "缺少 sendkey"
    if key.lower().startswith("sctp"):
        # SC3：https://<uid>.push.ft07.com/send/<key>.send
        uid = "".join(ch for ch in key[4:].split("t")[0] if ch.isdigit())
        url = f"https://{uid}.push.ft07.com/send/{key}.send"
    else:
        url = f"https://sctapi.ftqq.com/{key}.send"
    ok, body = http_post_form(url, {"title": title or "通知", "desp": content or ""},
                              opt["timeout"], opt["ignore_env_proxy"])
    if not ok:
        return False, body
    try:
        js = json.loads(body)
        code = js.get("code")
        if code in (0, 200):
            return True, "ok"
        return False, f"code={code} {js.get('message') or js.get('msg') or ''}"
    except Exception:
        return True, body[:120]


def _send_wxpusher(cfg: dict, title: str, content: str, opt: dict) -> tuple[bool, str]:
    spt = str(cfg.get("spt", "")).strip()
    if is_blank(spt):
        return False, "缺少 spt"
    payload = {
        "content": clip(f"{title}\n{content}".strip(), 8000),
        "summary": clip(title or "通知", 90),
        "contentType": 1,
        "spt": spt,
    }
    ok, body = http_post_json("https://wxpusher.zjiecode.com/api/send/message/simple-push",
                              payload, opt["timeout"], opt["ignore_env_proxy"])
    if not ok:
        return False, body
    try:
        js = json.loads(body)
        if js.get("code") == 1000:
            return True, "ok"
        return False, f"code={js.get('code')} {js.get('msg')}"
    except Exception:
        return True, body[:120]


def _send_pushplus(cfg: dict, title: str, content: str, opt: dict) -> tuple[bool, str]:
    token = cfg.get("token")
    if is_blank(token):
        return False, "缺少 token"
    payload = {"token": token, "title": title or "通知", "content": content or "",
               "template": cfg.get("template", "txt")}
    ok, body = http_post_json("https://www.pushplus.plus/send", payload,
                              opt["timeout"], opt["ignore_env_proxy"])
    if not ok:
        return False, body
    try:
        js = json.loads(body)
        if js.get("code") == 200:
            return True, "ok"
        return False, f"code={js.get('code')} {js.get('msg')}"
    except Exception:
        return True, body[:120]


def _send_dingtalk(cfg: dict, title: str, content: str, opt: dict) -> tuple[bool, str]:
    url = cfg.get("webhook")
    if is_blank(url):
        return False, "缺少 webhook"
    payload = {"msgtype": "text", "text": {"content": clip(f"{title}\n{content}".strip(), 3000)}}
    ok, body = http_post_json(url, payload, opt["timeout"], opt["ignore_env_proxy"])
    if not ok:
        return False, body
    try:
        js = json.loads(body)
        if js.get("errcode") == 0:
            return True, "ok"
        return False, f"errcode={js.get('errcode')} {js.get('errmsg')}"
    except Exception:
        return True, body[:120]


def _send_feishu(cfg: dict, title: str, content: str, opt: dict) -> tuple[bool, str]:
    url = cfg.get("webhook")
    if is_blank(url):
        return False, "缺少 webhook"
    payload = {"msg_type": "text",
               "content": {"text": clip(f"{title}\n{content}".strip(), 3000)}}
    ok, body = http_post_json(url, payload, opt["timeout"], opt["ignore_env_proxy"])
    if not ok:
        return False, body
    try:
        js = json.loads(body)
        if js.get("code") == 0 or js.get("StatusCode") == 0:
            return True, "ok"
        return False, f"code={js.get('code')} {js.get('msg')}"
    except Exception:
        return True, body[:120]


def _send_generic_webhook(cfg: dict, title: str, content: str, opt: dict) -> tuple[bool, str]:
    url = cfg.get("webhook")
    if is_blank(url):
        return False, "缺少 webhook"
    payload = {"title": title, "content": content, "text": f"{title}\n{content}".strip(),
               "timestamp": datetime.now().isoformat(timespec="seconds")}
    ok, body = http_post_json(url, payload, opt["timeout"], opt["ignore_env_proxy"])
    return (True, "ok") if ok else (False, body)


# --------------------------------------------------------------------------- #
# 通道：微信 ClawBot（复用 WorkBuddy 里已绑定的微信通道，零配置）
#   —— 走腾讯 iLink Bot 协议（WorkBuddy 内置通道，非公开文档，升级后可能变化）
# --------------------------------------------------------------------------- #
def load_claw_credentials() -> dict | None:
    """从本机 WorkBuddy 配置里读出已绑定的微信 ClawBot 凭据。"""
    candidates = []
    env_path = os.environ.get("WORKBUDDY_SETTINGS")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path.home() / ".workbuddy" / "settings.json")
    for path in candidates:
        try:
            if not path.is_file():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            users = ((data.get("claw") or {}).get("users") or {})
            for _uid, user in users.items():
                ch = ((user or {}).get("channels") or {}).get("weixinClawBot") or {}
                if ch.get("enabled") is False:
                    continue
                if ch.get("botToken") and ch.get("userId"):
                    return {
                        "botToken": ch["botToken"],
                        "userId": ch["userId"],
                        "baseUrl": (ch.get("baseUrl") or "https://ilinkai.weixin.qq.com").rstrip("/"),
                        "source": str(path),
                    }
        except Exception:
            continue
    return None


def _http_post_ilink(base_url: str, path: str, headers: dict, body: bytes,
                     timeout: int) -> tuple[bool, str]:
    """用 http.client 发请求，以便精确控制请求头大小写（iLink 对头部较敏感）。"""
    import http.client

    parsed = urllib.parse.urlsplit(base_url if "://" in base_url else "https://" + base_url)
    host = parsed.netloc
    base_path = parsed.path.rstrip("/")
    conn_cls = http.client.HTTPSConnection if parsed.scheme != "http" else http.client.HTTPConnection
    conn = conn_cls(host, timeout=timeout)
    try:
        conn.request("POST", base_path + path, body=body, headers=headers)
        resp = conn.getresponse()
        text = resp.read().decode("utf-8", "replace")
        if resp.status != 200:
            return False, f"HTTP {resp.status}: {text[:300]}"
        return True, text
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _send_clawbot(cfg: dict, title: str, content: str, opt: dict) -> tuple[bool, str]:
    cred = None
    if not is_blank(cfg.get("botToken")) and not is_blank(cfg.get("userId")):
        cred = {"botToken": cfg["botToken"], "userId": cfg["userId"],
                "baseUrl": (cfg.get("baseUrl") or "https://ilinkai.weixin.qq.com").rstrip("/"),
                "source": "config.json"}
    else:
        cred = load_claw_credentials()
    if not cred:
        return False, ("没找到已绑定的微信 ClawBot 凭据；"
                       "请先在 WorkBuddy 里绑定微信通道，或在 config.json 里直接填 botToken/userId")

    import base64
    import secrets

    text = clip(f"{title}\n{content}".strip(), 1200)
    uin = base64.b64encode(str(secrets.randbits(32)).encode("utf-8")).decode("ascii")
    client_id = f"wbnotify-{int(time.time())}-{secrets.randbits(16)}"
    payload = {
        "msg": {
            "from_user_id": "",
            "to_user_id": cred["userId"],
            "client_id": client_id,
            "message_type": 2,
            "message_state": 2,
            "item_list": [{"type": 1, "text_item": {"text": text}}],
        },
        "base_info": {"channel_version": "workbuddy-desktop-1.0.0"},
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": uin,
        "Authorization": f"Bearer {cred['botToken']}",
        "Content-Length": str(len(body)),
    }
    ok, resp = _http_post_ilink(cred["baseUrl"], "/ilink/bot/sendmessage",
                                headers, body, opt["timeout"])
    if not ok:
        return False, resp
    try:
        js = json.loads(resp) if resp.strip() else {}
    except Exception:
        return True, f"ok（返回无法解析：{resp[:80]}）"
    ret = js.get("ret")
    if ret in (0, None):
        return True, "ok"
    if ret == -2:
        return False, ("ret=-2：限流 / 配额用尽 / 会话已过期。"
                       "请在微信里给 clawbot 发任意一条消息刷新会话后重试"
                       "（单会话约 10 条，账号级约 7 条/5 分钟）")
    if ret == -14:
        return False, "ret=-14：登录态失效，需要在 WorkBuddy 里重新绑定微信通道"
    return False, f"ret={ret} {js.get('errmsg') or ''}"


SENDERS = {
    "clawbot": _send_clawbot,
    "wecom_app": _send_wecom_app,
    "wecom_webhook": _send_wecom_webhook,
    "serverchan": _send_serverchan,
    "wxpusher": _send_wxpusher,
    "pushplus": _send_pushplus,
    "dingtalk": _send_dingtalk,
    "feishu": _send_feishu,
    "webhook": _send_generic_webhook,
}

# 每个通道必填的字段，用于 dry-run 预览时提示"还缺什么"
REQUIRED_FIELDS = {
    "clawbot": (),  # 自动从本机 WorkBuddy 配置读取，无需填写
    "wecom_app": ("corpid", "corpsecret", "agentid"),
    "wecom_webhook": ("webhook",),
    "serverchan": ("sendkey",),
    "wxpusher": ("spt",),
    "pushplus": ("token",),
    "dingtalk": ("webhook",),
    "feishu": ("webhook",),
    "webhook": ("webhook",),
}


def missing_fields(name: str, cfg: dict) -> list[str]:
    return [f for f in REQUIRED_FIELDS.get(name, ()) if is_blank(cfg.get(f))]


# --------------------------------------------------------------------------- #
# 对外主函数
# --------------------------------------------------------------------------- #
class SendResult(dict):
    @property
    def ok(self) -> bool:
        return bool(self.get("ok"))


def send(title: str, content: str = "", config_path: str | None = None,
         only: list[str] | None = None, dry_run: bool = False,
         verbose: bool = True, timeout: int | None = None) -> list[SendResult]:
    """向所有已启用的通道推送。返回每个通道的结果列表。"""
    title, content = sanitize(title), sanitize(content)
    cfg, path = load_config(config_path)
    opt = {
        "timeout": int(timeout or cfg.get("timeout", DEFAULT_TIMEOUT)),
        "ignore_env_proxy": bool(cfg.get("ignore_env_proxy", True)),
    }
    channels = cfg.get("channels") or []
    auto_claw = False
    if not channels:
        # 零配置兜底：本机已绑定微信 ClawBot 就直接用它
        if load_claw_credentials():
            channels = [{"type": "clawbot"}]
            auto_claw = True
    results: list[SendResult] = []

    if not channels:
        msg = ("没有可用的推送通道。请把 config.example.json 复制为 config.json 并填写密钥，"
               "或先在 WorkBuddy 里绑定微信 ClawBot 通道。")
        if verbose:
            print(f"[notify] {msg}（配置文件：{path or '未找到'}）", file=sys.stderr)
        return [SendResult(channel="-", ok=False, detail=msg)]

    for ch in channels:
        name = str(ch.get("type", "")).strip()
        if only and name not in only:
            continue
        fn = SENDERS.get(name)
        if fn is None:
            results.append(SendResult(channel=name, ok=False, detail="未知通道类型"))
            if verbose:
                print(f"[notify] FAIL {name}: 未知通道类型", file=sys.stderr)
            continue
        if dry_run:
            miss = missing_fields(name, ch)
            note = f"（还缺字段：{'、'.join(miss)}）" if miss else ""
            results.append(SendResult(channel=name, ok=not miss,
                                      detail=f"dry-run 预览{note}"))
            if verbose:
                print(f"[notify][dry-run] → {name} {note}\n"
                      f"    标题: {title}\n    正文: {content[:120]}")
            continue
        if ch.get("enabled") is False:
            continue
        ok, detail = fn(ch, title, content, opt)
        results.append(SendResult(channel=name, ok=ok, detail=detail))
        if verbose:
            flag = "OK  " if ok else "FAIL"
            print(f"[notify] {flag} {name}: {detail}")

    _save_token_cache(cfg, path)
    if auto_claw and verbose:
        print("[notify] 提示：当前用的是零配置通道（复用 WorkBuddy 已绑定的微信 ClawBot）。",
              file=sys.stderr)
    return results


def _save_token_cache(cfg: dict, path: Path | None) -> None:
    if path is None:
        return
    try:
        # 简单缓存：把 token 写回 sidecar 文件（不污染用户的 config.json）
        cache_file = path.parent / TOKEN_CACHE_NAME
        data = {}
        if cache_file.is_file():
            try:
                data = json.loads(cache_file.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        for ch in cfg.get("channels") or []:
            if ch.get("type") == "wecom_app" and ch.get("_token"):
                data["wecom_app"] = {"token": ch["_token"], "expire": ch.get("_token_expire", 0)}
        if data:
            cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _load_token_cache(cfg: dict, path: Path | None) -> None:
    if path is None:
        return
    cache_file = path.parent / TOKEN_CACHE_NAME
    if not cache_file.is_file():
        return
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        entry = data.get("wecom_app") or {}
        if entry.get("token") and entry.get("expire", 0) > time.time():
            for ch in cfg.get("channels") or []:
                if ch.get("type") == "wecom_app":
                    ch["_token"] = entry["token"]
                    ch["_token_expire"] = entry["expire"]
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    _stdout_utf8()
    ap = argparse.ArgumentParser(description="把消息推送到你的微信（多通道）")
    ap.add_argument("-t", "--title", default="", help="消息标题")
    ap.add_argument("-d", "--desp", default="", help="消息正文")
    ap.add_argument("-c", "--config", default=None, help="配置文件路径")
    ap.add_argument("--only", action="append", default=None,
                    help="只发指定通道，可重复，例如 --only serverchan")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不真正发送")
    ap.add_argument("--test", action="store_true", help="发送一条测试消息")
    ap.add_argument("--quiet", action="store_true", help="不打印每个通道的结果")
    ap.add_argument("--list-channels", action="store_true", help="列出所有支持的通道类型")
    args = ap.parse_args(argv)

    if args.list_channels:
        print("支持的通道类型：")
        for k in SENDERS:
            print("  -", k)
        return 0

    title = args.title
    desp = args.desp
    if args.test:
        title = title or "✅ 测试消息"
        desp = desp or (f"如果你看到这条消息，说明推送通道已经打通。\n"
                        f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n"
                        f"来源：{os.environ.get('COMPUTERNAME', 'unknown')}")

    if not title and not desp:
        ap.print_help()
        return 2

    cfg, path = load_config(args.config)
    _load_token_cache(cfg, path)

    results = send(title, desp, config_path=args.config, only=args.only,
                   dry_run=args.dry_run, verbose=not args.quiet)
    attempted = [r for r in results if r.get("channel") != "-"]
    if not attempted:
        return 3
    ok_count = sum(1 for r in attempted if r.get("ok"))
    return 0 if ok_count == len(attempted) else 1


if __name__ == "__main__":
    raise SystemExit(main())
