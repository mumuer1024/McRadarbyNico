---
name: mcd-radar
description: "麦当劳 MCP 预算盲盒与优惠雷达：按预算随机生成真实可下单的餐品组合，盘点优惠券/可领券/活动，并走统一的算价→确认→下单流程。当用户提到麦当劳点餐、预算吃麦当劳、今天吃什么麦当劳、麦当劳优惠券/活动/领券/下单时使用。"
description_zh: "麦麦雷达 McRadar · 麦当劳预算盲盒与优惠雷达"
description_en: "McRadar - McDonald's budget blind box & deal radar (via official McDonald's MCP)"
version: 0.1.0
display_name: "麦麦雷达 McRadar"
display_name_zh: "麦麦雷达 McRadar"
---

# 麦麦雷达 McRadar

用**麦当劳官方 MCP 的真实数据**帮你做两件事：

1. **预算盲盒** —— 给预算和忌口，抽一个真实可下单的餐品组合，用官方接口实时算价
2. **优惠雷达** —— 盘点优惠券、可领券、活动，匹配适用商品，比价选最优单券

两条主线共享同一条流水线：**门店上下文 → 菜单与优惠 → 实时算价 → 订单摘要 → 用户确认 → 创建订单 → 用户自行支付**。

---

## 🔴 安全红线（每次执行前必读）

| # | 红线 |
|---|---|
| 1 | **绝不自动付款**。MCP 没有任何支付工具，支付必须由用户本人完成 |
| 2 | **未收到明确确认，绝不调用 `create-order` / `auto-bind-coupons`** |
| 3 | **`create-order` 绝不重试**。超时/断连/5xx/响应不明 → 标记结果未知 → 先核对 → 需用户新授权才可再试 |
| 4 | **`auto-bind-coupons` 绝不重试**。响应不明 → 先重查券池 |
| 5 | **确认后不得更换**商品、门店、优惠券。改动必须回到摘要重新确认 |
| 6 | **金额只认 `calculate-price` 的实时结果**（单位：分）。菜单价只用于筛选，不作为应付金额承诺 |
| 7 | **不做过敏原识别，不承诺过敏安全**。忌口仅基于分类与名称关键词 |
| 8 | **订单创建成功 ≠ 已支付** |
| 9 | **不得保存** Token、支付凭证、完整地址、手机号到本地状态 |

`create-order` / `auto-bind-coupons` 是**写操作**：无论何时都必须在收到用户**明确授权**后才可调用（**用户选择方案 ≠ 授权**，见下方「工作模式与日常点餐契约」第 5 条）。默认按**日常点餐模式**运行。

---

## 🧭 工作模式与日常点餐契约

**默认进入「日常点餐模式」。** 开发、调试、验收、故障复盘**不属于**日常流程；除非用户明确提出，**不得主动进入开发维护模式**（不改源码、不跑全量测试、不重装 Skill、不生成开发报告）。

### 日常点餐契约（预算盲盒 / 优惠雷达通用）

1. **预算内优惠盲盒** —— 以预算为约束，探索当前门店可用的**套餐、单品及优惠组合**，**不预设固定菜品**。
2. **需求优先** —— 探索前先确认**预算口径、人数、配送方式、必要忌口**；已明确的信息**不重复询问**；**临时偏好只对当前订单生效**（长期持久化须另行明确授权）。
3. **主动探索** —— 菜单、优惠、组合、实时核价查询**自动执行**；**不得为了减少工具调用而削弱有效候选探索**。
   > 与 §1.3 的关系：§1.3 中 `calculate-price` 的 ≤5 次上限是**核价成本控制**，只约束"算价"这一步，**不是**限制探索；探索阶段该查的菜单/券/组合照常查。
