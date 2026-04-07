# WORKSPACE — 工作区操作记录

> 本文档记录项目开发过程中的操作日志，按日期倒序排列。

---

## 2026-04-07

### 操作记录

#### 7. Docker API 自更新安全策略强化（策略A）

**时间**：2026-04-07 下午

**背景**：原有 Docker API 自更新功能存在安全隐患——本地构建镜像可能误触发更新，导致不可预期的行为。

**目标**：实施策略A（彻底禁止本地构建镜像触发 Docker API 更新），确保只有官方远程镜像才能触发更新。

**实施内容**：

1. **镜像白名单收紧**：
   - 移除 `outlook-email-plus`（无 namespace）白名单项
   - 仅保留 `guangshanshui/outlook-email-plus`官方镜像前缀
   
2. **新增本地构建检测**：
   - `validate_image_for_update()`：镜像白名单 + RepoDigests 检测双重校验
   - `_looks_like_local_image_ref()`：基于 namespace 的启发式本地镜像检测（修复 bug：改为 namespace 白名单判断）
   - `_has_repo_digests()`：通过 Docker API 检查镜像 RepoDigests（本地 build 镜像为空）
   
3. **API 层前置校验**：
   - `_trigger_docker_api_update()` 在触发阶段就获取容器镜像并校验
   - 校验失败返回 403/500，避免等到 spawn updater 内部才失败
   
4. **部署信息展示优化**：
   - `api_deployment_info()` 不再依赖 `DOCKER_SELF_UPDATE_ALLOW` 环境变量
   - 只要 docker.sock 可用就通过 Docker API 获取真实镜像名（更准确）
   
5. **测试用例调整**：
   - `docker-compose.docker-api-test.yml` 镜像改为 `guangshanshui/outlook-email-plus:latest`（形成负向用例：本地 build 但伪装官方名也会被 RepoDigests 检测拦截）

**修改文件**：
- `outlook_web/services/docker_update.py`：
  - 白名单收紧
  - 新增 `validate_image_for_update()`, `_looks_like_local_image_ref()`, `_has_repo_digests()`
  - `get_container_info()` 通过 `client.images.get()` 获取 RepoDigests
  - `spawn_update_helper_container()` 和 `self_update()` 调用新校验函数
  - Bug修复：`_looks_like_local_image_ref()` 改为 namespace 白名单判断（`guangshanshui`, `docker.io/guangshanshui`, `ghcr.io/guangshanshui`）
- `outlook_web/controllers/system.py`：
  - `_trigger_docker_api_update()` API 层镜像校验
  - `api_deployment_info()` 获取镜像名逻辑优化
- `docker-compose.docker-api-test.yml`：测试镜像名调整

**代码逻辑测试结果**（PowerShell环境）：
```
=== 白名单校验 ===
guangshanshui/outlook-email-plus:latest  → ✅ 通过
guangshanshui/outlook-email-plus:test    → ✅ 通过
outlook-email-plus:latest                → ❌ 拦截（无 namespace）
myregistry/outlook-email-plus:latest     → ❌ 拦截（非官方 namespace）

=== 启发式检测 ===
guangshanshui/outlook-email-plus:*       → False（正确识别为官方）
outlook-email-plus:*                     → True（正确识别为本地）
其他namespace/*                          → True（正确识别为非官方）
```

**文档产出**：
- `docs/DEV/manual-acceptance-checklist.md`：人工验收清单（4 个测试用例 + 验收标准 + 快速测试脚本）

**待验收项**：
- [ ] 负向用例1：本地构建镜像触发更新被拦截
- [ ] 负向用例2：本地构建伪装官方名触发更新被拦截
- [ ] 正向用例3：官方远程镜像成功触发更新流程
- [ ] 部署信息准确性验证

**关联 Issue/PR**：待 Docker 容器内实际验收通过后提交

---

#### 1. mystatus 插件状态确认

**背景**：尝试使用 `mystatus` 工具查询 AI 账户配额使用情况。

**实际情况**：

| 项目 | 状态 |
|------|------|
| 插件安装 | `opencode-mystatus@1.2.4` 已安装于全局 `~/.config/opencode/node_modules/` |
| 全局配置 | `~/.config/opencode/opencode.json` 已注册 `plugin` 和 `command` |
| 项目配置 | 项目级 `opencode.json` 未单独配置 mystatus 插件（使用全局配置） |
| 工具可用性 | 当前会话工具列表中**未注册** `mystatus` 工具，无法直接调用 |

**配置位置**：

