# QQbot 项目约定（公开脱敏版）

> 本文档是本地 `AGENTS.md` 的公开副本，机制与口径完全一致，只把下列内容换成了占位符：
> `<YOUR_SERVER_IP>` 服务器地址、`<REDACTED>` 各类密码、`<GROUP_ID>` / `p<QQ>` 真实群号与 QQ 号。
> 想照着自己部署一份，把这些占位符替换成你的值即可。

## 好感度：一个 QQ 号只有一个分数

一个人同时待两个群，不能有两副面孔。所以 `affinity[uid].score` 在所有群里必须一致。

**口径**（`memory_service.sync_affinity()`）

- 合并方式：按各群 `events`（互动次数）**加权平均**，不是相加——相加的话群越多分越高。
- 统一 `score`，同时把 `decayed` 对齐到同一时刻，之后各群用同一公式衰减，不会自己漂开。
- `pos` / `sad` / `events` **保持各群独立**：那是群内活跃度，不是态度，合并会污染统计。
- 只在单群出现的人不参与计算，分数不动。

**自动时机**

1. 每次 `make_event()` 落盘之后（事件只落一个群，靠这步摊平）；
2. `decay_loop()` 每小时兜底一轮（新进群、手改数据、历史偏差都靠它收敛）。

**新人进群**：第一次在某群出现时，用 `global_affinity_score(uid, exclude_gid=gid)` 继承别处已攒下的分数，不从 50 重新认识一遍。

**手动对齐**（改过数据、或发现两边不一致时）

```bash
ssh admin@<YOUR_SERVER_IP> 'sudo docker exec memwatch python -c "import memory_service as m; print(m.sync_affinity())"'
```

**动手前先备份**：`cp /opt/memwatch/data/groups/*.json ~/affinity-backup-$(date +%m%d%H%M)/`（历史上确实改过这块，`/opt/memwatch/backup_affinity` 里有更早的备份）。

## 其它已有机制（改之前先读源码顶部注释）

- **OneBot 连接**：memwatch 连 `ws://llbot:3001`（`compose.yaml` 的 `NAPCAT_WS_URL`）。跑 OneBot 的容器早先叫 napcat，现在是 `llbot`，`napcat` 只是它在 `napcat_default` 网络里的**别名**——别再把连接地址写死成 napcat，别名哪天清掉就静默断线。环境变量名保留 `NAPCAT_WS_URL` 是为了兼容代码，值写 llbot。
- 事件情绪走 **Emotion2Skill**：情绪决定技能（困惑→换词追问、期待→主动探索、嫌弃→避让话题）。
- 困惑（confusion）不扣好感，兴趣按 `|v|` 正向驱动，并把想问的话挂到 `topic.pending`，再由 `sync_group_interests()` 写进麦麦的群会话提示。
- 群友档案页只读 memwatch 的 `/opt/memwatch/data/groups/*.json`，不再读 `bot/profiles.json`（那是已停掉的 hongji-bot 写的）。
- 代码改动后部署：`scp memory_service.py admin@<YOUR_SERVER_IP>:/tmp/` → `sudo cp /tmp/memory_service.py /opt/memwatch/src/` → `cd /opt/memwatch && sudo docker compose up -d --build memwatch`。
- 面板（`napcat_desktop.py`）的 NapCat / MaiBot 两个 iframe 靠本地 SSH 隧道（16099 / 16097）。**隧道会静默断掉**（日志 `Secsh channel ... Connection refused`、`10054 远程主机强迫关闭`），本地端口仍在 LISTENING，所以旧版状态检查会误报「已连接」。现在 `Tunnel` 自带 `_ensure_transport()` 按需重连 + 20 秒 watchdog + `set_keepalive(30)`，状态里的 `tunnel_ok` / `maibot_ok` 改成真发 HTTP 请求探测。再遇到页面空白，先看 `/api/status` 这两个字段，别看端口。
- **发言频率（按精力）**：基础 `talk_value` / `private_talk_value = 0.5`（每 2 条触发一次）；memwatch 的 `sync_talk_frequency()` 每 60 秒按各会话精力写 `talk_value_rules`：≥70→0.8、40–70→0.6、18–40→0.4、6–18→0.2、<6→0；只写变化，状态在 `data/talk_frequency.json`，接口会自动打开 `enable_talk_value_rules`。被 @/点名走强制触发，不受阈值限制；精力 <6 时静默。
- **llbot 看门狗**：`/opt/llbot-watchdog/llbot_watchdog.py` 按日志最后一条 Login/Kick/扫码判状态（online / need_qr / kicked / down），容器没跑才自动拉起；被踢/待扫码只写 `status.json`。面板顶部 `#llbot-banner` 每 30 秒轮询 `/api/llbot_status`，`need_qr` 时常驻提示扫码，点按钮直达 LLBot 面板。

