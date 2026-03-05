from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from flask import jsonify, request

from outlook_web.audit import log_audit
from outlook_web.repositories import settings as settings_repo
from outlook_web.security.auth import login_required
from outlook_web.security.crypto import decrypt_data, encrypt_data, hash_password, is_encrypted

# ==================== 设置 API ====================


@login_required
def api_get_settings() -> Any:
    """获取所有设置"""
    all_settings = settings_repo.get_all_settings()

    def mask_secret_value(value: str, head: int = 4, tail: int = 4) -> str:
        if not value:
            return ""
        safe_value = str(value)
        if len(safe_value) <= head + tail:
            return "*" * len(safe_value)
        return safe_value[:head] + ("*" * (len(safe_value) - head - tail)) + safe_value[-tail:]

    # 仅返回前端需要的设置项（避免把敏感字段/内部状态直接返回）
    safe_settings = {
        "refresh_interval_days": all_settings.get("refresh_interval_days", "30"),
        "refresh_delay_seconds": all_settings.get("refresh_delay_seconds", "5"),
        "refresh_cron": all_settings.get("refresh_cron", "0 2 * * *"),
        "use_cron_schedule": all_settings.get("use_cron_schedule", "false"),
        "enable_scheduled_refresh": all_settings.get("enable_scheduled_refresh", "true"),
        # 轮询配置
        "enable_auto_polling": all_settings.get("enable_auto_polling", "false") == "true",
        "polling_interval": int(all_settings.get("polling_interval", "10")),
        "polling_count": int(all_settings.get("polling_count", "5")),
    }

    # 敏感字段：不返回明文/哈希，仅提供"是否已设置/脱敏展示"
    login_password_value = all_settings.get("login_password") or ""
    gptmail_api_key_value = all_settings.get("gptmail_api_key") or ""
    safe_settings["login_password_set"] = bool(login_password_value)
    safe_settings["gptmail_api_key_set"] = bool(gptmail_api_key_value)
    safe_settings["gptmail_api_key_masked"] = mask_secret_value(gptmail_api_key_value) if gptmail_api_key_value else ""

    # Telegram 推送配置
    tg_bot_token_raw = all_settings.get("telegram_bot_token", "")
    if tg_bot_token_raw and is_encrypted(tg_bot_token_raw):
        try:
            plain_token = decrypt_data(tg_bot_token_raw)
            safe_settings["telegram_bot_token"] = "****" + plain_token[-4:] if len(plain_token) > 4 else "****"
        except Exception:
            safe_settings["telegram_bot_token"] = "****"
    else:
        safe_settings["telegram_bot_token"] = ""
    safe_settings["telegram_chat_id"] = all_settings.get("telegram_chat_id", "")
    safe_settings["telegram_poll_interval"] = int(all_settings.get("telegram_poll_interval", "600") or "600")

    response = {"success": True, "settings": safe_settings}
    # 同时在顶层暴露 telegram 字段（兼容前端直接访问）
    response["telegram_bot_token"] = safe_settings.get("telegram_bot_token", "")
    response["telegram_chat_id"] = safe_settings.get("telegram_chat_id", "")
    response["telegram_poll_interval"] = safe_settings.get("telegram_poll_interval", 600)

    return jsonify(response)