- 全局插件配置：`C:\Users\PLA30\.config\opencode\opencode.json`
  ```json
  "plugin": ["opencode-mystatus"],
  "command": {
    "mystatus": {
      "description": "Query quota usage for all AI accounts",
      "template": "Use the mystatus tool to query quota usage. Return the result as-is without modification."
    }
  }
  ```
- 插件源码位置：`C:\Users\PLA30\.config\opencode\node_modules\opencode-mystatus\`

**支持平台**：

| Platform | Account Type |
|----------|-------------|
| OpenAI | Plus / Team / Pro |
| Zhipu AI | Coding Plan |
| Z.ai | Coding Plan |
| GitHub Copilot | Individual / Business |
| Google Cloud | Antigravity |

**结论**：`mystatus` 作为 opencode 插件需要在 opencode 运行时环境中通过 `/mystatus` 命令或自然语言触发，当前通过外部 Agent 调用时无法直接使用该工具。

#### 2. 热更新功能开发状态确认

**关联文档**：
- AI 提示词：`docs/DEV/hot-update-ai-prompt.md`
- 基线记录：`docs/DEV/hot-update-baseline.md`

**功能概述**：为 Outlook Email Plus 实现 Docker 部署环境下的一键更新功能，支持 Watchtower 和 Docker API 两种更新方式。

**实施进度（全部已完成）**：

| 阶段 | 内容 | 状态 | Commit |
|------|------|------|--------|
| Phase 1 | BUG 修复（Token 为空启动失败、浏览器缓存旧 JS） | ✅ | 91a8f35 |
| Phase 2 | UI 提示优化（镜像标签/构建模式检测） | ✅ | 91a8f35 |
| Phase 3 | 内置 Docker API 自更新 | ✅ | 91a8f35 |
| P0 | BUG-006 GitHub 仓库地址修复 | ✅ | e6d27b6 |

**核心产出**：
- 新增：`outlook_web/services/docker_update.py`（591 行，经代码验证 2026-04-07，原文档记录 839 行已修正）
- 新增 API：`/api/system/version-check`、`/api/system/trigger-update`、`/api/system/test-watchtower`、`/api/system/deployment-info`
- 新增设置项：`watchtower_url`、`watchtower_token`（加密存储）、`update_method`
- 前端：版本更新 Banner、Watchtower 配置 UI、Docker API 更新方式选择
- 前端补齐：设置页一键更新区域的部署信息警告（`/api/system/deployment-info` → `#deploymentWarnings`）
- 安全：默认关闭 Docker API 自更新、镜像白名单校验、审计日志

**当前版本**：v1.12.0，热更新验证已通过（v1.12.0 → v1.12.1）

#### 3. 文档更新

- 创建 `WORKSPACE.md` 工作区操作记录文档
- 确认项目结构：项目级 `opencode.json` 仅配置了子代理（context-retriever, small-task-executor），mystatus 依赖全局配置
- 记录热更新功能完整实施状态

#### 4. 热更新文档代码验证与清理

**操作内容**：

1. **代码验证**：逐一对比 `hot-update-ai-prompt.md` 中的描述与实际代码
   - ✅ 4 个 API 端点全部存在且已注册路由
   - ✅ `update_method` 设置项 GET/PUT 支持
   - ✅ 静态文件缓存控制 `set_static_cache_control()` 
   - ✅ GitHub 仓库地址 `ZeroPointSix/outlookEmailPlus`
   - ✅ `docker-compose.yml` 配置完整（Token 默认值、docker.sock 注释、DOCKER_SELF_UPDATE_ALLOW）
   - ✅ `.env.example` 模板完整
   - ⚠️ **发现差异**：`docker_update.py` 实际 591 行，文档记录为 839 行 → **已修正**

2. **文档清理**：清理 `hot-update-ai-prompt.md`
   - 删除"待实施任务 (可选扩展)"部分（第 154-318 行），该部分重复了已完成的 Phase 1-3 任务描述
   - 新增"代码验证记录"表格，记录 13 项验证结果
   - 修正 `docker_update.py` 行数为实际值 591
   - 保留"参考文件清单"和"注意事项"部分

**已修改文件**：
- `docs/DEV/hot-update-ai-prompt.md` — 删除冗余内容，新增验证记录，修正数据

#### 5. README 生产配置更新

**操作内容**：
- 更新 `README.md` 中 docker-compose 生产配置示例，同步 Phase 3 新功能
  - 新增 `DOCKER_SELF_UPDATE_ALLOW` 环境变量（注释状态）
  - 新增 docker.sock 挂载选项（注释状态）
  - 新增"更新方式"说明段落，指导用户如何切换 Watchtower/Docker API 模式
