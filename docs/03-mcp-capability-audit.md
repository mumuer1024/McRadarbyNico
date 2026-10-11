# 03 · 麦当劳 MCP 实际能力审计

> 文档级审计：2026-10-09 22:50（北京时间）
> **第一轮实测更新：2026-10-09 23:05（北京时间）**
> **第二轮实测更新：2026-10-09 23:55（北京时间）** —— 新增「订单」与「一键领券」专项调查
> 测试环境：门店 `1000001`（示例餐厅），`beType=1` 到店自取 / `orderType=1`
> 累计 **16 次**真实调用，**全部只读**，详见 `research/tool-test-results.md`
> ⛔ `create-order` 与 `auto-bind-coupons` 均未实际调用（会产生真实订单 / 改变账户权益），等待用户单独授权

---

## 0. 状态标记与可信度

| 标记 | 含义 |
|---|---|
| ✅ 已验证 | 本轮实际调用成功，输入参数与返回结构均已记录 |
| 📘 Schema 确认 | `inputSchema` 已从 MCP 服务端读取确认，但**未实际调用**（多为高风险或本轮不必要） |
| ⚠️ 部分不符 | 实测可用，但能力弱于预期 |
| ❌ 已否决 | 实测证明不可用或不支持 |
| ⛔ 禁止调用 | 会改变账户/订单状态，仅记录定义 |

---

## 1. 全局架构约束（实测确认）

**所有餐品/价格/下单类工具共享同一套必填上下文**，这是产品设计的硬约束：

```json
{
  "storeCode": "必填，门店编码，来自 query-nearby-stores / delivery-query-stores",
  "orderType": "必填，1=到店（含到店自取+得来速），2=外送（含麦乐送+团餐）",
  "beType":    "必填，1=到店取餐，2=麦乐送到家，5=得来速，6=企业团餐",
  "beCode":    "条件必填：beType=1 不传；beType=5 取 query-nearby-stores；beType=2/6 取 delivery-query-stores"
}
```

| beType | 场景 | orderType | beCode |
|---|---|---|---|
| 1 | 到店自取 | 1 | ❌ 不传（传了会报错） |
| 5 | 得来速（Drive Through） | 1 | ✅ 必传 |
| 2 | 麦乐送到家 | 2 | ✅ 必传 |
| 6 | 企业团餐 | 2 | ✅ 必传 |

> 📌 **门店上下文不可跳过**。Skill 首次使用必须引导用户定位门店，或缓存默认门店。

**金额单位差异（⚠️ 容易踩坑）**

| 来源 | 单位 |
|---|---|
| `query-meals` 的 `currentPrice` / `originalPrice` | **元**（字符串，如 `"33.5"`） |
| `calculate-price` 的所有金额字段 | **分**（整数，如 `2650`） |

---

## 2. 核心工具矩阵（功能 A 相关）

### ✅ `query-meals` — 餐品列表【本轮最关键】

| 字段 | 内容 |
|---|---|
| 功能 | 查询指定门店可售餐品菜单 |
| 必填参数 | `storeCode`、`orderType`、`beType` |
| 可选参数 | `beCode`、`reservationDate`（`yyyy-MM-dd HH:mm`，预约场景必传） |
| 返回 | `categories[]`（`name` + `meals[{code, tags[]}]`）、`meals{}`（code→详情映射）、`frequent`（常点） |
| 餐品详情字段 | `name`、`image`、`**currentPrice**`（现价，元）、`**originalPrice**`（原价，元）、`discountType`、`canWithOrder`、`withOrder{membershipCode, specId, cardId, cardType}` |
| 鉴权 | Token |
| 测试状态 | ✅ 已验证 |
| 关键限制 | 必须门店上下文；价格随门店/场景变化 |
| 产品用途 | **盲盒选品池 + 价格来源（无需逐个试算）** |

`discountType` 取值：`null`（不享受）、`早餐卡优惠`、`促销优惠`、`麦金卡优惠`、`随单购早餐卡优惠`、`随单购麦金卡优惠`

⚠️ **价格陷阱（必须处理）**：`canWithOrder=true` 的商品，其 `currentPrice` 是**绑定随单购会员卡的价格**。不买卡则按 `originalPrice` 计。
（实测：`9900016309 龙焰鸡腿堡四件套` currentPrice=29，originalPrice=57，价差 28 元）

**实测分类（13 个）**：人气热卖、精选单人餐、鸡肉汉堡/卷、小食拼盘/多人餐、蘸酱炸鸡、随心配1+1、巨无霸牛鱼肉堡、炸鸡、大堡口福单人餐、小食甜品/其他、开心乐园、麦咖啡™、饮品

