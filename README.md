# wechat-notify 📱

**AI 助手任务完成 / 卡住 / 需要你确认时，自动推送一条消息到你的微信（或企微 / 钉钉 / 飞书）。**

挂在你正在用的 AI 编程助手（WorkBuddy / Claude Code）的 hook 上，让"跑完了通知我"、"卡住了叫我"变成一件可靠的小事：你去干别的，手机会震。

> 纯 Python 标准库实现，**零第三方依赖**，Windows / macOS / Linux 均可。

---

## 它解决什么问题

用 AI 助手跑长任务时最常见的两种煎熬：

- 任务跑完了，你不知道，回来发现早就结束，白等半小时；
- 任务卡在等你点"允许 / 确认"，你没注意，它就一直停在那儿。

本项目把这两件事变成**手机上的系统级推送**：

| 事件 | 含义 | 你会收到 |
|---|---|---|
| `Stop` | 对话回合结束 = 干完活了 | `✅ 任务完成：<会话名>` + `📝 <本轮概要>` |
| `Notification`(idle) | 空闲等你回复 | `⏸️ 等你回复：<会话名>` |
| `Notification`(permission) / `PermissionRequest` | 要你点确认（默认只推高危操作） | `🔐 需确认：<会话名>` |
| 其他通知 | 其他提醒 | `👀 需要你看一眼：<会话名>` |

## 特性

- 🎯 **事件驱动**：由 AI 助手自己的 hook 主动上报"我停了 / 我在等你"，比在外面猜进程状态准得多
- 📱 **9 种推送通道任意组合**，多通道同时发，任一成功即算送达
- 🚦 **分组节流防刷屏**：任务完成组只挡 10 秒内的瞬时重复，绝不吞掉真正的完成通知；等待/确认组 60 秒防骚扰
- 🧪 **测试消息与真实通知完全隔离**：手动测试不污染节流状态（踩过坑后专门修的）
- 📝 **会话名自动跟随 UI 重命名**：直接读 WorkBuddy 数据库的 `custom_title`，你改了名通知就跟着变
- 🛡 **高危才打扰**：`rm -rf /`、写系统目录才推送；AI 日常清理临时目录不会吵你
- ⏱ **watchdog**：盯任意长命令 / 已在跑的进程，超时没动静就提醒
- 🧵 **硬超时保护**：hook 永远在几秒内返回，绝不拖慢 AI 助手；任何异常静默吞掉

## 快速开始（WorkBuddy 用户）

```bash
# 1) 把整个目录复制到技能目录
cp -r wechat-notify ~/.workbuddy/skills/wechat-notify

# 2) 生成配置：复制模板，填一个你有的通道
cd ~/.workbuddy/skills/wechat-notify
cp config.example.json config.json   # 然后编辑，见下文"推送通道"

# 3) 安装 hook（自动写入 ~/.workbuddy/settings.json，会先备份）
python install_hooks.py

# 4) 发一条真实的测试消息，手机收到即成功
python hook.py --test-stop
```

卸载：`python install_hooks.py --uninstall`（只删自己加的条目，不动其他 hook）。

## 快速开始（Claude Code 用户）

把目录克隆到任意位置，然后在 Claude Code 的 hooks 配置里加上：

```json
{
  "hooks": {
    "Stop": [
      { "hooks": [ { "type": "command", "command": "python /path/to/wechat-notify/hook.py --hook Stop" } ] }
    ],
    "Notification": [
      { "hooks": [ { "type": "command", "command": "python /path/to/wechat-notify/hook.py --hook Notification" } ] }
    ],
    "PermissionRequest": [
      { "hooks": [ { "type": "command", "command": "python /path/to/wechat-notify/hook.py --hook PermissionRequest" } ] }
    ],
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "command": "python /path/to/wechat-notify/hook.py --hook UserPromptSubmit" } ] }
    ]
  }
}
```

`UserPromptSubmit` 只用来记录会话标题（每会话第一句提问），永不推送。

## 推送通道

在 `config.json` 的 `channels` 数组里启用任意一个或多个，会**同时发送**（任一成功即算通知到达）：

| type | 通道 | 落点 | 备注 |
|---|---|---|---|
| `wecom_webhook` | 企业微信群机器人 Webhook | 企微群，**真·系统推送（锁屏横幅+响铃）** | 免费不限量，推荐主通道。须用「自定义机器人」（群设置→消息推送→添加） |
| `wecom_app` | 企业微信自建应用消息 | 经"微信插件"直达个人微信 | 2022-06 后创建的应用需配企业可信 IP |
| `serverchan` | Server酱 | 个人微信（服务号） | 免费约 5 条/天 |
| `wxpusher` | WxPusher（SPT） | 微信服务号对话 | 免费额度大，但消息埋在服务号会话里 |
| `pushplus` | PushPlus | 个人微信（公众号） | 免费约 200 条/天 |
| `dingtalk` | 钉钉群机器人 | 钉钉群，系统推送 | |
| `feishu` | 飞书群机器人 | 飞书群，系统推送 | |
| `clawbot` | WorkBuddy 微信 ClawBot | 个人微信聊天列表 | WorkBuddy 专属；有单会话/账号级限流 |
| `webhook` | 任意自定义 Webhook | 你自己的服务 | POST 一段 JSON |

配置示例（`config.json`）：

```json
{
  "timeout": 6,
  "ignore_env_proxy": true,
  "channels": [
    {
      "type": "wecom_webhook",
      "enabled": true,
      "webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=把这里换成你自己的key"
    }
  ]
}
```