## 记忆网络：激活度与遗忘

三个公开模型叠着用，公式只取主干（`memory_service.py` 里 `event_activation / note_activation / person_activation`）：

- **艾宾浩斯**：`recency = 0.5 ** (Δt小时 / stability)`；`stability` 由间隔重复抬高（同话题重现 ×1.5）。
- **ACT-R base-level**：`B_i = ln(Σ t_j^-d)`，练习越多越好想；这里用 `weight`（含每次重现 +0.2）当练习累积的代理。
- **Generative Agents**：检索分 = recency × importance × relevance；relevance 用事件在记忆网络里的连边数（fan）近似，`1 + 0.12·ln(1+n)`。

合起来：`act = min(1, weight/1.5) × 0.5^(Δt/stability × FORGET_WEIGHT) × fan`。

- 阈值：compose 实际用 `HALF_LIFE_HOURS=18`、`FORGET_ACT=0.15`（想不起来了，不画也不参与检索）、`FADE_ACT=0.35`（开始变淡）、`PRUNE_THRESHOLD=0.15`；代码默认是 24h / 0.06 / 0.22。
- **失忆权重** `FORGET_WEIGHT`：默认 1.0，compose 现为 **2.0**；乘在事件/印象的艾宾浩斯指数和 `decay_all()` 的 weight 衰减上（2.0 ≈ 半衰期再砍一半），不碰好感度和人物在场感。
- 落盘：`decay_all()` 每小时给事件写 `act`、给人写 `affinity[uid].act`、给印象写 `profiles[name].note_act`；面板直接读字段，前端不重复算公式。
- 遗忘：默认把 `weight`/`act` 掉到底的事件挪进 `g["forgotten"]` 留档；compose 设 `FORGET_PURGE=1` 时直接删除，不再写 `forgotten`（当前就是真删）。
- 面板：节点半径 ×(0.72+0.28·act)、透明度 ×(0.25+0.75·act)，低于 `FORGET_ACT` 直接不画，工具栏统计里显示 `淡忘 N`。
- **群实时心情**：每个会话一份 `g["mood"]`，事件只带自己的情绪；群心情用 `_blend_group_mood()` 按 `alpha=0.12+0.22×intensity`（0.08~0.30）吸收新事件，旧的先按 `MOOD_HALF_LIFE_HOURS=6` 衰减，所以不会被单条事件带跑。`compute_group_emotion()` 优先读这份持久化心情，画像、精力因子、面板全用它；没有旧数据的群由 `decay_all()` 用近期事件重心初始化。群和私聊各自隔离。
- **话题遗忘**：`topic_activation = min(1, |interest|/INTEREST_THRESHOLD)`；compose 用 `TOPIC_FORGET_ACT=0.05`、`TOPIC_FADE_ACT=0.15`、`TOPIC_FORGET_MIN_AGE_H=1`、`FORGET_PURGE=1`。低于阈值且距最后活跃 ≥1 小时的话题直接删除（关闭 `FORGET_PURGE` 才挪进 `g["forgotten_topics"]` 留档），不再进画像/兴趣提示；面板「话题兴趣」按会话折叠、超过 10 条默认收起。注意 `topic.last` 是衰减时间，面板的「最后活跃」读 `topic.updated`。
- 想立刻看遗忘效果：调 `FORGET_WEIGHT` / `FORGET_ACT` / `FADE_ACT`（compose 环境变量），不用改代码。
- **缩放/平移**：网络图用 SVG `viewBox` 做视口（`netVB`），滚轮以鼠标位置为锚点缩放、空白处拖动平移、双击复位；工具栏 `＋ / － / 适应 / 100%` 按钮，`netZoomFit()` 按节点包围盒自动装箱。缩放只改 `viewBox` 属性、不重绘节点，所以 200+ 节点也不卡。节点自带的拖拽（改 `node.x/y` 后重绘）和画布平移互不干扰——`pointerdown` 里先判断点在不在 `.net-node` 上。
- **正性记忆增益**：`POSITIVE_MEMORY_GAIN = {joy: 1.35, trust: 1.15, anticipation: 1.10}`，在 `emotion_weight()` 里乘上去（Pollyanna 效应：好事记得更牢）。它**只影响 `weight`**——也就是记多深、遗忘快慢、话题兴趣增量——不碰好感度（那用 `v`/`intensity`）、也不碰当下心情。别直接改 `EMOTIONS[key]["base"]`，base 表示情绪本身的显著性，两者混在一起就调不干净了；上限仍是 1.8。
- 历史事件补权重：跑一次性脚本，对 `emotion`（老数据看 `mood == "positive"`）按上表乘一遍并打 `gain_applied` 标记，写完跑一次 `decay_all()` 重算 `act`。


