---
name: wechat-notify
description: 把「任务完成 / 需要你操作确认」的消息推到用户微信。当用户说"跑完通知我""完成后发微信""卡住/等我的时候提醒我""监控这个进程，结束告诉我""长任务结束微信提醒"，或需要给电脑上的长任务（编译/训练/下载/安装）加微信通知时使用。也用于给 AI 助手的 hook 配置微信提醒（Stop/Notification/PermissionRequest）。
agent_created: true
---

# wechat-notify

本目录自带一套完整的"任务完成 / 需要你操作 → 微信提醒"工具，纯标准库实现，无需安装任何依赖。

## 文件

| 文件 | 作用 |
|------|------|
| `notify.py` | 多通道推送：`clawbot`(复用本机 WorkBuddy 微信绑定) / `wecom_webhook` / `wecom_app` / `serverchan` / `wxpusher` / `pushplus` / `dingtalk` / `feishu` / 自定义 `webhook` |
| `hook.py` | 挂在 AI 助手 hook 上：`Stop`→已完成，`Notification`(idle_prompt)→在等你回复，`PermissionRequest`→需要你确认（默认只推高风险），`UserPromptSubmit`→只记会话标题、永不推送 |
| `watchdog.py` | 盯普通长任务进程：退出→完成/失败；长时间静默或弹窗→"在等你操作" |
| `install_hooks.py` | 安装/卸载 `~/.workbuddy/settings.json` 里的 hook（4 条，自动备份、只动自己加的那几条） |
| `demo_task.py` | 假任务，用来验证效果 |
| `selftest.py` | 自检：危险命令判定 / 会话标题 / 推送去重 / 消息格式 |
| `config.json` | 实际生效的推送配置（通道按顺序推；`enabled:false` 即停用） |
| `config.example.json` | 推送配置模板 |

### 通知长什么样

```
✅ 任务完成
🖥 MY-PC
💬 我的项目
📝 已让通知自动读取你重命名后的会话名。

⏸️ 等你回复
🖥 MY-PC
💬 我的项目

🔐 需确认
🖥 MY-PC
💬 我的项目
```

- **Stop 消息**：标题带会话名，`📝` 一行是**本轮完成概要**（取 `last_assistant_message`，
  默认裁到 40 字，`--stop-summary-chars` 可调）。
- 其余事件：标题带会话名，正文按需补一行。样式**只用 Emoji**（微信/企微改不了字体颜色）。

**会话标题怎么来的**（按优先级）：
1. `~/.workbuddy/workbuddy.db` 的 `sessions` 表：`custom_title`（**你在 UI 里重命名的名字**）
   > `title`（自动生成的标题）。hook 用 `mode=ro` 只读查询，改完名**自动跟随**，无需手动同步。
2. 回退：`UserPromptSubmit` 时记下的"本会话第一句提问"（存 `.hook_state.json` 的 `titles[session_id]`）。
3. 再回退：项目文件夹名。

> 历史：早期版本因 hook payload 没有标题字段，只能拿"首句提问"当标题，改名后完全不知道。
> 后来发现 WorkBuddy 把标题（含 `custom_title`）存在 `workbuddy.db` 的 `sessions` 表里，
> 于是改为直接读库 → **改名自动识别**。`--set-title` 仍保留，但一般不再需要。

## 常用命令

```bash
PY="python"
DIR="~/.workbuddy/skills/wechat-notify"

# 测一条测试消息
"$PY" "$DIR/notify.py" --test

# 看会走哪些通道（不真发）
"$PY" "$DIR/notify.py" --test --dry-run

# 模拟 AI 干完活 / 在等你回复
"$PY" "$DIR/hook.py" --test-stop
"$PY" "$DIR/hook.py" --test-notify

# 监控一条长命令：10 分钟没动静就提醒
"$PY" "$DIR/watchdog.py" --label "训练A" --log train.log -- python train.py

# 附加到已运行的进程
"$PY" "$DIR/watchdog.py" --label "下载" --log dl.log --pid 12345

# 卸载 hook
"$PY" "$DIR/install_hooks.py" --uninstall
```

## 关键事实（别踩坑）