- 修正 README "最近更新"版本号：v1.11.0 → v1.12.0
- 新增"一键更新"功能说明段落

**已修改文件**：
- `README.md` — 版本号更新、docker-compose 示例更新、功能说明补充

#### 6. 热更新功能完整性详细分析

**结论：功能已完整实现**，所有文档描述的功能点均在代码中找到对应实现。

**后端功能验证（全部 ✅）**：

| 功能 | 位置 | 状态 |
|------|------|------|
| 版本检测 API（10 分钟缓存） | system.py:353 | ✅ |
| 更新触发 API（双模式） | system.py:402 | ✅ |
| Watchtower 更新（DB→env fallback + 加密 Token） | system.py:438 | ✅ |
| Docker API 更新（安全检查→socket→白名单→12 步流程） | system.py:500 + docker_update.py | ✅ |
| 部署信息检测（镜像/标签/本地构建/Watchtower 连通性） | system.py:561 | ✅ |
| Watchtower 连通性测试 | system.py:732 | ✅ |
| 设置项 update_method（GET/PUT） | settings.py:351/1013 | ✅ |
| 静态文件缓存控制 | app.py:124 | ✅ |

**前端功能验证（全部 ✅）**：

| 功能 | 位置 | 状态 |
|------|------|------|
| 页面加载版本检查 | main.js:3763 `checkVersionUpdate()` | ✅ |
| 双模式触发更新 + 差异化超时 | main.js:3790 `triggerUpdate()` (120s/10s) | ✅ |
| 重启轮询等待 | main.js:3880 `waitForRestart()` | ✅ |
| 部署信息警告渲染（设置页） | main.js: `loadDeploymentInfo()` / `renderDeploymentWarnings()` | ✅ |
| Watchtower 连通性测试 | main.js:2169 `testWatchtower()` | ✅ |
| 设置加载/保存 update_method | main.js:1743/2100 | ✅ |

**Docker API 自更新 12 步流程（docker_update.py）**：

| 步骤 | 函数 | 状态 |
|------|------|------|
| 1. 启用开关检查 | `is_docker_api_enabled()` | ✅ |
| 2. Socket 可访问性 | `check_docker_socket()` | ✅ |
| 3. 获取当前容器信息 | `get_current_container_info()` | ✅ |
| 4. 镜像名白名单校验 | `validate_image_name()` | ✅ |
| 5. 拉取最新镜像 | `pull_latest_image()` | ✅ |
| 6. Digest 比较 | `compare_image_digest()` | ✅ |
| 7. 创建新容器（复制配置） | `create_new_container()` + `_parse_volumes()` + `_parse_ports()` | ✅ |
| 8. 启动新容器 | `start_new_container()` | ✅ |
| 9. 健康检查 | `health_check_new_container()` | ✅ |
| 10. 停止旧容器 | `stop_old_container()` | ✅ |
| 11. 重命名容器 | `rename_containers()` | ✅ |
| 12. 清理/保留旧容器 | `cleanup_old_container()` | ✅ |

**发现的问题（非阻塞）**：

| # | 问题 | 严重度 | 说明 |
|---|------|--------|------|
| 1 | `can_auto_update` 未考虑 Docker API 模式 | 低 | `api_deployment_info()` 中 `can_auto_update` 仅检查 Watchtower 连通性，用户选 Docker API 模式且无 Watchtower 时会误报为不可更新 |
| 2 | `self_update()` 同步调用风险 | 低 | system.py:531 注释说明同步调用可能中断响应，但 Docker API 模式下前端有 120s 超时 + `waitForRestart()` 轮询兜底 |
| 3 | `docker-compose.hotupdate-test.yml` 含硬编码密钥 | 低 | 测试专用文件，不影响生产安全 |

#### 7. 热更新功能非阻塞问题修复

**修复 #1：`can_auto_update` 逻辑支持 Docker API 模式**

- **文件**：`outlook_web/controllers/system.py`
- **问题**：`api_deployment_info()` 中 `can_auto_update` 仅检查 Watchtower 连通性
- **修复**：新增 `docker_api_available` 检测（检查 `DOCKER_SELF_UPDATE_ALLOW` + socket 可用性），`can_auto_update` 逻辑改为 `watchtower_reachable or docker_api_available`
- **新增返回字段**：`deployment.docker_api_available`（布尔值）

**修复 #2：`self_update()` 同步→异步**