**实测标签（可用于忌口/偏好过滤）**：上新、麦金卡、套餐、单品、小食、冰淇淋、冷饮、热饮、爆款、川香风味、随心拼、第二份优惠、限定特惠、奶昔回归、蓝莓、灰焰、蘸酱、韩式风味、浓郁芝香、使用动物奶油、童年童款、鲜萃咖啡…

---

### ✅ `query-meal-detail` — 餐品详情【超出预期】

| 字段 | 内容 |
|---|---|
| 功能 | 查套餐组成、**可替换选项**、特制选项 |
| 必填参数 | `storeCode`、`orderType`、`beType`、`code` |
| 可选参数 | `beCode`、`reservationDate` |
| 返回 | `code`、`name`、`image`、`supportModify`、`rounds[]`、`modification{}` |
| `rounds[]` 结构 | `{id, name, quantity, maxQuantity, minQuantity, choices[]}` |
| `choices[]` 结构 | `{code, name, quantity, maxQuantity, **isDefault**, **diffPrice**, supportModify, modification}` |
| `diffPrice` | 差价字符串：`"+ ¥0"` / `"+ ¥2.5"` / `"- ¥0.5"`（**可为负**） |
| `modification.items[]` | `{maxValues, minValues, values[{code, price, name, selectedQuantity, selectedKey, unselectedKey}]}` |
| 测试状态 | ✅ 已验证 |
| 产品用途 | **套餐内精细替换 + 差价控制 + 忌口细化** |

**实测示例（麦辣鸡腿汉堡三件套 ¥33.5）**

| 轮次 | 默认选项 | 可替换（差价） |
|---|---|---|
| 1 麦辣鸡腿汉堡 | 1440 | 无 |
| 2 选择套餐内小食 | 4810 中薯条 +¥0 | 心形薯饼 +¥1 / 扭扭薯 +¥1.5 / 大薯条 +¥2.5 / **小杯玉米杯 -¥0.5** / 大杯玉米杯 +¥2.5 |
| 3 选择套餐内饮料 | 3050 可乐中杯 +¥0 | 无糖可乐 +¥0 / 雪碧 +¥0 / 纯牛奶 +¥0.5 / 可乐大杯 +¥2 / 奶昔 +¥8 / 冰美式 +¥9 / 冰奶铁 +¥11 … |

> 🎯 这是盲盒的隐藏王牌：**预算差一点时，可通过"换成便宜的子项"把总价压进预算**（如换玉米杯 -¥0.5）。

⚠️ 特调传参规则：含 `unselectedKey` 的特调组，组内**所有项都必须传入**——选中的用 `selectedKey`，未选中的用 `unselectedKey`。

---

### ✅ `calculate-price` — 价格计算【无副作用，可批量调用】

| 字段 | 内容 |
|---|---|
| 功能 | 计算商品金额（**可含 1 张券**）、配送费、打包费、餐具费、优惠与应付总价 |
| 必填参数 | `storeCode`、`orderType`、`beType` |
| 可选参数 | `beCode`、`items[]`、`needTableware`、`reservationDate`、`withOrder`、`gmServiceCode` |
| `items[]` 字段 | `productCode`(必)、`quantity`(必,≥1)、`couponId`、`couponCode`、`modification{values[{code,key,quantity}]}`、`roundList[]`（套餐子项） |
| 返回 | `productOriginalPrice`、`productPrice`、`deliveryOriginalPrice`、`deliveryPrice`、`packingOriginalPrice`、`packingPrice`、`tablewarePrice`、`originalPrice`、`discount`、`price`、`productList[]`、`takeWayList[]`、`mealAssistanceList[]`、`enjoyed{}`、`enjoyable{}` |
| 金额单位 | **分** |
| 鉴权 | Token |
| 测试状态 | ✅ 已验证（含重复调用比对） |
| 副作用 | ✅ **确认无副作用**：同参重复调用结果一致，不产生订单/购物车 |
| 产品用途 | **最终价格裁决 + 用券试算 + 取餐方式获取** |

实测：`麦香鸡(¥17) + 可乐(¥9.5)` → `price=2650`（¥26.5），与菜单完全吻合。

`enjoyed` / `enjoyable`：`{amountType(1特价/2折扣/3立减), realDiscount, balance(门槛差额,分)}`
→ 可做 **"再买 ¥X 就能享 Y 优惠"** 的临门一脚提示。

`takeWayList[]`：`{code, title, subtitle}`，实测 `[{code:"locker-out", title:"外带", subtitle:"店外取餐柜"}]`，**`code` 是 `create-order` 的必填参数来源**。

