# QQbot 便携整理包（2026-10-21）

这是从现有机器上整理出的**服务器端 + Windows 桌面端 + Android 壳**文件，不包含运行数据、日志、聊天记录、账号会话和真实密钥。

## 目录

```
QQbot_PORTABLE_20261021/
├─ server/                 Linux 服务器端
│  ├─ llbot/               LLBot + PMHQ（hook 模式）
│  ├─ llbot-watchdog/      LLBot 掉线看门狗
│  ├─ memwatch/            事件/情绪/记忆/经验服务
│  ├─ maibot/              MaiBot 主程序配置、补丁、SnowLuma 适配器
│  └─ panel/               手机/网页管理面板
├─ desktop/                Windows 桌面端（pywebview）
├─ android/                Android WebView 壳说明（APK 未打包，沿用现有文件）
└─ docs/                   端口、迁移、密钥说明
```

## 先看这里

- 所有 `CHANGE_ME_*` 都要替换成自己的值，尤其是 `DEEPSEEK_KEY`、`PMHQ_AUTH_TOKEN`、面板密码、SSH 私钥路径。
- **没有带** `data/`、`logs/`、`node_modules/`、`__pycache__`、QQ 登录会话，避免把私人聊天记录和账号凭据带出去。
- 想连原来的记忆/好感度数据，单独从旧机复制：
  - `/opt/memwatch/data`
  - `/opt/maibot/data/MaiMBot`（至少 `MaiBot.db`）
  - `/opt/llbot/llbot_config/qq-session-<QQ>.json`（可选，能少扫一次码）
- 详细步骤见 [服务器端说明](server/README.md)、[桌面端说明](desktop/README.md)。

## 快速路径

服务器：

```bash
# 把 server/ 下各目录放到原来的 /opt 路径，保持 systemd 和 compose 相对路径不变
sudo cp -r server/llbot /opt/llbot
sudo cp -r server/llbot-watchdog /opt/llbot-watchdog
sudo cp -r server/memwatch /opt/memwatch
sudo cp -r server/maibot /opt/maibot
sudo cp -r server/panel /opt/qqbot-panel
```

桌面：

```powershell
cd desktop
python -m pip install -r requirements.txt
copy config.example.json config.json
# 编辑 config.json：server / ssh_key / 端口
start_desktop.cmd
```

## 版本关系

- `llbot`：OneBot 11 协议端，`PMHQ` 有头模式，默认 WebUI 3080，OneBot 3001。
- `memwatch`：连 `ws://llbot:3001`，负责事件、情绪、好感度、经验、精力。
- `maibot`：聊天与回复，连 `napcat_default` 网络，配置在 `docker-config/mmc`。
- `panel`：`PANEL_SERVER=1` 模式，监听 8090，给手机/网页用。
- `desktop`：Windows 端通过 SSH 隧道连服务器，适合桌面管理。

> 不要同时启动 NapCat 和 LLBot 登录同一个 QQ，会互相踢下线。这个包默认只带 LLBot。
