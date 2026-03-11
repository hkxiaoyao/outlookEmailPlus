# TODO-00008｜对外验证码与邮件读取开放 API — 实施待办清单

- **文档状态**: P0 已完成
- **版本**: V1.1
- **日期**: 2026-03-11
- **对齐 PRD**: `docs/PRD/PRD-00008-对外验证码与邮件读取开放API.md`
- **对齐 FD**: `docs/FD/FD-00008-对外验证码与邮件读取开放API.md`
- **对齐 TDD**: `docs/TDD/TDD-00008-对外验证码与邮件读取开放API.md`
- **对齐测试文档**: `docs/TEST/TEST-00008-对外验证码与邮件读取开放API-测试文档.md`
- **对齐安全架构**: `docs/FD/AD-00008-对外开放API安全架构设计.md`

---

## 1. 当前阶段判断

### 1.1 当前结论

- [x] P0 主体能力已落地
- [x] 当前 `/api/external/*` 已具备受控私有接入闭环
- [x] `X-API-Key` 鉴权、设置页配置、开放路由、开放 controller、external service、基础测试已经存在
- [x] 文档口径已收敛为“受控私有接入”，不再把当前实现描述为可直接公网暴露
- [x] P0 最终收口验证仍需补一轮完整执行
- [ ] P1 公网模式防护未实现
- [ ] P2 `wait-message` 解耦未实现

### 1.2 当前版本可做与不可做

- [x] 可用于单实例、本地部署、单可信调用方的 API 接入
- [x] 可用于验证码读取、验证链接提取、邮件排查、自检联调
- [ ] 不应直接作为公网开放平台对外宣传或默认部署
- [ ] 不应把 `/api/external/messages/{message_id}/raw` 与 `/api/external/wait-message` 视为公网默认可放开的接口

---

## 2. 前置准备

### 2.1 文档与范围确认

- [x] PRD 已完成
- [x] FD 已完成
- [x] TDD 已完成
- [x] API 文档已完成
- [x] BUG 文档已完成
- [x] 再次确认本轮开发目标是：
  - 先完成 P0 收口与验证
  - 再按 P1 做安全收敛
  - 最后再评估 P2 轮询解耦

### 2.2 基线验证

- [x] 运行现有测试：`python -m unittest discover -s tests -v`
- [x] 单独运行开放接口专项测试：
  - `python -m unittest tests.test_external_api -v`
  - `python -m unittest tests.test_settings_external_api_key -v`
  - `python -m unittest tests.test_verification_extractor_options -v`
  - `python -m unittest tests.test_ui_settings_external_api_key -v`
- [x] 记录当前通过数量，作为后续安全改造的回归基线

---

## 3. P0 收口任务

### 3.1 配置与鉴权闭环

**目标**：确认当前受控私有接入能力真正可交付，而不是只有代码存在。  
**涉及文件**：`outlook_web/security/auth.py`、`outlook_web/repositories/settings.py`、`outlook_web/controllers/settings.py`、`static/js/main.js`、`templates/index.html`

- [x] `external_api_key` 已写入 `settings` 默认项
- [x] `get_external_api_key()` / 脱敏展示能力已存在
- [x] `api_key_required()` 已使用 `X-API-Key` + `secrets.compare_digest()`
- [x] 设置页已支持录入、保存、清空 `external_api_key`
- [ ] 手动验证“脱敏值不会被保存回数据库”
- [ ] 手动验证“清空后所有 `/api/external/*` 统一返回 `API_KEY_NOT_CONFIGURED`”
- [ ] 手动验证“错误 key / 缺失 key / 正确 key”三条路径响应与文档一致

### 3.2 开放接口闭环

**目标**：确认所有 P0 接口都能按文档工作。  
**涉及文件**：`outlook_web/routes/emails.py`、`outlook_web/routes/system.py`、`outlook_web/controllers/emails.py`、`outlook_web/controllers/system.py`、`outlook_web/services/external_api.py`

