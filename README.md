# 回声 · Echoloc QQbot

> 一个会遗忘的 QQ 群聊记忆体。

大多数 QQ 机器人是无状态转发器：收到消息、拼提示词、调模型、发回去。每一轮都是全新的陌生人。

这个项目把力气放在被省略掉的那一层——**长期状态**。群聊被切成事件、情绪、话题和人物印象，每条记忆带激活度并随时间衰减；好感度、心情、精力都是会变化的持久值，而不是每轮重算的临时数。于是她会有很像人的几个毛病：记得上周聊过什么、隔两周只剩个印象；对某个人态度好，换到另一个群还是同样的态度；聊久了会累，累了就少说话；心情被一句话带跑之后会慢慢回正。

对话生成交给 [MaiBot](https://github.com/MaiMbot-Pro-Max/MaiBot)（项目里叫「麦麦」），QQ 接入交给 [LLBot](https://github.com/LLOneBot)：这一层负责的是**她怎么记住、怎么忘、累不累、想不想说话**。

## 有什么不一样

| | 普通 QQ bot | 回声 |
| --- | --- | --- |
| 记忆 | 上下文窗口，超了就没 | 每条记忆带激活度，按艾宾浩斯 × ACT-R × 检索打分衰减 |
| 遗忘 | 不存在 | 低于阈值真删除，话题也会消失 |
| 态度 | 每次重新判断 | 好感度按 QQ 号跨群统一，30 天半衰期 |
| 心情 | 单条消息决定 | 按强度混色 + 6 小时半衰期 |
| 精力 | 无 | 按真实 token 用量结算，直接改写发言频率 |
| 不懂的事 | 硬答 | 先联网查证，查不到才问人，经验进库 |

机制细节见 [docs/AGENTS.md](docs/AGENTS.md)（就是原项目的开发约定，公开脱敏版）。

## 目录

```
.
├─ server/                 Linux 服务器端
│  ├─ llbot/               LLBot + PMHQ（hook 模式，OneBot 11）
│  ├─ llbot-watchdog/      LLBot 掉线 / 待扫码看门狗
│  ├─ memwatch/            记忆服务：事件、情绪、好感度、精力、经验、困惑查证
│  ├─ maibot/              MaiBot 配置、补丁、SnowLuma 适配器
│  └─ panel/               手机/网页管理面板（systemd）
├─ desktop/                Windows 桌面端（pywebview + SSH 隧道）
├─ android/                Android WebView 壳说明
└─ docs/                   使用说明、端口表、迁移步骤、密钥占位符清单
```

## 快速开始

**完整步骤看 [docs/usage.md](docs/usage.md)**，这里只给最短路径：

```bash
# 1. 复制到服务器（保持路径不变，compose 和 systemd 都用相对路径）
sudo cp -r server/llbot          /opt/llbot
sudo cp -r server/llbot-watchdog /opt/llbot-watchdog
sudo cp -r server/memwatch       /opt/memwatch
sudo cp -r server/maibot         /opt/maibot
sudo cp -r server/panel          /opt/qqbot-panel

# 2. 填密钥（所有 CHANGE_ME_* 都是占位符）
#    server/llbot/.env、server/memwatch/.env 等，见 docs/secrets.md

# 3. 依次起：LLBot 扫码 → MaiBot → memwatch → panel
```

桌面端：

```powershell
cd desktop
python -m pip install -r requirements.txt
copy config.example.json config.json   # 填 server / ssh_key / 端口
start_desktop.cmd
```

## 部署前必须做的

1. **替换所有 `CHANGE_ME_*`**，尤其是 DeepSeek Key、PMHQ token、面板密码、SSH 私钥路径。清单在 [docs/secrets.md](docs/secrets.md)。
2. **`memwatch` 必须设 `TZ=Asia/Shanghai`**。库里时间戳是本地时间字符串，容器跑 UTC 会把「从某时刻起」算成整个历史，一次把精力扣到 0。
3. **不要同时启动 NapCat 和 LLBot 登录同一个 QQ**，会互相踢下线。这个包默认只带 LLBot。
4. 真实配置（`.env`、`config_<QQ>.json`、`auth_token.txt` 里的真值）不要提交回仓库。

## 组成与端口

| 组件 | 作用 | 端口 |
| --- | --- | --- |
| `llbot` | OneBot 11 协议端，PMHQ 有头模式 | WebUI 3080 / OneBot 3001 |
| `maibot` | 对话与回复，planner、表情包、工具调用 | WebUI 18001 |
| `memwatch` | 记忆服务，连 `ws://llbot:3001` | 无对外端口 |
| `panel` | `PANEL_SERVER=1`，手机/网页管理 | 8090 |
| `desktop` | Windows 端，走 SSH 隧道 | 本地随机端口 |

## 数据与迁移

本包**不含**运行数据、日志、聊天记录、账号会话和真实密钥。想接回原来的记忆，从旧机单独复制：

- `/opt/memwatch/data`（记忆、好感度、精力、经验）
- `/opt/maibot/data/MaiMBot`（至少 `MaiBot.db`，精力系统的 token 来源）
- `/opt/llbot/llbot_config/qq-session-<QQ>.json`（可选，能少扫一次码）

没有这些也能跑，只是她会从「刚认识你」开始。

## 许可

尚未选择许可证。在明确之前，默认按「保留所有权利」理解；要复用请先开 issue 说明用途。
