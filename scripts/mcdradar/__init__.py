"""麦麦雷达 McRadar —— 麦麦 MCP 数据处理与订单/领券状态机。

架构约定（P0-B）：
- **MCP 调用由宿主（WorkBuddy Skill）负责**，本包不实现 MCP 客户端、不持有 Token
- 本包只做：数据规范化、金额换算、组合算法、缓存与本地状态、闸门校验
- 所有写操作（create-order / auto-bind-coupons）的真实调用由宿主执行，
  执行前后必须调用本包的状态机接口登记结果
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
