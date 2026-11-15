# 炉石传说 AI 助手（macOS）

只读炉石客户端日志，用大模型给战术建议。**不注入进程、不模拟键鼠、不代替你出牌。**

```
Power.log ──► 状态还原 ──► 局面 JSON ──► 大模型 ──► 操作建议 ──► 终端 / 悬浮窗
```

---

## 快速开始

```bash
git clone <你的仓库地址> hearthstone-copilot
cd hearthstone-copilot

# 0. 安装（Python 3.10+，macOS）
python3 -m venv .venv
.venv/bin/pip install -e .

# 1. 填自己的私有配置：API Key / 玩家名 / 账号 ID
#    config.yaml 只是公开示例；local 文件已被 .gitignore 忽略
cp config/config.local.yaml.example config/config.local.yaml
#    或直接用环境变量：export HS_LLM_API_KEY=sk-xxx

# 2. 自检（日志、卡牌数据、模型配置、日志截断）
.venv/bin/hs-copilot doctor

# 3. 看当前局面
.venv/bin/hs-copilot board

# 4. 要战术建议
.venv/bin/hs-copilot advise

# 5. 实战：实时监听 + 悬浮窗
.venv/bin/hs-copilot gui
```

> 开源协议 MIT；使用前请阅读文末「开源与合规」。工具只读日志、不代替出牌，
> 但需要炉石客户端开启 `[Power]` 日志并解除 10MB 截断（详见下方「前置条件」）。

### 已实测可用的模型（2026-10 验证）

DeepSeek 接口在 `models.list()` 里只返回 `deepseek-flash` / `deepseek-v4-pro`，
但 `deepseek-chat` 实际可正常调用（历史别名仍有效），当前配置即用它。

---

## 命令

| 命令 | 作用 |
|---|---|
| `doctor` | 环境自检：日志、卡牌数据、日志截断、模型配置 |
| `board` | 打印当前局面（表格 + 场面 + 手牌），`--json` 输出结构化数据 |
| `advise` | 请求大模型战术建议（斩杀检测、操作序列、手牌编号） |
| **`plan`** | **查看本局整局计划**：套牌定位、致胜条件、三阶段目标、核心卡与资源纪律；`--json` 输出结构化、`--refresh` 强制重规划 |
| **`gui`** | **可视化悬浮军师（推荐）**：置顶浮窗实时显示局面 + 战况日志 + 战术建议 |
| `whoami` | **身份自检**：显示两名玩家分别是谁、本机是哪个账号；`--detect` 自动探测本机 account_id |
| `stop` | **关闭军师**：`stop` 关最新一个，`stop --all` 关全部残留实例 |
| `overlay-info` | 查看当前屏幕布局与浮窗将要出现的位置（多屏时先跑这个） |
| `watch` | 同 `gui` 但在终端输出建议 |
| `replay` | 离线回放日志 |
| `refresh-cards` | 从 HearthstoneJSON 刷新卡牌数据 |

常用参数：`--log/-l` 指定日志、`--me` 指定自己对应的玩家编号、`--deck` 传套牌码、`--config/-c` 指定配置文件、`--no-llm` 只看局面不调模型。

---

## 可视化界面（`gui`）

```bash
.venv/bin/hs-copilot gui
```

在屏幕右上角生成一个置顶浮窗（400×620，可拖动），四个区域自上而下：

| 区域 | 内容 |
|---|---|
| 状态栏 | 当前回合与阶段；**右上角绿色显示「本机：<玩家名>」**，用于随时确认工具认的是不是自己 |
| 局面条 | **▲对手**（红，上方）在前、**▼我方**（绿，下方）在后 —— 与炉石界面上下位置一致。<br>含双方英雄血甲、法力、手牌数、牌库数，以及场面随从列表（含嘲讽/圣盾等关键词） |
| 战术建议 | 大模型给出的结论、操作顺序、风险提示、手牌编号；**斩杀时标红加粗**，结论金色 |
| 实时战况 |滚动显示伤害、死亡、触发效果、区域变更，带时间戳 |

> 敌我误读是这个工具最常见的问题，因此界面做了三重标识：
> 颜色（我绿敌红）、符号（▼我 ▲敌）、以及右上角常驻的本机玩家名。
> 三者任一都能独立确认身份，不依赖用户记忆。

设计要点：

- **置顶但不抢焦点**：用 `NSFloatingWindowLevel` + `NSPanel`，浮窗始终在游戏之上，但不会夺走窗口焦点，不影响游戏操作
- **主线程更新界面**：日志监听在后台线程，通过主线程队列派发 UI 更新，避免 Cocoa 线程安全问题
- **斩杀高亮**：【斩杀】行标红加粗 14pt，一眼可见
- **免安装依赖**：未装 PyObjC 时自动降级为终端输出，不影响核心功能
- **TTS 播报**：`config.yaml` 里 `watch.voice: true` 时用系统语音朗读结论首句

界面截图见 `docs/gui-screenshot.png`。

**左上角三个彩色圆点**（macOS 三键风格，浮窗无系统标题栏所以自己画）：

| 按钮 | 作用 |
|---|---|
| 🔴 红 | **关闭军师** —— 关窗并退出进程 |
| 🟡 黄 | 折叠到标题栏 / 展开（不退出，方便长时间挂着） |
| 🟢 绿 | 精简 / 完整切换 —— 只留双方状态条（140pt 高），全屏打游戏时更不挡视野 |

