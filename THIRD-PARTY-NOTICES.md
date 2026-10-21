# 第三方组件声明

本仓库包含两类东西：**本项目自己写的代码**，和**随仓库一起分发的第三方组件**。本文档说明后者——它们是什么、从哪来、什么许可、有没有改动。

维护原则：**拿不准的许可，宁可写"以官方仓库为准"，也不猜。** 每项都附来源地址，方便你自己核对。

---

## 一、随仓库分发的第三方源码

### MaiBot-SnowLuma-Adapter v1.1.7

| 项 | 内容 |
| --- | --- |
| 位置 | `server/maibot/data/MaiMBot/plugins/MaiBot-SnowLuma-Adapter/` |
| 用途 | QQ 适配器，让 MaiBot 通过 SnowLuma / NapCat 等客户端收发消息 |
| 作者 | MaiBot Team |
| 上游仓库 | https://github.com/Mai-with-u/MaiBot-SnowLuma-Adapter |
| 许可 | **GPL-3.0-or-later**（依据组件自带 `_manifest.json` 的 `license` 字段） |
| 许可证全文 | 同目录下的 `COPYING`（GPL-3.0 原文） |
| 是否改动 | **可能有**。目录里存在 `profile.py.bak.llbot` 等本地适配备份，说明这份副本曾被改过；本仓库已排除备份文件，但无法保证其余文件与上游逐字一致。要严格核对请和上游对应版本做 diff。 |

这个目录是**独立的第三方作品**，不受本仓库其他文件许可的影响。它按 GPL 原样附带完整源码分发；如果你要修改后再分发，需遵守 GPL-3.0 的条款（保留版权声明、提供源码、同许可授权衍生作品）。

`_manifest.json` 里另声明了一个 Python 依赖 `silk-python >= 0.2.8`，不在本仓库内，请自行安装并核对其许可。

---

## 二、运行时依赖（不随仓库分发）

这些通过 `docker pull` 或服务调用获得，仓库里只有配置和调用代码。

| 组件 | 获取方式 | 用途 | 许可 |
| --- | --- | --- | --- |
| **LLBot** | `docker pull linyuchen/llbot:latest` | OneBot 11 协议端，负责 QQ 登录与收发 | 上游为 LLOneBot 项目（https://github.com/LLOneBot/LLOneBot）。镜像与仓库内均未附许可证文件，**以官方仓库为准** |
| **PMHQ** | `docker pull linyuchen/pmhq:latest` | LLBot 的 hook 辅助进程 | 同上，**以官方发布为准** |
| **MaiBot** | `docker pull sengokucola/maibot:latest` | 对话核心：planner、表情包、工具调用 | 上游为 MaiBot 项目（https://github.com/Mai-with-u/MaiBot）。本仓库只提供两个覆盖补丁，见下节，**主程序许可以官方仓库为准** |

### 本仓库对 MaiBot 的两个补丁

`server/maibot/patch/` 下的 `reply.py` 与 `database.py` 是**覆盖上游同名文件**的修改版（Dockerfile 里 `COPY` 到 `/MaiMBot/src/...`）。

这两个文件是上游代码的衍生作品，**其许可随上游**。上游若是 GPL/AGPL 一类 copyleft 许可，这两个补丁文件也受同一许可约束，不能单独按其他许可再分发。

### 外部服务

| 服务 | 用途 | 说明 |
| --- | --- | --- |
| DeepSeek API | 对话、情绪判定、困惑时联网查证 | 服务条款见 https://platform.deepseek.com 。本仓库不含 Key |
| PMHQ 授权站点 | LLBot 登录用的 token | 第三方服务，地址见 `server/llbot/README.md`，可用性自负 |
| GitHub | 代码托管 | — |

---

## 三、Python 依赖

`memwatch`、`panel`、`desktop` 的依赖清单在各目录的 `requirements.txt`。主要几项：

| 包 | 用途 | 许可 |
| --- | --- | --- |
| `requests` | HTTP | Apache-2.0 |
| `websocket-client` | OneBot WebSocket | Apache-2.0 |
| `trafilatura` | 网页正文抽取 | Apache-2.0 |
| `paramiko` | SSH 隧道（桌面端） | LGPL-2.1 |
| `pywebview` | 桌面窗口 | BSD-3-Clause |
| `python-docx` | 生成项目文档 | MIT |

以上取自当前环境 `pip show` 的 License 字段。版本升级可能改变许可，部署前请以各包元数据为准。

---

## 四、商标与免责

- **QQ、腾讯** 是腾讯公司的商标。本项目与腾讯无任何关联，不是官方客户端。
- 使用非官方客户端自动化登录 QQ 存在**账号被风控、限制或封禁**的风险，尤其是用主号。
- 本项目按现状提供，不附带任何担保。因使用导致的账号、数据、费用损失由使用者自行承担。

---

## 五、本项目自身代码

**尚未选定开源许可证**，因此默认按「保留所有权利」理解。想复用、二改或商用，请先开 issue 说明用途。

如果之后加上许可证（例如 MIT），注意上面第一节的 SnowLuma 目录与第二节的 MaiBot 补丁是例外——它们的许可不随本仓库的许可改变。

---

## 六、怎么更新这份文件

新增或升级第三方组件时，按同样格式补一条：位置、用途、上游地址、许可、是否改动。许可字段只填**组件自己声明的**（manifest / LICENSE 文件 / 包元数据），查不到就写「以官方仓库为准」，不要凭印象填。