---

### ❌ 券叠加 — 已否决

实测传入 2 张券：

```json
{"success":false,"code":600022,"message":"暂不支持多张券使用"}
```

🎯 **一个订单只能用 1 张券。这是官方错误码，不是推测。**

单券实测可用：`麦旋风任选1` 原价 ¥14 → 用券 ¥9.9（discount=410 分），与 `query-my-coupons` 标注的 ¥9.9 **完全吻合**。
另注意：券的 `productCode`（如 `9900014239`）**不在菜单里**，是券专用商品码，但可直接用于 `calculate-price`。

---

### ✅ `list-nutrition-foods` — 营养信息【无过敏原】

| 字段 | 内容 |
|---|---|
| 功能 | 常见餐品营养成分 |
| 参数 | 无 |
| 返回 | **纯文本表格，160 条**，表头 `{productName, nutritionDescription, energyKj, energyKcal, protein, fat, carbohydrate, sodium, calcium}` |
| 测试状态 | ✅ 已验证 |
| ❌ 缺失 | **无过敏原、无配料成分、无餐品编码** |
| 匹配难点 | 名称与菜单不完全一致（如"纯牛奶（盒装）" vs 菜单"纯牛奶(盒装)"全半角差异），需名称归一化 |
| 产品用途 | 热量/钠上限过滤（**必须带免责声明**）；**不能做过敏原规避** |

实测样例：`巨无霸,null,2146,513,27,26,42,961,171`｜`无糖可口可乐中杯,null,0,0,0,0,1,35,0`

---

### ✅ `now-time-info` — 时间基准

- 参数：无
- 返回：`timestamp`(ms)、`datetime`(ISO)、`formatted`、`date`、`year`、`month`、`day`、`dayOfWeek`、`timezone`(GMT+08:00)、`offset`、`utc`
- 实测：服务端时间即北京时间
- 用途：活动/券有效期判断、时段感知（早餐/午餐/夜市）

---

## 3. 门店与地址

### ✅ `query-nearby-stores`

| 字段 | 内容 |
|---|---|
| 必填 | `beType`(1/5)、`searchType`(1=收藏餐厅 2=按位置搜索) |
| 条件必填 | `searchType=2` 时需 `city` + `keyword` |
| 返回 | `storeCode`、`storeName`、`address`、`distance`(米)、`businessStatus`(bool)、`businessStartTime`/`businessEndTime`、`reservation`、`reservationTimeOptions[{date, today, reservationOptionText}]`、`beCode`、`longitude`/`latitude`（实测均为 0.0） |
| 测试状态 | ✅ 已验证 |
| 已知错误码 | `600050` 收藏餐厅列表为空 |
| 产品用途 | 门店定位；`businessStatus` 过滤打烊店；`reservationTimeOptions` 提供**时段划分**（早餐/午餐/下午茶/夜市/宵夜） |

### 📘 `delivery-query-stores` / `delivery-query-addresses`
- Schema 已确认存在；本轮未调用（涉及真实收货地址）
- `delivery-query-stores`：外送场景取 `storeCode` + `beCode`
- `delivery-query-addresses`：读取用户已创建地址，**敏感，返回后须脱敏**

### ⛔ `delivery-create-address`
- 会修改账户持久数据，仅记录定义，禁止本轮调用

---

## 4. 优惠券三分天下（重要）

实测发现三个券接口**能力完全不同**，必须组合使用：

| 工具 | 返回量 | 有 couponId？ | 有有效期？ | 有适用商品？ | 能驱动算价？ |
|---|---|---|---|---|---|
| `query-store-coupons` | 2 张（当前门店可用） | ✅ **有** | ✅ `tradeDateTime` | ✅ **`products[].productCode`** | ✅ **能** |
| `query-my-coupons` | 14 张（账户全部） | ❌ 无 | ✅ 精确到时段+星期 | ❌ 无（仅券名） | ❌ 不能直接 |
| `available-coupons` | 可领取列表 | ❌ 无 | ❌ 无 | ❌ 无 | ❌ 不能 |

### ✅ `query-store-coupons` — 唯一能驱动算价的券源

- 入参：`storeCode`、`orderType`、`beType`、`beCode?`、`reservationDate?`
- 返回：`{title, couponId, couponCode, tradeDateTime, products[{productCode, productName}], promotionId}`
- 有效期格式：`2026-10-05 10:30:00-2026-10-09 23:59:59`
- 🎯 **`products[].productCode` 让"优惠优先"模式真正可实现**：可反查哪些餐品能用券
- ⚠️ 只返回 2 张（而 `query-my-coupons` 有 14 张）→ 差异在于"当前门店 + 已持券码可直接核销"

