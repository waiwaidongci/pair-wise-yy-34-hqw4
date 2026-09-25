# 工伤事故调查与纠正措施

记录工伤经过、伤害、现场和证人，维护调查、纠正措施、验证与关闭流程。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- **重复事故合并（与调查流程库表分离，独立纵切）**：
  - `src/accident_domain.py`：事故/证据/措施/合并组的数据结构与状态。
  - `src/accident_rules.py`：命中规则、冲突判定、严重度/期限/责任重算（纯函数）。
  - `src/accident_repository.py`：合并业务建表、事务、合并快照、曾用编号。
  - `src/accident_service.py`：补报命中、指定主单、冲突中止、撤销、补材料重试。
  - `src/accident_http.py`：合并业务的请求入口与错误转换。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --accident-db ./accidents.db --port 8311
```

默认端口为`8311`，首次启动自动建库（调查库与合并库分开）。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

允许角色：reporter, investigator, safety_manager, viewer。严重度越高、伤害指数越大或未关闭措施越多，优先级越高；严重事故必须在4小时内启动调查。

## 重复事故合并

针对班组与安全科分别补报、现场留下多份编号、责任对不上的问题，提供独立的合并流程。规则、存储、请求入口分别由 `accident_rules` / `accident_repository` / `accident_http` 承担，互不耦合。

### 流程

1. **补报登记**：补报时登记现场编号 `scene_no`、伤者身份（姓名、证件号）和事发时段 `occurred_start/occurred_end`。
2. **自动命中**：在同类**未结案**事故中命中即归入待合并组（`pending`）。命中口径：
   - 现场编号一致；或
   - 伤者身份一致（优先比对证件号，缺失时比对姓名）且事发时段重叠。
   - 已关闭（`closed`）、已并入（`merged`）的单不命中；一份补报桥接多个待合并组时自动归并为最早的组。
3. **指定主单**：安全科（`safety_manager`）`designate-master` 指定主单：
   - 先做结论冲突检测；两边判定（有责/无责）互斥或已定结论文字不一致时**停止合并**，组置 `conflict`，响应中逐条写明冲突；
   - 无冲突则把各成员的证据与纠正措施转到主单（按类型+标题去重，保留来源单），非主单置 `merged` 并指向主单；
   - **严重度**取各单最高级（只上调）、**期限**按新严重度时限自合并时刻重算、**责任**按新材料并集重算（主单责任在前、去重）。
4. **冲突处理**：按新材料调用 `amend-conclusion` 修正结论（须填写原因）；冲突消除后组自动回到 `pending`，可重新指定主单。
5. **撤销**：已合并的组可 `revoke`。各原件按合并前快照恢复（编号、严重度、责任、期限、结论、版本全部复原），转入主单的证据与措施退回删除，组置 `revoked`；曾用编号（各单现场编号、并入单编号）作为别名长期保留在主单上。撤销后各单恢复为未结案，可再次被命中合并。

### 合并业务接口

- `POST /api/accidents`：补报（reporter/investigator/safety_manager），命中时返回 `merge` 与 `matched_cases`
- `GET /api/accidents`、`GET /api/accidents/{id}`
- `POST /api/accidents/{id}/evidence`、`POST /api/accidents/{id}/measures`
- `POST /api/accidents/{id}/amend-conclusion`：补充/修正结论材料
- `GET /api/merge-groups[?status=...]`、`GET /api/merge-groups/{id}`
- `POST /api/merge-groups/{id}/designate-master`：指定主单并执行合并（冲突时返回409及冲突清单）
- `POST /api/merge-groups/{id}/revoke`：撤销错误合并

合并组状态：`pending`（待合并）、`conflict`（结论冲突中止）、`merged`（已合并）、`revoked`（已撤销）。所有合并动作写入 SHA-256 审计链。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