4. **简洁展示** —— 默认给出**少量、有差异性**的候选组合，每个都包含：**商品明细**、**优惠后的商品实付价**、**预算剩余**，以及**配送费、打包费等额外费用**。
5. **明确授权** —— **用户选方案 ≠ 授权下单**。创建订单前必须取得**明确的下单授权**；§4 的全部安全闸门（确认状态机 / 算价覆盖校验 / 四重 `preflight` / `begin-submit` 内部复核）与**实时核价**要求**一律保留**，不得简省。
6. **支付与状态** —— 下单成功后给出支付指引（见 §7.1）；**支付由用户本人完成**；**用户反馈已付款后**，再做**只读**状态核对（`query-order`）。
7. **正常结束** —— 订单核对完成后**简短汇报**即可；**不自动**生成开发报告、修改源码、运行全量测试或重新安装 Skill。
8. **异常分流** —— 非阻断问题**记录证据**、留待事后复盘；一旦涉及**订单安全、价格异常或重复提交风险**，**立即停止危险操作**并如实告知用户。

### 测试与推广边界

- **新会话**按日常流程运行时，**不得**把历史测试订单的门店、地址、优惠或个人偏好当作当前用户数据。
- 保留运行日志**供事后效率审计**，但正常点餐过程中**不主动**做性能重构或代码修复。
- **不得**把某个城市 / 门店 / 账号的测试结果推广为"所有地区均适用"。

> ⚠️ 本节只约束**交互方式与收尾动作**，**不改变**任何授权、核价、幂等与支付红线；与上文红线 1–9 冲突时**以安全红线为准**。

---

## 🏗️ 调用架构（重要）

**MCP 调用由你（宿主）负责，Python 脚本只做数据处理与状态校验。**

```
你 ──调用 MCP 工具──> 麦当劳 MCP
 │                        │
 │<──── JSON 返回 ────────┘
 │
 ├── 把返回喂给 scripts/mcd_radar.py（--json / --file / stdin）
 │      脚本负责：单位换算、过滤、组合、券匹配、摘要、闸门校验
 │
 └── 按脚本返回的 allowed/ok 决定是否执行真实写操作
```

**为什么这么设计**：MCP Token 由宿主持有，脚本不实现 MCP 客户端、不接触 Token，也就不存在 Token 泄露与协议兼容问题。

### 命令入口

```bash
PY="<python>"          # 见下方「环境」
SK="<skill_dir>"       # 已安装 Skill 的目录
$PY "$SK/scripts/mcd_radar.py" <command> --json '{...}'    # 或 --file x.json
$PY "$SK/scripts/mcd_radar.py" <command> --md --json '{...}'  # 输出 Markdown
```

命令一览：`selftest` `stores` `save-store` `load-store` `money`
`normalize-menu` `plan-combos` `parse-rounds` `plan-swaps`
`normalize-coupons` `match-coupons` `build-trials` `pick-best` `campaign`
`build-order` `prepare-order` `preflight` `begin-submit` `finish-unknown`
`finish-confirmed` `finish-failed` `authorize-retry` `reconcile`
`parse-order` `order-status` `gc-orders`
`plan-bind` `prepare-bind` `preflight-bind` `begin-bind`
`finish-bind-unknown` `finish-bind-done`

---

## 0. 门店上下文（所有流程的前置）

`storeCode` 是菜单/价格/下单类工具的**必填项**，必须先确定。

```bash
# 读已有默认门店
$PY .../mcd_radar.py load-store --json '{}'
```

若没有默认门店 → 调用 MCP `query-nearby-stores`（`beType=1, searchType=1` 收藏餐厅；
或 `searchType=2` + `city` + `keyword`）→ 让用户选 → 落盘：

```bash
$PY .../mcd_radar.py stores  --file nearby.json        # 规范化
$PY .../mcd_radar.py save-store --json '{"store_code":"1000001","store_name":"...","be_type":1,"order_type":1}'
```

beType 与 orderType / beCode 的对应：

| beType | 场景 | orderType | beCode |
|---|---|---|---|
| 1 | 到店自取 | 1 | **不传**（传了会报错） |
| 5 | 得来速 | 1 | **必传**（来自 `query-nearby-stores`） |
| 2 | 麦乐送到家 | 2 | **必传**（来自 `delivery-query-stores`） |
| 6 | 企业团餐 | 2 | **必传**；另需 `gmServiceCode` |

