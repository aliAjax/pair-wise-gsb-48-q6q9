# 证券结算与企业行动处理

纯Python标准库实现的证券结算与企业行动处理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、净额结算、交收完整性和公司行动调整、公告比例修正和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

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
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`，`data.action_ratio`为公告到账前的临时比例，随指令入库；可选`data.announcement_id`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
- `POST /api/announcements`：公司行动专员（`corporate_actions`角色）确认公告比例，请求体为`{"reference":"ANN-1","data":{"instrument":"ACME","corporate_action":"split|dividend|merger","action_ratio":3.0}}`。确认后同一证券、同一公司行动类型的未交收指令（`captured`/`adjusted`/`approved`/`failed`）按新比例重算（版本+1并写审计），已交收（`settled`）和已冲正（`reversed`）指令保留原比例。同一公告仅首个比例生效：同比例重试幂等返回，不同比例返回409冲突。
- `GET /api/announcements/{reference}`：公告确认结果。

### 比例冻结与冲正追回

- 指令入库时临时比例写入`action_ratio`并留档`original_action_ratio`。
- 交收时把当时比例冻结到`settled_action_ratio`，此后公告再修正也不影响该指令。
- 冲正（`reverse`，结算主管或专员可执行）按冻结的原比例追回现金差额，股息记录写入`cash_recovered = quantity × settled_action_ratio`，不读取公告修正后的最新比例。
- 升级前创建的旧指令缺少新字段时，读取按旧参数兼容，冻结比例按`settled_action_ratio → original_action_ratio → action_ratio`顺序兜底。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突，以及公告比例修正、冲正按原比例追回、并发确认冲突、重试幂等和旧指令兼容。
