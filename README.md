# hypoloop

三个角色，一个循环，跑在 [agy](https://antigravity.google)（Antigravity CLI）上：

| 角色 | 干什么 | 能改文件吗 |
|---|---|---|
| **假设者** | 读代码，对任务提出**可证伪**的假设 | ❌ |
| **质疑者** | 攻击这些假设，给修正案或全新假设 | ❌ |
| **验证者** | 动手改、自己搭取证手段、把结论推向被实证的方向 | ✅ **只有他** |

一轮走完，下一轮把**上一轮的证据和裁决**喂回去 —— 所以这个循环是收敛的，不是三个
agent 各说各话。

```
$ cd E:/Projects/neon-racer
$ hypoloop "让游戏画面更好更精致一些" --rounds 2 --commit

开跑前额度（Gemini Models，档位 Google AI Pro (g1-pro-tier)）：
  5 小时窗口  剩余 92.149%   重置于 2026-09-03 22:08
  周窗口      剩余 98.692%   重置于 2026-09-10 17:08

============================================================
第 1/2 轮
============================================================
  假设者 跑起来了（gemini-3.8-flash-medium，plan）…
    用时 125s，190,898 tokens
  → 提了 3 条假设
  质疑者 跑起来了（gemini-3.8-flash-medium，plan）…
    用时 208s，254,372 tokens
  → 3 条意见（驳回 0、要求修正 2），另提 1 条新假设
  验证者 跑起来了（gemini-3.8-flash-high，accept-edits）…
    用时 549s，655,129 tokens
  → 实证 1、证伪 3、未决 0；改了 1 个文件
  已提交第 1 轮的 1 个文件
============================================================
第 2/2 轮
============================================================
  ...
  → 实证 3、证伪 0、未决 0；改了 3 个文件

跑完后额度（Gemini Models，档位 Google AI Pro (g1-pro-tier)）：
  5 小时窗口  剩余 76.936%   重置于 2026-09-03 22:08
  周窗口      剩余 96.156%   重置于 2026-09-10 17:08
  本次吃掉 5 小时窗口 的 15.213 个百分点
  本次吃掉 周窗口 的 2.536 个百分点

本次 token 消耗（来自 agy 每次调用返回的 usage）：
  角色           调用数  total_tokens
  验证者              2     1,285,923
  假设者              1       194,133
  质疑者              1       158,625
  合计               4     1,638,681

报告：~/.hypoloop/runs/20260903-174503-让游戏画面更好更精致一些/report.md
```

上面是一次**真实运行**的输出（neon-racer，一个 Three.js 网页游戏），不是编出来的示例。

那一轮里，假设者提的第一条「把 bloom threshold 从 0.25 抬到 0.80 就能去掉全屏灰雾」
被质疑者算出了致命问题：LuminosityHighPassShader 用 BT.601 权重算亮度，青色 0x00ffff
的 luma 只有 0.701、品红 0xff00ff 只有 0.413 —— 抬到 0.80 会把游戏**全部霓虹一起斩掉**。
验证者实测确认了这个反驳：局内青色发光像素从 34728 暴跌到 19310（−44.4%），而天空亮度
纹丝不动（18.83 → 18.79）。**这条被证伪并回滚了，一行都没留在代码里。**

同一轮里「EffectComposer 缺 MSAA 让 antialias:true 失效」那条被实证保留：
`renderTarget.samples` 0 → 4，赛道线框的拉普拉斯边缘噪声方差从 352.71 降到 244.30。
事后用完全独立的一套 harness 复测，在不同区域、不同帧上量到 −42.3%，方向和量级都对得上。

**这就是这套东西存在的理由**：四条假设里三条是错的，而错的那三条没有留在代码里。

## 装

零第三方依赖，只用 Python 标准库（3.8+）。

```bash
pip install git+https://github.com/flufy3d/hypoloop
```

前提：装好 [agy](https://antigravity.google) 并登录过。hypoloop 会自己在 PATH 和
`~/AppData/Local/agy/bin/` 里找它；都找不到就设 `HYPOLOOP_AGY=<agy 的完整路径>`。

## 用

```bash
hypoloop "任务描述"                      # 在目标项目目录里跑，默认 2 轮
hypoloop "任务" -C /path/to/project      # 或者指定目录
hypoloop "任务" --rounds 3 --commit      # 收一个提交到 hypoloop/<run> 分支
hypoloop "任务" --dry-run                # 只打印将要发的提示词，一个 token 不花
hypoloop "任务" --evidence-hint "用无头浏览器截图对比首屏"
hypoloop quota                           # 只看额度
hypoloop history                         # 看历史 token 账
```

常用开关：

| 开关 | 作用 |
|---|---|
| `--rounds N` | 跑几轮（默认 2） |
| `--hypotheses N` | 每轮提几条假设（默认 3） |
| `--model` / `--verifier-model` | 分别指定只读角色和验证者的模型 |
| `--evidence-hint` | 给验证者的取证建议；**默认空，通常就该空着**（见下） |
| `--commit` | 整个 run 的改动**收成一个提交**，放在 `hypoloop/<run>` 分支上 |
| `--allow-dirty` | 目标不是 git 仓库、或工作区不干净时也硬跑 |
| `--save-config` | 把本次参数存成这个项目的默认值 |

## 六件它认真对待的事

### 1. 「只有验证者能改文件」是**机制**，不是提示词里的一句请求

提示词里写「你不许改文件」，模型哪天不听就破了，而且破了没人知道。所以：

- 假设者和质疑者跑在 agy 的 `--mode plan`（实测只往 agy 自己的 brain 目录写产物）；
- **更重要的是**，`guard.py` 在它们跑之前给工作区拍一次指纹，跑完再拍一次，不一致
  就中止整轮并打印出到底改了什么。

指纹覆盖内容而不只是文件名 —— `git status --porcelain` 只列未跟踪文件的路径，
内容被改了它一个字都不会变，所以未跟踪文件的内容单独哈希。

### 2. 「往被实证的方向走」靠 schema 逼出来

每条假设**必须**带 `predicted_observable`：如果这条成立，哪个可测量的量会怎么变。
写「画面会更好看」这种没法测的，等于没写。验证者对每条给出
`supported` / `refuted` / `inconclusive` 和**具体的前后读数**，被证伪的改动自己回滚。

`inconclusive` 是个一等公民 —— 测不出来就说测不出来，比编一个没测过的读数强得多。

**怎么取证是验证者自己的事。** 它有完整的 shell：跑现成的测试、写个一次性脚本、
起本地服务、装个无头浏览器截图对比 …… 都行。hypoloop 不替它做主，也就不用背它的
依赖。唯一的边界是：取证用的工具和临时产物不许留在目标仓库里。

### 3. agy 有三个会静默出错的坑，都踩过了

这三条都是实测撞出来的，改 `agy.py` 之前先读一遍：

| 坑 | 症状 | 怎么办 |
|---|---|---|
| **agy 不认子进程的 cwd** | 不给 `--add-dir`，agent 在 `~/.gemini/antigravity-cli/scratch` 里干活。**全程 `status=SUCCESS`**：你让它建文件，它回报「已创建」，路径在 scratch 底下；你让它读代码，它看到一个空目录。 | 每次调用都带 `--add-dir <目标目录>` |
| **headless 下权限一律自动拒绝** | 「a tool required the "read_file" permission that headless mode cannot prompt for, so it was auto-denied」——只读角色连代码都读不了，直接交白卷 | 所有角色都得给 `--dangerously-skip-permissions`；只读靠 `--mode plan` + 指纹管，不是靠扣这个标志 |
| **`--disable-slash-commands` 会顺手废掉 `--mode`** | agy 只是警告一句「--mode plan has no effect while slash command expansion is disabled」，然后只读角色**悄悄变成了可写角色** | 别用这个标志 |

### 4. 额度是**真额度**，不是本地估算

agy 没有任何 `quota` / `usage` 子命令，`~/.gemini` 下也没有额度文件。真数字只有一条路：

`-v=3` 是 glog 的 verbosity 标志（`agy --help` 里没列），打开后 agy 会把完整 HTTP
响应体写进 `~/.gemini/antigravity-cli/log/cli-*.log`。跑一次 `agy -v=3 models` ——
它只列模型、不生成内容，**不烧额度** —— 日志里就有：

```
v1internal:retrieveUserQuotaSummary
  groups[].buckets[]{window: "5h"|"weekly", resetTime, remainingFraction}
```

额度**按模型组分**（Gemini 一组，Claude+GPT 另一组），所以要按你用的模型选对组，
否则会拿另一组的数字冒充自己的。同一份日志里的 `loadCodeAssist` 给订阅档位。

这套办法的出处和踩过的坑来自 [flufy3d/taiji](https://github.com/flufy3d/taiji) 的
`hub/service/quota.py`，这里按 agy 1.1.25 重新实测过。

**它随时可能失效**（`-v=3` 是未公开标志）。失效时 hypoloop 会如实说"读不到"并把原因
打出来，**绝不编一个数字**，也绝不因此让任务跑不下去。

### 5. 环境事实自动探测，**任务结论一个字都不许预先喂**

`--evidence-hint` 很容易被用错。一开始它像是个方便的地方，可以把"我知道的事"都倒进去。
但倒着倒着就会倒进这种东西：

> "这个项目的分级是靠吃道具升的，不是靠里程 —— 里程这条轴是空的。"

这句话是**假设者的活**。一旦预先喂进去，这一轮就再也不能说明"系统能自己发现问题"了 ——
它只是把你写的答案抄了一遍。这个坑是实际踩过的。

所以现在划三层：

| 层 | 谁产出 | 举例 |
|---|---|---|
| **环境事实**（机器的属性） | `environ.py` 自动探测并注入验证者 | "agy 自带的 browser 工具在这台机器上装不上驱动" |
| **取证提示**（人的可选叮嘱） | `--evidence-hint`，**默认空** | "这个服务得先起 docker-compose" |
| **任务结论**（该改什么） | **只能**由三个角色自己得出 | —— |

`environ.py` 只报**这台机器上真的观测到的**东西：翻 agy 自己的日志确认内置 browser
工具是不是真的失败过、`ms-playwright` 缓存在不在、有没有 node/npm/git。不联网、不花额度、
不猜；探测不到就什么都不说。有一个测试专门盯着这段自动注入的文本里不许出现任何项目相关的
内容。

（顺带记一下那个 browser 工具的坑：agy 把 playwright-go 钉在了 driver 1.57.0，而微软已经
不在 `/builds/driver/` 托管 driver zip 了，新旧 CDN 全 404，Linux 上一样 —— 上游
[#638](https://github.com/google-antigravity/antigravity-cli/issues/638) /
[#629](https://github.com/google-antigravity/antigravity-cli/issues/629)。坏的只是 agy
内置的那个 Go 驱动，npm 上的 playwright 包是另一套东西，正常可用。）

### 6. 别信信封，看产出 —— 以及一条防止再犯的自查

这套东西的立论是「别信 agent 的声明，看它测出来的读数」。结果它自己栽在同一个坑里。

一次验证者调用返回了：

```
status            : ERROR
error             : The stream was interrupted. Please continue the task you were working on.
returncode        : 0            ← 正常退出
structured_output : 完整
response          : 连收尾总结都写完了
```

agent 真的把活干完了，中断的只是中途某一段流，agy 自己接上继续跑完，**只是信封上的
`status` 没改回来**。而 `AgyCall.ok` 只认 `status == "SUCCESS"`，于是这份完整产出被
整个丢弃、转去续接、续接没成、整轮判死。代价：一份含 4 条实证 1 条证伪的验证结果消失，
倒贴一次续接的额度，还在目标仓库的提交信息里写下一句假话（「第 2 轮：中止」）。

**修法分两层。**

一层是具体的：`AgyCall.degraded` —— 有完整产出就认，别信信封，同时把 agy 报了什么
如实打出来，不静默吞掉。

另一层是防这**一整类**的：`audit.py`。同样的错误还能从别的地方再犯 —— 提前 return、
schema 名改了导致产出落到别的文件名下、异常路径上漏了一次写盘。自查不去猜将来会
怎么错，只做一件事：**比对磁盘**。每步都会写 `<stem>.raw.json`（agy 原始返回）和
`<stem>.json`（采纳后的产出），凡是「raw 里有可用产出、却没有对应的 .json」的，就是
被丢掉的工作，跑完在提交**之前**大声报出来。这个判据不依赖任何一处具体逻辑，将来
换了实现照样成立。

拿 5 个历史运行目录实测过：精确抓到那 1 次事故，对另外 4 个保持沉默。

## 产物落在哪

全部在 `~/.hypoloop/`：

```
~/.hypoloop/
  runs/<时间戳>-<任务名>/
    roundN-hypotheses.json        # 各角色的结构化输出
    roundN-critique.json
    roundN-verification.json
    roundN-*.prompt.md            # 发出去的完整提示词，可复现
    roundN-*.raw.json             # agy 的原始返回，含 usage
    report.md                     # 人看的报告
  ledger.jsonl                    # 历次运行的 token 账
  projects/<hash>.json            # 每个项目的默认配置
```

**目标项目里一个字节都不留** —— 只有验证者对源码的正当修改会留在那边。这是硬约束：
用在别人的仓库上时，不能往人家的 `git status` 里塞东西。

## 开发

```bash
pip install -e .
python -m unittest discover -s tests -t .
```

额度解析的 fixture 是从真实 agy 日志里截下来的，不是手写的 —— 手写 fixture 只能证明
解析器和我脑子里的格式一致，证明不了它和 agy 真实吐出来的东西一致。

## License

MIT
