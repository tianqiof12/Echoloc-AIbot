# Windows 桌面端

目录里是 pywebview 管理面板，通过 SSH 隧道连接服务器上的 LLBot 和 MaiBot。

## 1. 环境

- Windows 10/11 x64
- Python 3.12（安装时勾选 Add to PATH）
- WebView2 Runtime（新版 Windows 一般自带）

```powershell
cd desktop
python -m pip install -r requirements.txt
```

## 2. 配置

```powershell
copy config.example.json config.json
notepad config.json
```

示例：

```json
{
  "server": "你的服务器IP",
  "ssh_user": "admin",
  "ssh_key": "C:/Users/你的用户名/.ssh/id_ed25519",
  "ssh_port": 22,
  "llbot_port": 3080,
  "llbot_webui_token": "",
  "maibot_port": 18001
}
```

要求：

- 服务器能用这个 SSH 私钥登录，且 `admin` 有免密 `sudo`（面板要读 `/opt` 数据）。
- 服务器安全组/防火墙允许你的 IP 访问 22。
- 面板本地端口：LLBot 16099，MaiBot 16097。

也可以用环境变量覆盖：

```text
QQBOT_SERVER
QQBOT_SSH_USER
QQBOT_SSH_KEY
QQBOT_LLBOT_PORT
QQBOT_MAIBOT_PORT
```

## 3. 启动

```powershell
start_desktop.cmd
```

或：

```powershell
python napcat_desktop.py
```

首次启动会建立 SSH 隧道：

```text
127.0.0.1:16099 -> 服务器:3080
127.0.0.1:16097 -> 服务器:18001
```

## 4. 页面

- **LLBot 面板**：内嵌 LLBot WebUI
- **运行状态 / 精力 / 人物档案 / 事件记忆 / 话题兴趣 / 记忆网络 / 经验 / Token 统计 / 好感度 / 会话设置 / MaiBot 面板**
- **设置**：深浅色主题

## 5. 抓取 CLI（可选）

`webcli.py` 是独立联网抓取工具：

```powershell
python webcli.py fetch https://example.com
python webcli.py batch urls.txt --out-dir pages
```

依赖 `trafilatura`；如果不想用，删掉这个文件不影响桌面端。

## 6. 常见问题

**页面空白**：先看 `/api/status` 里的 `tunnel_ok`、`maibot_ok`，不要只看端口是否 LISTENING。隧道断了会自动重连，20 秒 watchdog。

**pythonnet 报错**：确认 Python 是 64 位，WebView2 已安装；某些安全软件会拦截 `Python.Runtime.dll`，需要放行。

**LLBot 面板要求登录**：WebUI 密码在服务器 `/opt/llbot/llbot_config/webui_token.txt`，或首次启动时设置。