### 建议什么时候弹出

**只在我方回合开始时**（`step=MAIN_ACTION` 且 `CURRENT_PLAYER` 是我方），每回合恰好一次。

实测一个回合内的 step 序列决定了触发时机：

```
MAIN_READY → MAIN_START_TRIGGERS → MAIN_START → MAIN_ACTION → MAIN_END → MAIN_CLEANUP →…
              ↑ 只是「准备就绪」        ↑ 回合触发器  ↑ 真正能出牌 ← 这里提示
```

三个容易踩的点：

| 坑 | 后果 | 现在 |
|---|---|---|
| 把 `MAIN_READY` 当可行动步骤 | 每回合提示**两次** | `ACTIONABLE_STEPS = {MAIN_ACTION}` |
| 把 `MAIN_START` 当换牌步骤 | 每回合弹「起手留牌」 | `MULLIGAN_STEPS` 已移除它 |
| 只用「回合号 ≠ 上次回合号」去重 | 解析器重建对局时重复提示 | 用「本次启动已提示过的回合号」集合 |

另外 `MAIN_ACTION` 在同一回合内会反复进出（出牌 → 结算 → 再出牌），
因此还要求 `prev_step` 不在 `MAIN_ACTION` 里（即从别的步骤切进来）。

配置项：

```yaml
watch:
  trigger_on_my_turn: true      # 回合开始时提示（默认）
  trigger_on_mulligan: true     # 起手留牌（默认，仅开局一次）
  cooldown: 3.0                 # 两次请求的最小间隔（防频繁调用 API）
```

### 非对局状态下的显示

炉石**只在战斗进行中持续写 Power.log**，套牌/选人/主界面完全不写。
因此工具靠「日志静默时长」判断是否在局内（阈值 90 秒）：

| 状态 | 标题栏 | 双方状态条 |
|---|---|---|
| 对局中 | `第 N 回合 · MAIN_ACTION` | 完整数据（血甲/法力/手牌/场面） |
| 上一局已结束 | `上一局已结束（73 分钟前）` | 只留名字 + `—`（清空数据） |
| 从未对局 | `等待对局` | 同上|

> 判定终局时同时匹配 `step=FINAL_GAMEOVER` 与 `state=COMPLETE`——
> 后者是炉石写的**原始字符串**，漏掉它会导致已结束的对局仍被当成进行中。

界面见 `docs/gui-idle-state.png`。

### ⚠️ 改完代码必须重启实例

浮窗是**独立进程**，加载的是启动那一刻的代码快照。
改了代码不重启，旧实例会继续按老逻辑显示——

> 实测事故：修完身份识别后没重启旧实例，用户看到「英雄又反了」，
> 而数据层其实完全正确。排查了很久才发现跑的是十几轮修改前的进程。

**每次改完代码：先 `hs-copilot stop --all`，再重新启动。**

现在启动时会做两道防护：

1. **检测已有实例**并询问是否仍要启动（避免多实例并存互相冲突）；
2. **打印身份判据**（`player_name` / `account_id` / `my_player_id`），
   一眼就能确认配置对不对：

```
可视化军师已启动  日志 …
模型 deepseek-chat · Ctrl+C 退出
身份判据：player_name='玩家甲' account_id='100000001' my_player_id=1（后两者会被自动判定覆盖）
```

### 重复启动会留下残留实例

浮窗是独立进程，反复 `gui` 会留下多个实例（窗口重叠、CPU 占用翻倍）。
用这个命令清理：

```bash
.venv/bin/hs-copilot stop        # 关闭最新的一个
.venv/bin/hs-copilot stop --all  # 全部关闭
```

它按**窗口所属 PID** 精确关闭，比 `pkill` 可靠 —— `pkill` 依赖进程名匹配，
而 GUI 实例的进程名在 `ps` 里常被截断或包在 shell 包装层内（实测漏杀）。
本机 `ps` 在受限环境下也不可用，`stop` 改用 `kill -0` 探测存活。

**多屏与定位**

```bash
hs-copilot overlay-info                      # 先看浮窗会落在哪块屏
hs-copilot gui --screen 0 --pos right        # 指定屏与位置
hs-copilot gui --screen 1                    # 放在另一块屏
```

`--screen` 取值：`auto`（默认，放炉石之外的屏）/ `main` / `game` / 屏序号。
`--pos` 取值：`left`（默认）/ `center` / `right`。

> 实测本机双屏：主屏 1728×1117（炉石全屏占满）+ 副屏 1440×2560 竖屏（工作窗口在此）。
> 全屏玩游戏时浮窗若与炉石同屏会被盖住，因此默认放另一块屏。
> 浮窗可拖动，拖到顺手的位置后记住 `--screen`/`--pos` 组合即可。

**启动方式说明**

GUI 是前台事件循环程序。在自动化环境（IDE 终端、脚本）里启动时，
shell 结束会连带 `SIGTERM` 杀掉子进程（表现为窗口一闪即逝）。
建议在**独立终端窗口**里直接跑：

```bash
cd hearthstone-copilot && .venv/bin/hs-copilot gui
```