## 存储容量预警

面板每 5 分钟通过一条 SSH 命令采一次数（`df -P /` + 5 个目录的 `du -sm`），结果在 `Api.get_storage()` 里缓存 55 秒。

- 监控目录在 `napcat_desktop.py` 的 `STORAGE_DIRS`：`prompt_imgs` 200MB、`emoji` 800MB、`images` 100MB、`maibot_logs` 500MB、`qq_emoji` 400MB。
- 磁盘：`disk_warn=80%` 拉黄条，`disk_crit=90%` 拉红条；目录超上限黄、超 120% 红。
- 阈值存在本机 `ui_settings.json` 的 `storage` 字段（和 `theme` 共存，保存时合并、不会互相覆盖），面板「运行状态 → 预警阈值」可直接改，改完立即清缓存重采。
- 触发时：`#storage-banner` 固定在各页顶部（可点 × 本次隐藏），同时 `napcat_desktop.log` 打印一行 `[存储] ...`。
- 新增/调整监控目录只改 `STORAGE_DIRS` 即可，`STORAGE_DEFAULTS` 会自动带上默认上限。
- 注意：`df` 走宿主，`/opt/napcat` 是 root 所有，所以命令用 `sudo sh -c`，依赖 admin 的免密 sudo。

## 精力系统（每群一份）

`memory_service.py` 里，精力值存在 `g["energy"]`，`energy_loop()` 每 60 秒结算：

- **消耗（主）**：读 MaiBot 的真实用量库（compose 里把 `/opt/maibot/data/MaiMBot` 只读挂到 `/maibot`，`MAIBOT_DB=/maibot/MaiBot.db`），按 `llm_usage ⋈ chat_sessions` 取该群增量 token，`cost = ENERGY_TOKEN_COST × 千token × 心情因子`（compose 全局默认 `0.045`，代码默认 `0.03`）；面板「会话设置」可按群/私聊覆盖 `energy_cost`，没设就回落到全局。
- **心情因子**：`1 - 0.35×valence + 0.10×arousal`（心情好省电，低落/激动更费），夹在 0.6~1.6。
- **恢复**：一阶趋近 `recover = (100-E) × (1 - e^(-Δt/τ))`，`τ=4h`，夜里（22:00-08:00）`τ=2h`。
- **档位**：≥70 精神不错 / ≥40 有点累 / ≥18 很累 / ≥6 快没电 / <6 没电。
- **说话频率/效率**：档位变化时把 `【精力】…` 写进该群会话提示（复用 `sync_group_interests` 的 session-prompt 机制，标记 `【精力】`，值按 5 点取整防抖）；低档明确「少说话／只回点名」，并要求一句说完、不发表情包、不查资料/不调用工具，用更短的回复省 token。
- **见底提示**：`< 6` 且距上次 `drained_at` 超过 6 小时，就用 WS `send_msg` 在群里说一句（`ENERGY_EMPTY_WORDS` 可改），避免刷屏。
- **面板**：左侧导航「精力」是独立页（每群一张卡：数值/档位/上次消耗恢复/心情因子/累计 token/24h 曲线），运行状态页保留一条精简版；都是 60 秒刷新。
- **曲线**：`g["energy"]["history"]` 每 10 分钟记一点 `{ts, v}`，最多 144 点（24h），面板画 sparkline；不足两点时显示「曲线还在攒」。
- **新会话初始化**：`energy_of()` 首次建 `energy` 时把 `tok_ts` 往前挪 2 秒；否则 `update_energy_all()` 的 `dt < 1s` 保护会一直 `continue`，新群/私聊永远写不进精力字段，面板看起来「停摆」。
- **坑**：memwatch 必须设 `TZ=Asia/Shanghai`。DB 里的 `timestamp` 是本地时间字符串，容器跑 UTC 会把「从某时刻起」算成整个历史，一次把精力扣到 0。