- [x] `/api/external/messages`
- [x] `/api/external/messages/latest`
- [x] `/api/external/messages/{message_id}`
- [x] `/api/external/messages/{message_id}/raw`
- [x] `/api/external/verification-code`
- [x] `/api/external/verification-link`
- [x] `/api/external/wait-message`
- [x] `/api/external/health`
- [x] `/api/external/capabilities`
- [x] `/api/external/account-status`
- [x] 再核对一次返回结构与 OpenAPI 是否完全一致
- [x] 再核对一次错误码与 HTTP status 是否完全一致

### 3.3 P0 设计实现关注点

- [ ] `messages` 接口当前 `has_more=False` 为固定值，后续是否要补真实分页语义
- [ ] `health` 当前只证明“服务进程 + DB 可用”，不证明上游 Graph / IMAP 可读，文档需保持这个边界
- [ ] `account-status` 当前只做静态可读性判断，不主动拉信，这个边界需继续保留
- [ ] `raw` 当前直接复用详情 service 裁剪字段，后续如做风险分级，优先在 controller 入口处理而不是新建第二套读取链路

### 3.4 P0 测试收口

**涉及文件**：`tests/test_external_api.py`、`tests/test_settings_external_api_key.py`、`tests/test_verification_extractor_options.py`、`tests/test_ui_settings_external_api_key.py`

- [x] 开放接口测试文件已存在
- [x] 设置项测试文件已存在
- [x] 提取器参数化测试文件已存在
- [x] UI 接线测试文件已存在
- [x] 增加“OpenAPI 返回字段抽样校验”测试
- [x] 增加“`raw` 仅返回裁剪字段”测试
- [x] 增加“`wait-message` 不命中旧消息”测试复核

---

## 4. P1 公网模式安全收敛

### 4.1 先做设计，再动代码

**目标**：把“受控私有接入”与“半开放公网部署”彻底分层。  
**核心主线**：鉴权收敛。

- [ ] 明确 `public_mode=false` 时保持当前行为
- [ ] 明确 `public_mode=true` 时需要新增哪些限制
- [ ] 明确这些限制落在 `auth.py`、controller，还是独立 guard 模块
- [ ] 明确高风险接口分级清单：
  - `/api/external/messages/{message_id}/raw`
  - `/api/external/wait-message`

### 4.2 配置项与落库

**建议涉及文件**：`outlook_web/db.py`、`outlook_web/repositories/settings.py`、`outlook_web/controllers/settings.py`、`static/js/main.js`、`templates/index.html`

- [ ] 增加 `external_api_public_mode`
- [ ] 增加 `external_api_ip_whitelist`
- [ ] 增加 `external_api_disable_wait_message`
- [ ] 增加 `external_api_disable_raw_content`
- [ ] 增加 `external_api_rate_limit_per_minute`
- [ ] 设置页补充公网模式说明文案，避免误开

### 4.3 安全控制实现

**建议涉及文件**：`outlook_web/security/auth.py`、`outlook_web/controllers/emails.py`、`outlook_web/controllers/system.py`

- [ ] 增加 `enforce_external_api_public_controls()`
- [ ] 接入来源 IP 白名单校验
- [ ] 接入高风险接口禁用判断
- [ ] 接入基础限流能力
- [ ] 保证 `api_key_required()` 仍是必经入口

### 4.4 自检接口升级

**建议涉及文件**：`outlook_web/controllers/system.py`、`outlook_web/services/external_api.py`

- [ ] `/api/external/capabilities` 返回 `public_mode`
- [ ] `/api/external/capabilities` 返回 `restricted_features`
- [ ] `/api/external/health` 明确是否返回 `upstream_probe_ok`
- [ ] `/api/external/account-status` 是否增加最近一次探测结果字段

### 4.5 P1 测试

**建议涉及文件**：`tests/test_external_api.py`

- [ ] 增加 `public_mode=false` 回归测试
- [ ] 增加白名单拒绝测试
- [ ] 增加 `wait-message` 禁用测试
- [ ] 增加 `raw` 禁用测试
- [ ] 增加限流命中测试

---

## 5. P2 `wait-message` 解耦

### 5.1 先回答清楚的设计问题

