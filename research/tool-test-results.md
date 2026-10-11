# MCP 工具实测记录

> 首轮探测：2026-10-09 22:47（北京时间）
> **实测执行：2026-10-09 23:02 — 23:04（北京时间），MCP 已接入**
> 测试门店：`1000001` 麦当劳示例餐厅（beType=1 到店自取，orderType=1）
> 本轮共发起 **12 次** MCP 调用，全部为只读，未触发限流

---

## 一、环境与连通性探测（首轮，无 Token）

### T-001 · 根路径可达性
- 时间：2026-10-09 22:47
- 结果：**HTTP 403**
- 结论：域名可解析、TLS 正常、请求到达麦当劳网关；无鉴权被拒

### T-002 · 匿名工具枚举
- 时间：2026-10-09 22:47
- `POST https://mcp.mcd.cn/` `{"method":"tools/list"}` → **HTTP 403**，`{"code":"0001","msg":"系统错误","data":null}`
- 响应头含 `X-WAF-UUID`、`x-request-id` → **网关层先于 MCP 业务层拦截**
- 结论：匿名枚举工具不可行；工具清单只能来自授权后的 `tools/list`

### T-003 / T-004
- 连接器清单：`No connectors installed.`
- 现有 `~/.workbuddy/mcp.json`：仅 `lighthouse-ops`，未被修改

---

## 二、实测记录（Token 到位后）

### P-01 · now-time-info ✅
- 请求：`{}`
- 返回字段：`timestamp`(ms)、`datetime`(ISO)、`formatted`、`date`、`year`、`month`、`day`、`dayOfWeek`、`timezone`(GMT+08:00)、`offset`、`utc`
- 实测值：`2026-10-09 23:02:29`，`dayOfWeek=FRIDAY`，`timezone=GMT+08:00`
- 关键发现：**服务端时间即北京时间**，可作活动/券有效期的基准

### P-15 · query-nearby-stores ✅
- 请求 A：`{beType:1, searchType:1}`（收藏餐厅）→ ❌ `600050 收藏餐厅列表为空，请您提供城市+关键词进行搜索`
- 请求 B：`{beType:1, searchType:2, city:"上海市", keyword:"人民广场"}` → ✅ 5 家门店
- 返回字段：
  - `storeCode` 门店编号（后续所有调用的必填入参）
  - `storeName`、`address`、`distance`(米)
  - `businessStatus`(bool，是否营业中)、`businessStartTime`/`businessEndTime`
  - `reservation`(bool)、`reservationTimeOptions[]`：`{date, today, reservationOptionText}`
  - `beCode`：beType=1 时**为空**；beType=5(得来速) 时才有值
  - `longitude`/`latitude`：实测均为 `0.0`（未返回真实坐标）
- 关键发现：
  - **门店上下文是所有业务的前置条件**，无法跳过
  - `businessStatus` 可用于过滤打烊门店
  - `reservationTimeOptions` 暴露了**时段划分**（早餐/午餐/下午茶/夜市/宵夜及其时间区间）→ 可做"时段感知"推荐

### P-04 · campaign-calendar ✅（⚠️ 部分不及预期）
- 请求：`{}`（默认返回当月全部活动）
- 返回形态：**Markdown 渲染文本，按日期分组**
  - 分组标题：`2026年10月7日 往期回顾` / `2026年10月9日 今日` / `2026年10月10日`（未来）
  - 每条活动：`活动标题` + `活动内容介绍`（含 emoji 的营销文案）+ `活动图片介绍`（img URL）
- ❌ **没有结构化的起止日期字段** —— 日期只是分组标签
- ❌ **没有适用范围 / 参与条件 / 门店字段**
- ⚠️ 但文案中**内嵌价格锚点**：`15.9元起`、`26.9元起`、`29.9元`、`9.9元`、`13.9r`、`49元150天`
- ✅ 有 `specifiedDate` 可选入参：`yyyy-MM-dd`，传了则返回"该日 + 前后各一个最近有活动的日子"共三天
- 实测样例（脱敏后保留结构）：
  ```
  2026年10月9日 今日
  - 韩式风味蘸酱上新❤️就「酱」心有所「薯」
    鸡薯双全盒仅需15.9元起 / 蘸酱炸鸡仅需11.9元起 / 10月8日 10:30起正式开售
  - 麦当劳联动G-DRAGON：龙焰鸡腿堡三件套 26.9 元起
  ```
