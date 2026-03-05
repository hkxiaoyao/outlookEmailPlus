# BUG-00008 Telegram 推送三项缺陷深度分析

> 关联功能: TODO-00007 / FD-00007  
> 参考实现: `exeample/MailAggregator_Pro/app/services/telegram.py` + `fetcher.py`  
> 分析时间: 2026-03-05

---

## 一、问题概述

在 Telegram 推送功能实测过程中，发现以下三个紧密关联的缺陷：

| 编号 | 标题 | 严重程度 | 状态 |
|------|------|---------|------|
| **D-1** | 禁用后重新启用推送，历史邮件被全量推送 | 🔴 高 | ✅ 已修复 |
| **D-2** | 推送账号缺乏明显视觉标识 | 🟡 中 | ✅ 已修复 |
| **D-3** | 轮询间隔 min/max 限制过严 | 🟢 低 | 🔧 待修复 |

三者本质上是**同一个功能的不同维度**：D-1 影响数据正确性，D-2 影响用户感知，D-3 影响灵活性。

---

## 二、D-1：禁用后重新启用推送，历史邮件被全量推送

### 2.1 现象复现

1. 用户为账号 A 启用推送 → 游标设为 `T₀`
2. 推送正常工作数天，游标推进到 `T₅`
3. 用户关闭推送
4. 数天后重新启用 → **游标仍为 `T₅`**
5. 下次 Job 运行 → 拉取 `T₅` 至今的**所有邮件** → 一次性全部推送 → **信息轰炸**

### 2.2 根因分析

**原代码** (`outlook_web/repositories/accounts.py:toggle_telegram_push`):
```python
if enabled:
    if row["telegram_last_checked_at"] is None:  # 仅首次
        db.execute("UPDATE ... SET telegram_last_checked_at = ?", (now,))
    else:
        # 重新启用时保留旧游标 ← BUG
        db.execute("UPDATE ... SET telegram_push_enabled = 1")
```

逻辑缺陷：只在 `telegram_last_checked_at IS NULL` 时（即首次启用）重置游标。再次启用时走 `else` 分支，旧游标原封不动。

### 2.3 与 MailAggregator_Pro 对比

| 维度 | 我们的实现 | MailAggregator_Pro |
|------|-----------|-------------------|
| 防重机制 | **时间游标** (`telegram_last_checked_at`) | **Message-ID 去重** + 数据库记录 |
| 首次同步保护 | 游标为 NULL 时设当前时间，跳过推送 | `is_initial_sync = (existing_total == 0)` → 跳过整个推送块 |
| 重新启用行为 | ❌ 保留旧游标 → 历史轰炸 | ✅ 基于 Message-ID，已推送的不会重复 |
| 时效过滤 | ❌ 无 | ✅ `PUSH_RECENCY_HOURS = 12`：超过 12 小时的邮件不推送 |
| 消息间延迟 | ❌ 无 | ✅ `TELEGRAM_PUSH_DELAY_SEC = 1.5` 秒 |

**核心差异**：MailAggregator_Pro 用 **Message-ID 去重 + 时效阈值** 双保险。即便逻辑有误，超过 12 小时的旧邮件也不会被推送。而我们仅依赖时间游标，一旦游标不正确就完全失控。

### 2.4 修复方案（已实施）

```python
def toggle_telegram_push(account_id, enabled):
    row = db.execute("SELECT id, telegram_push_enabled, telegram_last_checked_at ...").fetchone()
    if enabled:
        if row["telegram_push_enabled"]:
            return True  # 幂等：已启用 → 不变
        # 从禁用 → 启用：总是重置游标到当前时间
        now_utc = datetime.now(timezone.utc).strftime(...)
        db.execute("UPDATE ... SET telegram_push_enabled=1, telegram_last_checked_at=?", (now_utc,))
```

**关键改进**：
- 检测 `telegram_push_enabled` 当前值（0 还是 1）
- 从 0→1 转换：**重置游标到 NOW**
- 已经 1→1：**幂等不变**（保护正在运行的 Job）

**后续可选增强**（参考 MailAggregator_Pro）：
- 增加 `PUSH_RECENCY_HOURS` 时效阈值（12 小时），作为兜底防线
- 增加消息间 1~2 秒延迟（`TELEGRAM_PUSH_DELAY_SEC`），防止 Telegram API 限流

---

## 三、D-2：推送账号缺乏明显视觉标识

### 3.1 现象

账号卡片上的 🔔 按钮通过 `.tg-push-active` 类添加蓝色高亮和发光：
```css
.btn-icon.tg-push-active { color: #0088cc; filter: drop-shadow(0 0 2px #0088cc66); }
```
但 🔔 图标在操作按钮区域（底部），尺寸小、位置不显眼，用户难以一眼看出哪些账号已开启推送。

### 3.2 与 MailAggregator_Pro 对比

MailAggregator_Pro 无前端实现（仅后端 + CLI），不适用直接对比。但其数据模型更清晰：
- `EmailAccount.telegram_push_enabled` 字段直接影响 `should_push_telegram()` 判断
- 配合 `TelegramFilterRule` 表支持 per-account 的 allow/deny 规则

### 3.3 用户期望

用户明确提出：**用标签 (tag) 代替按钮**。
- 现有 tag 系统：账号卡片已有 `<span class="tag" style="background-color:${color}">${name}</span>`
- 标签位于邮箱地址下方的标签区域，非常显眼
- 点击标签即可操作（删除标签 = 关闭推送）