- **文件**：`outlook_web/controllers/system.py`
- **问题**：`_trigger_docker_api_update()` 同步调用 `self_update()`，旧容器被停止时响应无法到达客户端
- **修复**：使用 `threading.Thread(daemon=True)` 在后台线程执行自更新，主线程立即返回 `{"success": True, "message": "Docker API 自更新已启动，容器即将重启"}`
- **审计日志**：移入后台线程，确保更新结果被记录
- **前端兼容**：前端已有 `waitForRestart()` 轮询 `/healthz` 等待新容器启动，无需修改

**修复 #3：清理测试配置硬编码密钥**

- **文件**：`docker-compose.hotupdate-test.yml`
- **问题**：SECRET_KEY 和 WATCHTOWER_HTTP_API_TOKEN 为硬编码明文值
- **修复**：改为 `${SECRET_KEY:-please-change-this-secret-key-for-testing}` 和 `${WATCHTOWER_HTTP_API_TOKEN:-test-hotupdate-token}` 格式，支持 `.env` 文件注入
- **附加**：更新文件头注释（移除版本号引用，添加使用方式说明）

**已修改文件汇总**：
- `outlook_web/controllers/system.py` — can_auto_update 逻辑 + self_update 异步化
- `docker-compose.hotupdate-test.yml` — 密钥环境变量化

#### 8. README 环境变量补充

**操作内容**：
- 在 `README.md` 的"常用环境变量"部分新增"一键更新相关"小节
- 补充环境变量说明：
  - `WATCHTOWER_HTTP_API_TOKEN` — Watchtower API 鉴权令牌
  - `WATCHTOWER_API_URL` — Watchtower API 地址
  - `DOCKER_SELF_UPDATE_ALLOW` — 是否启用 Docker API 自更新
  - `DOCKER_IMAGE` — 当前容器镜像名（可选）
- 添加安全提示说明 Docker API 自更新的风险

**已修改文件**：
- `README.md` — 新增一键更新相关环境变量说明

#### 9. 一键更新功能人工验收 BUG 分析

**分析范围**：dev 分支相对于 main 分支新增的一键更新功能

**功能概述**：
- 版本检测：GET `/api/system/version-check`，对比 GitHub 最新 release 与本地版本
- 触发更新：POST `/api/system/trigger-update?method=watchtower|docker_api`
- Watchtower 配置：设置页可配置 URL + Token（加密存储）
- Docker API 自更新：12 步流程（拉取镜像→创建容器→健康检查→切换）

**潜在 BUG 分析**：

| # | 问题描述 | 严重度 | 复现条件 | 影响 | 建议处理 |
|---|---------|--------|----------|------|----------|
| 1 | **镜像名检测依赖 DOCKER_IMAGE 环境变量** | 低 | 未设置 DOCKER_IMAGE 时 | `api_deployment_info()` 无法准确获取镜像名，可能显示 `unknown` | 可接受，用户可手动设置 |
| 2 | **容器名冲突风险** | 低 | Docker API 自更新失败后重试 | 新容器使用 `{name}_new` 临时名称，若上次失败未清理可能冲突 | 代码中已有 force 删除逻辑，风险较低 |
| 3 | **审计日志在后台线程中记录** | 低 | Docker API 自更新 | 若新容器启动后旧容器被停止，审计日志写入数据库时机可能不稳定 | 非阻塞，日志可能丢失但不影响功能 |
| 4 | **前端超时固定 120s** | 信息 | Docker API 大镜像拉取 | 若镜像很大，拉取时间超过 120s，前端可能误报超时（但后台仍在执行） | 可接受，前端会继续轮询 `/healthz` |

**健康检查说明**：
- `docker_update.py` 中的 `health_check_new_container()` 检查的是 Docker 容器状态和 Docker 原生 healthcheck
- 前端 `waitForRestart()` 轮询的 `/healthz` 端点是应用级健康检查（已存在于 `system.py:39`）
- 两者是独立的：容器启动后，后端健康检查通过 → 前端轮询 `/healthz` 确认应用可用

**验收建议**：

1. **Watchtower 模式验收**：
   - [ ] 部署 docker-compose（含 watchtower 服务）
   - [ ] 在设置页配置 Watchtower URL + Token
   - [ ] 点击"测试连通性"按钮，确认返回成功
   - [ ] 触发版本检测，确认 Banner 显示
   - [ ] 点击"立即更新"，确认容器重启

2. **Docker API 模式验收**：
   - [ ] 修改 docker-compose 启用 `DOCKER_SELF_UPDATE_ALLOW=true`
   - [ ] 挂载 `/var/run/docker.sock`
   - [ ] 在设置页切换"更新方式"为 Docker API
   - [ ] 确认部署信息显示 `docker_api_available: true`
   - [ ] 触发更新，确认 12 步流程正常执行