- 对产品的影响：
  - **"倒计时/即将结束"只能靠日期分组文本解析，不可靠**
  - 但可以做「今天有什么」「未来 N 天有什么」「新增了什么（diff 标题）」
  - 活动文案里的价格可作为"预算锚点"提示

### P-02 · query-meals ✅✅ **（本轮最重要的发现）**
- 请求：`{storeCode:"1000001", orderType:1, beType:1}`
- 返回结构：
  - `data.categories[]`：`{name, meals:[{code, tags[]}]}`
    - 实测分类：人气热卖、精选单人餐、鸡肉汉堡/卷、小食拼盘/多人餐、蘸酱炸鸡、随心配1+1、巨无霸牛鱼肉堡、炸鸡、大堡口福单人餐、小食甜品/其他、开心乐园、麦咖啡™、饮品
    - 实测标签：上新、麦金卡、套餐、单品、小食、冰淇淋、冷饮、热饮、爆款、川香风味、随心拼、第二份优惠、限定特惠、奶昔回归、蓝莓、灰焰…
  - `data.meals{}`：**code → 餐品详情映射**
    - `name`、`image`(URL)
    - **`currentPrice`：现价（字符串，单位：元）**
    - **`originalPrice`：原价/划线价（字符串，单位：元）**
    - `discountType`：`null` / `早餐卡优惠` / `促销优惠` / `麦金卡优惠` / `随单购早餐卡优惠` / `随单购麦金卡优惠`
    - `canWithOrder`(bool)、`withOrder{membershipCode, specId, cardId, cardType}`
  - `data.frequent`：常点餐品（用户个人数据）
- 实测样例（真实数据，非敏感）：
  ```json
  "1450":       {"name":"麦香鸡",           "currentPrice":"17",   "originalPrice":"17"}
  "9900008751": {"name":"可乐",             "currentPrice":"9.5",  "originalPrice":"9.5"}
  "9900005456": {"name":"麦辣鸡腿汉堡三件套","currentPrice":"33.5","originalPrice":"46"}
  "9900016073": {"name":"龙焰鸡腿堡三件套",  "currentPrice":"26.9","originalPrice":"47.5"}
  "521952":     {"name":"龙焰鸡腿堡",        "currentPrice":"24",   "originalPrice":"24"}
  "4810":       {"name":"薯条",             "currentPrice":"13.5", "originalPrice":"13.5"}
  "521951":     {"name":"灰焰圆筒",          "currentPrice":"5",    "originalPrice":"5"}
  ```
- ⚠️ **价格陷阱**：部分商品 `discountType="随单购麦金卡优惠"` 且 `canWithOrder=true`，其 `currentPrice` 是**必须同时随单购会员卡才能拿到的价格**，否则按 `originalPrice` 计。
  例：`9900016309 龙焰鸡腿堡四件套 currentPrice=29 / originalPrice=57`。
  → **盲盒必须默认排除或单独标注这类商品**，否则会报出用户到店拿不到的价格。
- 对产品的影响：**U1 成立 → 盲盒可完全离线枚举组合，无需逐个试算**

### P-09 / P-10 / P-13 · calculate-price ✅✅（含副作用验证）
- 请求：`{storeCode:"1000001", orderType:1, beType:1, items:[{productCode:"1450",quantity:1},{productCode:"9900008751",quantity:1}]}`
- 返回：
  ```json
  {"productOriginalPrice":2650,"productPrice":2650,"originalPrice":2650,
   "discount":0,"price":2650,
   "productList":[{"productCode":"1450","productName":"麦香鸡","quantity":1,"originalSubtotal":1700,"subtotal":1700},
                  {"productCode":"9900008751","productName":"可乐","quantity":1,"originalSubtotal":950,"subtotal":950}],
   "takeWayList":[{"code":"locker-out","title":"外带","subtitle":"店外取餐柜"}]}
  ```
- 字段全表：`productOriginalPrice`、`productPrice`、`deliveryOriginalPrice`、`deliveryPrice`、`packingOriginalPrice`、`packingPrice`、`tablewarePrice`、`originalPrice`、`discount`、`price`、`productList[]`、`takeWayList[]`、`mealAssistanceList[]`、`enjoyed{}`、`enjoyable{}`
- ⚠️ **所有金额单位为「分」**（2650 = ¥26.5），与 `query-meals` 的「元」不同，必须做 ÷100 转换
- ✅ **与菜单价格完全吻合**（17 + 9.5 = 26.5）
- ✅ **U3 通过**：同参数重复调用两次，结果完全一致（均 2650），**未返回任何订单号/购物车 ID，无副作用**
- `takeWayList[].code`（如 `locker-out`）是 `create-order` 的必填来源
- `enjoyed` / `enjoyable`：`{amountType(1特价/2折扣/3立减), realDiscount, balance(门槛差额,分)}` → **可做"再买 X 元即可享 Y 优惠"的临门一脚提示**

