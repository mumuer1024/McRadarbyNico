# 02 · 麦当劳 MCP 接入记录

> 记录时间：2026-10-09 22:50（北京时间）｜**更新：2026-10-09 23:05 已接通**
> 当前状态：✅ **已接入并验证** —— 用户在 WorkBuddy【自定义连接器 → 配置 MCP】中自行填入 Token 并启用 `mcd-mcp`，工具已可被枚举与调用；**Token 全程未进入对话、未写入本项目任何文件**

---

## 1. 接入规格（来源：官方仓库 mcd-mcp-server README）

| 项目 | 值 |
|---|---|
| 接入地址 | `https://mcp.mcd.cn` |
| 传输协议 | **Streamable HTTP**（需 Client 支持） |
| 鉴权方式 | HTTP 请求头 `Authorization: Bearer <你的_MCP_TOKEN>` |
| 配置类型标识 | `"type": "streamablehttp"` |
| 限流 | 每个 Token **600 次/分钟**，超限返回 **429** |
| 错误码 | **401** Token 无效/过期/未提供；**429** 触发限流 |
| 覆盖范围 | 麦乐送点餐、到店取餐、团餐、积分兑换券、活动日历查询等 |
| 适用地区 | 中国大陆地区（不含港澳台） |

官方 MCP 配置 JSON 模板：

```json
{
  "mcpServers": {
    "mcd-mcp": {
      "type": "streamablehttp",
      "url": "https://mcp.mcd.cn",
      "headers": {
        "Authorization": "Bearer <你的_MCP_TOKEN>"
      }
    }
  }
}
```

> **只修改 `<你的_MCP_TOKEN>` 为你拿到的 TOKEN，不要删除前面的 `Bearer`。**

---

## 2. Token 获取流程（必须用户本人完成）

官方流程（https://open.mcd.cn/mcp 控制台）：

1. 打开 https://open.mcd.cn/mcp ，点击右上角【登录】
2. 跳转登录页，**使用手机号完成验证登录**
3. 登录成功后右上角"登录"变为【控制台】
4. 点击【控制台】→ 点击【激活】按钮，申请 MCP Token
5. 阅读并同意《使用条款》及《麦当劳 MCP 服务规则》
6. Token 申请成功后可一键复制

> ⛔ 这一步涉及**手机号验证登录与个人账户授权**，属于必须由用户本人完成的操作。本 Agent 不代劳、不代填验证码、不尝试绕过。

---

## 3. 已完成的接入准备工作

### 3.1 现有 MCP 配置检查 ✅

- 路径：`~/.workbuddy/mcp.json`（本机 WorkBuddy 用户级 MCP 配置）
- 现有内容（已确认，未修改；下方为**结构示意**，条目名与地址已泛化）：

```json
{
  "mcpServers": {
    "<已有 MCP Server>": {
      "type": "streamableHttp",
      "url": "https://<your-mcp-endpoint>",
      "timeout": 30000,
      "disabled": false
    }
  }
}
```

- 结论：当前已存在 1 个 MCP 配置条目。**后续新增 `mcd-mcp` 时将采用合并写入，绝不覆盖已有条目**。

### 3.2 连接器状态检查 ✅

- 调用连接器列表查询，返回：**"No connectors installed."**
- 结论：当前 WorkBuddy 环境**尚未安装任何连接器**，`mcd-mcp` 需要全新添加。

### 3.3 端点可达性探测 ✅（结果：不可匿名访问）

| 探测项 | 命令 | 结果 |
|---|---|---|
| 根路径 | `GET https://mcp.mcd.cn/` | **HTTP 403** |
| 工具枚举（匿名） | `POST https://mcp.mcd.cn/` `{"method":"tools/list"}` | **HTTP 403**，响应体 `{"code":"0001","msg":"系统错误","data":null}` |

响应头显示请求到达了麦当劳侧网关（可见 `x-request-id`、`X-WAF-UUID`、`x-envoy-upstream-service-time` 等），说明：

- 网络可达，DNS 与 TLS 正常
- **未携带有效 Token 的请求被网关直接拒绝（403），不会返回工具清单**
- 因此 **无法在未授权状态下猜测或枚举工具**，工具列表只能来自官方文档 + 授权后的实际调用

> 这也意味着：**本轮不可能用"猜参数 + 试调用"的方式做能力审计**，符合安全边界要求。

---

## 4. 实际接入结果（2026-10-09 23:02 完成）

采用 **方案 ②（用户自行在 UI 配置，最安全）**：

1. 用户在 WorkBuddy【专家·技能·连接器 → 连接器 → 自定义连接器 → 配置 MCP】中粘贴官方 JSON 并替换真实 Token
2. 保存后在【自定义连接器】中启用 `mcd-mcp`
3. Agent 侧确认：工具已注册为 `mcp__mcd-mcp__*` 前缀，可正常枚举 `inputSchema` 并调用

✅ **Token 未进入对话，未写入 `~/.workbuddy/mcp.json`（由 WorkBuddy 连接器托管），未写入本项目任何文件。**
✅ 现有 `lighthouse-ops` 配置保持原样，未被改动。

### 工具数量差异（值得注意）

官方 README 列出 **33 个** 工具，但实际 `tools/list` 中**多于 33 个**，包含 README 未列出的工具，例如：

- `query-promotions`：查询门店促销规则（满减/满折），文档标注仅团餐场景
- `query-survey-coupon`：按订单号查询满意度答卷及关联奖券

→ **不要以 README 的 33 个为上限**，实际可用工具集以后续 `tools/list` 为准。

---

## 5. 安全约束（本轮及后续均遵守）

- ✅ 真实 Token **绝不写入** 本项目任何文档、日志、`research/` 记录
- ✅ 对外提交的配置文件只使用 `mcp-config.example.json` + `<你的_MCP_TOKEN>` 这类占位符
- ✅ 不修改现有 `lighthouse-ops` 配置
- ✅ 不做匿名枚举、不猜参数、不批量请求
- ✅ 只读优先；任何涉及领券、下单、支付、改账户状态的工具，只查定义不执行
- ✅ 控制调用频率（< 600 次/分钟，实际会远低于此）

---

## 6. 验证清单（已全部完成）

- [x] MCP 服务连接成功，可被 WorkBuddy 识别
- [x] 工具清单可枚举（实际 > 33 个，多于 README 所列）
- [x] 记录核心工具 `inputSchema`（必填/可选参数）
- [x] 连通性与只读抽样测试（12 次调用，见 `research/tool-test-results.md`）
- [x] 记录实际返回结构与鉴权要求
- [x] 回填 `docs/03-mcp-capability-audit.md` 与 `docs/04-product-feasibility.md`

### 已记录的实测错误码

| 错误码 | 含义 | 出现场景 |
|---|---|---|
| `600050` | 收藏餐厅列表为空 | `query-nearby-stores` 用 `searchType=1` 但用户无收藏 |
| `600022` | **暂不支持多张券使用** | `calculate-price` 传入 2 张券 |
| `403`（HTTP） | 网关层拒绝 | 未携带有效 Token |
| `401`（官方） | Token 无效/过期/未提供 | 本轮未触发 |
| `429`（官方） | 触发限流 600 次/分钟 | 本轮未触发 |
