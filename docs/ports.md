# 端口与网络

| 端口 | 服务 | 监听建议 |
|---|---|---|
| 22 | SSH | 仅自己的 IP |
| 3001 | LLBot OneBot 11 | Docker 内网，不映射公网 |
| 3080 | LLBot WebUI | 127.0.0.1 或安全组限制 |
| 18001 | MaiBot WebUI | 127.0.0.1 |
| 8090 | 手机 / 网页管理面板 | 公网只放这一个 |
| 6099 | 旧 NapCat WebUI | 本包不使用 |

## Docker 网络

```text
napcat_default
```

- `llbot` 容器在该网络里有别名 `napcat`，兼容旧配置 `ws://napcat:3001`。
- `memwatch` 实际连 `ws://llbot:3001`。
- `maibot-core` 加入同一个网络，通过 `napcat:3001` 连 LLBot。

## 桌面端隧道

```text
本机 16099 -> 服务器 127.0.0.1:3080（LLBot）
本机 16097 -> 服务器 127.0.0.1:18001（MaiBot）
```

`/api/status` 的 `tunnel_ok` / `maibot_ok` 是实际 HTTP 探测，不是端口占用检测。
