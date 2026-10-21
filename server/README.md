# 服务器端部署说明

目标系统：Linux + Docker + Docker Compose。下面的路径保持和原机一致，systemd 和 compose 的相对路径就不用改。

## 1. 放置文件

```bash
sudo cp -r server/llbot          /opt/llbot
sudo cp -r server/llbot-watchdog /opt/llbot-watchdog
sudo cp -r server/memwatch       /opt/memwatch
sudo cp -r server/maibot         /opt/maibot
sudo cp -r server/panel          /opt/qqbot-panel
sudo chown -R root:root /opt/llbot /opt/llbot-watchdog /opt/memwatch /opt/maibot /opt/qqbot-panel
```

创建共享网络：

```bash
docker network create napcat_default 2>/dev/null || true
```

## 2. LLBot（OneBot 11 + PMHQ hook）

```bash
cd /opt/llbot
cp .env.example .env
nano .env
```

`.env` 至少填：

```text
BOT_QQ=你的机器人QQ
PMHQ_AUTH_TOKEN=从 https://auth.luckylillia.com 绑定QQ后拿到的token
```

`llbot_config/auth_token.txt` 也要填同一个 token（LLBot 本体登录校验用）。

把 `llbot_config/config.example.json` 改名为 `config_<你的QQ>.json`（例如 `config_123456789.json`）。WebUI 首次密码写在 `llbot_config/webui_token.txt`，先改成自己的密码。

```bash
sudo chmod -R 777 /opt/llbot/llbot_config
sudo docker compose up -d
sudo docker logs -f llbot-pmhq
```

- 日志出现二维码后，用**机器人 QQ**扫码。
- WebUI：`http://服务器IP:3080`
- OneBot：容器内 `llbot:3001`，MaiBot 通过 `napcat_default` 网络访问。
- 已登录过可把旧机的 `llbot_config/qq-session-<QQ>.json` 放进 `llbot_config/`，减少扫码。

如果只想本机访问 WebUI，把 compose 的端口改成：

```yaml
- "127.0.0.1:3080:3080"
```

## 3. memwatch（事件 / 情绪 / 好感度 / 经验 / 精力）

```bash
cd /opt/memwatch
cp .env.example .env
nano .env
```

填：

```text
BOT_QQ=你的机器人QQ
DEEPSEEK_KEY=你的 DeepSeek Key
```

```bash
sudo docker compose up -d --build
sudo docker logs -f memwatch
```

它会连：

```text
ws://llbot:3001
```

数据在 `/opt/memwatch/data`，第一次启动会自动建。想保留旧机的记忆，把旧机的 `data/` 复制过来再启动。

## 4. MaiBot

```bash
cd /opt/maibot
nano docker-config/mmc/model_config.toml
```

把 `CHANGE_ME_DEEPSEEK_KEY`、`CHANGE_ME_SILICONFLOW_KEY` 换成真实 Key。`bot_config.toml` 是人设和聊天风格，按需改；如果机器人 QQ 不是 `CHANGE_ME_BOT_QQ`，把里面的 `qq_account` 和 `alias_names` 一起改掉。

```bash
sudo docker compose up -d --build
sudo docker logs -f maibot-core
```

- `patch/` 里是对 MaiBot 的回复和数据库补丁，compose 会构建 `maibot-custom:latest`。
- SnowLuma 适配器代码在 `data/MaiMBot/plugins/MaiBot-SnowLuma-Adapter`，已经包含 LLBot 识别补丁。
- 适配器配置在 `data/MaiMBot/plugins/MaiBot-SnowLuma-Adapter/config.toml`，`client_type = "napcat"`，LLBot 会按 NapCat 画像跑。
- WebUI：`http://127.0.0.1:18001`（compose 里是 18001:8001）。

## 5. 手机 / 网页面板

```bash
cd /opt/qqbot-panel
python3 -m pip install -r requirements.txt
sudo cp qqbot-panel.service /etc/systemd/system/qqbot-panel.service
sudo nano /etc/systemd/system/qqbot-panel.service
```

把 `PANEL_PASSWORD=CHANGE_ME_PANEL_PASSWORD` 改成自己的密码，确认 `ExecStart` 路径是：

```text
/opt/qqbot-panel/napcat_desktop.py
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now qqbot-panel
sudo systemctl status qqbot-panel
```

访问：

```text
http://服务器IP:8090/
```

面板是 `PANEL_SERVER=1` 模式，LLBot/MaiBot 页面走同源 `/llbot`、`/maibot` 反向代理，不需要额外开放 3080/18001。

## 6. LLBot 看门狗（可选）

```bash
sudo cp /opt/llbot-watchdog/llbot-watchdog.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now llbot-watchdog
```

它只负责“容器没跑时拉起”，被踢或停在扫码页时只写 `status.json`，不会反复重启换二维码。

## 7. 端口

| 端口 | 用途 | 建议 |
|---|---|---|
| 3001 | OneBot 11（容器内） | 不暴露公网 |
| 3080 | LLBot WebUI | 只开 127.0.0.1 或安全组限制 |
| 18001 | MaiBot WebUI | 只开 127.0.0.1 |
| 8090 | 手机面板 | 公网只放这个，必须改密码 |

## 8. 数据备份

```bash
sudo tar czf ~/qqbot-data-$(date +%m%d).tar.gz \
  /opt/memwatch/data \
  /opt/maibot/data/MaiMBot \
  /opt/llbot/llbot_config
```

不要把备份提交到公开仓库。
