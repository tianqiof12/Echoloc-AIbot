# 发布说明

打包成两个附件：**服务器端**（Linux，Docker）和**桌面端**（Windows，pywebview 面板）。

两个包都只包含配置模板与源码，**不含**运行数据、日志、聊天记录、账号会话和真实密钥。

## 下载

| 包 | 适用 | 解压后 |
| --- | --- | --- |
| `echoloc-server-v1.0.0.tar.gz` | Linux 服务器 | `echoloc-server-v1.0.0/`（`server/` + `docs/`） |
| `echoloc-desktop-v1.0.0.zip` | Windows 10/11 | `echoloc-desktop-v1.0.0/`（`desktop/`） |

## 服务器端

```bash
tar xzf echoloc-server-v1.0.0.tar.gz
cd echoloc-server-v1.0.0

sudo cp -r server/llbot          /opt/llbot
sudo cp -r server/llbot-watchdog /opt/llbot-watchdog
sudo cp -r server/memwatch       /opt/memwatch
sudo cp -r server/maibot         /opt/maibot
sudo cp -r server/panel          /opt/qqbot-panel
```

然后按 [`docs/usage.md`](usage.md) 走：先起 LLBot（网络由它创建）→ MaiBot → memwatch → panel。所有 `CHANGE_ME_*` 在部署前必须替换，清单见 [`docs/secrets.md`](secrets.md)。

## 桌面端

桌面端是**遥控器**，不是本地运行环境：它通过 SSH 隧道连服务器上的 LLBot（3080）和麦麦（18001），把两个 WebUI 嵌在面板里。所以服务器端必须已经在跑。

```powershell
# 1. 依赖（Python 3.12，安装时勾 Add to PATH）
cd desktop
python -m pip install -r requirements.txt

# 2. 配置
copy config.example.json config.json
notepad config.json
```

`config.json` 里填：

| 键 | 填什么 |
| --- | --- |
| `server` | 服务器地址或域名 |
| `ssh_user` | SSH 用户名 |
| `ssh_key` | **私钥文件路径**，如 `C:/Users/you/.ssh/id_ed25519` |
| `ssh_port` | 一般 22 |
| `llbot_port` | LLBot WebUI 端口，默认 3080 |
| `llbot_webui_token` | LLBot WebUI 密码（在服务器 `llbot_config/webui_token.txt`） |
| `maibot_port` | 麦麦 WebUI 端口，默认 18001 |

```powershell
# 3. 启动
start_desktop.cmd
```

也可以用环境变量代替 `config.json`：`QQBOT_SERVER`、`QQBOT_SSH_USER`、`QQBOT_SSH_KEY` 等，键名就是上面表格里的大写形式。

### 连不上时先看这两个字段

面板状态接口返回的 `tunnel_ok` 和 `maibot_ok`，是**真发 HTTP 请求**探测的结果。

**别去看本地端口有没有 LISTENING**——SSH 隧道会静默断掉，本地端口却还亮着，这是这个面板历史上最容易误判的坑。

## 校验和

```
2f4ac0fa1a115c917143dccef24e294d9c9f38a76ceae99183ffba7ea4483e5f  echoloc-desktop-v1.0.0.zip
2cd9f9b5029abc447fa009e44c7526fc8495f2c2940b6899d8d9ea1bb5db9eda  echoloc-server-v1.0.0.tar.gz
```

Windows 上核对：

```powershell
Get-FileHash .\echoloc-desktop-v1.0.0.zip -Algorithm SHA256
```

## 已验证

- 两个包解包后文件完整，`server/` 88 个文件、`desktop/` 12 个文件
- 包内所有 Python 文件通过 `py_compile`
- 桌面端隧道目标在服务器上确认在监听：`3080`（LLBot）、`18001`（麦麦）、`8090`（面板）
- 打包基于 git 已提交内容（`git archive`），自动排除 `.env`、真实 `config_*.json`、日志与数据库

**未验证**：GUI 窗口实际渲染。打包环境是命令行，没开图形界面；`pywebview` / WebView2 的兼容性请在自己的 Windows 上确认。

## 版本

- `v1.0.0` —— 首个整理版：服务器三件套 + 面板 + 桌面端 + 文档

后续改动走 tag，两个包一起重新打，版本号对齐。