3. **边界条件验收**：
   - [ ] 使用固定版本标签（如 `:v1.12.0`），确认 UI 警告正确
   - [ ] 本地构建镜像，确认 UI 警告正确
   - [ ] 未配置 Watchtower Token，确认错误提示

**结论**：一键更新功能已基本完整，无阻塞性 BUG。建议按上述验收清单进行人工测试。

#### 10. Docker API 自更新实测发现阻塞 BUG 并修复（dev 分支）

**实测背景**：尝试在 Docker 容器中调用 `/api/system/trigger-update?method=docker_api` 做完整 12 步自更新模拟。

**实际问题**：接口直接返回 500。

- 容器日志报错：`ModuleNotFoundError: No module named 'outlook_web.models'`
- 根因：`outlook_web/controllers/system.py::_trigger_docker_api_update()` 中错误引用 `from outlook_web.models import AuditLog`，但项目不存在 `outlook_web/models.py` 以及 `AuditLog` 类

**修复策略（方案 A）**：移除 `AuditLog` 依赖。

- 主线程：使用现有 `outlook_web.audit.log_audit()` 记录一次 `trigger_docker_api_update_start`（含 method/remove_old/username）
- 后台线程：仅执行 `docker_update.self_update()` 并写入应用日志（logger），避免后台线程依赖 Flask request context / DB 连接

**修改文件**：
- `outlook_web/controllers/system.py` — 移除 `outlook_web.models.AuditLog` 引用，改用 `log_audit`

---

#### 11. A2 方案实现：按需 helper job 容器（避免"自杀"问题）

**背景问题**：Docker API 模式实测发现核心阻塞——容器无法在内部 stop 自己后继续执行后续步骤（进程被杀死）。原始方案使用 daemon 线程在后台执行 self_update()，但旧容器被 stop 的瞬间后台线程也会被杀死，导致"create 新容器→stop 旧→rename→cleanup"流程中断。

**方案选型**：

| 方案 | 描述 | 优势 | 劣势 |
|------|------|------|------|
| A1: 两阶段脚本 | app 容器内写脚本→nohup 后台执行→exit | 最简单 | 可靠性差，进程管理困难 |
| **A2: 按需 helper job 容器** | app 通过 Docker API 临时创建 updater 容器 | 可靠、隔离、auto_remove 自动清理 | 短暂 2 容器并存 |
| A3: 外部 updater 服务 | 额外部署常驻 updater 容器 | 最稳 | 增加部署复杂度 |

**选定方案**：A2（按需 helper job 容器）

**架构设计**：

```
┌─────────────────────────────┐
│  App 容器（用户请求）          │
│                             │
│  1. 鉴权 + 安全校验            │
│  2. 记录审计日志（主线程）       │
│  3. Docker API 创建 updater 容器│
│  4. 立即返回 HTTP 响应          │
└─────────────┬───────────────┘
              │ docker.sock
              ▼
┌─────────────────────────────┐
│  Updater 容器（短生命周期）     │
│                             │
│  1. sleep(2) 等 HTTP 响应     │
│  2. pull 最新镜像              │
│  3. create 新容器（复制配置）   │
│  4. stop 旧容器（释放端口）     │
│  5. start 新容器               │
│  6. healthcheck 新容器         │
│  7. rename 容器                │
│  8. cleanup 旧容器             │
│  9. 退出 → auto_remove 自动清理 │
└─────────────────────────────┘
```

**关键设计决策**：

1. **start_delay_seconds=2**：updater 容器启动后延迟 2 秒再执行更新操作，给 app 容器的 HTTP 响应留出到达客户端的时间
2. **先 stop 旧容器再 start 新容器**：解决 host port 映射场景下端口冲突问题（docker-compose 常见 5000:5000 映射）
3. **auto_remove=True**：updater 容器退出后自动删除，保持"单容器部署体验"
4. **失败回滚**：新容器启动失败或健康检查失败时，尝试恢复旧容器
5. **透传 Docker 凭证**：支持 DOCKER_AUTH_CONFIG / DOCKER_CONFIG 环境变量，确保 updater 可拉取私有镜像
6. **Watchtower 排除**：updater 容器添加 `com.centurylinklabs.watchtower.enable=false` 标签

**新增/修改文件清单**：

