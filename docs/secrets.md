# 密钥与占位符

包内所有敏感值都已替换，真实值请自己填。

| 占位符 | 位置 | 说明 |
|---|---|---|
| `CHANGE_ME_SERVER_IP` | `server/panel/napcat_desktop.py`、`desktop/napcat_desktop.py` | 服务器公网 IP |
| `CHANGE_ME_DEEPSEEK_KEY` | `server/memwatch/.env`、`server/maibot/docker-config/mmc/model_config.toml` | DeepSeek API Key |
| `CHANGE_ME_SILICONFLOW_KEY` | `server/maibot/docker-config/mmc/model_config.toml` | 硅基流动 Key（如不用可留空） |
| `CHANGE_ME_PMHQ_AUTH_TOKEN` | `server/llbot/.env` | LLBot/PMHQ hook 鉴权 token |
| `CHANGE_ME_LLBOT_AUTH_TOKEN` | `server/llbot/llbot_config/auth_token.txt` | LLBot 本体登录鉴权 token（通常与 PMHQ token 同一个） |
| `CHANGE_ME_PANEL_PASSWORD` | `server/panel/qqbot-panel.service` | 手机面板 Basic Auth 密码 |
| `CHANGE_ME_WEBUI_PASSWORD` | `server/llbot/llbot_config/webui_token.txt` | LLBot WebUI 密码 |
| `CHANGE_ME_MAIBOT_TOKEN` | `server/memwatch/src/memory_service.py` 默认值 | 建议删掉默认值，用 compose 环境变量 |
| `C:/Users/you/.ssh/id_ed25519` | `desktop/napcat_desktop.py`、`desktop/config.example.json` | 桌面端 SSH 私钥路径 |

## 原则

- 面板密码至少 12 位，不要用 `admin/admin`。
- 8090 是唯一建议公网开放的端口，最好再套 HTTPS。
- 不要把 `server/memwatch/data`、`server/maibot/data/MaiMBot`、`qq-session-*.json` 提交到公开仓库。
- `server/maibot/docker-config/mmc/bot_config.toml` 里可能有群聊提示词，公开前自己检查一遍。