## 会话提示同步（【我的兴趣】/【没弄懂的事】/【精力】）

`sync_group_interests()` 把这三类提示写进麦麦的群会话（`/api/chat/sessions/{sid}/prompts`），三个要点：

1. **幂等**：先算好这轮应该有哪些提示，和服务器上已有的逐字比对，一致就整组跳过。每次 PUT/DELETE 都会改写 `bot_config.toml` 并触发一轮配置热重载（还会清空模型客户端缓存），所以「没变就别写」很重要。
2. **倒序删除**：删旧提示必须先收齐下标、从大到小删，边遍历边删会让后面的索引错位，删不干净——之前就是这么攒出重复的【精力】条目的。
3. **文件锁**：`/data/sync_prompts.lock` + `flock` 非阻塞，两个进程（容器主循环 + `docker exec` 手动跑）同时同步时后来的直接跳过，避免删写交错又产生重复。

## 人物档案（群 + 私聊对等）

「群友」已泛化成「人物」：群聊和私聊在数据层完全对等，一份会话一份档案。

- **key 规则**（`conv_key()`）：群 = 纯群号 `<GROUP_ID>`；私聊 = `p` + QQ 号，如 `p<QQ>`。文件名同 key，如 `p<QQ>.json`。
- `on_message()` 同时接受 `message_type` 为 `group` 和 `private`；私聊里自己发的消息照样过滤。
- `make_event()` 会给私聊换措辞（「只有我和对方两个人」），`members` 名字对不上时**兜底**取唯一对话人的 uid——否则私聊永远建不出好感度条目。
- **精力**：`_energy_tokens_since()` 对私聊走 `chat_sessions.user_id + group_id IS NULL`，群走 `group_id`；见底提示按 key 前缀决定发群消息还是私聊消息。
- **困惑时联网查证**：`make_event()` 遇到 `confusion` 且模型给了 `question` 时，会调 `web_search()`（DeepSeek `tools/search`，复用同一个 Key，锁外请求），把前 3 条 `title/snippet` 存进 `topic.pending_search`；`sync_group_interests()` 拼出的【没弄懂的事】会带上「我刚联网查了下：…」，让她按查到的理解、对不上再问。**这就是默认处理办法**：`compose.yaml` 里显式写了 `CONFUSION_SEARCH=1`，困惑一律先查证再决定要不要问人；查到写 `results`，查不到也写 `checked=true`（提示词会变成「查过一遍没查到，别硬编」），免得反复重试。限流 `CONFUSION_SEARCH_MAX_PER_HOUR=60`（全局滑动窗口），`CONFUSION_SEARCH=0` 可整体关掉；失败静默、退回「回头自己查」。历史 pending 由 `backfill_loop()` 自动补：启动 90 秒后，每 `CONFUSION_BACKFILL_INTERVAL=120` 秒补 `CONFUSION_BACKFILL_LIMIT=2` 条（新→旧），补完调一次 `sync_group_interests()` 刷新提示；搜索预算 `CONFUSION_SEARCH_MAX_PER_HOUR` 默认 60。50 多条积压大约 50 分钟跑完，日志带 `[补搜]` 前缀。
- **会话提示**：`sync_group_interests()` 的 session 索引同时收 `group_id` 和 `user_id`（私聊 key 是 `p<uid>`），兴趣/疑问/精力三张提示对私聊一视同仁。
- **面板**：导航「人物档案」，各页用 `convLabel(gid,obj)` 出标签——群显示 `群 <号>`，私聊显示 `私聊 <昵称>`（昵称从 `affinity` 或 `profiles` 里取，取不到退化 `QQ <号>`）。精力、好感度、事件、记忆网络都按同一个标签并列展示。
- **好感度跨会话统一**：`sync_affinity()` 按 uid 聚合，群和私聊里同一个人也是同一个分数。
- **页面筛选**：事件记忆 / 话题兴趣 / 记忆网络 / 好感度 / 精力五页共用一套「全部 / 群聊 / 私聊」标签（全局变量 `convFilter` + `scopeMatch()` + `setConvFilter()`，切换后会重载当前页）。精力页只筛**卡片列表**，运行状态页那条精简概览始终显示全部会话。人物档案页有自己独立的 `profFilter`（互不干扰），因为那边是按会话分组的表格，语义不同。
- **档案筛选**：人物档案页顶部有「全部 / 群聊 / 私聊」三个标签（`profFilter` + `setProfFilter()`），按 key 是否以 `p` 开头过滤，20 秒自动刷新时保持当前筛选；统计行会跟着显示「N 个群 / 个私聊 / 个会话」。
- **Token 统计**：私聊名字优先取 memwatch 的 `p<uid>.json` 档案（affinity → profiles），没有的话退回麦麦 `person_info` 表（platform=qq），再没有才显示 `QQ <uid>`。
- **Token 统计**：`token_stats.py` 的 SQL 现在同时取 `group_id` 和 `user_id`——有群号算群、只有 user_id 算私聊（key `p<uid>`）、都没有算「未归属/系统任务」，每行带 `kind` 字段。面板 Token 页多了一组「全部 / 群聊 / 私聊」标签（独立的 `tokScope`，因为 token 页是缓存数据、重渲不重新拉数），合计行按当前筛选重算。
- **禁言状态**：memwatch 的 `on_message()` 现在也处理 `notice.group_ban`（`sub_type=ban/lift_ban`，且 `user_id == BOT_ID`），写进 `g["muted"] = {until, at, duration, operator}`，`until=0` 表示未定期限。面板 60 秒轮询 `/api/memories`，顶部横幅提示「被禁言，剩 N 分钟」，同时 `convLabel()` 会给该会话加 🔇 前缀。查真实剩余时间可以用 WS 调 `get_group_member_info` 读 `shut_up_timestamp`。注意：llbot 只在 ban 发生时推 notice，历史禁言不会自动补，必要时手动写一次。
- **推理档位与精力系数**：「会话设置」页的 `/api/group_settings` 返回 `groups: [{id, kind, name}]`，群和私聊并列，各自设 `effort`（off/low/high/max）和 `energy_cost`（每 1000 token 扣多少精力，默认 0.045）；新增会话接受群号或 `p+QQ号`。memwatch 侧 `load_group_effort(key)` / `load_group_energy_cost(key)` 按 key 读，私聊直接生效；精力卡片会显示当前 `cost_rate`。