| 文件 | 操作 | 说明 |
|------|------|------|
| `outlook_web/services/docker_update_helper.py` | **新增**（69 行） | updater 容器入口模块，读取环境变量调用 `self_update()` |
| `outlook_web/services/docker_update.py` | 修改 | 新增 `get_container_info()`、`spawn_update_helper_container()`；增强 `validate_image_name()` 支持 digest 和 registry port；增强 volumes 解析支持 named volume；`self_update()` 新增 `target_container_id` 参数；调整步骤顺序（先 stop 旧再 start 新）；失败时尝试恢复旧容器 |
| `outlook_web/controllers/system.py` | 修改 | `healthz()` 新增 `boot_id` 和 `version` 字段；`_trigger_docker_api_update()` 改为调用 `spawn_update_helper_container()`；`api_deployment_info()` 增强 Docker API 检测和上下文感知警告 |
| `static/js/main.js` | 修改 | `waitForRestart()` 增加 boot_id 变化检测；Docker API 模式超时放宽到 180s；`triggerUpdate()` 统一走 waitForRestart 逻辑；`loadSettings()` 触发部署信息加载；新增 `loadDeploymentInfo()` / `renderDeploymentWarnings()`；语言切换时重渲染部署警告 |
| `templates/index.html` | 修改 | 新增 `#deploymentWarnings` 容器；微调缩进格式 |
| `tests/test_error_and_trace.py` | 修改 | 适配 healthz 新增 `boot_id` / `version` 字段 |
| `tests/test_smoke_contract.py` | 修改 | 适配 healthz 新增字段 |
| `docker-compose.docker-api-test.yml` | **新增**（45 行） | Docker API 模式专用测试 compose 配置 |
| `docker-compose.hotupdate-test.yml` | 修改 | 新增 DOCKER_IMAGE 环境变量 |

**self_update() 步骤顺序调整**：

原方案（先 start 新再 stop 旧）在 host port 映射场景下会产生端口冲突：

```
原: pull → compare → get_info → validate → pull_image → compare_digest → create → start_new → health_check → stop_old → rename → cleanup
新: pull → compare → get_info → validate → pull_image → compare_digest → create → stop_old → start_new → health_check → rename → cleanup
```

**前端轮询优化**：

通过 `boot_id`（`{timestamp}-{pid}`）判断容器是否发生了真正的进程重启：
- 首次轮询前记录 `initialBootId`
- 后续轮询中检测 `boot_id` 是否变化
- `boot_id` 变化 或 `seenDown`（曾看到服务不可用）时判定为重启完成

#### 12. A2 方案本地 Docker 验证（dev 分支）

**验证环境**：
- Docker Desktop 4.43.2 (Engine 28.3.2)
- 本地构建镜像 `outlook-email-a2-test:latest`（基于 dev 分支源码）
- 容器名 `outlook-dockerapi-test`，端口映射 5003:5000
- 挂载 docker.sock，DOCKER_SELF_UPDATE_ALLOW=true

**验证步骤与结果**：

| # | 测试项 | 结果 | 说明 |
|---|--------|------|------|
| 1 | `healthz` 返回 `boot_id` + `version` | ✅ | `{"status":"ok","boot_id":"1775563642828-8","version":"1.12.0"}` — A2 代码已生效 |
| 2 | 登录 + CSRF token 获取 | ✅ | Cookie-based session + X-CSRFToken |
| 3 | 部署信息 API | ✅ | `docker_api_available:true`，`is_local_build:true`，警告正确 |
| 4 | 触发更新 API（白名单校验） | ✅ | 返回 `"镜像名不在白名单内: outlook-email-a2-test:latest"` — 正确拦截 |
| 5 | Docker socket 连通性 | ✅ | `check_docker_socket()` 返回可用 |
| 6 | 容器内省 `get_container_info()` | ✅ | 返回完整容器信息（name/image/volumes/networks/restart_policy） |
| 7 | updater 容器创建 + 运行 + auto_remove | ✅ | 容器创建成功 → 正常运行 → 退出后自动删除 |
| 8 | 完整 helper 流程 (`python -m docker_update_helper`) | ✅ | 步骤 1-3 通过（权限/socket/容器信息），步骤 4 白名单拦截（本地构建镜像），原容器完好 |

**关键发现**：

1. **A2 核心逻辑完全通过**：updater 容器可以由 app 容器通过 docker.sock 创建、运行、自动清理
2. **白名单机制正常**：本地构建镜像被正确拦截，不会误更新
3. **auto_remove 有效**：updater 容器退出后自动删除，保持"单容器体验"
4. **原容器保护有效**：即使更新流程被拦截，原容器状态不受影响（status=running）
5. **无法端到端验证 pull→create→stop→start→rename**：因为本地构建镜像无法从远程 registry pull；完整流程需在真实远程镜像环境下验证