### P-11 · calculate-price 带单券 ✅
- 请求：`items:[{productCode:"9900014239", quantity:1, couponId:"AAAACOUPON000000000000000000001", couponCode:"MCDEXAMPLECODE000000001"}]`
- 返回：
  ```json
  {"productOriginalPrice":1400,"productPrice":990,"originalPrice":1400,
   "discount":410,"price":990,
   "productList":[{"productCode":"9900014239","productName":"麦旋风任选1","quantity":1,"originalSubtotal":1400,"subtotal":990}]}
  ```
- ✅ 原价 ¥14 → 用券 ¥9.9，优惠 ¥4.1 —— 与 `query-my-coupons` 中"麦旋风任选 ¥9.9"**完全吻合**
- 关键发现：券对应的 `productCode`（如 `9900014239`）**不在 `query-meals` 菜单里**，是券专用商品码，但**可直接用于 calculate-price**

### P-12 · calculate-price 多券叠加 ❌ **（官方明确不支持）**
- 请求：两个商品各带一张券
- 返回：`{"success":false,"code":600022,"message":"暂不支持多张券使用"}`
- 🎯 **决定性结论：优惠券不可叠加，一个订单只能用 1 张券。这是官方错误码，不是推测。**

### P-05 · query-store-coupons ✅✅（关键）
- 请求：`{storeCode:"1000001", orderType:1, beType:1}`
- 返回字段：
  - `title` 券名
  - **`couponId`**、**`couponCode`**（传给 calculate-price / create-order 用）
  - `tradeDateTime` 有效期，格式 `2026-10-05 10:30:00-2026-10-09 23:59:59`
  - **`products[]`：`{productCode, productName}`** —— 券的适用商品
  - `promotionId`
- 实测（券码已脱敏截断）：
  ```json
  {"title":"麦旋风任选","couponId":"AAAACOUP…","couponCode":"MCDEXAMP…",
   "tradeDateTime":"2026-10-05 10:30:00-2026-10-09 23:59:59",
   "products":[{"productCode":"9900014239","productName":"麦旋风任选1"}]}
  {"title":"薯薯任选","couponId":"AAAACOUP…","couponCode":"MCDEXAMP…",
   "tradeDateTime":"2026-10-05 10:30:00-2026-10-09 23:59:59",
   "products":[{"productCode":"9900016370","productName":"薯薯任选"}]}
  ```
- ⚠️ 只返回 **2 张**，而 `query-my-coupons` 返回 **14 张** → 差异原因：`query-store-coupons` 是当前门店下**已领取且持券码、可直接核销**的券
- 对产品的影响：**`products[].productCode` 让"优惠优先"模式真正可实现** —— 可反查哪些商品能用券

### P-06 · query-my-coupons ✅（⚠️ 无 couponId）
- 返回：Markdown 渲染，**共 14 张**，第 1/1 页，每页 200 条
- 每条字段：
  - 券名（如"麦旋风任选"、"火腿扒堡早餐两件套"、"巧克力味厚松饼猪柳蛋套餐"）
  - `优惠`：**用券价格**（如 ¥9.9、¥29.9）
  - `有效期`：**精确到日期 + 时段 + 星期**，如 `2026-10-09 23:59 周一、二、三、四、五 10:30-23:59`、`2026-10-12 05:00-2026-10-12 10:29 周一`
  - `领取时间`："4日前收到"
  - `标签`：**`今日到期`、`到店专用`、`外送专用`**
  - 图片 URL
- ❌ **不返回 couponId / couponCode** → 不能直接用于算价，需与 `query-store-coupons` 按券名关联
- ✅ 但**有效期 + 标签**足以支撑「今日到期提醒」「到店/外送区分」这两个高价值功能

### P-07 · available-coupons ✅（弱）
- 返回：券标题 + **`状态`（已领取 / 可领取）** + 图片
- ❌ **无 couponId、无有效期、无适用商品**
- 用途：只能做"还有哪些券没领"的清单展示，**不能驱动算价**