## 说话称呼

三条规则，写在 `bot_config.toml`（热重载生效）：

1. **多语境（多人同时说、几个话题交叉）** → 回谁的事就把谁的名字自然带进去锚住对象（像「阿毛那个方案我觉得…」），不用每句都带，也别每句都 @。
2. **跟同一个人连着聊一段** → 第一次叫一声就够，后面直接说事；一旦换人、换话题，再自然带一次名字（私聊同样适用，`private_chat_prompts` 里也写了换话题再叫）。
3. **只有被点名、被提问、需要区分到底在跟谁说话时** → 才用 @；@ 是提醒，不是标点。

`enable_reply_at` 保持 `true`：不是禁掉 @，是把「什么时候值得 @」交给她自己判断，一刀切关掉会导致被单独问话时也不理人。






## 经验库：分类与整理

`/opt/memwatch/data/experiences.json` 存全局经验（所有群/私聊共享，一对 `question → learned`）。
现在每条带 `category`，固定六类：**梗与暗号 / 人物关系 / 技术 / 常识科普 / 群规约定 / 其他**。

- **规则先行**：`guess_exp_category()` 只看内容词（暗号、谐音、token、全称…），**别把「什么意思」这种问句词当关键词**——早先这么写导致 13 条全归到「梗与暗号」。
- **兜底给模型**：规则判不出的 leftovers 批量丢给 `llm()`，只要求输出 `[{"i":0,"c":"梗与暗号"}]`，解析失败就落到「其他」。
- **入口**：`classify_experiences()`，启动后 20 秒自动跑一次，只处理没有 `category` 的条目；想重跑就先把 `category` 清掉再调。
- **面板**：「经验」页按类别分区展示（`expCardHtml` + `loadExperiences` 分组版），顶部有「共 N 条，已搞懂 M 条，分 K 类」，空类别不显示，没分类的归到「其他」。