`launchctl` / `setsid` 在受限环境下不可用，`nohup` 也不可靠。

### 启动时自动回填当前对局（重要）

炉石一个会话会把**多个对局写进同一个 Power.log**。如果启动时只从文件末尾跟读，
`CREATE_GAME` 和所有 `FULL_ENTITY` 声明都已写进历史日志里，实体表会一直是空的 ——
表现为**血量 0、场面 0、手牌 0**，大模型据此回复「双方生命均为0，对局已结束」。

所以 `LogWatcher.backfill_current_game()` 会在启动时**全量回放一遍日志**
（8MB 约 0.2 秒，只做一次），把当前对局的状态建立起来，再继续增量跟读。

> 这里刻意**没有**自己「反向找 `CREATE_GAME` 再截段回放」——
> 实测炉石会在对局途中重发 `CREATE_GAME`，而英雄的 `FULL_ENTITY` 声明在更早的位置，
> 分段回放极易漏掉初始血量（试过按 `CREATE_GAME` 回溯、按 `CARDTYPE`/`HEALTH` 判据回溯，都不可靠）。
> 复用 `parse_log_file` 保证实时与离线走同一套代码，不会出现两套逻辑不一致。

---

## 配置

编辑 `config/config.yaml`：

```yaml
my_player_id: 1
deckstring: ""          # 留空自动读 Decks.log 的「Finding Game With Deck:」识别本局套牌
                        # 手填优先级更高；套牌用于牌库推演与整局规划

llm:
  base_url: "https://api.deepseek.com/v1"
  api_key: ""            # 或用环境变量 HS_LLM_API_KEY
  model: "deepseek-chat"
  temperature: 0.3
```

任何 OpenAI 兼容接口都能用，改`base_url` + `model` 即可：

| 服务 | base_url | model示例 |
|---|---|---|
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-plus` |
| Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| 硅基流动 | `https://api.siliconflow.cn/v1` | `Qwen/Qwen2.5-72B-Instruct` |
| 本地 Ollama | `http://localhost:11434/v1` | `qwen2.5:14b`（无需 key） |

环境变量优先级高于配置文件：`HS_LLM_API_KEY`、`HS_LLM_BASE_URL`、`HS_LLM_MODEL`、`OPENAI_API_KEY`。

---

## 前提：日志必须开启

项目**只读日志**，不碰游戏进程。需要炉石客户端写出 `Power.log`。

检查 `~/Library/Preferences/Blizzard/Hearthstone/log.config`，确保含：

```ini
[Power]
LogLevel=1
FilePrinting=true
ConsolePrinting=false
ScreenPrinting=false
```

日志位置：`/Applications/Hearthstone/Logs/Hearthstone_<时间戳>/Power.log`
程序会自动定位最新的一份（`doctor` 会显示找到的路径）。

### 已为本机解除 10MB 截断

炉石默认在日志写到 10MB 时截断，长对局后半段数据会全部丢失。已创建
`/Applications/Hearthstone/client.config`：

```ini
[Log]
FileSizeLimit.Int=-1
```

**若重装炉石或日志目录被清空，记得重新写入这个配置**，否则长局会缺数据。

---

## 套牌推演与整局规划

**默认情况下无需手填**：工具会读同目录 `Decks.log` 里最后一次 `Finding Game With Deck:` 自动识别本局套牌。手填 `deckstring` 或运行时 `--deck` 优先级更高。启用后提供：

- 起手留牌优先级（整局计划里给出；换牌阶段默认不单独弹建议）
- 关键牌剩余追踪（战吼/亡语/奥秘/联动类优先）
- 尚未见到的牌构成、法力曲线
- **整局规划**：致胜条件、阶段目标、核心卡与资源纪律（见专章）

deckstring 解析与官方 `hearthstone` 库交叉验证一致。用 `hs-copilot plan` 可随时查看当前规划。

---

## 项目结构

```
hearthstone-copilot/
├── config/config.yaml        # 配置文件
├── data/cards_zhCN.json      # 卡牌数据（36,390 张，refresh-cards 可更新）
├── src/hs_copilot/
│   ├── log_parser.py         # Power.log 解析 + 实体标签状态机
│   ├── cards.py              # 卡牌知识库（中文卡表 + dbfId 索引）
│   ├── deck.py               # 套牌码编解码 + 牌库推演 + Decks.log 自动识牌
│   ├── planner.py            # 整局规划：套牌画像 / 跨回合 GamePlan / 低频生成
│   ├── advisor.py            # 提示词构造 + LLM 调用 + 启发式预判
│   ├── watcher.py            # 实时日志跟随与触发
│   ├── overlay.py            # macOS 悬浮窗
│   ├── config.py             # 配置加载（yaml + 环境变量）
│   └── cli.py                # 命令行入口
├── tests/verify_replay.py# 离线回放校验
├── tests/test_board_state.py  # 局面读取回归测试（场面数/技能归属/玩家绑定）
└── run.sh                    # 便捷执行入口
```

---

## 设计要点

**为什么读日志而不是截屏 OCR**：日志里直接带中文卡名、卡牌 id、区域坐标与玩家归属，是结构化数据；截屏需要 OCR，受分辨率、主题、动画影响，慢且易错。

