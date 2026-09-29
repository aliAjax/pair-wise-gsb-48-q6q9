# 证券结算与企业行动处理

纯Python标准库实现的证券结算与企业行动处理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、净额结算、交收完整性、公司行动公告比例重算、原比例冻结追回和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、公司行动比例调整/冲正/并发/幂等/旧指令兼容和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8324
```

默认端口为`8324`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/announcements`：公司行动公告列表。
- `GET /api/announcements/{reference}`：公告详情（含当前比例与版本）。
- `POST /api/announcements/{reference}/adjust`：公司行动专员确认公告新比例，请求体为`{"expected_version":0,"data":{"ratio":0.8,"request_id":"REQ-..."}}`。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`，`data.announcement_ref`可选，用于把指令挂到公司行动公告。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 公司行动比例规则

- 指令入库时`action_ratio`随指令落库；带`announcement_ref`的指令会自动登记公告。
- 公告到账前，公司行动专员（`corporate_actions`）可多次确认新比例：同一公告下**未交收**指令（captured/adjusted/approved/failed）按新比例重算派生数量、价格和现金权益；**已交收 settled / 已冲正 reversed 的指令跳过，保留交收时原比例**。
- 交收时冻结当时比例（`settled_action_ratio`、`settled_cash_entitlement`）。结算主管（`settlement_officer`）冲正时按冻结的原比例追回现金差额（`recouped_cash`、`reversed_action_ratio`）；`reverse`仅结算主管可执行。
- 两位专员同时确认不同比例：公告版本号乐观锁配合写事务串行化，只有一人生效，后到者收到`409 conflict`，刷新版本后可再确认。
- 同一公告的同一`request_id`重试为幂等调用，返回首次结果且不重复调整指令。
- 升级前的旧数据库与旧指令（无公告编号、无冻结字段）可直接读取；冲正时按入库的`action_ratio`兜底计算追回金额，建表脚本自动补加新表。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突、公告比例重算与冻结、冲正原比例追回、并发冲突、重试幂等和升级前旧指令兼容。
