# 使用说明

从零把「回声」跑起来。按顺序做，别跳步——这套东西的依赖链是单向的。

> 所有命令里的 `/opt/xxx` 都是本包的默认约定路径。你改路径就必须同步改 `compose.yaml` 和 systemd 单元里的交叉引用，否则容器之间会找不到网络。

## 0. 你会得到什么

| 组件 | 跑起来之后 | 没有它的后果 |
| --- | --- | --- |
| LLBot | QQ 上下线、收发消息 | 什么都收不到 |
| MaiBot | 会说话 | 收到消息但不回 |
| memwatch | 会记住、会忘、会累 | 能聊，但每轮都是陌生人 |
| panel | 能看见她在想什么 | 一切正常，只是全黑箱 |

最小可用是前三个。面板强烈建议装——记忆、好感、精力全是抽象数字，没有面板等于蒙眼调参。

## 1. 前置条件

- Linux 服务器，Docker + Docker Compose v2
- 一个专门用来当机器人的 QQ 号（**别用主号**）
- DeepSeek API Key（对话、情绪判定、困惑查证都用它）
- Python 3.10+（只有桌面面板和抓正文脚本需要）

内存参考：MaiBot 约 1G、memwatch 300M、LLBot 视镜像而定。1 核 2G 能跑，但会紧。

## 2. 一次性准备

### 2.1 放目录

```bash
sudo cp -r server/llbot          /opt/llbot
sudo cp -r server/llbot-watchdog /opt/llbot-watchdog
sudo cp -r server/memwatch       /opt/memwatch
sudo cp -r server/maibot         /opt/maibot
sudo cp -r server/panel          /opt/qqbot-panel
```

`memwatch` 和 `maibot` 的 compose 里都写着 `../maibot/data/MaiMBot` 这种相对挂载——所以这两个目录必须保持是**兄弟目录**，别单独挪走其中一个。

### 2.2 写 `.env`

`server/memwatch/compose.yaml` 用 `${DEEPSEEK_KEY}` 和 `${BOT_QQ}` 取环境变量，缺了直接报错（这是故意的，比静默拿空 Key 去请求好）。

```bash
cd /opt/memwatch
cp .env.example .env
# 编辑 .env：
#   BOT_QQ=你的机器人QQ
#   DEEPSEEK_KEY=sk-...
```

`/opt/llbot/.env` 同理，填 `BOT_QQ` 和 `PMHQ_AUTH_TOKEN`。

### 2.3 替换所有 `CHANGE_ME_*`

完整清单见 [secrets.md](secrets.md)。至少这些必须改：

| 占位符 | 位置 | 说明 |
| --- | --- | --- |
| `CHANGE_ME_BOT_QQ` | 各处 `.env`、`maibot/.../bot_config.toml` | 机器人 QQ，`qq_account` 和 `alias_names` 一起改 |
| `CHANGE_ME_GROUP_ID` | `bot_config.toml` 的 `item_id` | 要说话的群号，有多个就都改 |
| `CHANGE_ME_DEEPSEEK_KEY` | `memwatch/.env`、`maibot/.../model_config.toml` | 对话和查证都用它 |
| `CHANGE_ME_SILICONFLOW_KEY` | `maibot/.../model_config.toml` | 视觉/表情包那条链路用；不用可留空 |
| `CHANGE_ME_PMHQ_AUTH_TOKEN` | `llbot/.env` + `llbot_config/auth_token.txt` | LLBot 登录校验，两处要同一个值 |
| `CHANGE_ME_PANEL_PASSWORD` | `panel/qqbot-panel.service` | 手机面板 Basic Auth |

另外把 `llbot_config/config.example.json` 改名成 `config_<你的QQ>.json`。

## 3. 启动顺序

**先 LLBot**，因为 `napcat_default` 网络是它建的，另外两个容器都挂在上面。

### 3.1 LLBot

```bash
cd /opt/llbot
sudo chmod -R 777 llbot_config
sudo docker compose up -d
sudo docker logs -f llbot-pmhq
```

首次要扫码。扫码入口在 PMHQ 的 WebUI（默认 `3080`），登进去后按它的引导绑定。登录成功后注意看日志里最后一条是不是 `Login`。

### 3.2 MaiBot

```bash
cd /opt/maibot
sudo docker compose up -d
sudo docker logs -f maibot-core
```

它会读 `docker-config/mmc/bot_config.toml`（人设、群号、表情包规则）和 `model_config.toml`（模型与 Key）。这两个文件都支持热重载，改完不用重启。

### 3.3 memwatch

```bash
cd /opt/memwatch
sudo docker compose up -d --build
sudo docker logs -f memwatch
```

第一次启动会建 `data/groups/`。日志里应该看到连上 `ws://llbot:3001`。

### 3.4 面板