**清理**：
- 停止并删除测试容器和 volume
- 删除临时测试脚本 `test_a2_spawn.py` / `test_a2_helper.py`
- 删除本地测试镜像 `outlook-email-a2-test:latest`

**结论**：A2 方案的核心逻辑（updater 容器创建、运行、自动清理、白名单保护）已全部验证通过。完整端到端测试（含 pull→create→stop→start→rename）需在远程镜像环境下进行。

---

### 待办：本地端到端测试指南

> 以下是用户自行在本地进行端到端测试的完整步骤，覆盖 Watchtower 模式和 Docker API 模式。

#### 前提条件

1. Docker Desktop 运行中（Engine 28.x+）
2. dev 分支最新代码
3. 端口 5002、5003 未被占用

#### 方式一：Docker API 模式测试（A2 方案核心验证）

```bash
# 1. 构建本地镜像（含 A2 代码改动）
docker compose -f docker-compose.docker-api-test.yml up -d --build

# 2. 等待容器启动（约 20 秒）
# 查看健康状态
docker ps --filter "name=outlook-dockerapi-test"

# 3. 浏览器访问
# 打开 http://localhost:5003
# 使用密码 admin123 登录

# 4. 测试验证项
# 4a. 访问 /healthz 确认 boot_id 和 version 字段
#     浏览器直接访问 http://localhost:5003/healthz
#     期望: {"status":"ok","boot_id":"...","version":"1.12.0"}

# 4b. 进入"设置"→"自动化"→"一键更新"
#     - 切换更新方式为"Docker API"
#     - 确认看到部署信息警告（本地构建提示/Docker API 可用提示）
#     - 点击"立即更新"按钮
#     期望: 弹出"镜像名不在白名单内"错误（本地构建镜像无法自动更新，这是正确行为）

# 5. 测试完毕后清理
docker compose -f docker-compose.docker-api-test.yml down -v
docker rmi outlook-email-a2-test:latest  # 清理本地测试镜像
```

**注意**：本地构建镜像无法完成完整 pull→create→stop→start→rename 流程，因为远程 registry 没有 `outlook-email-a2-test` 镜像。白名单校验会正确拦截。

#### 方式二：Watchtower 模式测试（原有功能回归验证）

```bash
# 1. 启动 app + watchtower 双容器
docker compose -f docker-compose.hotupdate-test.yml up -d

# 2. 等待容器启动（约 20 秒）
docker ps --filter "name=outlook-hotupdate-test"

# 3. 浏览器访问 http://localhost:5002
# 使用密码 admin123 登录

# 4. 测试验证项
# 4a. 进入"设置"→"自动化"→"一键更新"
#     - 确认 Watchtower 配置显示
#     - 点击"测试连通性"
#     期望: 返回"连接成功"

# 4b. 点击"检查更新"（页面顶部或设置页）
#     期望: 显示当前版本和最新版本信息

# 4c. 点击"立即更新"
#     期望: 按钮变为"等待容器重启..."，前端轮询 /healthz
#     注意: 如果已是最新版本，Watchtower 不会触发容器重建

# 5. 测试完毕后清理
docker compose -f docker-compose.hotupdate-test.yml down -v
```

#### 方式三：远程镜像 + Docker API 端到端测试（最完整，需发布新版本）

```bash
# 1. 先提交 A2 代码到 dev 分支
# 2. 合并到 main 并发布新版本（如 v1.13.0）
# 3. 等待 Docker Hub 镜像发布
# 4. 使用远程镜像启动容器

docker run -d \
  --name oep-e2e-test \
  -p 5004:5000 \
  -e SECRET_KEY=test-secret-key \
  -e LOGIN_PASSWORD=admin123 \
  -e DOCKER_SELF_UPDATE_ALLOW=true \
  -e SCHEDULER_AUTOSTART=true \
  -v /var/run/docker.sock:/var/run/docker.sock \
  guangshanshui/outlook-email-plus:v1.13.0

# 5. 浏览器访问 http://localhost:5004，登录
# 6. 设置 → 自动化 → 一键更新 → 切换到 Docker API 模式
# 7. 点击"立即更新"
#    期望:
#    - 后端创建 updater 容器 (oep-updater-xxxxx)
#    - updater 容器 pull 最新镜像
#    - 如果有新版本：stop 旧容器 → create/start 新容器 → rename → cleanup
#    - 如果已是最新：返回"镜像已是最新，无需更新"
#    - 前端检测到 boot_id 变化 → 刷新页面

# 8. 清理
docker rm -f oep-e2e-test
```