> ⚠️ beCode 规则按 **beType** 判定，不是按 orderType。
> 得来速是 `orderType=1` 但**需要** beCode，与到店自取相反（修复过的坑）。

---

## 1. 预算盲盒（功能 A）

### 1.1 流程

```
① 解析用户意图 → 预算(元)、模式(random/full/value)、忌口、是否吃辣、候选数
② MCP: query-meals(storeCode, orderType, beType)          ← 只读
③ 脚本: plan-combos  → 候选组合（指纹已去重、预算硬约束已生效）
④ 对每个候选（最多 3-5 个）调用 MCP: calculate-price      ← 只读、幂等、无副作用
⑤ 脚本: pick-best（若要选券）/ 组装 priced 映射
⑥ 渲染输出：明细 / 估算 / 实时应付 / 优惠 / 剩余预算
⑦ 用户说"换一个" → 把已抽中的 fingerprint 放进 exclude_fingerprints 重跑 ③
```

### 1.2 调用示例

```bash
$PY .../mcd_radar.py plan-combos --json '{
  "menu": <query-meals 的原始返回>,
  "budget_yuan": 30, "count": 3, "mode": "random", "seed": 7,
  "prefs": {"no_spicy": true, "exclude_keywords": ["鱼","咖啡"], "exclude_categories": [], "dislike_codes": []}
}'
```

### 1.3 必须遵守的约束

- **预算是硬约束**：估算总额 > 预算的候选直接丢弃
- **只对少量候选调 `calculate-price`**（≤5），不做无意义批量请求
- **`canWithOrder=true` 的商品默认排除**（其 `currentPrice` 依赖随单购麦金卡，不买卡拿不到）
- **低预算无解时如实说明**，返回 `exhausted=true` 与最便宜单项，绝不编造组合
- **辣度三态**：命中辣味词 → 排除；无法判断 → 保留并标记，输出时必须说明「无法保证完全不辣」
- **超预算救场**：对套餐调 `query-meal-detail` → `plan-swaps`，优先用 `diffPrice` 为负的子项替换压进预算

```bash
$PY .../mcd_radar.py plan-swaps --json '{"detail": <query-meal-detail 返回>, "over_fen": 150}'
```

---

## 2. 优惠雷达（功能 B）

```bash
# 券池（三个接口能力不同，必须分开喂）
$PY .../mcd_radar.py normalize-coupons --json '{
  "store": <query-store-coupons 返回>,
  "my": <query-my-coupons 返回>,
  "available": <available-coupons 返回>
}' --md
```

| 接口 | 能给什么 | 能不能驱动算价 |
|---|---|---|
| `query-store-coupons` | couponId + couponCode + 有效期 + **适用商品 productCode** | ✅ 能 |
| `query-my-coupons` | 券名、有效期、时段、星期、「今日到期」标签 | ❌ 不能（无 couponId） |
| `available-coupons` | 券名 + 状态（已领取/可领取） | ❌ 不能 |

### 2.1 单券试算（一单只能用 1 张）

```bash
$PY .../mcd_radar.py match-coupons --json '{"coupons":[...], "item_codes":["1450","3050"]}'
$PY .../mcd_radar.py build-trials  --json '{"base_request":{...}, "coupons":[...]}'
# 对每个 trial 逐个调 calculate-price
$PY .../mcd_radar.py pick-best     --json '{"entries":[{"coupon":..., "result":<返回>}, ...]}'
```

> 官方错误码 `600022 暂不支持多张券使用` —— **绝不承诺"叠加最优"**，只能逐券试算取实付最低。

### 2.2 活动

```bash
$PY .../mcd_radar.py campaign --json '{"calendar": <campaign-calendar 返回>, "action":"diff"}' --md
```

⚠️ 活动接口**没有结构化起止日期、没有适用范围、没有门店字段**。
- 分组标题里的日期**不是有效期**
- 不做倒计时
- 范围/截止/适用门店一律标注「未知」

---

## 3. 一键领券闸门（功能 B4）