```bash
sudo cp /opt/qqbot-panel/qqbot-panel.service /etc/systemd/system/
# 先改里面的 CHANGE_ME_PANEL_PASSWORD
sudo systemctl daemon-reload
sudo systemctl enable --now qqbot-panel
```

浏览器开 `http://<服务器IP>:8090`。**安全组只放行 8090**，3080 / 18001 不要对公网开放。

## 4. 验证

跑通的最小证据，一条条对照：

| 检查 | 怎么看 |
| --- | --- |
| LLBot 在线 | 面板顶部无扫码提示；容器日志最后一条是 `Login` |
| 麦麦在回话 | 群里 @ 它一句，30 秒内有回复 |
| memwatch 收到事件 | 面板「事件记忆」出现新条目 |
| 好感度生成 | 面板「人物档案」里能看到说话的人，分数不是空的 |
| 精力在动 | 面板「精力」页数值低于 100，且 24h 曲线在攒点 |
| Token 统计 | 面板「Token」页有数，说明 `MaiBot.db` 挂对了 |

如果「精力」一直是 100 不动，九成是 `TZ` 没设成 `Asia/Shanghai`，或者 `MAIBOT_DB` 挂载路径错了。

## 5. 桌面端 / 手机端

**桌面端**（Windows，适合自己盯着看）：

```powershell
cd desktop
python -m pip install -r requirements.txt
copy config.example.json config.json
# 填 server / ssh_user / ssh_key / 端口
start_desktop.cmd
```

它通过 SSH 隧道连服务器，本地端口每次启动随机。**页面空白先看状态接口的 `tunnel_ok` / `maibot_ok`，别去看端口有没有 LISTENING**——隧道会静默断掉，端口却还亮着。

**手机端**：直接用浏览器开 `http://<服务器IP>:8090`，LLBot 和 MaiBot 的 WebUI 都嵌在同一端口下，不需要额外开洞。

## 6. 日常运营

常用的几个旋钮（都在 `memwatch/compose.yaml` 的环境变量里，改完 `docker compose up -d` 生效）：

| 变量 | 当前值 | 调它会怎样 |
| --- | --- | --- |
| `HALF_LIFE_HOURS` | 18 | 调小 = 忘得更快 |
| `FORGET_WEIGHT` | 2.0 | 乘在衰减指数上，2.0 ≈ 半衰期再砍一半 |
| `FORGET_ACT` | 0.15 | 低于它就「想不起来了」 |
| `FORGET_PURGE` | 1 | 1 = 遗忘即删除；改 0 会留档到 `forgotten` |
| `ENERGY_TOKEN_COST` | 0.045 | 每千 token 扣多少精力，直接决定她多快累 |
| `CONFUSION_SEARCH` | 1 | 困惑时先联网查证再决定问不问人 |

备份就是拷目录：

```bash
cp -r /opt/memwatch/data ~/memwatch-backup-$(date +%m%d)
```

**改数据前先备份**。好感度和精力都在这些 JSON 里，改坏了没有 undo。

## 7. 常见故障

| 现象 | 先查什么 |
| --- | --- |
| 群里完全没反应 | LLBot 是否在线；`BOT_QQ` 和 `config_<QQ>.json` 文件名是否对得上 |
| 收到消息不回 | `maibot-core` 日志里有没有报错；模型 Key 是否欠费 |
| 面板空白 | `/api/status` 的 `tunnel_ok`、`maibot_ok`（不是端口） |
| 精力不消耗 | `MAIBOT_DB` 挂载路径；`TZ` 是否 `Asia/Shanghai` |
| 精力一直是 100 | 同上；新会话的 `tok_ts` 初始化问题 |
| 记忆不衰减 | `decay_loop` 是否在跑；`memwatch` 日志有没有异常 |
| 面板提示被禁言 | 真的被禁言了；或 `get_group_member_info` 里的 `shut_up_timestamp` 还没到 |
| 两个号互相踢 | 同一个 QQ 同时被 NapCat 和 LLBot 登录了，停掉其中一个 |
| 提交代码后配置丢了 | 你八成把 `.env` 或真实 `config_*.json` 提交上去了，它们本该在 `.gitignore` 里 |

## 8. 成本

精力系统的消耗读的是**真实 token 用量**，所以「她聊得多」等于「你在花钱」，这不是比喻。

控制成本的三处，按性价比排序：

1. 精力档位本身——低档会要求一句说完、不发表情包、不调工具，省 token 最直接
2. `EFFORT`（`off` / `low` / `high` / `max`）——推理档位，`low` 和 `max` 差价很大
3. 会话提示的开关——兴趣、疑问、精力三类提示每轮都会进上下文

别为了省这点钱去爬网页版模型：工具调用、结构化输出、视觉理解、token 用量会一起消失，换完不是同一个 bot。