@login_required
def api_update_settings() -> Any:
    """更新设置"""
    # 延迟导入避免循环依赖
    from flask import current_app

    from outlook_web.services import graph as graph_service
    from outlook_web.services import scheduler as scheduler_service

    data = request.json
    updated = []
    errors = []
    scheduler_reload_needed = False

    # 更新登录密码
    if "login_password" in data:
        new_password = data["login_password"].strip()
        if new_password:
            if len(new_password) < 8:
                errors.append("密码长度至少为 8 位")
            else:
                # 哈希新密码
                hashed_password = hash_password(new_password)
                if settings_repo.set_setting("login_password", hashed_password):
                    updated.append("登录密码")
                else:
                    errors.append("更新登录密码失败")

    # 更新 GPTMail API Key
    if "gptmail_api_key" in data:
        new_api_key = data["gptmail_api_key"].strip()
        if new_api_key:
            if settings_repo.set_setting("gptmail_api_key", new_api_key):
                updated.append("GPTMail API Key")
            else:
                errors.append("更新 GPTMail API Key 失败")

    # 更新刷新周期
    if "refresh_interval_days" in data:
        try:
            days = int(data["refresh_interval_days"])
            if days < 1 or days > 90:
                errors.append("刷新周期必须在 1-90 天之间")
            elif settings_repo.set_setting("refresh_interval_days", str(days)):
                updated.append("刷新周期")
            else:
                errors.append("更新刷新周期失败")
        except ValueError:
            errors.append("刷新周期必须是数字")

    # 更新刷新间隔
    if "refresh_delay_seconds" in data:
        try:
            seconds = int(data["refresh_delay_seconds"])
            if seconds < 0 or seconds > 60:
                errors.append("刷新间隔必须在 0-60 秒之间")
            elif settings_repo.set_setting("refresh_delay_seconds", str(seconds)):
                updated.append("刷新间隔")
            else:
                errors.append("更新刷新间隔失败")
        except ValueError:
            errors.append("刷新间隔必须是数字")

    # 更新 Cron 表达式
    if "refresh_cron" in data:
        cron_expr = data["refresh_cron"].strip()
        if cron_expr:
            try:
                from croniter import croniter

                croniter(cron_expr, datetime.now())
                if settings_repo.set_setting("refresh_cron", cron_expr):
                    updated.append("Cron 表达式")
                    scheduler_reload_needed = True
                else:
                    errors.append("更新 Cron 表达式失败")
            except ImportError:
                errors.append("croniter 库未安装")
            except Exception as e:
                errors.append(f"Cron 表达式无效: {str(e)}")

    # 更新刷新策略
    if "use_cron_schedule" in data:
        use_cron = str(data["use_cron_schedule"]).lower()
        if use_cron in ("true", "false"):
            if settings_repo.set_setting("use_cron_schedule", use_cron):
                updated.append("刷新策略")
                scheduler_reload_needed = True
            else:
                errors.append("更新刷新策略失败")
        else:
            errors.append("刷新策略必须是 true 或 false")

    # 更新定时刷新开关
    if "enable_scheduled_refresh" in data:
        enable = str(data["enable_scheduled_refresh"]).lower()
        if enable in ("true", "false"):
            if settings_repo.set_setting("enable_scheduled_refresh", enable):
                updated.append("定时刷新开关")
                scheduler_reload_needed = True
            else:
                errors.append("更新定时刷新开关失败")
        else:
            errors.append("定时刷新开关必须是 true 或 false")

    # 更新轮询配置
    if "enable_auto_polling" in data:
        enable_polling = str(data["enable_auto_polling"]).lower()
        if enable_polling in ("true", "false"):
            if settings_repo.set_setting("enable_auto_polling", enable_polling):
                updated.append("自动轮询开关")
            else:
                errors.append("更新自动轮询开关失败")
        else:
            errors.append("自动轮询开关必须是 true 或 false")

    if "polling_interval" in data:
        try:
            interval = int(data["polling_interval"])
            if interval < 5 or interval > 300:
                errors.append("轮询间隔必须在 5-300 秒之间")
            elif settings_repo.set_setting("polling_interval", str(interval)):
                updated.append("轮询间隔")
            else:
                errors.append("更新轮询间隔失败")
        except ValueError:
            errors.append("轮询间隔必须是数字")

    if "polling_count" in data:
        try:
            count = int(data["polling_count"])
            if count < 0 or count > 100:
                errors.append("轮询次数必须在 0-100 次之间（0 表示持续轮询）")
            elif settings_repo.set_setting("polling_count", str(count)):
                updated.append("轮询次数")
            else:
                errors.append("更新轮询次数失败")
        except ValueError:
            errors.append("轮询次数必须是数字")

    # Telegram 推送配置
    if "telegram_poll_interval" in data:
        try:
            tg_interval = int(data["telegram_poll_interval"])
            if tg_interval < 10 or tg_interval > 86400:
                errors.append("Telegram 轮询间隔必须在 10-86400 秒之间")
            elif settings_repo.set_setting("telegram_poll_interval", str(tg_interval)):
                updated.append("Telegram 轮询间隔")
                scheduler_reload_needed = True
            else:
                errors.append("更新 Telegram 轮询间隔失败")
        except (ValueError, TypeError):
            errors.append("Telegram 轮询间隔必须是数字")

    if "telegram_bot_token" in data:
        tg_token = str(data["telegram_bot_token"]).strip()
        if tg_token and not tg_token.startswith("****"):
            encrypted_token = encrypt_data(tg_token)
            if settings_repo.set_setting("telegram_bot_token", encrypted_token):
                updated.append("Telegram Bot Token")
            else:
                errors.append("更新 Telegram Bot Token 失败")
        elif not tg_token:
            if settings_repo.set_setting("telegram_bot_token", ""):
                updated.append("Telegram Bot Token（已清空）")
        else:
            # 脱敏占位符（****xxx），跳过不覆盖
            updated.append("Telegram Bot Token（未变更）")

    if "telegram_chat_id" in data:
        tg_chat_id = str(data["telegram_chat_id"]).strip()
        if settings_repo.set_setting("telegram_chat_id", tg_chat_id):
            updated.append("Telegram Chat ID")
        else:
            errors.append("更新 Telegram Chat ID 失败")

    if errors:
        return jsonify({"success": False, "error": "；".join(errors)})

    if updated:
        scheduler_reloaded = None
        if scheduler_reload_needed:
            try:
                scheduler = scheduler_service.get_scheduler_instance()
                if scheduler:
                    scheduler_service.configure_scheduler_jobs(scheduler, current_app, graph_service.test_refresh_token)
                    scheduler_reloaded = True
                else:
                    scheduler_reloaded = False
            except Exception:
                scheduler_reloaded = False

        try:
            details = json.dumps(
                {
                    "updated": updated,
                    "scheduler_reload_needed": scheduler_reload_needed,
                    "scheduler_reloaded": scheduler_reloaded,
                },
                ensure_ascii=False,
            )
        except Exception:
            details = f"updated={','.join(updated)}"
        log_audit("update", "settings", None, details)
        return jsonify(
            {
                "success": True,
                "message": f'已更新：{", ".join(updated)}',
                "scheduler_reloaded": scheduler_reloaded,
            }
        )
    else:
        return jsonify({"success": False, "error": "没有需要更新的设置"})