**局面是tag 状态的投影**：解析器不维护第二套记账逻辑，所有数值直接读客户端自己写下的 tag，避免与实际局面不一致。

**提示词里带卡牌效果文本**：每张卡都附上中文效果与关键词，模型无需外部知识，新扩展也能理解。

**启发式作为锚点**：斩杀检测、场面价值评估由本地规则先算一遍写进提示词，给模型稳定参考，降低幻觉。

**优雅降级**：未配置 key 或接口报错时，自动退回本地启发式建议，不中断使用。

---

## 合规说明

本项目定位是**辅助决策工具**，属于卡牌跟踪器（HDT / HSTracker）同一范畴：

- ✅ 只读本地日志、计算概率、给出建议、高亮提示
- ❌ 不模拟点击、不注入进程、不读取游戏内存

暴雪用户协议 1.C.ii.2 禁止 BOT 自动操作。本项目全程不发送任何输入，出牌由你本人执行。

---

## 信息准确性（2026-11-14 修复）

模型拿不到完整场面与卡牌描述时，建议会整体失真。以下是实测定位到的根因与修复：

| 问题 | 修复 |
|---|---|
| 按中文卡名查卡表，同名卡命中酒馆战棋英雄 / 旧版重印（「梦魇之王萨维斯」变成 0 费、无效果文本的 `BG36_HERO_105`） | 一律优先按日志 `cardId` 精确匹配中文卡表；`cards.py` 同名卡改为「非战棋 + 可收藏 + 有效果文本」优先；payload 写明匹配方式，兜底匹配会标注「仅供参考」 |
| 地标（`LOCATION`，如安保盲区 / 赤红深渊）被算成 0/2 随从 | 随从与地标分列，地标带剩余耐久、本回合冷却与中文效果文本 |
| `Entity=[...]` 里的 `zone=` 是**变更前**快照，会把已入墓地的随从「复活」回 PLAY，导致场面随从数超过 7、站位重复 | 区域与站位只认权威 `tag=ZONE` / `tag=ZONE_POSITION`；快照仅在实体尚无区域时初始化一次 |
| 提示词缺英雄技能、武器、奥秘、墓地、近期战况；牌库剩余直接给 0 | 全部补齐；牌库无 DECK 区实体时输出「未知」/ `?`；终局快照标注「不要给本回合出牌建议」 |
| 模型自己算法力会错（3 费 + 5 费当成 7 费内还能再挂 2 费技能） | 本地枚举合法费用组合写进提示词；返回前校验 plan 里「（N费）」合计，超费时置顶警告 |
| `board --json` 抛 `TypeError`；`board_state()` 硬编码 PlayerID 1 | 改用 `print_json(data=...)`，并按本机 PlayerID 导出敌我 |
| 主界面时启动军师，开局后界面没有任何数据 | 炉石每次启动客户端新建 `Logs/Hearthstone_<时间戳>/`，且 `Power.log` 只在进入对局后才写；旧版只在启动时定位一次日志、只检测同路径 inode 轮转。现在 `LogWatcher` 每 3 秒重新定位最新日志并整体切换（重建解析器/局面、重置回合去重、重新判定身份），`gui` / `watch` 默认开启 |
| 界面显示「我方 法力 0/0」 | `FIRST_PLAYER` 只标记先手，旧版没把后手名字绑定到 PlayerID，挂在「名#号」实体上的 `RESOURCES` 从未同步；且 `player_slot_of()` 会被历史残留键干扰返回 0。现在补上后手配对（`3 - 先手`）、按虚拟 id 取名，并在回放结束后统一重放玩家级 tag |
| 整局一次 AI 建议都没给 | 日志里 `CURRENT_PLAYER` 是挂在「名#号」实体上的 **0/1 布尔标记**（`value=1` 表示这一方正在行动），不是玩家编号。旧版 `int(value)` 后恒为 1，本机为 PlayerID 2 时「轮到我方」永不成立。现在按实体解析真实 PlayerID；同时只认 `GameEntity` 上的全局 `TURN`，忽略名字实体上的玩家级回合数（否则回合去重失效） |
| 启动时机导致漏建议 / 想马上看到建议 | `LogWatcher` 首轮检查「已处于我方 MAIN_ACTION」并补触发；`gui` 启动即生成一份「当前局面参考」（先建立日志偏移再调模型，不漏读）；收到回合触发先显示「分析中」再请求模型 |
| AI 让「刚上场」的随从去攻击、虚报斩杀 | 客户端在随从入场时写 `tag=EXHAUSTED value=1`（召唤失调），旧版只在有状态时给一句含混的「本回合已行动/无法攻击」，无字段时模型默认能打；`detect_lethal` 更把所有随从攻击力一律累加。现在 `Entity.attack_status()` 统一判定召唤失调 / 已攻击 / 冻结 / 突袭 / 冲锋 / 风怒，每个我方随从**恒定输出**「本回合能否攻击 / 能否打脸 / 刚召唤 / 状态依据」，并新增「【系统预判】本回合可用攻击」锚点；斩杀只统计可打脸伤害，返回前 `_validate_attack` 会拦下违规方案并撤销虚报的斩杀标记。对手随从改按「下个对手回合能否攻击」评估威胁，避免把对手的场上威胁算成 0 |
| `board --json` 输出里混入「日志：…」提示行，无法直接解析 | `--json` 模式下不打印提示行，输出为纯 JSON；随从条目新增 `can_attack` / `just_summoned` / `attack_reason` 字段 |
| 在对手回合也弹建议（浮窗显示「第 2 回合建议」，模型却回复「非我方行动回合」；实测连续 13/14 回合各弹一次） | ① `gui` 旧版无条件做一次「启动即分析」，不看是否轮到我方；现在删掉，改由 `LogWatcher` 首轮补触发（仅在我方 `MAIN_ACTION` 时）给建议。② `CURRENT_PLAYER value=0` 原本被忽略，配对的 `value=1` 一旦解析失败，`current_player` 会停在旧值；现在 0 本身也用于把行动方切到 `3-pid`，虚拟名字实体上的布尔 `1` 不再被当成 PlayerID。③ 新增 `turn_owner_name` / `current_player_name`：炉石每个回合开始都会把**该玩家自己的回合数**写在他的名字实体上（`TURN=8 → 玩家甲#12345 tag=TURN value=4`），这是最可靠的「现在轮到谁」，不受布尔标记与多局残留槽位影响；`is_my_action_phase()` 与 watcher 都以「名字是否等于本机」为最终判据，编号只作兜底。④ **回合间隔硬约束**：炉石 `TURN` 每个玩家回合 +1，两次我方建议之间必差 2，`gap < 2` 一律不触发。⑤ 建议标题改用 `t.turn`，不再用会被后续日志覆盖的 `t.recon.turn`；Trigger 保存触发瞬间的 `turn / current_player / step` 快照作为最后一道闸 |