### ✅ `query-my-coupons` — 适合做提醒

- 入参：无
- 返回 Markdown：券名、`优惠`（用券价格，如 ¥9.9）、`有效期`（日期+时段+星期）、`领取时间`、**`标签`（今日到期 / 到店专用 / 外送专用）**、图片
- 🎯 **「今日到期」标签是现成的提醒信号**，直接支撑功能 B 的到期提醒

### ✅ `available-coupons` — 只能做清单

- 入参：无
- 返回：券标题 + `状态`（已领取 / 可领取）+ 图片
- 用途：展示"还有哪些券没领"

### ⛔ `auto-bind-coupons` — 一键领券【第二轮专项调查】

**官方定义（README 原文）**：「麦麦省一键领券 —— 自动领取麦麦省所有当前可用的麦当劳优惠券。无需指定具体的优惠券和 couponId，系统会自动领取用户可领的所有券。」

| 调查项 | 结论 | 依据 |
|---|---|---|
| 入参 | **无**。`additionalProperties: false`，`properties: {}` | Schema 读取 |
| 是否一次性领取所有可领券 | ✅ **是**（官方明确"自动领取…所有券"） | 官方描述 |
| 是否存在单券领取接口 | ❌ **没有**。工具清单中领取类只有 `auto-bind-coupons` 一个；`available-coupons` 只查不领；`query-survey-coupon` 是满意度答卷查询，非领取 | 全量工具枚举 |
| 当前可领数量 | 实测 **仅 3 张**（免费脆薯饼 / 人气麦旋风买一送一 / 9.9元中杯冰美式） | P-16 |
| 是否有领取数量限制 | ❓ **未知**，官方文档与 Schema 均未说明 | — |
| 是否有账户条件（等级/实名/地域） | ❓ **未知** | — |
| 是否可能领到**付费**权益 | 🟡 **低风险**：实测可领 3 张中「9.9元中杯冰美式」是到店付费 ¥9.9 使用的券，但**领取动作本身免费**；麦麦省为官方优惠券中心 | P-16 |
| 是否可能**消耗积分** | 🟡 **倾向不会，未证实**。消耗积分的是 `mall-create-order`（积分兑换）与 `draw-lottery`（积分抽奖），是**另外的工具**；`auto-bind-coupons` 描述中无任何积分语义。<br/>✅ 已记录领券前积分基线 `availablePoint=145`，未来实测可直接比对 | P-17 |
| 重复调用的行为 | ❓ **未知**（幂等？重复发券？报错？均无证据） | — |
| 部分领取失败的返回结构 | ❓ **未知**（Schema 未暴露 outputSchema） | — |
| 可否查询领取前后券池变化 | ✅ **可以**：`available-coupons`（可领/已领状态）+ `query-my-coupons`（卡包）领前后各调一次即可对比 | P-16 / 第一轮 P-06 |
| 状态 | ⛔ **本轮未调用**（会改变账户权益） | 安全约束 |

⚠️ **两个必须向用户明示的事实**

1. **它是「批量领取全部」，不是「单张领取」**。产品文案与确认话术不得暗示"只领这一张"。
2. **领取范围由服务端决定**，用户看到的 3 张清单来自 `available-coupons`，但服务端实际发放范围可能与之不完全一致 → 领取后必须再查一次券池并如实汇报，不得预先承诺"会领到这 3 张"。

---

## 5. 活动与营销

### ⚠️ `campaign-calendar` — 能力弱于预期

| 字段 | 内容 |
|---|---|
| 入参 | `specifiedDate`（可选，`yyyy-MM-dd`；传入则返回该日 + 前后各一个最近有活动的日子共三天；不传返回当月全部） |
| 返回形态 | **Markdown 渲染文本，按日期分组** |
| 分组标题 | `2026年10月7日 往期回顾` / `2026年10月9日 今日` / `2026年10月10日`（未来） |
| 每条活动 | `活动标题` + `活动内容介绍`（emoji 营销文案）+ `活动图片介绍`（img URL） |
| ❌ 缺失 | **无结构化起止日期**、**无适用范围**、**无参与条件**、**无门店字段** |
| ⚠️ 内嵌价值 | 文案里含价格锚点：`15.9元起`、`26.9元起`、`29.9元`、`9.9元`、`13.9r`、`49元150天` |
| 测试状态 | ✅ 已验证（但能力不及预期） |
| 产品用途 | 「今天有什么」「未来 N 天有什么」「新增了什么（按标题 diff）」；**倒计时不可靠** |