### P-03 · query-meal-detail ✅✅（超出预期）
- 请求：`{storeCode:"1000001", orderType:1, beType:1, code:"9900005456"}`（麦辣鸡腿汉堡三件套）
- 返回结构：
  - `code`、`name`、`image`、`supportModify`
  - `rounds[]`：**套餐选配轮次**
    - `{id, name, quantity, maxQuantity, minQuantity, choices[]}`
    - `choices[]`：`{code, name, quantity, maxQuantity, isDefault, **diffPrice**, supportModify, modification}`
    - `diffPrice` 差价字符串：`"+ ¥0"`、`"+ ¥1"`、`"+ ¥2.5"`、`"- ¥0.5"`、`"+ ¥11"`
  - `modification.items[]`：`{maxValues, minValues, values[]}`，`values[]` 含 `{code, price, name, selectedQuantity, selectedKey, unselectedKey}`
- 实测样例（麦辣鸡腿汉堡三件套）：
  - Round 1「麦辣鸡腿汉堡」：唯一选项 `1440`
  - Round 2「选择套餐内小食」：默认 `4810 中薯条 +¥0`；可换 `521983 心形薯饼 +¥1` / `521901 扭扭薯 +¥1.5` / `4820 大薯条 +¥2.5` / `4437 小杯玉米杯 -¥0.5` / `4438 大杯玉米杯 +¥2.5`
  - Round 3「选择套餐内饮料」：默认 `3050 可乐中杯 +¥0`；可换 `3071 无糖可乐 +¥0` / `3060 雪碧 +¥0` / `3100 可乐大杯 +¥2` / `3755 纯牛奶 +¥0.5` / `507411 冰美式 +¥9` / 奶昔 `+¥8` …
- 🎯 对产品的影响（重大）：
  - **套餐内替换 + 精确差价** → 可做到"把可乐换成纯牛奶只加 ¥0.5"这类精细控制
  - `diffPrice` 可为负（如玉米杯 -¥0.5）→ **换选项还能降价**，对预算约束极有价值
  - 忌口可细化到"换掉套餐里的辣味小食"而不必丢弃整个套餐

### P-08 · list-nutrition-foods ✅（⚠️ 无过敏原）
- 返回：纯文本表格，**160 条**，表头 `[160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}`
- 字段：能量(kJ/kcal)、蛋白质、脂肪、碳水、钠、钙
- 实测样例：`巨无霸,null,2146,513,27,26,42,961,171` / `无糖可口可乐中杯,null,0,0,0,0,1,35,0` / `麦辣鸡腿汉堡,null,2029,485,24,24,42,1208,154`
- ❌ **确认无过敏原、无配料成分字段**
- ⚠️ 名称与菜单**不完全一致**（如营养表"纯牛奶（盒装）"vs 菜单"纯牛奶(盒装)"全半角括号差异）→ 匹配需做名称归一化
- 用途：热量/钠上限过滤（带免责声明），**不能做过敏原规避**

---

## 三、明确未执行（安全边界）

| 工具 | 原因 |
|---|---|
| `create-order` | 会产生真实订单 |
| `auto-bind-coupons` | 会真实领券 |
| `cancel-order` | 会改变订单状态 |
| `mall-create-order` | 会扣积分/库存 |
| `draw-lottery` | 会消耗积分或次数 |
| `delivery-create-address` | 会修改账户数据 |
| `party-order-create` | 会产生真实预约订单 |
| `delivery-query-addresses` | 涉及真实收货地址，本轮无必要 |
| `create-order` | **未调用** —— 会产生真实订单，需用户单独授权 |

---

## 四、脱敏与统计

- 券码在本文件中已截断处理（`MCDEXAMP…`）
- 未记录任何地址、手机号、会员 ID、订单号
- 门店信息仅用于测试定位，非用户常驻位置
- 本轮 MCP 调用次数：**16 次**（远低于 600 次/分钟限制，未触发限流）
  - 第一轮 12 次（P-01 ~ P-13）
  - 第二轮 4 次（P-14 ~ P-17，订单/券专项调查）

---

## 五、U1–U10 验证结论汇总

