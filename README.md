# 跨海光缆故障与抢修协调

纯Python标准库实现的跨海光缆故障与抢修协调原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 区段接续档案

接续损耗不只留在故障单里，而是归入按`(cable, segment)`建立的区段接续档案，跨多张故障单共享：

- **按现场发生时刻累计**：每条接续记录以`occurred_at`（ISO时间）为现场时刻，晚到的现场数据按其发生时刻并入档案。
- **预算控制**：创建故障单时可给`splice_loss_budget_db`（默认0.5dB）；已确认接续的累计损耗超过预算时，故障单自动退回`rectifying`（待整改），原`tested`测试结论随之失效。
- **整改重做**：`rectify`动作以`replaces_entry_id`替换不合格接续，旧条目置为`superseded`并不再计入累计；累计回到预算内才能继续测试、恢复。
- **并发去重**：同一接续点同一现场时刻的已确认记录唯一。两名工程师同时提交时只收一条（`confirmed`），另一条留作`pending`现场数据待确认；待确认项处理完毕前不能恢复流量。
- **重试幂等**：所有提交必须带`submission_id`；写入失败后用相同`submission_id`重试不会重复插入、不会重复累加（返回既有结果）。
- **旧单补历史**：创建时可用`legacy_unrecorded_splices`声明遗留接续数。此类旧单必须用`backfill`补齐历史接续项，补齐前`test`/`restore`一律冻结；补登项超预算同样退回待整改。
- **恢复前复核**：执行`restore`时按最新档案复核历史项、待确认数据与累计预算，任一不满足即拒绝。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、海况、船机许可、备缆窗口、接续质量、预算与历史项检查。
- `src/repository.py`：SQLite建表、区段接续档案、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发、幂等和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败场景与接续档案测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8330
```

默认端口为`8330`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `GET /api/segments/archive?cable=...&segment=...`：区段接续档案（累计损耗、预算、各状态条目数与明细）。
- `GET /api/splice-entries?status=pending`：接续条目列表，可按状态过滤。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
  动作包括`approve`/`mobilize`/`survey`/`splice`/`rectify`/`backfill`/`test`/`restore`/`cancel`。
- `POST /api/records/{id}/field-splices`：提交（晚到的）现场接续数据，请求体为`{"data":{...}}`，无需版本号；同点同时刻重复提交自动留`pending`，同`submission_id`重试幂等。
- `POST /api/splice-entries/{id}/resolve`：处理待确认数据，`{"data":{"decision":"confirm|discard"}}`；确认后超预算会联动退回待整改。

### 接续相关数据字段

- `splice`：`submission_id`、`splice_point_km`（须在区段里程内）、`occurred_at`、`splice_loss_db`（0~2dB）、`spare_used_km`。
- `rectify`：除`spare_used_km`外同上，另带`replaces_entry_id`。
- `backfill`：`{"items":[{submission_id, splice_point_km, splice_loss_db, occurred_at}, ...]}`，一次可补多项。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突，以及区段累计超预算、整改替换、晚到记录失效、并发去重、幂等重试、旧单补历史和恢复前复核。