### 3.4 修复方案（已实施）

**前端** (`static/js/features/groups.js`)：
在标签区域追加 Telegram 推送标签：
```javascript
${acc.telegram_push_enabled 
  ? `<span class="tag tg-push-tag" 
          onclick="event.stopPropagation(); toggleTelegramPush(${acc.id}, false)" 
          title="点击关闭推送">🔔 推送</span>` 
  : ''}
```

**CSS** (`static/css/main.css`)：
```css
.tg-push-tag {
    background-color: #0088cc !important;  /* Telegram 品牌蓝 */
    color: white !important;
    cursor: pointer;
    font-size: 0.7rem;
    padding: 1px 6px;
    border-radius: 3px;
    transition: opacity 0.2s;
}
.tg-push-tag:hover { opacity: 0.8; }
```

**交互逻辑**：
- 🔔 按钮（底部操作区）→ 开启推送 → 标签出现
- `🔔 推送` 标签（标签区域）→ 点击关闭推送 → 标签消失
- 标签与现有 tag 系统视觉一致，用户零学习成本

---

## 四、D-3：轮询间隔 min/max 限制过严

### 4.1 现象

**后端验证** (`outlook_web/controllers/settings.py:216`):
```python
tg_interval = int(data["telegram_poll_interval"])
if tg_interval < 60 or tg_interval > 3600:
    # → 400 错误
```

**前端限制** (`templates/index.html`):
```html
<input type="number" id="telegramPollInterval" min="60" max="3600" value="600">
```

**调度器下限** (`outlook_web/services/scheduler.py:_get_telegram_interval`):
```python
return max(60, interval)  # 硬下限 60 秒
```

用户无法设置：
- **< 60 秒**：开发调试时需要快速验证
- **> 3600 秒**：低频推送场景（如每小时一次 = 3600 秒 OK，但每 2 小时 = 7200 秒 → 被拒绝）

### 4.2 与 MailAggregator_Pro 对比

| 维度 | 我们的实现 | MailAggregator_Pro |
|------|-----------|-------------------|
| 最小间隔 | 60 秒 | **5 秒** |
| 最大间隔 | 3600 秒 | **无上限** |
| 默认值 | 600 秒 | 300 秒 |
| 粒度 | 全局统一 | **per-account 可覆盖** (`account.poll_interval_seconds`) |

MailAggregator_Pro 的策略更灵活：全局默认 300 秒，每个账号可独立覆盖，下限仅 5 秒。

### 4.3 修复方案

**推荐范围**: `10 ≤ interval ≤ 86400`（10 秒 ~ 24 小时）

需要修改三个位置：

1. **后端** (`outlook_web/controllers/settings.py:216`):
```python
# 修改前
if tg_interval < 60 or tg_interval > 3600:
# 修改后
if tg_interval < 10 or tg_interval > 86400:
```

2. **前端** (`templates/index.html`):
```html
<!-- 修改前 -->
<input type="number" min="60" max="3600">
<!-- 修改后 -->
<input type="number" min="10" max="86400">
```

3. **调度器** (`outlook_web/services/scheduler.py:_get_telegram_interval`):
```python
# 修改前
return max(60, interval)
# 修改后
return max(10, interval)
```

4. **测试 T-29** (`tests/test_telegram_push.py`): 需同步调整边界值断言

---

## 五、综合对比与改进路线

### 5.1 与 MailAggregator_Pro 功能差距一览

| 功能 | 我们 | MailAggregator_Pro | 优先级 |
|------|-----|-------------------|--------|
| 时间游标防重 | ✅ | - (用 Message-ID) | - |
| Message-ID 去重 | ❌ | ✅ | P2 |
| 时效阈值 (RECENCY) | ❌ | ✅ 12 小时 | P1 |
| 消息间延迟 | ❌ | ✅ 1.5 秒 | P1 |
| per-account 过滤规则 | ❌ | ✅ allow/deny 规则表 | P3 |
| 可配置推送模板 | ❌ | ✅ full/short/title_only | P3 |
| 初始同步检测 | ✅ (NULL 游标) | ✅ (existing_total==0) | - |
| 代理支持 | ✅ | ❌ | - |
| 前端推送标签 | ✅ | ❌ | - |

### 5.2 下一步建议

**立即修复** (本轮)：
- [x] D-1: 重置游标 → 已修复
- [x] D-2: 标签 UI → 已修复
- [ ] D-3: 放宽间隔范围 → 待实施

**短期增强** (P1)：
- 增加 `PUSH_RECENCY_HOURS = 12` 时效阈值
- 增加 `TELEGRAM_PUSH_DELAY_SEC = 1.5` 消息间延迟
- 错误时不推进游标（仅成功拉取后推进）

**中期增强** (P2/P3)：
- Message-ID 去重（防止极端情况重复推送）
- per-account 推送过滤规则
- 可配置推送模板（详细/简短/仅标题）

---

## 六、修复验证

| 测试 | 结果 |
|------|------|
| T-12: 重复开启不重置游标 | ✅ 通过（幂等行为保留） |
| T-12b: 禁用后重新启用重置游标 | ✅ 通过（新增） |
| 全量测试 (203 项) | ✅ 全部通过 |