- 所有字段说明、各通道所需参数，见 **`config.example.json`** 内注释。
- 配置查找顺序：`--config 参数` > 环境变量 `WECHAT_NOTIFY_CONFIG` > 脚本同目录 `config.json` > `~/.wechat-notify/config.json`。

## 命令参考

```bash
python hook.py --test-stop            # 模拟一次"任务完成"真实推送（与节流完全隔离）
python hook.py --test-notify          # 模拟一次"等你回复"真实推送
python hook.py --test-stop --dry-run  # 只打印消息样式，不发送
python hook.py --hook Stop < payload.json   # hook 实际调用方式
python selftest.py                    # 回归自检：危险命令判定/标题/去重/消息格式

python notify.py -t "标题" -d "正文"          # 单独发一条
python notify.py --test                       # 按配置发一条测试消息
python notify.py -t "T" -d "D" --only wxpusher  # 只用指定通道

python watchdog.py -- python long_task.py     # 盯一条长命令
python watchdog.py --pid 1234                 # 盯已在跑的进程
python watchdog.py --name python --idle 600   # 按进程名找，10 分钟没动静提醒
```

常用 `hook.py` 参数：

| 参数 | 说明 |
|---|---|
| `--dry-run` | 只打印消息，不发送 |
| `--verbose` | 调试信息打到 stderr |
| `--stop-summary-chars N` | 完成概要最多取多少字（默认 40） |
| `--min-interval S` | 各分组节流兜底默认秒数（默认 60，完成组内置 10s） |
| `--all-permissions` | 所有权限请求都推送（默认仅高危） |
| `--set-title "名字"` | 手动设定当前会话显示名（一般用不着，已支持自动跟随改名） |

## 消息样式

```
✅ 任务完成：我的项目
📝 已把部署脚本跑通，测试 12/12 通过。

⏸️ 等你回复：我的项目
📁 my-project

🔐 需确认：我的项目
🛠 工具：Bash
⚠️ 原因：危险命令：rm -rf /
💻 命令：rm -rf /
```

**会话名从哪来**（按优先级）：
1. `~/.workbuddy/workbuddy.db` 的 `sessions` 表：`custom_title`（你在 UI 里改的名）> `title`（自动生成）——以**只读**方式查询，改完名自动跟随；
2. 本会话第一句提问（`UserPromptSubmit` 时记录）；
3. 项目文件夹名。

## 高危操作判定

`PermissionRequest` / `Notification`(permission) 默认**只推真正危险的操作**，避免每次写文件都骚扰你：

- 本身灾难性的命令：`format`、`mkfs`、`dd if=`、`shutdown`、fork 炸弹、`git push --force`、`git reset --hard`、`drop table` 等；
- 带递归/强制标记、且目标是**根目录 / 家目录 / 通配符 / 系统目录**的删除（`rm -rf <项目临时目录>` 属于 AI 的日常清理，**不会**报警）；
- 写入 `Windows`、`Program Files`、`Desktop/Documents/Downloads` 根等敏感位置；
- 带 `delete` / `recursive` 标记的结构化写操作。

想全量推送加 `--all-permissions`。

## 安全说明

- ⚠️ `config.json` 含你的推送凭据（webhook key / token / SPT），**已在 `.gitignore` 排除，切勿提交或分享**。对外分享请用 `config.example.json` 模板。
- hook 对 WorkBuddy 数据库只做**只读**查询（SQLite `mode=ro`），不写入、不阻塞。
- 消息内容只包含会话名、项目文件夹名与事件本身；`PermissionRequest` 会带上工具名和命令（最多 300 字），介意的话可自行精简 `build_message()`。
- 所有推送在几秒内超时返回，失败只记日志（`hook.log`），绝不抛错给 AI 助手。

## 常见问题

**收不到消息？**
1. 先 `python notify.py --test` 验证通道本身；
2. 企微机器人必须是「自定义机器人」（群设置 → 消息推送 → 添加），"智能机器人"没有 Webhook；
3. 看 `hook.log`：显示 `节流跳过` 是防刷屏（真实完成组仅 10 秒）；显示 `已推送` 但手机没弹，去系统设置里检查对应 App 的通知权限（横幅/锁屏/声音）；
4. 企微接口返回 `{"errcode":0}` 即成功。

**推送会拖慢 AI 吗？**
不会。hook 全程带硬超时（默认 6s，网络再差也强制返回），异常全部静默吞掉。

**改了会话名，通知还是旧名？**
正常情况会自动跟随（读数据库）。若你的环境读不到数据库，可用兜底：`python hook.py --set-title "新名字"`。

**可以只在"跑完"时提醒，不要"等你回复"？**
可以。卸载时用 `install_hooks.py --uninstall` 后只重装需要的 hook；或直接在 settings.json 里删掉对应事件条目。

## 项目结构

```
wechat-notify/
├── hook.py              # 核心：挂 AI 助手 hook，事件 → 消息 → 推送（含高危判定/节流/标题）
├── notify.py            # 多通道推送库（9 通道，纯标准库），可独立使用
├── watchdog.py          # 进程/命令监视器：超时无动静提醒
├── install_hooks.py     # 安装/卸载 WorkBuddy hooks（自动备份）
├── selftest.py          # 回归自检
├── demo_task.py         # 演示任务
├── config.example.json  # 配置模板（9 通道字段说明）
└── SKILL.md             # WorkBuddy 技能格式说明
```

## License

[MIT](LICENSE)