| # | 不确定性 | 结论 | 依据 |
|---|---|---|---|
| U1 | `query-meals` 含价格？ | ✅ **是**，`currentPrice`/`originalPrice`（单位：元，字符串） | P-02 |
| U2 | 券能否叠加？ | ❌ **否**，官方错误码 `600022 暂不支持多张券使用` | P-12 |
| U3 | `calculate-price` 有副作用？ | ✅ **无副作用**，幂等，可批量调用 | P-13 |
| U4 | 是否依赖门店上下文？ | ✅ **必须**，`storeCode`/`orderType`/`beType` 全必填 | P-02 |
| U5 | 券字段含有效期/适用商品？ | ✅ `query-store-coupons` 全有（含 `products[].productCode`）；`query-my-coupons` 有有效期与标签但**无 couponId** | P-05 / P-06 |
| U6 | 活动含起止日期字段？ | ⚠️ **否**，只有日期分组文本；价格以文案形式内嵌 | P-04 |
| U7 | 活动适用范围分层？ | ❌ **否**，无任何范围/门店/用户专属字段 | P-04 |
| U8 | 营养数据含过敏原？ | ❌ **否**，仅 160 条营养成分，无过敏原/配料 | P-08 |
| U9 | `create-order` 流程 | 📘 已知 `takeWayCode` 来自 `calculate-price.takeWayList`；未实际调用 | 文档 + P-09 |
| U10 | 限流 | ✅ 12 次调用未触发，正常用量安全 | 本轮全部测试 |

---

## 六、第二轮专项测试记录（订单 / 领券调查）

> 时间：2026-10-09 23:50 — 23:51（北京时间）
> 范围：**全部只读**。未创建订单、未领取优惠券、未消耗积分、未修改任何账户状态。

### P-14 · `order-list` —— 历史订单列表 ✅

**请求**：`{}`（无参数）

**返回结构（脱敏）**：

```json
{"success":true,"code":200,"data":{"list":[
  {"orderId":"1000000000000000000000000002","orderType":"2","createTime":"2025-12-05 20:08:12",
   "beType":"2","beCode":"100000202","storeCode":"1000002","storeName":"麦当劳示例得来速餐厅",
   "orderStatus":"订单已完成",
   "orderProductList":[{"productCode":"9900009775","productName":"2份麦乐鸡","quantity":1,
     "comboItemList":[{"productCode":"1401","name":"麦乐鸡5块","quantity":2}]}],
   "realTotalAmount":"7.9"}
]}}
```

**字段清单**：`orderId`、`orderType`、`createTime`、`beType`、`beCode`、`storeCode`、`storeName`、`orderStatus`（**中文文本**）、`orderProductList[]`（`productCode`/`productName`/`quantity`/`comboItemList`）、`realTotalAmount`

**关键结论**

- ✅ 列表接口**不含**支付链接、不含 `expirePayTime`
- ⚠️ `orderStatus` 是**中文字符串**（如"订单已完成"），不是枚举码 → 状态判断必须做文本匹配
- ✅ 可用于**下单后核对是否重复创建**（比对 `createTime` + 商品组合）

---

### P-15 · `query-order` —— 订单详情 ✅【本轮最关键】

**请求**：`{"orderId":"1000000000000000000000000002"}`

**返回字段清单**（官方 schema 注释原文摘录）：

| 字段 | 说明 |
|---|---|
| `orderId` | 订单编码 |
| `orderStatus` | 订单状态（中文文本） |
| `status` | 订单状态（数字码） |
| `storeName` / `storeAddress` | 门店名 / 地址 |
| `orderProductList[]` | `productName`、`productImage`、`quantity`、`price`（原价，元）、`comboItemList[]`（`itemName`/`itemQuantity`，**含特调定制信息**） |
| `totalAmount` / `realTotalAmount` / `totalDiscountAmount` | 总金额 / 实付 / 总优惠 |
| `couponList[]` | `couponName`、`discountAmount` |
| `deliveryInfo` | `deliveryType`(1立即送出/2预约送出)、`deliveryAddress`、`addressDetail`、`customerNickname`、`mobilePhone`、`expectDeliveryTime`、`riderNickName`、`riderMobilePhone` |
| `createTime` / `deliveryPrice` / `realDeliveryPrice` / `realPackingFeeTotalPrice` / `tablewarePrice` / `productPrice` | 时间与费用明细（单位：元） |
| `takeWay` | 取餐方式 |
| `pickupCode` | 取餐码 |
| `lockerCode` / `lockerQrCode` | 取餐柜密码 / 取餐柜二维码（1.0.9 新增） |
| **`expirePayTime`** | **过期支付时间** |
| `payId` | 支付 ID |
| `remark` / `needTableware` | 备注 / 是否要餐具 |

**官方注释给出的 `orderStatus` 枚举**：`1待支付、2配餐中-已支付、4配送中、6已完成、7已取消、8已评价、10配餐中-餐厅确认配餐中`

⚠️ **实测与该注释不一致**：返回 `status:"40"`、`orderStatus:"订单已完成"`，而注释枚举中并无 `40`。
→ **工程结论：状态判断必须以 `orderStatus` 文本为准，不得依赖 `status` 数字码。**