```
① MCP: available-coupons                                   ← 只读
② 脚本: plan-bind → 展示可领清单 + 批量领取说明
③ 【必须明确告知】auto-bind-coupons 会尝试领取全部当前可领券，
   MCP 没有单张领取接口，无法只领其中一张
④ 用户明确确认（一次性确认全部可领券，且必须让用户知道无法只领一张）
⑤ 脚本: prepare-bind（登记可领范围快照 scope_digest，状态 prepared）
   → confirm-bind(user_confirmed=true)  ← **宿主收到用户明确同意后才调用**
     返回一次性凭证 nonce，状态转 confirmed
   → preflight-bind(nonce=...) → allowed=true 才继续
⑥ 脚本: begin-bind --json '{"nonce":"..."}'  ← 凭证校验通过才登记 submitted（仅一次）
   ⚠️ begin-bind 不接受空凭证；直接调用会被拒绝
⑦ MCP: auto-bind-coupons  ← 写操作，需用户**明确授权**；无授权则到此为止并说明
⑧ 脚本: finish-bind-unknown（响应不明）或 finish-bind-done（拿到结果后重查券池）
⑨ 汇报：只报告可可靠确认的变化，不虚构精确领取数量
```

```bash
$PY .../mcd_radar.py plan-bind        --json '{"pool":{...}}'
$PY .../mcd_radar.py prepare-bind     --json '{"pool":{...}}'
#   → 状态 prepared（**不可提交**），登记可领范围快照 scope_digest
$PY .../mcd_radar.py confirm-bind     --json '{"pool":{...},"user_confirmed":true}'
#   → 返回 {"nonce":"..."}，状态 confirmed
#   🔴 券池范围变化（增加或减少）→ ok=false + requires_redisplay=true，
#      旧确认**立即作废**（nonce 清空、状态回 prepared）。
#      必须先用 prepare-bind 重新登记、向用户重新展示变化后的领取范围，
#      再重新 confirm-bind 才能拿到新凭证 —— 严禁静默替换快照后沿用原确认。
#     上次结果 unknown/failed/done → 需 "reauthorize":true（用户新的明确授权）
$PY .../mcd_radar.py preflight-bind   --json '{"nonce":"..."}'
$PY .../mcd_radar.py begin-bind       --json '{"nonce":"..."}'
$PY .../mcd_radar.py finish-bind-unknown --json '{"reason":"超时"}'
# 或：重新查券池后
$PY .../mcd_radar.py finish-bind-done --json '{"pool":{...}}'
```

> 券池存在**同名重复批次**，对比时不能用券名做唯一键。
> 接口没有可靠唯一标识时，只报「可领取券从 N 张变为 M 张」，**不声称精确领取张数**。

---

## 4. 统一订单闭环（功能 A/B 共用）

```
① 用户明确选择商品与数量（含套餐子项、特调）
② 确定门店、就餐方式、取餐方式
③ MCP: calculate-price                        ← 只读，实时核价
       · 顺便取 takeWayList[].code → takeWayCode
       · 若要选券：逐券试算取实付最低
③b 【必做】check-pricing 核对算价结果是否覆盖全部所选商品
       🔴 实测（2026-10-10 二次复现）：请求 {麦香鸡, 可乐中杯}，
          服务端 productList 只回了麦香鸡，price 也只算了麦香鸡 —— 商品被**静默丢弃**。
          缺任何一个商品都必须拦下，重新拉菜单重来，绝不让用户为"少了东西"的订单付款。
③c 脚本: record-pricing-check（必须传 request + pricing 原始响应，由脚本现场计算；
       🔴 不接受外部直接给的 ok=true，也不接受与 digest 不符的请求）
④ 构造最终订单摘要并展示给用户（商品/子项/门店/券/应付/核价时间）
⑤ 用户明确确认（一次完整展示 + 一次明确确认即可，不需要机械地连问两次）
⑥ 脚本: prepare-order（登记 digest，状态 awaiting_confirm）
   → confirm-order（**宿主在收到用户明确下单授权后**才调用，状态转为 user_confirmed）
⑦ preflight（四重校验：请求结构合法 + 请求未变更 + **用户已确认**
   + 算价覆盖校验新鲜、可靠且通过）
   allowed=true → begin-submit（**内部再跑一遍同一闸门**，不依赖你是否先跑过 preflight）
⑧ MCP: create-order  ← 写操作，需用户**明确授权**（用户选方案 ≠ 授权）；无授权则到此为止并说明
⑨ 脚本: finish-confirmed(orderId) / finish-failed(msg) / finish-unknown(reason)
⑩ 解析支付入口：有链接就展示并给出**已实测**的付款步骤（见 §7）；没有就降级为"打开麦当劳 App / 微信小程序找订单"
⑪ 用户自行支付；用户说"付完了" → MCP: query-order → 脚本 order-status
```

