# 工伤事故调查与纠正措施

记录工伤经过、伤害、现场和证人，维护调查、纠正措施、验证与关闭流程；同时支持班组与安全科重复补报的待合并、主单合并、冲突停止和错误撤销。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态、角色、现场编号/伤者/时段等基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限、重复命中、材料重算和结论冲突规则。
- `src/repository.py`：SQLite建表、事务、版本控制、证据归属、合并快照和审计链。
- `src/service.py`：权限检查、补报编排、合并/撤销、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则、合并与HTTP失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8311
```

默认端口为`8311`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 补报与合并规则

### 1. 补报登记

`POST /api/items`必须登记：

- `site_ref`：现场编号，自动去除首尾空格并转大写。
- `injured_person`：伤者身份标识，自动去除空白。
- `incident_start`、`incident_end`：ISO 8601事发时段，结束时间不能早于开始时间。
- `report_source`：`team`（班组）或`safety`（安全科），缺省为`team`。
- 原有事故字段：`title`、`description`、`severity`、`quantity`、`threshold`、`external_ref`。
- 可选责任与结论：`responsibility`、`conclusion_code`、`conclusion_note`。

系统只把**现场编号相同、伤者相同、时段重叠且未结案**的事故视为同类事故。补报命中后不会自动改数据，而是创建或加入`pending`合并请求；响应中返回`merge_request_id`。

### 2. 安全科指定主单

安全科提交：

```json
{
  "master_item_id": 2,
  "expected_version": 1
}
```

到`POST /api/merge-requests/{id}/merge`。系统会：

1. 校验主单属于该合并请求且所有成员未结案。
2. 先检查两边确定性结论是否冲突。
3. 将各成员的证据和纠正措施转到主单（记录仍保留原事故ID和原现场编号）。
4. 非主单状态改为`merged`，不再接收新增记录。
5. 按新材料重算：严重度取最高，伤害指数取最高，阈值取最低；优先级和响应期限由这些值重新计算。
6. 责任优先采用安全科材料，其次调查材料，再采用班组材料。

### 3. 结论冲突即停止

若同一合并组同时存在`work_related`和`not_work_related`确定性结论：

- 合并请求变为`conflicted`。
- 不转证据、不改状态、不重算主单。
- `reason`写明“事故定性冲突”，`conflict_detail`列明冲突事故、来源、结论和责任。
- 接口返回`409 Conflict`。

### 4. 撤销错误合并

安全科可调用`POST /api/merge-requests/{id}/undo`。系统从事务合并前快照恢复：

- 各原事故状态、严重度、伤害指数、阈值、责任和结论。
- 证据与措施的归属。
- 主单合并前材料。
- 所有原始编号继续保留；合并记录留在审计链中，可追溯曾用合并请求和各事故编号。

合并后新增到主单的材料不会被删除，撤销后仍归主单。

## 主要接口

- `GET /health`
- `GET /api/items`，可选`?status=reported|investigating|corrective_action|verification|closed|merged`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/merge-requests`，可选`?status=pending|conflicted|merged|undone`
- `GET /api/merge-requests/{id}`
- `POST /api/merge-requests/{id}/merge`，仅`safety_manager`
- `POST /api/merge-requests/{id}/undo`，仅`safety_manager`
- `GET /api/audit`，可选`entity_id`和`entity_type`

允许角色：reporter、investigator、safety_manager、viewer。严重度越高、伤害指数越大或未关闭措施越多，优先级越高；严重事故必须在4小时内启动调查。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