**🔥 决定性发现：订单支付时限 15 分钟**

```
createTime     = 2025-12-05 20:08:12
expirePayTime  = 2025-12-05 20:23:12     ← 相差 15 分钟
```

→ `create-order` 后用户有 **15 分钟**完成支付；超时后订单状态由服务端推进（具体是自动取消还是保留为"已过期"，**未验证**）。

**⚠️ 关键缺口：返回中没有 `payUrl` / 支付链接字段**

- `query-order` 的字段清单里只有 `payId`，**没有支付链接**
- 但 `create-order` 官方描述明确写"返回订单详情与**支付链接**"，且 Next 引导语为"通过支付链接扫码或者打开麦当劳APP完成支付"
- 推断：支付链接**只在 `create-order` 的首次返回中出现**，`query-order` 不回显（该订单已完成也可解释）
- ❗ **无法在未创建订单的前提下验证**，必须实测 `create-order` 才能确认支付链接的字段形态

**脱敏说明**：返回中的 `mobilePhone`、`customerNickname`、`deliveryAddress`、`addressDetail` 已在本文件中**完全不记录**；`orderId`、`storeCode`、`beCode`、门店名与 `couponId` / `couponCode` **均已替换为占位符**，仅保留字段结构（原始样例为历史已完成订单，现已等价脱敏）。

---

### P-16 · `available-coupons` —— 麦麦省可领券清单 ✅

**请求**：`{}`

**返回**（Markdown 文本，共 **9 张**）：

| 券名 | 状态 |
|---|---|
| 麦旋风任选 | 已领取 |
| 巧克力味厚松饼猪柳蛋套餐 ×4（4 个不同批次，图片 URL 不同） | 已领取 |
| 薯薯任选 | 已领取 |
| **免费脆薯饼** | **可领取** |
| **人气麦旋风买一送一** | **可领取** |
| **9.9元中杯冰美式** | **可领取** |

**关键结论**

- ✅ 当前**可领取的只有 3 张** → `auto-bind-coupons` 若执行，预期影响范围就是这 3 张（数量很小，风险可控）
- ⚠️ 券名中「9.9元中杯冰美式」是**付费券**（到店付 9.9 元购买），但"领取"动作本身免费
- ⚠️ 返回**不含任何价格/积分/门槛字段**，无法从返回值判断领取是否消耗积分
- ⚠️ 同一券名重复出现 4 次（不同批次）→ 券池存在重复批次，做"领取前后对比"时不能用券名做唯一键

---

### P-17 · `query-my-account` —— 积分基线 ✅

**请求**：`{}`

**返回（脱敏）**：

```json
{"data":{"availablePoint":"145","accountId":"1C59…9A35","accumulativePoint":"587.9",
 "currency":"麦享会积分","currentMouthExpirePoint":"0","expiredPoint":"442.9",
 "frozenPoint":"0","lastMouthExpirePoint":"0","nextMouthExpirePoint":"0","usedPoint":"0"}}
```

**用途**：记录领券前积分基线（`availablePoint=145`），**未来若实测 `auto-bind-coupons`，可直接比对积分是否被扣减**。
（⚠️ 账户 ID 已在本文截断，不记录完整值。）

---

## 七、第二轮新增不确定性（U11–U15）

| # | 不确定性 | 当前结论 | 依据 |
|---|---|---|---|
| U11 | `create-order` 返回是否含 `payUrl`？ | 📘 **未知**。官方描述称有支付链接，但 `query-order` 字段清单中无此字段，只能实测确认 | 文档 + P-15 |
| U12 | 未支付订单超时后的最终状态？ | 🟡 **部分已知**：15 分钟支付时限已确认（`expirePayTime`）；超时后是"已取消"还是"已过期"**未验证** | P-15 |
| U13 | 未支付订单是否占用优惠券？ | ❓ **未知**。订单详情有 `couponList` 说明券会被记入订单，但超时/取消后是否归还**未验证** | P-15 推断 |
| U14 | 支付链接能否在浏览器打开 / 跳转微信小程序？ | ❓ **未知**。官方文档无任何说明 | — |
| U15 | `auto-bind-coupons` 是否消耗积分或领到付费权益？ | 🟡 **倾向安全，未证实**。麦麦省为官方优惠券中心，消耗积分的是 `mall-create-order` 与 `draw-lottery`（不同工具）；积分基线已记录，待实测比对 | P-16 / P-17 |