### 4.1 命令

```bash
$PY .../mcd_radar.py check-pricing --json '{"items":[{"productCode":"1450","quantity":1}],
                                            "pricing":<calculate-price 返回>}'
$PY .../mcd_radar.py build-order --json '{
  "store_code":"1000001","order_type":1,"be_type":1,
  "items":[{"productCode":"1450","quantity":1},{"productCode":"3050","quantity":1}],
  "take_way_code":"locker-out"}'          # → {request, errors, valid}

$PY .../mcd_radar.py prepare-order  --json '{"order":{...}, "est_payable_fen":2650}'
#   → 状态 awaiting_confirm（此时**不可提交**）
$PY .../mcd_radar.py record-pricing-check --json '{"digest":"...",
        "request":{...完整订单请求...}, "pricing":<calculate-price 原始返回>}'
#   🔴 ok 由脚本用 request + pricing 现场算出，不接受调用方声称的 ok
#   ⚠️ 必须 ok=true 才继续；ok=false → 回到 ③ 重新拉菜单重算
#   ⚠️ 结论只表示"本次返回覆盖了全部所选商品"，
#      不等于服务端承诺最终订单价格不变
$PY .../mcd_radar.py confirm-order  --json '{"digest":"...","request":{...},
        "user_confirmed":true,"note":"用户已口头确认"}'
#   ⚠️ 只能在收到用户明确下单授权后调用 → 状态 user_confirmed
$PY .../mcd_radar.py confirmation-status --json '{"digest":"..."}'
$PY .../mcd_radar.py preflight      --json '{"digest":"<上一步返回>","request":{...}}'
$PY .../mcd_radar.py begin-submit   --json '{"digest":"...","request":{...完整订单请求...}}'
#   🔒 begin-submit 内部强制复核：指纹 + 用户确认 + 状态 + 算价覆盖，
#      跳过 preflight 直接调用也会被拦；校验通过后若状态被改动则中止（TOCTOU）
# 调用 create-order 后三选一：
$PY .../mcd_radar.py finish-confirmed --json '{"digest":"...","order_id":"..."}'
$PY .../mcd_radar.py finish-failed    --json '{"digest":"...","message":"..."}'
$PY .../mcd_radar.py finish-unknown   --json '{"digest":"...","reason":"超时"}'
$PY .../mcd_radar.py reconcile        --json '{"digest":"..."}'
$PY .../mcd_radar.py parse-order      --json '{"result":<create-order 返回>}' --md
$PY .../mcd_radar.py order-status     --json '{"result":<query-order 返回>}' --md
```

### 4.2 异常处理（必须照做）

| 情况 | 处理 |
|---|---|
| 只 `prepare-order` 没 `confirm-order` | **不调用**。状态为 `awaiting_confirm`，preflight 必拒 |
| `confirm-order` 传 `user_confirmed:false` | 确认不生效，状态不变。**禁止**用模型推断代替用户确认 |
| 确认后用户要改商品/门店/券/备注/随单购 | 回到 ④ 重新生成摘要并重新确认（digest 变了，旧确认自动失效） |
| 确认超过 15 分钟 | 重新展示摘要并重新确认 |
| 缺少 / 过期 / 未通过的 `record-pricing-check` | **不调用**，回到 ③ 重新 `calculate-price` |
| `preflight.allowed=false` | **不调用** `create-order`，把 reason 告诉用户 |
| `create-order` 超时 / 断连 / 5xx / 响应不明 | `finish-unknown`；**禁止再次提交**；按 `reconcile` 的步骤先查 `order-list` / `query-order` |
| 核对后确认已创建 | `finish-confirmed`，展示已有订单 |
| 核对后确认未创建且服务端明确报错 | `finish-failed`；重新提交需用户新授权 → `authorize-retry` |
| 核对后仍无法确定 | 保持 unknown，**如实告诉用户"无法确定是否已创建"**，不得再提交 |