| 开局瞬间模型断言「对手生命为 0，对局已结束，无需再出牌」，但状态条下一秒显示对手 28 血 | 新局开局时英雄实体 / `HEALTH` 还没写进 `Power.log`，`hero_hp` 读到 0，而 `step=MAIN_ACTION`、`state=RUNNING` 表明对局正在进行。现在：① payload 判断「血量数据未就绪」并在**最前面**放「【数据告警】血量未就绪」，`我方/对手.生命` 遮罩成「未知」，终局快照仍保留原始读数；② 系统提示词规则 18（最高优先级）禁止断言对局结束 / 只能空过 / 按 0 血算斩杀，并要求照常给本回合出牌建议，规则 7/12 让位；③ `LLMClient._validate_readiness()` 在返回前撤销虚报的 `lethal`、改写结论、移除「空过」步骤并把警告置顶 |

**建议触发**：每次轮到**我方**且进入 `MAIN_ACTION` 时自动给出，每个我方回合恰好一次，无需任何操作。对手回合、同一回合内反复进出 `MAIN_ACTION`、对局已结束、日志长时间停更都不会请求模型；启动/切换日志时若正处于我方回合，首轮补触发会立刻给出该回合建议。换牌阶段默认**不**再单独给「起手留牌建议」（`watch.trigger_on_mulligan: false`）——那时英雄血量/场面尚未建立，模型容易基于脏数据乱答，也不符合「只在我的回合开始」。为压低延迟，`max_tokens` 降到 900，系统提示词要求 4 步内给结论、不复述局面，输出 JSON 由 `_validate_mana` 兜底校验费用。

提示词现在每条卡牌都带「卡牌效果 / 类型 / 法力消耗 / 卡面关键词 / 卡表匹配方式 / 当前攻血 / 站位 / 当前生效关键词」。回归用例见 `tests/test_board_state.py` 的 `[测试14]`。

### 手牌卡号尚未揭示时（2026-11-15 修复）

实时 tail `Power.log` 时，起手 / 新抽的手牌先是 `UNKNOWN ENTITY ... CardID=`，真实卡号由随后几百毫秒内的 `SHOW_ENTITY` 写入。**离线全量回放会把后来的卡号回写进同一实体对象，看起来「全部已知」，其实是未来信息**，测不出这个问题。旧版此刻把「实体14 / 卡面未知」塞进 payload，规则 6 还要求模型「按有 N 张未知手牌给建议」，于是产出「#1 实体14：若为低费随从/解牌可本回合打出」这种毫无信息量的建议。

现在分四层处理（回归用例 `[测试22]`）：

- `cli.py` 的 `wait_for_hand_reveal()`：回合触发后先补读最多 **1.2 秒**日志，等 `SHOW_ENTITY` 把卡号补齐再构造 payload；超时才走兜底，不阻塞建议。补读产生的触发事件暂存，不会丢。
- `advisor.py` 的 `describe_entity()`：内部回退名「实体N」改成「（卡面未揭示）」，并显式标记 `不可用于出牌步骤`。
- `build_prompt()`：注入「【未揭示手牌推演】」——把套牌 30 张扣除局面里已揭示的牌，给出带费用 / 中文效果的概率候选池（最多 14 条），**只用于判断资源倾向，不能指派给某个编号**；同时给出「手牌总数 / 已揭示 / 未揭示」三个计数。
- 规则 6 改写 + `LLMClient._validate_unknown_hand()`：禁止把未知编号写进 `plan` 或 `hand_hints`，禁止「若为低费随从 / 需按实际费用判断」这类条件猜测；只依据已揭示手牌、场面、英雄技能、地标与【整局计划】给明确可执行的操作序列。返回前硬校验会剔除违规 `hand_hints` 与猜测步骤，并置顶「日志尚未揭示 N 张手牌」告警；手牌全部就绪时完全不介入。