@login_required
def api_validate_cron() -> Any:
    """验证 Cron 表达式"""
    try:
        from croniter import croniter
    except ImportError:
        return jsonify(
            {
                "success": False,
                "error": "croniter 库未安装，请运行: pip install croniter",
            }
        )

    data = request.json
    cron_expr = data.get("cron_expression", "").strip()

    if not cron_expr:
        return jsonify({"success": False, "error": "Cron 表达式不能为空"})

    try:
        base_time = datetime.now()
        cron = croniter(cron_expr, base_time)

        next_run = cron.get_next(datetime)

        future_runs = []
        temp_cron = croniter(cron_expr, base_time)
        for _ in range(5):
            future_runs.append(temp_cron.get_next(datetime).isoformat())

        return jsonify(
            {
                "success": True,
                "valid": True,
                "next_run": next_run.isoformat(),
                "future_runs": future_runs,
            }
        )
    except Exception as e:
        return jsonify({"success": False, "valid": False, "error": f"Cron 表达式无效: {str(e)}"})


@login_required
def api_test_telegram() -> Any:
    """发送 Telegram 测试消息，验证 bot_token + chat_id 配置是否正确"""
    from outlook_web.services.telegram_push import _send_telegram_message

    bot_token_raw = settings_repo.get_setting("telegram_bot_token", "")
    chat_id = settings_repo.get_setting("telegram_chat_id", "")

    if not bot_token_raw or not chat_id:
        return jsonify({"success": False, "error": "请先配置 Telegram Bot Token 和 Chat ID"})

    bot_token = decrypt_data(bot_token_raw) if is_encrypted(bot_token_raw) else bot_token_raw

    ok = _send_telegram_message(bot_token, chat_id, "✅ Outlook Email Plus 测试消息：配置正确！")
    if ok:
        log_audit("telegram_test", "settings", None, "测试消息发送成功")
        return jsonify({"success": True, "message": "测试消息已发送，请检查 Telegram"})
    else:
        return jsonify({"success": False, "error": "发送失败，请检查 Bot Token 和 Chat ID 是否正确"})