- [ ] 后台轮询结果放在哪里：
  - 内存缓存
  - SQLite 状态表
  - 独立 worker 缓存
- [ ] 匹配结果是缓存“最近邮件摘要”还是缓存“按条件命中的探测结果”
- [ ] 新模型是否仍保留现有 `/api/external/wait-message` 路径
- [ ] 如果保留原路径，返回语义是否需要从“同步等待”改成“查询最近探测状态”

### 5.2 可能的实现拆分

**建议涉及文件**：`outlook_web/services/external_api.py`、`outlook_web/services/scheduler.py`、`outlook_web/db.py`

- [ ] 设计 `external_probe_runs` 或类似状态表
- [ ] 新增后台探测任务
- [ ] 将“拉信”与“HTTP 请求等待”解耦
- [ ] 让 Web API 只负责读状态，不负责 `sleep`

### 5.3 P2 风险点

- [ ] 不能把当前同步轮询逻辑直接搬到 scheduler 中而不定义状态模型
- [ ] 不能先做异步接口再决定状态存储，否则接口会先失稳
- [ ] 不能让新旧 `wait-message` 语义混杂，否则调用方很难升级

---

## 6. 设计实现问题清单

### 6.1 现在必须定的设计点

- [ ] 外部安全控制放在哪一层
  - 倾向：`auth.py` 保留鉴权，公网模式 guard 独立函数，由 controller 在入口调用
- [ ] 高风险接口是“返回 403 禁用”还是“返回降级字段”
  - 倾向：P1 先返回 `403 FEATURE_DISABLED`
- [ ] `/api/external/capabilities` 是否承担“当前模式声明”
  - 倾向：承担，避免调用方靠 README 猜测
- [ ] `account-status` 是否做真实拉信探测
  - 倾向：P0/P1 不做，只暴露静态状态；真实探测放到后续后台任务

### 6.2 现在暂时不该提前实现的点

- [ ] 多 API Key
- [ ] Key 级邮箱范围授权
- [ ] 调用方配额审计后台
- [ ] 开发者门户
- [ ] Webhook 推送式开放接口

---

## 7. 文档与交付待办

### 7.1 文档同步

- [x] PRD / FD / TDD / API 文档已成套存在
- [ ] 后续如实现 P1，需同步更新：
  - `docs/PRD/PRD-00008-对外验证码与邮件读取开放API.md`
  - `docs/FD/FD-00008-对外验证码与邮件读取开放API.md`
  - `docs/TDD/TDD-00008-对外验证码与邮件读取开放API.md`
  - `docs/FD/AD-00008-对外开放API安全架构设计.md`
  - `docs/api.md`
- [ ] 后续如实现 P2，需单独补 `wait-message` 解耦设计稿，不建议只在原 TDD 中追加几段说明

### 7.2 执行日志

- [ ] 开始 P1 开发前，更新 `docs/LOG/LOG-00008-对外验证码与邮件读取开放API-执行日志.md`
- [ ] 每个阶段结束后记录：
  - 已做功能
  - 未做功能
  - 风险
  - 回滚点

---

## 8. 发布前检查

- [x] P0 测试全量通过
- [ ] 手动联调通过
- [x] 文档与代码口径一致
- [ ] README / API 文档未把当前版本描述为公网开放平台
- [ ] `raw` 与 `wait-message` 的风险说明对外可见
- [ ] 确认本次发布属于：
  - P0 收口
  - 或 P1 安全收敛
  - 或 P2 解耦演进

---

## 9. 阶段性结论

当前最合理的实施顺序不是继续平铺接口，而是：

1. 先把 **P0 收口与验证** 做扎实，确认当前版本可稳定用于受控私有接入。
2. 再沿 **鉴权收敛** 主线推进 P1，把公网模式、高风险接口分级、白名单、限流做成单独的一层。
3. 最后沿 **轮询解耦** 主线推进 P2，把 `wait-message` 从同步阻塞模型迁移出去。

这份 TODO 的核心目的不是重复文档，而是约束后续实现顺序：

- **先收口，再加固**
- **先分层，再扩展**
- **先解决安全边界，再谈公网能力**