---

## 整局规划（2026-11-15 新增）

**问题**：旧版每回合只把「当前手牌 + 当前场面」丢给模型，模型从零判断，必然做**单回合局部最优**。实测「超级进化萨」里它建议用「妖术」变形一个普通大随从，而这副牌的胜负手是「群山之灵任务 → 铺场 → 升腾进化 → 兆示减费死亡之翼」，妖术与闪电风暴是仅有的解场资源，必须留给关键威胁。

**做法：慢层定战略、快层做执行**（借鉴 HearthCoach 的「LLM 慢规划 + 规则快评分」）

| 层 | 频率 | 输入 | 产出 |
|---|---|---|---|
| 慢层 `PlanEngine` | **每局 1 次**（阶段推进最多再评 2 次，实测 8 秒） | 套牌码解出的 30 张完整中文卡表、曲线、战术角色、双方英雄、先后手 | `GamePlan`：套牌定位、致胜条件、前/中/后期可检验目标、核心卡与 `keep` 标记、资源纪律、终结组件、对局应对 |
| 快层 `LLMClient.advise` | 每个我方回合 1 次（实测 2–4 秒） | 原有完整局面 **+ 恒定的「【整局计划】」+「【计划进度】」** | 服从既定战略的本回合操作序列 |

**套牌从哪来**：`config.yaml` 的 `deckstring` 留空时，自动读同目录 `Decks.log` 里**最后一次** `Finding Game With Deck:` 块（含 `### 套牌名`、`# Deck ID`、套牌码）。换卡组、换日志目录、同一客户端连续多局都会自动跟上（按 Deck ID 变化重建计划）；手填的 `deckstring` 优先级更高。套牌码前缀兼容 `AAEB` / `AAEC`（实测同一份 Decks.log 里两种都有，只认 AAEC 会漏掉老卡组）。实测本局识别为「超级进化萨」(ID 9465884738)，30 张全部命中中文卡表。

**约束**：系统提示词新增规则 19/20/21 —— 整局计划优先且不得另起打法；「必须保留=是」的核心卡非关键回合不能随手打掉；summary 必须点明在推进哪个目标；输出新增 `plan_step`。`_apply_plan()` 在返回前补齐整局目标一行，并对无理由动用核心卡给出温和提醒（「留到下回合用」不会误报）。回合建议**不能修改计划**，计划只在换套牌 / 回合号回退（新局）/ 阶段边界推进时重评，避免每回合摇摆。

**用法**

```bash
.venv/bin/hs-copilot plan                 # 查看当前整局计划
.venv/bin/hs-copilot plan --json          # 结构化输出
.venv/bin/hs-copilot plan --refresh       # 强制重规划
.venv/bin/hs-copilot advise              # 建议自动带上整局计划
```

`watch.plan_enabled: true` 为默认；设 `false` 或 `--no-llm` 时退回原来的单回合建议（仍会用本地规则兜底计划，不中断使用）。首次制定计划时浮窗显示「正在读取套牌并制定整局计划…」，之后每回合不再有额外请求。**卡表**已刷新到 36,390 张（补齐 F3TCH3R寻回机 / 不稳定的原型机 / 同化软泥怪等新卡）；`refresh-cards` 同时修了 HearthstoneJSON 对默认 UA 返回 403 的问题。回归用例见 `[测试21]`。

---

## 已知限制

- 对手手牌与牌库内容日志中不可见（客户端自身也不显示），只能基于已打出的牌推算
- 牌库剩余数在日志里没有权威来源，界面显示 `?`、提示词标注「未知」而非 0；识别到套牌后可做更准的推演
- 旧版日志（被 10MB 截断过的）会缺失实体初始声明，导致法力/牌库数显示为 0；配置好 `FileSizeLimit.Int=-1` 后新对局无此问题
- 炉石更新可能改变日志格式，版本大更新后建议跑一次 `doctor` 和 `tests/verify_replay.py` 验证
- 酒馆战棋 / 战斗模式等特殊模式的日志结构与标准天梯不同，暂未专门适配

---

## ⚠️ 最重要：先配置 player_name（否则敌我必然反）

**「我方/敌方场面反了」「血量法力都是 0」这类问题，根因都是身份没绑定对。**

先跑一次自动探测：

```bash
hs-copilot whoami --detect
```

它会列出两个证据表，**参与对局数最多的那个名字就是本机**：

```
玩家名· 参与对局数
玩家名       ┃ 对局数 ┃ 出现过的 PlayerID
玩家甲     │7      │ 1,2            ◀ 本机
EAGhost      │      1 │ 2
Jessica      │      1 │ 2
冷锋         │      1 │ 2
...（其余 7 个名字各只 1 局）
```

```yaml
player_name: "玩家甲"
account_id: "100000001"   # 可选，交叉校验
```

### 判据说明（三个坑，都实测踩过）

