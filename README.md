# qq2wx-relay

把 QQ 群的消息截获、整合，发到微信群。
已经写的非常非常轮椅了，只要按 README 里头搞就不可能搞不了。

- QQ 侧：[NapCat](https://github.com/NapNeko/NapCatQQ)（OneBot 11，正版 NTQQ 注入）
- 微信侧：[wx4py](https://github.com/claw-codes/wx4py)（微信 UI 自动化，不注入、不碰数据库）

> ⚠️ **微信 4.1.13 及以上无法自动化 —— 这玩意我他妈也绕不过。
> 必须先降级到 4.1.8.28 并封锁它的自动更新。** 原因和实测证据见下一节。

```
QQ 群消息 ──> NapCat ──OneBot 11──> qq2wx-relay ──> wx4py ──> 微信群
                                    ├ 过滤（关键词/发送人/类型）
                                    ├ 去重（message_id）
                                    ├ 整合（按时间窗合并成一条）
                                    ├ 限流（每天上限 + 最小间隔）
                                    └ 留档（JSONL，可回溯）
```

---

## ⚠️ 先读这一段：这是**微信客户端**的限制，本项目绕不过

微信 4.1.13+ 把整个界面改成了**自绘渲染** —— 所有 UI 都画在 `MMUIRenderSubWindowHW`
这一块画布上，一个控件都不向 Windows 的 UIAutomation 暴露。自动化工具就瞎了，啥也找不到。

`qq2wx-relay` 改不了、也绕不过。想让 `driver: wx4py` 能用，只有一条路：
把微信降到 4.1.8.28，并且关掉它的自动更新。

**但是经过我大概两轮测试，我发现微信侧会自动更新**，无论是关掉进程还是自启动项都没办法。
想要长远用这玩意的话，可以自己写个降微信版本的脚本。

实测证据（同一台机器，只换微信版本）：

| 检查项 | 4.1.13.65 | 4.1.8.28 |
|---|---|---|
| 注册表 `RunningState` | ✓ = 1 | ✓ |
| 系统屏幕阅读器标志 | ✓ = True | ✓ |
| 重启微信（进程全换） | ✓ 无效 | — |
| 热写 `Weixin.dll` 的 Qt accessibility gate | ✓ **成功**，根节点变成 `mmui::MainWindow` | — |
| **UIA 控件树里的内部控件** | ✗ **始终只有 2~3 个节点**，搜索框/输入框全为空 | ✅ **74~75 个节点 / 54~56 个 mmui 控件** |

以后我还会定期更新，换几台电脑试试有没有可能会出现一个结果。

**别找什么参数能打开，没有。**

不过也想过添加视觉来规避这个问题，但是传输效率会大大缩减，而且更消耗 token。这个以后再说吧。

> 另一条花了大代价才试出来的结论：**`InvokePattern.Invoke()` 对微信自绘按钮无效**，
> 必须用 `Click(simulateMove=True)`（走 SendInput 真正模拟鼠标点击）。
> 这是登录能成功的关键，`qq2wx/senders.py` 里已经这么处理。

### 三条路，按稳定性排

| 方案 | 能用到什么 | 说明 |
|---|---|---|
| **A. 降到 4.1.8.28** | QQ → **普通微信群** | **本项目实际采用的方案**，已实测跑通 |
| B. `driver: webhook` | QQ → **企业微信/钉钉群** | 不需要微信客户端自动化，最省心，但进不了普通微信群 |
| C. `driver: dry-run` | QQ → 日志 | 验证 QQ 链路和整合效果 |

降级与封锁更新的做法见 [`微信侧方案说明.md`](微信侧方案说明.md)，
配套脚本是 `降级微信并封锁更新.bat` 和 `tools/fix_wechat_version.py`。

### ⚠️ 两个必须知道的坑

1. **微信窗口绝对不能最小化，也不要点右上角的 ×**（那只是收进托盘）。
   实测：窗口一进托盘，UIA 树会从 74 个节点塌成 1 个，而且**恢复窗口不会自愈**，
   必须重启微信。程序里有 `wechat_uia_healthy()` 守卫会安全跳过，但那就发不出去了。
2. **微信会静默自动升级**，升级过程还可能崩成白屏。必须按上面的方法封锁更新。

> QQ 那半边（NapCat → 过滤 → 去重 → 整合 → 限流 → 留档）**已经完全可用并实测通过**，
> 与微信侧用哪个通道无关。

---

## 一、这套东西为什么这么选

| | 选择 | 理由 |
|---|---|---|
| QQ 侧 | NapCat | OneBot 11 是事实标准，成熟稳定；扫码登录，不用碰加密数据库 |
| 微信侧 | wx4py | 微信 4.0 换了架构，老的 wcferry / wxauto 3.9 **全部失效**；wx4py 是专门适配 4.x 的 |
| 不做的事 | 解密微信数据库 | 微信 4.x 的库是 SQLCipher 加密的，取密钥要 hook 内存，成本高且更危险 |

**已实测环境**：Windows 11 + 微信 **4.1.8.28**（降级后）+ QQ NT 9.9.27 + Python 3.12.10 + wx4py 0.2.1

---

## 二、安装

### 1. 装 Python 依赖

```powershell
cd qq2wx-relay
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

> `.venv` 已经建好了。如果你想自己重建：`python -m venv .venv`

### 2. 安装并配置 NapCat

1. 去 [NapCatQQ Releases](https://github.com/NapNeko/NapCatQQ/releases) 下载 **`NapCat.Shell.zip`**，解压到**不带空格和中文**的目录（例如 `D:\NapCat`）
2. 双击 `launcher.bat` 启动（Win10 用 `launcher-win10.bat`）
3. 控制台会打印一个带 token 的地址，形如：
   ```
   [WebUI] WebUI Local Panel Url: http://127.0.0.1:6099/webui?token=xxxxx
   ```
   复制到浏览器打开，首次会要求改密码
4. 点「QQ 登录」→「QRCode 扫码」→ **用要当机器人的那个 QQ 号**的手机 QQ 扫码
5. 左侧「网络配置」→「新建」，加一个连接：

   **方式 A（推荐，配正向 WebSocket）**

   | 字段 | 值 |
   |---|---|
   | 类型 | **WebSocket 服务端** |
   | 端口 | `3001` |
   | Token | 留空（或自己填一个，同步到 `config.yaml`） |
   | Message Post Format | `array` |

   **方式 B（反向 WebSocket）**

   | 字段 | 值 |
   |---|---|
   | 类型 | **WebSocket 客户端** |
   | URL | `ws://127.0.0.1:6200` |
   | Message Post Format | `array` |
   | Reconnect Interval | `5000` |

   > 两种都支持，程序看 `qq.ws_url` 自动判断：
   > 地址是 `127.0.0.1` 之类就走正向连接；地址写成 `0.0.0.0` 就走反向监听。

> ⚠️ **建议用一个小号当机器人** —— NapCat 什么尿性你们也清楚，小心把你们的号封了。

### 3. 改配置

```powershell
copy config.example.yaml config.yaml
```

改这几处：

```yaml
qq_groups:
  "你的QQ群号":            # 换成真实的 QQ 群号
    - "你的微信群名"        # 换成真实的微信群名（不是群成员的昵称）

wechat:
  driver: "dry-run"       # 先保持 dry-run，验证通了再改 wx4py
```

---

## 三、跑起来（按顺序验证，别跳）

### 第 1 步：校验配置

```powershell
.\.venv\Scripts\python.exe -m qq2wx --check
```

### 第 2 步：静态检查微信接口

不连微信、不动键鼠，只确认库的接口对得上：

```powershell
.\.venv\Scripts\python.exe -m qq2wx --selfcheck-wx
```

### 第 3 步：打通 QQ → 转发链路（不真的发微信）

保持 `driver: dry-run`，启动：

```powershell
.\.venv\Scripts\python.exe -m qq2wx
```

然后用另一个 QQ 号往你配置的那个群里**发几条消息**。控制台应该出现：

```
准备发送：QQ群「xxx」3 条消息 -> 微信 班团（1 片）｜...
===== 2026-xx-xx xx:xx:xx -> 微信「班团」 =====
...
```

看到这个说明 **QQ 那半边已经完全通了**。

### 第 4 步：验证微信能发出去

先做真实自检（会给**文件传输助手**发一条，不会打扰任何群）：

```powershell
.\.venv\Scripts\python.exe -m qq2wx --probe-wx
```

> 这一步会操作微信窗口，**执行期间别动鼠标键盘**。

然后把 `config.yaml` 改成 `driver: "wx4py"`，发一条测试消息到你配置的群：

```powershell
.\.venv\Scripts\python.exe -m qq2wx --test
```

微信群里收到测试消息，就可以正式跑了：

```powershell
.\.venv\Scripts\python.exe -m qq2wx
```

### 离线回放（不接 QQ 也能验证整合逻辑）

仓库里带了一份样例事件：

```powershell
.\.venv\Scripts\python.exe -m qq2wx --replay samples\events.sample.jsonl --speed 0.1
```

想拿真实的 QQ 事件做回放样本：

```powershell
.\.venv\Scripts\python.exe -m qq2wx --capture        # 只记录不发送，Ctrl+C 结束
.\.venv\Scripts\python.exe -m qq2wx --replay logs\captured.jsonl
```

---

## 四、配置说明

### 整合策略（`aggregate`）

这是"整合"的核心。默认 **60 秒内的消息合成一条**发出去，而不是来一条转一条。

```yaml
aggregate:
  window_seconds: 60     # 汇总窗口。0 = 实时逐条转发
  max_batch_size: 40     # 攒够 40 条立即发，不等窗口到期
  quiet_seconds: 0       # >0 时，最后一条消息之后还要静默这么久才发
  template: |            # 整批的格式
    【QQ群 · {group_name}】{count} 条新消息
    ---------------- 截至 {time} ----------------
    {messages}
  line_template: "{sender}: {content}"   # 单条的格式
```

可用变量：`{group_name}` `{group_id}` `{count}` `{time}` `{messages}` `{sender}` `{content}` `{index}` `{total}`

**常见调法**

| 想要的效果 | 怎么配 |
|---|---|
| 实时逐条转发 | `window_seconds: 0` |
| 每 5 分钟汇总一条 | `window_seconds: 300` |
| 只在下班前汇总一次 | 见下方"定时汇总" |
| 消息太长自动分片 | `wechat.wx4py.max_chars`（默认 1800） |

### 过滤（`filters`）

```yaml
filters:
  ignore_senders: ["123456"]      # 这些 QQ 号不转
  ignore_keywords: ["拼多多"]      # 含这些词不转
  include_keywords: []            # 填了就变白名单：只转含这些词的
  forward_images: true            # 图片转成 [图片]
  forward_files: true             # 文件转成 [文件: 名字.pdf]
  forward_face: false             # [表情] 不转
  ignore_self: true               # 忽略机器人自己发的，防回环
```

### 限流（`limits`）

```yaml
limits:
  max_sends_per_day: 200   # 每天最多发多少条，0 = 不限
  min_send_interval: 3     # 两条之间的最小间隔秒数
```

**这两个是防封号的关键**，别调得太大胆。

---

## 五、三种微信通道

`wechat.driver` 三选一：

| driver | 说明 | 稳定性 |
|---|---|---|
| `wx4py` | 真实发到微信群（要微信在前台） | 中，够用 |
| `webhook` | 发到企业微信/钉钉群机器人 | **最稳**，但收不到普通微信群 |
| `dry-run` | 只写日志不发送 | — |

如果你能接受把通知发到**企业微信群**，用 `webhook` 是最省心的：

```yaml
wechat:
  driver: "webhook"
  webhook:
    url: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx"
    kind: "wecom"
```

---

## 六、必须知道的限制和风险

### wx4py 的硬限制（微信 4.x 的技术天花板）

1. **微信窗口必须在前台、不能最小化**，运行时最好别用鼠标键盘（它是模拟点击的）
2. **拿不到微信群消息的发送者**——微信 4.x 的 UI 不暴露这个信息。所以本工具**只支持单向**（QQ → 微信），没法在微信侧按人区分
3. 同名的群/联系人可能选错目标
4. 微信一升级就可能失效，等 wx4py 跟进

### 账号风险

- NapCat 注入 QQ 属于第三方工具，违反腾讯用户协议，**有封号可能**，用小号
- wx4py 模拟真人操作、不修改客户端，风险相对低，但**控制频率**（用默认限流）
- 不要把群聊内容用于商业营销，也别往群里刷屏

### 法律与隐私

聊天记录属于通信秘密。处理**自己的**账号数据、在自己有权限的群里转发，通常没问题；如果涉及他人消息的对外扩散，请自行确认有合法依据。用途请自己把握，出了问题我不负责 🙂

---

## 七、排障

| 症状 | 原因 / 解法 |
|---|---|
| `连接 NapCat 断开，xx 秒后重连` | NapCat 没启动，或「网络配置」里没建 **WebSocket 服务端**，或端口不是 3001 |
| 一直没反应，群里发消息也不动 | 群号写错；或该群不在 `qq_groups` 里；看日志 DEBUG 级别 |
| `微信通道初始化失败` | 微信没登录 / 窗口被最小化 / 先跑 `--probe-wx` 定位 |
| `调用微信发送接口时参数不匹配` | wx4py 版本变了，跑 `--selfcheck-wx` 看实际可用方法（程序有自适应，通常不会走到这） |
| 微信群里消息太长只发出一半 | 调小 `wechat.wx4py.max_chars` |
| 收到重复消息 | 已经按 `message_id` 去重；如果 NapCat 重连后重复，属于正常兜底 |
| `已经有一个实例在运行` | 上次没退干净，删掉 `logs\qq2wx.lock` |
| 中文日志乱码 | 程序已自行切 UTF-8；若仍乱码，`$env:PYTHONIOENCODING="utf-8"` |

---

## 八、代码结构

```
qq2wx-relay/
├─ qq2wx/
│  ├─ __main__.py    CLI 入口（--check/--test/--probe-wx/--selfcheck-wx/--replay/--capture）
│  ├─ config.py      配置加载 + 校验
│  ├─ core.py        日志/限流/状态持久化/OneBot 消息渲染
│  ├─ qq.py          QQ 侧：正向 WS、反向 WS、离线回放
│  ├─ aggregate.py   整合引擎（时间窗/去重/渲染/分片）
│  ├─ senders.py     微信通道（wx4py / webhook / dry-run）
│  └─ pipeline.py    主流程串联 + 单实例保护
├─ samples/
│  └─ events.sample.jsonl   样例 QQ 事件，用于离线回放
├─ config.example.yaml
├─ requirements.txt
└─ logs/             运行日志 / 留档 / 状态
```

### 想扩展？两个自然的入口

- **换微信通道**：在 `senders.py` 里加一个 `Sender` 子类、在 `build_sender()` 里注册，主流程完全不用动
- **改整合逻辑**：`aggregate.py` 的 `Aggregator.render()` 决定"长什么样"；想接 LLM 做摘要，在 `_emit_locked()` 里把 `messages` 先丢给模型再渲染

---

## 九、自测覆盖情况

已经用离线回放验证过的分支：

- ✅ 群消息规范化（数组格式 + CQ 码字符串两种都能解析）
- ✅ 群名片优先、无名片回退昵称
- ✅ `@` / 图片 / 文件 / 表情 / 引用回复 的类型开关
- ✅ `ignore_self` 自我消息过滤（防回环）
- ✅ `message_id` 去重
- ✅ 关键词黑名单过滤
- ✅ 非监听群、私聊消息忽略
- ✅ 时间窗到期汇总
- ✅ 条数阈值立即发送
- ✅ 超长自动分片 + `（i/n）` 后缀
- ✅ 留档 JSONL 完整记录
- ✅ 正向/反向 WS 地址识别

**已实测通过的**（在真实环境里跑过）：

- ✅ NapCat 实际连上并收到真实群消息（同步到 27 个群）
- ✅ wx4py 实际把消息发进微信群（微信 4.1.8.28，`--selfcheck-wx` 通过）
- ✅ 图片 / 文件真实下载并发送（`.xlsx` 等 Office 文件扩展名正确）
- ✅ 微信登录流程（关键：`Click(simulateMove=True)` 而非 `InvokePattern.Invoke()`）
- ✅ 只转发进程启动**之后**的消息，不会把历史消息一股脑刷出去

### 自动化测试套件

| 启动器 | 测什么 | 项数 |
|---|---|---|
| `7-离线自检.bat` | 管线逻辑（过滤 / 去重 / 聚合 / 分片 / 限流 / 媒体 / 留档） | 36 |
| `8-真实数据自检.bat` | 用真实 NapCat 消息跑完整管线 + 真实下载媒体 | 18 |
| `9-接口绑定自检.bat` | 传给 wx4py 的参数与真实签名是否匹配 | 30 |

合计 **84 项，全部通过**（其中 18 项需要 NapCat 在线）。

**没有 NapCat / 没登录时**，第二套可以离线回放跑，不需要任何真实环境：

```powershell
.\.venv\Scripts\python.exe tools\test_e2e_real.py --from-file samples\events.sample.jsonl
# 结果：11 通过 / 0 失败 / 2 跳过
```

跳过的两项是媒体下载（样本里的 URL 是假的，必然下不动，改由在线模式验证）。
「非监听群被忽略」和「重复消息被去掉」这两个负例由 `7-离线自检.bat` 覆盖，
所以离线回放时会先把它们排除掉，好让「解析出的消息全都发出去」这条强断言成立。

> 其中 `9-接口绑定自检` 抓到一个登录后必然崩溃的 bug：参数探测会用 `target=`
> 去调老式签名的 `send_msg(who, msg)`，直接抛 `TypeError`。已改成**位置传参**。

---

## 十、合规与免责声明

**请读完这一节再决定要不要用。**

1. **QQ 侧（NapCat）靠往 QQ 客户端注入 DLL 工作，违反腾讯用户协议。**
   腾讯有权对相关账号警告、冻结或封禁，本项目作者不对账号损失负责。
   **强烈建议用专用小号，不要用你的常用 QQ。**
   已知 QQ 会弹出「发现外挂程序」警告 —— 这不是误报，是这条技术路线固有的代价。
2. **微信侧（wx4py）靠 UI 自动化操作微信客户端**，同样在微信用户协议之外。
3. **微信会自动更新并破坏自动化能力**，本项目通过替换 `WeixinUpdate.exe` 封锁更新。
   这属于修改本机软件，请自行评估。
4. **转发他人聊天内容可能涉及隐私。** 是否转发、转发到哪里，后果由使用者承担。
   请遵守所在群组的规定和当地法律。
5. 本项目按 MIT 协议**完全不提供任何担保**，详见 `LICENSE`。

> 想规避前两条风险，改用 `driver: webhook` 推到企业微信/钉钉群 —— 那条路不碰这两个客户端。

---

## 十一、贡献前请注意

仓库装了**提交前敏感信息扫描**：

```powershell
python tools\check_secrets.py          # 扫描已跟踪文件
python tools\check_secrets.py --all    # 扫描整个工作区
```

启用 git hook（克隆后执行一次）：

```powershell
git config core.hooksPath .githooks
```

之后每次提交都会自动扫描，命中就阻断。误报可在那一行加 `# secrets-allow` 注释放过。

**绝对不要提交的东西**（都已在 `.gitignore` 里）：
真实群号 / QQ 号 / webhook key → `config.yaml`；
个人笔记与真实凭据 → `notes/`；
抓包样本 → `samples/captured*.jsonl`、`logs/captured.jsonl`。