### 📘 其余活动类工具
- `query-lottery-info` / `draw-lottery` / `query-my-prizes`：积分抽奖（⛔ `draw-lottery` 消耗积分，禁止调用）
- `query-party-city` / `query-party-store` / `query-partystore-date` / `query-party-store-session`：主题活动预约
- `query-promotions`：**README 未列出**，查门店满减/满折促销规则，文档标注仅团餐场景可用

---

## 6. 订单（第二轮专项调查）

### 6.0 状态总览

| 工具 | 本轮状态 | 说明 |
|---|---|---|
| `order-list` | ✅ **已验证**（只读） | P-14 |
| `query-order` | ✅ **已验证**（只读，查历史已完成订单） | P-15 |
| `create-order` | ⛔ **未调用**，Schema + 官方描述已确认 | 产生真实订单，需用户单独授权 |
| `cancel-order` | ⛔ **未调用**，Schema 已确认 | 会改变订单状态 |

### 6.1 ⛔ `create-order` —— 完整参数与 `calculate-price` 的映射

**Schema `required`**：`storeCode`、`orderType`、`beType`（**仅这三个**）
**但按场景实际必填的还有**：`takeWayCode`（orderType=1）、`addressId`（orderType=2）、`items[]`、`beCode`（beType=2/5/6）

#### 参数逐项对照表（`calculate-price` → `create-order`）

| `create-order` 参数 | 类型 | 必填条件 | 来源 / 与 `calculate-price` 的关系 |
|---|---|---|---|
| `storeCode` | string | **必填** | 与 `calculate-price` 同参，取自 `query-nearby-stores` / `delivery-query-stores` |
| `orderType` | 1/2 | **必填** | 与 `calculate-price` 同参 |
| `beType` | 1/2/5/6 | **必填** | 与 `calculate-price` 同参 |
| `beCode` | string | beType=2/5/6 必填；**beType=1 不传** | 与 `calculate-price` 同参 |
| `items[].productCode` | string | 必填 | ⚠️ 命名不同：`calculate-price` 也是 `productCode`，**一致** |
| `items[].quantity` | int | 必填（≥1） | 一致 |
| `items[].couponId` / `couponCode` | string | 选填 | 与 `calculate-price` 完全一致；**一单只能 1 张**（`600022`） |
| `items[].modification.values[]` | `{code, key, quantity}` | 选填 | 与 `calculate-price` 同结构；`key` 规则：选中项用 `selectedKey`，含 `unselectedKey` 的组内**未选中项也必须用 `unselectedKey` 传入** |
| `items[].roundList[]` | `{round, comboItemList[].{code, quantity, modification}}` | 选填（套餐） | 与 `calculate-price` 同结构；**这就是"套餐子项替换"的传入位置**（配合 `query-meal-detail` 的 `diffPrice`） |
| `withOrder` | `{membershipCode, membershipSpecId, cardId, cardType}` | 选填 | ⚠️ **在 Schema 中是顶层参数，不是 item 级**（官方 Input 描述写在 items 下，但 JSON Schema 明确在 root）；来自 `query-meals` 的 `withOrder` 字段 |
| `needTableware` | bool | 选填 | 与 `calculate-price` 同参 |
| `reservationDate` | string `yyyy-MM-dd HH:mm` | 预约场景必填，否则**不传** | 与 `calculate-price` 同参 |
| `gmServiceCode` | string | 仅 beType=6（团餐） | 来自 `query-meal-assistance` |
| **`takeWayCode`** | string | **orderType=1 必填**；orderType=2 不传 | 🔑 **唯一不在 `calculate-price` 入参里的必填项**：取自 `calculate-price` 返回的 `data.takeWayList[].code`（实测值 `locker-out`） |
| `addressId` | string | **orderType=2 必填** | 来自 `delivery-query-addresses` |
| `remark` | string ≤50 字 | 选填，外送场景建议引导填写 | 无对应算价参数 |

> 🎯 **结论：`create-order` 的入参 ≈ `calculate-price` 的入参 + `takeWayCode`（+ 外送的 `addressId`）。**
> 这意味着**可以把一次 `calculate-price` 的请求体直接复用为 `create-order` 的骨架**，只补 `takeWayCode`。这大幅降低了实现成本。

#### 返回内容（官方文档口径）