| 判据 | 可靠吗 | 原因 |
|---|---|---|
| **参与对局数最多** | ✅ | 每个玩家在本机日志里都是「一次性的对手」，唯独本机贯穿所有对局 |
| `account_id`（战网 lo）恒定 | ⚠️ 仅交叉验证 | 能确认某账号贯穿始终，但**无法证明那个账号是本机** |
| ~~lo / 名字横跨 PlayerID 1/2~~ | ❌ | **本机换边时对手也跟着换边，两人都横跨**，无区分度 |
| ~~名字的出现顺序~~ | ❌ | 顺序与 PlayerID 无关。实测某局里 EAGhost 先出现却对应 PlayerID 2 |
| ~~`my_player_id` 固定值~~ | ❌ | 本机随先后手在 1↔2 之间切换，固定值必然在某些对局指错人 |

### 名字怎么绑定到 PlayerID：FIRST_PLAYER 权威配对

日志里玩家级的关键数据（法力 `RESOURCES`、疲劳）只挂在一种特殊实体上：

```log
TAG_CHANGE Entity=玩家甲#12345 tag=RESOURCES value=7
```

这个「名#号」是**虚拟实体**，与 Player 实体（id 2/3）之间：

- `GameAccountId` 的 `hi`/`lo` 与 `#编号` **无任何算术关系**（`>>11`、除以常数、位运算全试过）；
- 名字的**出现顺序**与 PlayerID 无关。

唯一可靠的关联是 **`FIRST_PLAYER=1` 同时出现在两侧**，标记同一个人：

```log
TAG_CHANGE Entity=3              tag=FIRST_PLAYER value=1   ← Player 实体侧，给出 PlayerID
TAG_CHANGE Entity=EAGhost#5498   tag=FIRST_PLAYER value=1   ← 名字侧，给出显示名
```

解析器据此配对；未被标记的名字即另一侧（`pid = 3 - fp_pid`）。
配对有延迟（两侧出现时机可差上万行），期间会先按出现顺序兜底，
待权威信息到齐时立刻纠正并迁移已同步的 `RESOURCES` 等 tag。

---

## Power.log 格式要点（维护参考）

炉石日志格式随版本变动，以下是实测确认的关键形态（2026-08 / 2026-10两个版本一致）。改解析器前务必对照：

**顶层动作**（顶格，缩进不可靠——实测有 0/2/4/6/8 混杂，必须按动作名判断）：
`CREATE_GAME` / `FULL_ENTITY` / `SHOW_ENTITY` / `TAG_CHANGE` / `BLOCK_START` / `Player` / `GAME_ENTITY`

**实体标识有三种写法**，这是最容易踩的地方：

```log
TAG_CHANGE Entity=28 tag=ZONE value=HAND                          # 数字 id
TAG_CHANGE Entity=GameEntity tag=TURN value=3# 全局标签挂在游戏实体上
TAG_CHANGE Entity=玩家甲#12345 tag=RESOURCES value=7            # 玩家名#编号
```

第三种是**玩家级 tag 的挂载点** —— 法力（`RESOURCES`）、疲劳等只出现在这里，不在玩家实体（id 2/3）上。编号无法反推 `GameAccountId`（试过 `>>11`、除以常数、位运算全不成立），因此按**首次出现顺序**认定玩家 1、2。

**`entityName` 内部可能含方括号**：

```log
SHOW_ENTITY - Updating Entity=[entityName=UNKNOWN ENTITY [cardType=INVALID] id=4 zone=HAND ...] CardID=CATA_558
```

正则必须用「贪婪匹配到最后一个 `]`」再做键值解析。用非贪婪 `.*?` 会在 `entityName` 内部的 `]` 处提前截断，导致 `id`/`cardId`/`zone` 全部丢失。

**实体 id 会跨对局复用**：同一份日志里 `id=4` 在上一局是心灵扫荡者，下一局又变成进击的募援官。残留旧实体会让新对局场面被污染（表现为出现上一局随从、攻血为 0）。因此新对局必须换代实体表。

**换牌/选牌界面会产生占位实体**：`DebugPrintEntityChoices` 输出的
`entityName=UNKNOWN ENTITY [cardType=INVALID] ... cardId=`（空），zone=DECK。
它们不是场上随从，必须过滤，否则会混进场面显示。

**场上并存多种非随从实体**，统计随从数时必须逐一排除，否则会虚高
（实测22 个 PLAY 区实体里只有 7 个是真随从）：

| 卡型 | 实体 | 是否计入随从 |
|---|---|---|
| `MINION` | 随从 | ✅ |
| `HERO` | 英雄本体 | ❌ |
| `HERO_POWER` | 英雄技能 | ❌ |
| `WEAPON` | 武器 | ❌ |
| `ENCHANTMENT` | 附魔/光环载体 | ❌ |
| `PLAYER` | 玩家实体 | ❌ |
| （空卡号） | 换牌占位实体 | ❌ |

**手牌卡号有两个来源**，缺一不可：

```log
SHOW_ENTITY - Updating Entity=38 CardID=TLC_229# 对手手牌、被揭示的牌
FULL_ENTITY - Updating [entityName=... id=7 ...] CardID=XXX            # **我方手牌的主要来源**
```

只解析 `SHOW_ENTITY` 会导致**我方手牌永远没有卡号**（炉石对我方手牌走 `FULL_ENTITY - Updating`）。

