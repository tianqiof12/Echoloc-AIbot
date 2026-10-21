# 从旧机迁移

## 只迁代码（推荐先跑通）

按 `server/README.md` 部署，所有数据从零开始。适合换服务器、重新养号。

## 连记忆和好感度一起迁

停服务后复制：

```bash
sudo tar czf /tmp/qqbot-data.tar.gz \
  /opt/memwatch/data \
  /opt/maibot/data/MaiMBot \
  /opt/llbot/llbot_config
```

新机解压到对应路径，再启动 compose。至少保留：

```text
/opt/memwatch/data/groups/*.json
/opt/memwatch/data/experiences.json
/opt/memwatch/data/fetch_cache.json
/opt/maibot/data/MaiMBot/MaiBot.db
```

## 少扫一次码

旧机：

```text
/opt/llbot/llbot_config/qq-session-<QQ>.json
```

复制到新机同路径，重启 LLBot。会话文件是敏感凭据，不要公开。

## 迁移后检查

```bash
docker ps
curl -s http://127.0.0.1:8090/api/status
```

再用手机面板看：

- LLBot 在线
- MaiBot 已连接
- 群消息能收发
- 事件/经验数据存在

## 不要同时登录

NapCat 和 LLBot 登录同一个 QQ 会互踢。迁移时先停旧协议端，再起新的。