| 项 | 结论 |
|---|---|
| 官方描述 | 「根据门店信息、就餐方式、商品列表等信息创建订单，**返回订单详情与支付链接**」 |
| Next 引导语（官方原文） | 「您可以通过支付链接**扫码**或者**打开麦当劳APP**完成支付，支付完成后请回复'支付完成'或'支付失败'，会帮您查询最新的订单状态」 |
| 订单详情字段 | 结构与 `query-order` 返回一致（P-15 已验证该结构） |
| **支付链接字段名** | ❓ **未知** —— MCP 未暴露 outputSchema；`query-order` 字段清单中**没有 payUrl 类字段**，只有 `payId`。需在实测 `create-order` 时确认 |
| 支付链接能否在浏览器打开 | ❓ **未知**，官方文档无说明 |
| 是否支持跳转微信小程序 | ❓ **未知**，官方文档**从未提及**微信小程序，只提"扫码"与"打开麦当劳APP" |
| 用户是否需要重新登录 | ❓ **未知**。MCP 侧用 Token 代表用户身份，推测支付发生在麦当劳侧（App/扫码），可能要求登录同一账号；**无证据** |

#### 未支付订单的生命周期（部分已验证）

| 问题 | 结论 | 依据 |
|---|---|---|
| 支付时限 | ✅ **15 分钟** —— 实测 `createTime 20:08:12` / `expirePayTime 20:23:12` | P-15 🔥 |
| 超时后最终状态 | ❓ **未验证**。"已取消"还是"已过期"无证据；官方 `orderStatus` 注释枚举中只有 `7已取消`，无"已过期" | P-15 |
| 是否占用优惠券 | ❓ **未验证**。`query-order` 有 `couponList[]`，说明券被记入订单；但超时/取消后是否归还**无证据** | P-15 |
| 取消机制 | ✅ 有 `cancel-order`（`orderId` + `cancelReasonCode`：1改主意 2重复下单 3点错 4地址错 5时间错 -1其他） | Schema |
| 取消是否归还券 | ❓ **未验证** | — |

#### 工程侧防重与异常设计（建议，尚未实现）

| 风险 | 处理方式 |
|---|---|
| 重复创建订单 | ① 会话内维护"已下单指纹"（`storeCode`+`beType`+`items`排序+`couponId`+分钟级时间戳）；② 同一指纹在 **N 分钟内**拒绝二次创建，改为提示已有订单号；③ 创建后调 `order-list` 核对 `createTime` 是否出现新记录 |
| 创建失败 | 结构化错误码透出，**绝不重试** `create-order`（重试可能造成重复单）；改为提示用户手动确认后再次确认 |
| 超时 / 网络中断 | 超时**不得自动重试**；应先 `order-list` 查询是否已创建成功，再决定是"告知已有订单"还是"重新确认后创建" |
| 下单前是否必须重算价格 | ✅ **必须**。菜单/券缓存可能过期；流程规定为 `calculate-price`（取 `takeWayCode` + 校验实付）→ 展示摘要 → 用户确认 → `create-order` |
| 确认后变更 | 🔒 用户确认后**严禁**再更换商品、门店、优惠券；如需更改，必须回到确认前重新走一遍 |

### 6.2 ✅ `order-list` —— 历史订单（已验证）

- 入参：无
- 字段：`orderId`、`orderType`、`createTime`、`beType`、`beCode`、`storeCode`、`storeName`、`orderStatus`（**中文文本**）、`orderProductList[]`、`realTotalAmount`
- ⚠️ **不含**支付链接、不含 `expirePayTime`
- 🎯 用途：下单后核对是否重复创建；长期版做"再来一单"

### 6.3 ✅ `query-order` —— 订单详情（已验证）

- 入参：`orderId`
- 完整字段见 `research/tool-test-results.md` P-15
- ⚠️ **官方注释的 `orderStatus` 枚举（1/2/4/6/7/8/10）与实测返回 `status:"40"` 不一致** → **状态判断必须用 `orderStatus` 中文文本，不得依赖 `status` 数字码**
- ✅ 含 `expirePayTime`（支付截止时间）→ 可直接用于"还剩 X 分钟支付"提示
- ✅ 含 `pickupCode`（取餐码）、`lockerQrCode`（取餐柜二维码，1.0.9 新增）
- ⚠️ 含 `deliveryInfo.mobilePhone` / `customerNickname` / 地址 → **必须脱敏**（服务端本身已做部分脱敏：`138****0000`）

### 6.4 ⛔ `cancel-order`

- 入参：`orderId`（必填）、`cancelReasonCode`（必填，默认 1）
- 取值：`1=改主意了`、`2=重复下单`、`3=点错了/点多了/点少了`、`4=地址/电话填错了`、`5=送达时间选错了`、`-1=其它原因`
- ⛔ 本轮未调用；设计为：仅在用户明确要求时执行，且需再次确认订单号

---

## 7. 需求维度覆盖检查（实测后）