## 任务请求：嘴硬但会做

`bot_config.toml` 里加了三处，热重载生效，别只改一处：

1. `personality.personality`：「懒是演的：真有人拜托合理的事，嘴上嫌麻烦、先损两句，但会真的去做并把结果给出来——傲娇不是不帮忙，是不肯好好说话地帮忙。」
2. `group_chat_prompt`：合理请求（查资料、解释、算数、看图、小忙）先答应再动手，嘴上可以损，结果要给；明显不合理或做不到的才推掉。
3. `private_chat_prompts`：同样口径的简版。

核心是**答应概率高 + 执行要落地**，傲娇只体现在语气上，不能变成不干活的借口。改完用
`/api/webui/config/bot/raw` 验收三处都在，再看 planner prompt 日志里有没有「先在心里答应」。



## 表情包：先看懂再发，权重靠提示词

麦麦（`maibot-core`）自带表情包链路，别自己造：

- 收图时用 VLM 打情绪标签（日志 `理解表情包情绪: 震惊,无语,...`），存进表情库；
- 回复时走 `send_emoji` 内置工具（容器内 `/MaiMBot/src/maisaka/builtin_tool/send_emoji.py`）——参数为空，内部把最近对话 + 候选表情拼图交给视觉子代理挑，所以「理解意思 + 用对语境」是现成的；
- 真正能调的是**频率**和**候选质量**，都在 `/opt/maibot/docker-config/mmc/bot_config.toml`：
  - `group_chat_prompt` 里那段表情包规则（现在的口径：情绪到位就发、先读对方表情再接、一次一个、连续两条不都带）；
  - `[emoji] emoji_send_num`（候选池，40）、`max_reg_num`（库存上限，512）、`steal_emoji = true`（自动从聊天里攒图）。
- 这个文件**热重载**（有 FileWatcher，约 1 秒生效），改完看日志 `[配置] 检测到配置文件变更，触发热重载` 即可，不用重启容器、不用重建镜像。
- 想确认提示词真的进了上下文：`grep -rl "情绪一到位" /MaiMBot/logs/maisaka_prompt/planner`。
- 改之前备份（脚本里已做）：`bot_config.toml.bak-<mmddHHMM>`。
- 还没动的大开关：`use_new_send_logic = true` 让 planner 直接看图挑（planner 已是 `deepseek-flash-vl`，技术上可行），属于行为大改，先用当前这版观察。

## 手机端（Android + 公网 8090）

- 服务器面板：`/opt/qqbot-panel/napcat_desktop.py`，systemd 服务 `qqbot-panel`，监听 `0.0.0.0:8090`，Basic Auth 默认 `admin / <REDACTED>`（在 `/etc/systemd/system/qqbot-panel.service` 的 Environment 里改，改完 `daemon-reload` + `restart`）。
- 运行模式：`PANEL_SERVER=1`，`Api` 用 `LocalRemote` 直接读写本机文件/执行 bash，不再走 SSH；LLBot 走同源 `/llbot/` -> `127.0.0.1:3080`，MaiBot 走同源 `/maibot/` -> `127.0.0.1:18001`，HTML/JS/CSS 和 `/api` 都会改写到对应前缀，WebSocket 也转发。
- 访问方式：阿里云安全组只放行 TCP 8090；手机开 `http://<YOUR_SERVER_IP>:8090/`，LLBot/MaiBot 页都在面板内走 8090，不需要再开 3080/18001；Tailscale 已停用（`tailscaled` disabled）。
- APK：`E:\QQbot\QQbotPanel.apk`，源码在 `E:\QQbot\android_app`；默认地址已填公网面板，首次启动直接加载，长按页面可改地址/账号。构建用 Android SDK commandline tools + `aapt2` / `javac` / `d8` / `zipalign` / `apksigner`，不依赖 Gradle。
- 安全：现在是 **HTTP + Basic Auth**，只有 8090 公网可见；LLBot 自己的 WebUI 密码是 `<REDACTED>`（可在 LLBot 面板改）。密码别用弱口令，最好后面套一层 HTTPS。