**卡号未揭示 ≠ 没有手牌**：抽牌时手牌先以「无卡号占位实体」入区
（`entityName=UNKNOWN ENTITY [cardType=INVALID] ... cardId=` 为空），
炉石只在必要时才补写卡号。所以统计手牌数**不能要求卡号非空**——
否则我方手牌会整体消失，模型据此回复「手牌为空，只能空过」，而实际手里是有牌的。
提示词里这类手牌会标记 `卡面未知: true`，并显式禁止模型断言「手牌为空」。

>炉石**不是每局都写全我方手牌卡号**（防外挂读牌），所以部分回合确实拿不到手牌内容。
> 工具会明确告诉你「N 张手牌但内容未知」，而不是谎报为空。

**英雄技能要取 PLAY 区的实体**：牌库里也存在同名技能实体
（如尚未召唤/已被替换的技能卡），遍历时先撞到牌库区的就会取错——
曾让猎人玩家显示成萨满的「图腾召唤」。

**手牌卡名**只能从 `- Updating ... CardID=xxx` 拿到；起手发牌只有 `TAG_CHANGE Entity=28`（无卡名）。注意这个形态有两条路径，缺一不可（详见上方「手牌卡号的两个来源」）。

**实体声明期的 tag 只出现一次**（`CARDTYPE` / `PLAYER_ID` / `HERO_ENTITY` / `HEALTH`），必须与后续变更期的 tag 分开存储，否则实体收到 `PLAYSTATE` 等 tag 后会「看起来失去类型」。但 `ZONE` / `ZONE_POSITION` 是例外 —— 它们会随实体重挂区域而变化，读取时必须**优先用实时值（`tags`）而非声明期快照（`declared`）**，否则会拿到 SETASIDE 阶段的旧站位。

**进入战斗时会发两次 `CREATE_GAME`**（第二次在真实对局开始前），而 `FULL_ENTITY` 声明只在第一次。所以重置对局时不能简单清空实体表 —— 玩家实体（2/3）与英雄实体的声明整份日志只出现一次，需要继承到新对局。

---

## 开源与合规

**协议**：[MIT](LICENSE)。

**定位**：这是一个**辅助决策**工具，不是外挂。它只读炉石客户端自己写出的 `Power.log`，
不注入进程、不读内存、不模拟键鼠，所有出牌仍由玩家本人完成；与 HDT / HSTracker
这类卡牌跟踪器同属「只读日志」范畴。本项目与 Blizzard Entertainment 无任何关联，
使用前请自行确认符合你所在地区的游戏服务条款，作者不对账号风险负责。

**隐私**：发往大模型之前统一经过 `src/hs_copilot/privacy.py` 脱敏层
（`privacy.redact: false` 可关闭），移除：

- 玩家显示名与「名#编号」→ `[本机玩家]` / `[对手玩家]` / `[玩家#编号]`
- 战网账号 lo、形似 API Key 的字符串 → `[账号]` / `[密钥]`
- 日志时间戳、原始 `tag=XXX value=Y`、本机绝对路径 / 日志路径

payload 本身已用「我方 / 对手」两个分组表达身份，脱敏不影响判断，反而避免模型
靠名字猜 PlayerID。API Key、玩家名、账号 ID 请放在 `config/config.local.yaml`
（已被 `.gitignore` 忽略）或环境变量里，**不要写进 `config/config.yaml`**。

**模型侧质量控制**：校验器（费用 / 攻击资格 / 血量就绪 / 未揭示手牌 / 输出格式）
驳回方案时，会把**具体驳回原因**回传模型自动改正一次（`llm.auto_correct`），
请求复用 `system + 原 payload + 原输出` 作为前缀以命中服务商缓存；
改正后仍不通过才保留本地修正版，绝不无限重试。

**数据来源**：中文卡表来自 [HearthstoneJSON](https://hearthstonejson.com/)
（`hs-copilot refresh-cards` 可更新，随仓库附带一份快照）；整局规划读客户端
`Decks.log` 里本局套牌码。两者都只在本机解析，发往模型前同样过脱敏层。

**开发与测试**：

```bash
.venv/bin/python -m compileall -q src/hs_copilot tests
.venv/bin/python tests/test_board_state.py
.venv/bin/python tests/verify_replay.py <某份 Power.log 路径>
```

`tests/test_board_state.py` 里依赖本机 `Power.log` 的用例在找不到日志时会自动跳过；
纯合成用例（脱敏、自动改正、攻击资格、整局规划、触发时机等）在任何环境都能跑。

**参考过的同类项目**：`HearthSim/python-hslog`、`HearthSim/HSTracker`、
`HearthstoneDeckTracker/HearthstoneDeckTracker`、`jleclanche/fireplace`，
以及 2026 年出现的 `barnabys-drew/hearthstone-ai-coach`、
`CuZaoo/hearthstone-agent-assistant`、`OstrichHermit/hearthstone-cli`、
`yunwuneo/AI-Hearthstone`、`1403955284/lscs-public`。
完整对比见 [`research/开源项目调研报告.md`](research/开源项目调研报告.md)。

**贡献**：欢迎 issue / PR。`Power.log` 格式会随版本变化，改解析器时请同步补一条
`tests/test_board_state.py` 回归用例——把踩过的坑写成注释很容易随迭代漂移。