| 需求维度 | 结论 | 支撑工具 | 实测状态 |
|---|---|---|---|
| 菜单和餐品查询 | ✅ 完全支持 | `query-meals` | ✅ |
| 分类 / 标签 | ✅ 支持 | `query-meals.categories[].tags` | ✅ |
| **价格** | ✅ **支持** | `query-meals.currentPrice` | ✅ |
| 规格（大小杯/套餐子项） | ✅ 支持 | `query-meal-detail.rounds[].choices` | ✅ |
| 售卖状态 | ⚠️ 间接 | 菜单即"当前可售"；门店有 `businessStatus` | ✅ |
| 门店与地理位置 | ✅ 支持 | `query-nearby-stores`、`delivery-query-stores` | ✅ |
| 优惠券查询（可算价） | ✅ 支持 | `query-store-coupons`（含 couponId + 适用商品） | ✅ |
| 优惠券到期提醒 | ✅ 支持 | `query-my-coupons` 有效期 + 「今日到期」标签 | ✅ |
| 优惠券**叠加** | ❌ **不支持** | 错误码 `600022` | ✅ 已否决 |
| 活动查询 | ⚠️ 部分 | `campaign-calendar`（仅文本，无结构化字段） | ✅ |
| 活动**有效期** | ❌ 无结构化字段 | — | ✅ 已否决 |
| 活动**适用范围分层** | ❌ 无 | — | ✅ 已否决 |
| 限定新品 | ⚠️ 间接 | `query-meals` 的「上新」标签 + 活动文案 | ✅ |
| 优惠组合与价格计算 | ✅ 支持 | `calculate-price`（无副作用，可批量） | ✅ |
| 订单创建 | ✅ 有，高风险 | `create-order`（入参 ≈ `calculate-price` + `takeWayCode`） | 📘 Schema 确认，⛔ 未调用 |
| 订单查询/状态 | ✅ 有 | `query-order`、`order-list` | ✅ 已验证 |
| **订单支付时限** | ✅ **15 分钟** | `query-order.expirePayTime` | ✅ 已验证 |
| **未支付订单占用券？** | ❓ **未知** | — | ❓ 未验证 |
| **支付链接形态** | ❓ **未知** | `create-order` 返回（未暴露 schema） | ❓ 未验证 |
| **一键领券（批量）** | ✅ 有，高风险 | `auto-bind-coupons` | 📘 Schema+官方描述，⛔ 未调用 |
| **单张领券** | ❌ **不存在** | 无此工具 | ✅ 已否决 |
| 用户积分 | ✅ 有 | `query-my-account`（实测可用积分 145） | ✅ 已验证 |
| **过敏原 / 忌口精确过滤** | ❌ **不支持** | 无官方数据源 | ✅ 已否决 |
| **通知 / 订阅 / 变化检测** | ❌ **不存在** | MCP 侧无推送能力 | ✅ 已否决 |

---

## 8. 实测带来的产品级新发现（原方案未预见）

| # | 发现 | 产品价值 |
|---|---|---|
| N1 | `query-meals` 直接返回价格 | 盲盒可**完全离线枚举**，无需批量调 `calculate-price`，速度更快、限流风险归零 |
| N2 | 券**不可叠加**（`600022`） | "优惠优先"模式 = 对每张可用券各算一次取最优；**不是叠加** |
| N3 | `query-meal-detail` 有 `diffPrice` 且**可为负** | 预算差一点时可用"换子项降价"压进预算（玉米杯 -¥0.5） |
| N4 | `query-store-coupons.products[].productCode` | 可精确知道"哪些餐品能用券"，优惠优先模式有据可依 |
| N5 | `query-my-coupons` 自带「今日到期」标签 | 到期提醒是**现成能力**，不需要自己算 |
| N6 | `calculate-price` 有 `enjoyable.balance`（门槛差额） | 可做"再买 ¥X 享 Y 优惠"提示，很有黏性 |
| N7 | `canWithOrder` 商品价格陷阱 | **必须默认排除或标注**，否则会报出到店拿不到的价格——这是信任问题 |
| N8 | 门店 `reservationTimeOptions` 含时段划分 | 可做"早餐/午餐/夜市"时段感知推荐 |
| N9 | 实际工具数 > README 的 33 个 | 存在 `query-promotions`、`query-survey-coupon` 等未文档化工具 |
| N10 | `calculate-price` 单位为「分」，菜单为「元」 | 必须做单位转换，否则会差 100 倍 |
| **N11** | **`create-order` 入参 ≈ `calculate-price` 入参 + `takeWayCode`** | 算价请求体可直接复用为下单骨架，实现成本远低于预期 |
| **N12** | **订单支付时限 15 分钟**（`expirePayTime` − `createTime`） | 可做"还剩 X 分钟支付"倒计时提示；也说明未支付订单会自行消解 |
| **N13** | `query-order` 的 `status` 数字码（实测 `40`）**与官方注释枚举不符** | 状态判断只能靠 `orderStatus` 中文文本，不得解析数字码 |
| **N14** | `available-coupons` 当前**只有 3 张可领** | `auto-bind-coupons` 的实际影响面很小，风险比预想低 |
| **N15** | 券池存在**同名重复批次**（"巧克力味厚松饼猪柳蛋套餐"出现 4 次） | 领券前后对比不能用券名做唯一键 |

