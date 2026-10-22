# 更新日志

本项目在服务器上的实际变更记录。按日期倒序，只记「做了什么、为什么、怎么回退」，不含服务器地址、密钥与账号信息。

类型沿用 Keep a Changelog：`新增` / `变更` / `修复` / `性能` / `运维` / `回滚`。

---

## 2026-10-20

### 新增

- **私聊与群聊对等**：会话 key 规则（群 = 群号，私聊 = `p<QQ>`）；好感度、精力、会话提示对私聊一视同仁
- **会话设置页**：按会话设置推理档位（`off` / `low` / `high` / `max`）与精力系数（每千 token 扣减）
- **禁言状态**：处理 `notice.group_ban`，写 `g["muted"]`，面板顶部红横幅 + 会话标签加 🔇
- **困惑默认联网查证**：查到写 `results`，查不到也写 `checked=True`，避免反复重试烧额度
- **自动补搜**：每 120 秒补 2 条（新→旧），带搜索预算预留
- **经验库六分类**：规则先行，判不出的交模型兜底
- **命中 URL 抓正文**：只抓搜索第一条，6 秒超时、800 字、缓存 1 天、2 并发

### 变更

- 面板五页共用「全部 / 群聊 / 私聊」筛选；人物档案页有独立的筛选状态
- Token 统计 SQL 同时取 `group_id` / `user_id`，私聊单列，未归属归入「系统任务」
- 精力曲线改为时间轴：按 `ts` 定位横坐标，不再按索引均匀铺开
- compose 文件名统一为 `compose.yaml`（早先叫 `memory_compose.yaml`，迁移后只剩前者）

### 修复

- **搜索预算留口子**：新增预留 12 次/小时给实时查证，补搜最多用 48 次；没额度时不写 `checked`，下轮还能补
- 补搜后按规则跑一次经验分类（不烧模型、不把没想清的塞进「其他」）；每小时再兜底一次完整分类

### 性能

- 图片任务节流：`job_poll_interval_seconds` 2→10、`job_batch_size` 4→1、`job_max_retries` 5→1
- 视觉超时：`[model_task_config.vlm] hard_timeout` 240→90
- 容器内存：`maibot-core` `mem_limit` 900m→1024m
- 宿主加固：加 2G swap + `vm.swappiness` 0→30，fstab 持久化
- 效果：planner 平均 8.8s→3.3s、最长 128s→5.0s；摘要触发 45 次/10min→12 次/5min

### 回滚（三次，别再踩）

| 尝试 | 结果 | 教训 |
| --- | --- | --- |
| `planner` 换 `deepseek-v4-flash` | **彻底丧失 tool calling**，连续 24 次不返回 `reply`/`wait`，bot 静默一个多小时 | `planner` 必须留 `deepseek-flash-vl` |
| provider `timeout` 120→75 | 制造 75~85 秒超时重试，平均耗时反而从 1.2s/1.9s 涨到 24s/11.7s | 这个 timeout 别往下压 |
| `vlm` 换 `Qwen/Qwen3-VL-30B-A3B-Instruct` | 单图 0.7s（比原来快 6 倍），但账号余额不足直接 402 | 充值后已恢复，是目前的最优解 |

### 运维

- SiliconFlow 余额到账后恢复三个开关：`use_vector_expression`、`paragraph_vector_backfill.enabled`、`vlm` 指向 `qwen3-vl-30b`（`planner` 不动）
- 存储上限重设：`prompt_imgs 400 / emoji 800 / images 400 / maibot_logs 800 / qq_emoji 600`（原 200/800/100/500/400，images 一直超限拉黄条）
- 面板重启后 SSH 隧道可能没建起来（只有界面端口在听，`/api/status` 报 `tunnel_ok:false`），**再重启一次**即可，别只等 watchdog

### 已知问题

- **服务器假死**（20:20 起）：SSH 卡在 `Connection timed out during banner exchange`，宝塔面板 HTTP 也超时，但 ping 正常——内核活着、用户态无响应。怀疑与 2G swap + 容器内存上限提高有关，或 `maibot-core` 内存泄漏
- **`maibot-core` 内存疑似泄漏**：40 分钟从 381MB 涨到 926MB，被 cgroup OOM 杀过一次（`RestartCount=1`）

---

## 待办

- [ ] 长期撑住低精力群：会话设置里把档位降到 `off`，或等自然恢复（τ=4h）
- [ ] `use_new_send_logic`（planner 直接看图挑表情）尚未开启，属行为大改，继续观察
- [ ] 面板「精力」页视觉需肉眼确认（曲线逻辑有单测，服务端 HTML 已含新代码）
- [ ] 面板进程当天被反复重启（端口一直变），确认是崩溃自启还是人为
- [ ] 「按 API 余额主动开口」功能未实现

---

## 踩过的坑

- **时区**：记忆服务必须 `TZ=Asia/Shanghai`，否则精力会一次性扣到 0
- **并发**：可能有别的会话同时改同一批文件，改前先读、改后 `py_compile` 验证
- **compose 文件名**：服务器上是 `compose.yaml`，别再写 `memory_compose.yaml`
- **临时目录不跨调用**：每次 shell 是新的，`/tmp/xxx` 在两次调用间会消失
- **提示词幂等**：改一次配置就触发一轮热重载，必须逐字比对后再写
- **删除下标倒序**：prompt 删除必须从大到小，否则索引错位、删不干净
- **禁言 notice**：只在发生时推送，历史不会补；查真实剩余时间用 `get_group_member_info`
- **分类关键词**：别把「什么意思」这类问句词当特征词，会把所有条目归成一类
- **分类「其他」是终点**：补搜期间调用必须传 `fallback_other=False`
- **iframe 登录**：LLBot 前端只认 Cookie，不解析 `?token=`，做不到自动登录
- **pywebview 点不动**：UIA 点击看着成功但视图不切，改用语 `/api/*` 直接验证或人工点

---

> 更完整的技术背景见 `AGENTS.md`（长期约定，权威）与 `HANDOFF.md`（交接细节，含拓扑）。