- 微信**个人号没有官方推送 API**，任何"个人微信 API"都是协议逆向、有封号风险。可用的官方通道只有：企业微信群机器人/自建应用消息、微信服务号模板消息。
- 本机已绑定 WorkBuddy 的微信 ClawBot，`notify.py` 无需配置即可用它。**实测消息落在用户的「个人微信聊天列表」里**（和普通联系人会话一样有红点提醒），不是只进 WorkBuddy 小程序。但限制很硬：
  **只对活跃会话可推（24h 内用户给 bot 发过消息）、单会话约 10 条、账号级约 7 条/5 分钟**。
  报 `ret=-2 prepare failed` 或 `getconfig ret=-4 GetTypingTicket rpc failed` = 会话不活跃 →
  **让用户在微信里给 clawbot（不是 WorkBuddy 小程序）发任意一条消息即可刷新**，不是登录失效。
- **⚠️ 编码坑（已修，别再踩）**：Windows 上 hook 的 stdin 走 bash 管道，Python 会按 **GBK** 解码
  UTF-8 的 JSON payload，解不出的字节变成孤立 surrogate（U+DC80–DCFF），随后
  `json.dumps(...).encode()` 抛 `UnicodeEncodeError: surrogates not allowed`，
  表现为**「任务完成」提醒静默失败**（只在 `hook.log` 里留下"推送异常"）。
  修法：**必须 `sys.stdin.buffer.read()` 拿原始字节再 `.decode("utf-8")`**，
  绝不能直接 `sys.stdin.read()`；另在 `sanitize_text()` / `notify.sanitize()` 里做兜底清洗。
  新增通道代码时要沿用这个约定。
- **stdin 读不到 payload 时不要默认成 `Stop`**，否则会误报一条"任务完成"；直接返回 0。
- `settings.json` 里的 hook 命令由 **bash** 执行，路径必须「正斜杠 + 双引号」，否则反斜杠会被当转义符吃掉导致静默失败。
- hook 脚本默认不向 stdout 输出任何内容，避免干扰 hook 的 JSON 约定；日志写在脚本同目录 `hook.log`。
- 节流状态在 `.hook_state.json`，推不出去时不要误以为被节流 —— 先看 `hook.log`。
- **推送去重（重要）**：同一个权限请求会同时触发 `PermissionRequest` 和
  `Notification`，两者都用 **`attention`** 这一个节流分组键，所以只会推一条。
  别把节流键改回 `last_{event}`，否则会重复推送、白耗微信配额。
  分组键：`stop` / `idle` / `attention` / `notice`，默认 60s。
- **危险命令判定收紧了**：`rm -rf <项目里的临时目录>` 是 AI 的日常清理，**不再报警**；
  只有目标是 根目录/家目录/上一级/通配符/`C:/Windows`/`/usr` 之类，或命令本身是灾难性的
  （`diskpart`/`format`/`git push --force`/`DROP TABLE`…）才推。改判定逻辑跑 `selftest.py` 回归。
- **ClawBot 配额很容易撞**：实测一个会话里发 6~7 条就返回 `ret=-2`。
  区分方法：`getconfig` 返回 `ret=0` + `typing_ticket` ⇒ 会话**是活的**，`ret=-2` 就是限流/配额；
  `getconfig` 返回 `ret=-4` 才是会话过期。所以长期用必须配第二条通道（WxPusher / 企微）。
- `hook_payload.jsonl` 会记录每次 hook 的 payload 结构（长字符串截断到 80 字、超 128KB 自动清空），
  用来排查"某字段从哪来"。不需要可以删掉，会自动重建。
- **多通道是"任一成功即算通知到了"**：只要有一条通道成功就更新节流状态。
  别改成 `all(...)` —— 一条通道被限流（clawbot 天天限）会让节流永不生效，反而刷屏。
  日志会写 `已推送(1/2 通道成功…)` 这种部分成功。
- **当前通道组合**：`clawbot` + `wxpusher`（WxPusher 走个人微信聊天列表，免费额度大，
  是 clawbot 限流时的兜底）。真实 SPT 只写在运行目录的 `config.json` 里，别往工作区/仓库里带。
- **和 WorkBuddy 手机 App 的关系**：官方 App（iOS/Android/鸿蒙）自带「任务完成提醒」，
  对"WorkBuddy 任务跑完"这件事它更原生。但 App **只认 WorkBuddy 自己的会话**，
  管不了你电脑上别的长任务（编译/训练/下载），也没有"只在危险操作时才叫我"的过滤，
  且提醒落在 App 通知而不是微信。要装 App 就把本 skill 的 hook 卸掉，别让同一件事响两遍。