---

## 8.1 第二轮新增发现汇总

| 类别 | 发现 | 产品价值 |
|---|---|---|
| 🟢 简化 | `create-order` 与 `calculate-price` 入参高度同构 | 下单模块只需在算价请求体上补 `takeWayCode`，开发量小 |
| 🟢 可用 | 15 分钟支付时限已确认 | 可做支付倒计时；未支付订单不会长期挂账，重复单风险可控 |
| 🟢 可用 | 积分基线已记录（145） | 未来实测领券后可直接验证是否消耗积分 |
| 🟠 风险 | `status` 数字码与文档不符 | 必须用文本判断状态，否则状态机写错 |
| 🔴 缺口 | 支付链接字段形态未知 | **必须实测 `create-order` 才能确定如何呈现支付入口** |
| 🔴 缺口 | 未支付订单是否占券未知 | 影响"券被锁住"的用户提示文案，暂按"可能占用"保守表述 |
| 🟡 限制 | `auto-bind-coupons` 是批量接口，无法单张领 | 确认话术必须说清"会领取全部可领券" |

---

## 8.2 开发期新增实测发现（S1–S4 编码过程中补测）

> 时间：2026-10-10 00:29 — 00:30（北京时间），3 次真实只读调用
> 这三条此前未记录，且都直接影响产品安全，故补记于此。

| # | 发现 | 证据 | 影响与处理 |
|---|---|---|---|
| **N16** 🔴 | **`calculate-price` 会静默丢弃未匹配的商品** | 请求 `{1450 麦香鸡, 3050 可乐中杯}`，返回 `productList` 只有 1450，`price=1700`（只算了麦香鸡） | 用户会为"少了东西"的订单付款。已新增 `verify_pricing_covers_items()`，下单前强制核对，缺一件即拦截并提示重新拉菜单 |
| **N17** 🟠 | `query-store-coupons` 可能返回**空数组** | 第一轮（10-09 23:0x）返回 2 张，本轮（10-10 00:29）返回 `data: []` —— 原券已于 10-09 23:59:59 到期 | 券池随时可能为空，解析层与渲染层必须优雅处理 |
| **N18** 🟠 | `query-meal-detail` 对失效商品返回 **`60001 未匹配到商品，请重新拉取全量商品列表`** | 第一轮可用的 `9900005456`（麦辣鸡腿汉堡三件套）本轮已失效 | 商品编码会失效 → 菜单缓存必须有 TTL，失效时重新拉取；替换方案在拿不到 rounds 时不得崩溃 |

**N16 是本轮最重要的发现。** 它意味着"菜单价 → 组合 → 算价"这条链路上，最后一步的结果可能与用户输入不一致，
而服务端不会报错。任何省略"算价结果覆盖核对"的实现都存在让用户付错钱的风险。

---

## 9. 安全边界遵守情况

| 项 | 状态 |
|---|---|
| 未领取优惠券（`auto-bind-coupons`） | ✅ |
| 未兑换积分（`mall-create-order`、`draw-lottery`） | ✅ |
| 未创建/提交/支付/取消订单 | ✅ |
| 未修改地址、购物车、账户状态 | ✅ |
| 未用猜测参数绕过权限 | ✅ |
| 未保存用户身份凭证 | ✅ |
| 调用频率受控 | ✅ 累计 16 次，未触发限流 |
| 返回结果脱敏 | ✅ 券码截断；地址/收件人/手机号/配送地址**完全不记录**；账户 ID 截断；历史订单号仅作结构样例 |
| ⛔ `create-order` 未调用 | ✅ 未经用户单独授权，不创建真实订单 |
| ⛔ `auto-bind-coupons` 未调用 | ✅ 未领取任何优惠券 |

> 📌 **所有写操作（`create-order` / `auto-bind-coupons` / `cancel-order` / `draw-lottery` / `mall-create-order` / `delivery-create-address`）在本轮均标记为「📘 Schema/文档确认，⛔ 未调用」，不得在任何文档中描述为「已验证」。**