> 本地指纹只在同一设备同一数据目录有效，**不提供跨设备/跨进程全局幂等保证**，
> 不要对 user 声称"绝对不会重复下单"。

### 4.2b 信任边界（必须知道）

**Python 状态机只能记录「宿主声明已收到用户确认」这一事实，不能证明用户的真实意图。**

- `confirm-order` 的 `user_confirmed` 只能由你在**收到用户明确的下单授权后**填 `true`
- 禁止用"他大概是这个意思""上文已经说过要买"之类的推断代替
- 状态机的价值在于：让「未确认」这件事**可被验证、可被拦截**，而不是证明意图
- 同理，`preflight` 的通过只代表三个机械条件成立，不代表"用户同意了"

### 4.3 状态与支付期限

- 状态**只看 `orderStatus` 文本**。实测 `status:"40"` 与官方数字枚举不符，禁止解析数字码
- 未识别的状态文本 → 判为 `unknown`，**不推断成功或失败**
- 有 `expirePayTime` → 按该订单实际时间算剩余；**没有就不显示倒计时**

---

## 5. 金额单位（最容易错的地方）

| 来源 | 单位 |
|---|---|
| `query-meals.currentPrice` / `originalPrice` | **元**（字符串） |
| `calculate-price` 全部金额字段 | **分**（整数） |

脚本内部一律用**整数分**运算；展示前转元。禁止用浮点累加。

---

## 6. 环境

- Python：**3.10 及以上**（实测 3.13）。使用**你本机可用的解释器**：
  - Windows：`python`（或 `where python` 查绝对路径）
  - macOS / Linux：`python3`（或 `which python3`）
  - 下文命令统一用 `$PY` 代指该解释器，请替换为你自己的实际命令或路径
- **零第三方依赖**（只用标准库）
- 本地状态目录：Skill 目录下 `data/`（可用 `--data-dir` 覆盖，或环境变量 `MCD_RADAR_DATA`）

---

## 7. 已知限制（对用户要如实说）

- 没有过敏原接口 → 忌口只是分类 + 名称关键词，不承诺过敏安全
- 券不可叠加（官方 `600022`）
- 活动无结构化日期 → 不做倒计时、不做范围过滤
- 未支付订单是否自动释放、是否占用优惠券 → **仍未验证**（不得当作已知事实）
- MCP 无任何订阅/推送能力 → 提醒只能靠用户主动触发或 WorkBuddy automation

### 7.1 ✅ 已实测确认（2026-10-11 首次真实下单，用户亲测反馈）

以下是**真实观测**，不是推测，可放心对用户陈述：

| 项 | 实测结论 |
|---|---|
| 支付链接可直开 | ✅ 浏览器可直接打开，呈现**扫码画面** |
| 扫码后行为 | 用**麦当劳 App 或微信**扫码 → 跳转麦当劳 App；**未安装 App 则跳应用商店**（iPhone 实测跳 App Store 麦当劳页） |
| 登录要求 | App **未登录过需登录**；**已登录过免登录** |
| 不扫码是否可行 | ✅ **可行**。未支付订单会直接出现在**麦当劳 App / 麦当劳微信小程序**的订单列表里 |
| 15 分钟支付期限 | ✅ **真实有效**；在麦当劳 App 内查看该订单可看到倒计时 |

> ⚠️ 15 分钟是本单的**服务端 `expirePayTime` 实际值**，不是可以写死的全局常数。