#### 关键验证检查清单

- [ ] `GET /healthz` 返回 `boot_id` + `version`
- [ ] `GET /api/system/deployment-info` 返回正确的部署信息
- [ ] `docker_api_available` 在挂载 docker.sock 时为 true
- [ ] 更新方式切换 UI 正常（Watchtower ↔ Docker API）
- [ ] 部署警告根据更新方式动态变化（Watchtower 不可达时 info vs error）
- [ ] 触发更新时 CSRF 保护正常
- [ ] 白名单校验正确拦截非白名单镜像
- [ ] updater 容器创建成功并正确退出
- [ ] 前端 waitForRestart 轮询正常（boot_id 变化检测）
- [ ] 语言切换时部署警告重渲染

#### 13. 生成功能验证提示词（给其他 AI 审查用）

**操作内容**：
- 创建 `VERIFICATION_PROMPT.md`，包含 A2 方案的完整功能验证提示词
- 覆盖 5 大类验证点（后端 API / Docker 服务 / 前端 / 安全 / 边界条件），共 30+ 个具体检查项
- 附带改动文件清单和已知限制说明
- 用于交给其他 AI 审查代码变更的完整性和正确性

**已新增文件**：
- `VERIFICATION_PROMPT.md` — 功能验证提示词

---

### 待办：项目文件归类清理（暂缓，提交后执行）

> 以下为扫描项目结构后的清理建议，待 dev 分支提交后再执行。

#### 需删除的文件

| 文件 | 原因 |
|------|------|
| `fix_format.py` | 一次性格式修复脚本，已完成使命 |
| `NUL` | Windows 空文件，已在 .gitignore |
| `EhushaokangData-codeoutlookEmailserver.log` | 日志文件（文件名异常），已在 .gitignore |
| `-p/` 空目录 | 空目录，无内容 |
| `.ruff_cache/` | Linter 缓存目录 |

#### 需移动归类的文件

| 文件 | 目标位置 |
|------|---------|
| `注册与邮箱池接口文档.md` | → `docs/API/注册与邮箱池接口文档.md` |
| `registration-mail-pool-api.en.md` | → `docs/API/registration-mail-pool-api.en.md` |
| `VERIFICATION_PROMPT.md` | → `docs/DEV/VERIFICATION_PROMPT.md` |
| `docs/2026-04-05-设置页面重构-AI执行提示词.md` | → `docs/DEV/` 或删除 |

#### .gitignore 需补充

```
.ruff_cache/
-p/
```

**执行结果**（Commit: `04824bc`）：
- ✅ 删除 `fix_format.py`
- ✅ 移动 `注册与邮箱池接口文档.md` → `docs/API/`
- ✅ 移动 `registration-mail-pool-api.en.md` → `docs/API/`
- ✅ 移动 `VERIFICATION_PROMPT.md` → `docs/DEV/`
- ✅ 移动 `设置页面重构-AI执行提示词.md` → `docs/DEV/`
- ✅ `.gitignore` 补充 `.ruff_cache/` 和 `-p/`
- 注：`NUL`、`-p/`、`.ruff_cache/`、`Ehushaokang...server.log` 已在 .gitignore 中，物理文件已被清理

---

### 当前未提交修改（A2 方案 + 文档更新）

| 文件 | 修改类型 | 说明 |
|------|---------|------|
| `outlook_web/services/docker_update_helper.py` | **新增** | updater 容器入口模块 |
| `outlook_web/services/docker_update.py` | Modified | helper 容器创建、步骤顺序调整、失败回滚 |
| `outlook_web/controllers/system.py` | Modified | A2 触发逻辑、healthz 增强、部署信息增强 |
| `static/js/main.js` | Modified | boot_id 检测、部署警告渲染、超时优化 |
| `templates/index.html` | Modified | deploymentWarnings 容器 |
| `tests/test_error_and_trace.py` | Modified | 适配 healthz 新字段 |
| `tests/test_smoke_contract.py` | Modified | 适配 healthz 新字段 |
| `docker-compose.docker-api-test.yml` | **新增** | Docker API 测试 compose |
| `docker-compose.hotupdate-test.yml` | Modified | 新增 DOCKER_IMAGE 环境变量 |
| `docs/DEV/hot-update-ai-prompt.md` | Modified | 文档清理 + 补充 |
| `docs/DEV/hot-update-baseline.md` | Modified | 文档补充 |
| `WORKSPACE.md` | Modified | 工作区操作记录 |
| `VERIFICATION_PROMPT.md` | **新增** | 功能验证提示词（给其他 AI 审查用） |
