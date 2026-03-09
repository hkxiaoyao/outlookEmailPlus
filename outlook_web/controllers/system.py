from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from flask import jsonify

from outlook_web import config
from outlook_web.db import (
    DB_SCHEMA_LAST_UPGRADE_ERROR_KEY,
    DB_SCHEMA_LAST_UPGRADE_TRACE_ID_KEY,
    DB_SCHEMA_VERSION,
    DB_SCHEMA_VERSION_KEY,
    create_sqlite_connection,
)
from outlook_web.repositories import settings as settings_repo
from outlook_web.security.auth import login_required

# 常量
REFRESH_LOCK_NAME = "token_refresh"


def utcnow() -> datetime:
    """返回 naive UTC 时间（等价于旧的 datetime.utcnow()）"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ==================== 系统 API ====================


def healthz() -> Any:
    """基础健康检查（用于容器/反代探活）"""
    return jsonify({"status": "ok"}), 200


@login_required
def api_system_health() -> Any:
    """管理员健康检查：可服务/可刷新状态概览"""
    conn = create_sqlite_connection()
    try:
        # DB 可用性
        db_ok = True
        try:
            conn.execute("SELECT 1").fetchone()
        except Exception:
            db_ok = False

        # Scheduler 心跳
        heartbeat_row = conn.execute("""
            SELECT updated_at
            FROM settings
            WHERE key = 'scheduler_heartbeat'
        """).fetchone()

        heartbeat_age_seconds = None
        if heartbeat_row and heartbeat_row["updated_at"]:
            try:
                hb_time = datetime.fromisoformat(heartbeat_row["updated_at"])
                heartbeat_age_seconds = int((utcnow() - hb_time).total_seconds())
            except Exception:
                heartbeat_age_seconds = None

        scheduler_enabled = settings_repo.get_setting("enable_scheduled_refresh", "true").lower() == "true"
        scheduler_autostart = config.get_scheduler_autostart_default()
        scheduler_healthy = (heartbeat_age_seconds is not None) and (heartbeat_age_seconds <= 120)

        # 刷新锁/运行中
        lock_row = conn.execute(
            """
            SELECT owner_id, expires_at
            FROM distributed_locks
            WHERE name = ?
        """,
            (REFRESH_LOCK_NAME,),
        ).fetchone()
        locked = bool(lock_row and lock_row["expires_at"] and lock_row["expires_at"] > time.time())

        running_run = conn.execute("""
            SELECT id, trigger_source, started_at, trace_id
            FROM refresh_runs
            WHERE status = 'running'
            ORDER BY started_at DESC
            LIMIT 1
        """).fetchone()

        return jsonify(
            {
                "success": True,
                "health": {
                    "service": "ok",
                    "database": "ok" if db_ok else "error",
                    "scheduler": {
                        "enabled": scheduler_enabled,
                        "autostart": scheduler_autostart,
                        "heartbeat_age_seconds": heartbeat_age_seconds,
                        "healthy": scheduler_healthy if scheduler_enabled else True,
                    },
                    "refresh": {
                        "locked": locked,
                        "running": dict(running_run) if running_run else None,
                    },
                    "server_time_utc": utcnow().isoformat() + "Z",
                },
            }
        )
    finally:
        conn.close()


@login_required
def api_system_diagnostics() -> Any:
    """管理员诊断信息：关键状态一致性/过期清理可见性"""
    conn = create_sqlite_connection()
    try:
        now_ts = time.time()

        export_tokens_count = conn.execute(
            """
            SELECT COUNT(*) as c
            FROM export_verify_tokens
            WHERE expires_at > ?
        """,
            (now_ts,),
        ).fetchone()["c"]

        locked_ip_count = conn.execute(
            """
            SELECT COUNT(*) as c
            FROM login_attempts
            WHERE locked_until_at IS NOT NULL AND locked_until_at > ?
        """,
            (now_ts,),
        ).fetchone()["c"]

        running_runs = conn.execute("""
            SELECT id, trigger_source, started_at, trace_id
            FROM refresh_runs
            WHERE status = 'running'
            ORDER BY started_at DESC
            LIMIT 5
        """).fetchall()

        last_runs = conn.execute("""
            SELECT id, trigger_source, status, started_at, finished_at, total, success_count, failed_count, trace_id
            FROM refresh_runs
            ORDER BY started_at DESC
            LIMIT 10
        """).fetchall()

        locks = conn.execute("""
            SELECT name, owner_id, acquired_at, expires_at
            FROM distributed_locks
            ORDER BY name ASC
        """).fetchall()

        # 数据库升级状态（可验证）
        schema_version_row = conn.execute(
            "SELECT value, updated_at FROM settings WHERE key = ?", (DB_SCHEMA_VERSION_KEY,)
        ).fetchone()
        schema_version = int(schema_version_row["value"]) if schema_version_row else 0

        last_migration = None
        try:
            mig = conn.execute("""
                SELECT id, from_version, to_version, status, started_at, finished_at, error, trace_id
                FROM schema_migrations
                ORDER BY started_at DESC
                LIMIT 1
            """).fetchone()
            last_migration = dict(mig) if mig else None
        except Exception:
            last_migration = None

        return jsonify(
            {
                "success": True,
                "diagnostics": {
                    "export_verify_tokens_active": export_tokens_count,
                    "login_locked_ip_count": locked_ip_count,
                    "running_runs": [dict(r) for r in running_runs],
                    "last_runs": [dict(r) for r in last_runs],
                    "locks": [dict(r) for r in locks],
                    "schema": {
                        "version": schema_version,
                        "target_version": DB_SCHEMA_VERSION,
                        "up_to_date": schema_version >= DB_SCHEMA_VERSION,
                        "last_migration": last_migration,
                    },
                },
            }
        )
    finally:
        conn.close()


@login_required
def api_system_upgrade_status() -> Any:
    """数据库升级状态（用于验收"升级过程可验证/失败可定位"）"""
    from outlook_web import config as app_config

    conn = create_sqlite_connection()
    try:
        row = conn.execute("SELECT value, updated_at FROM settings WHERE key = ?", (DB_SCHEMA_VERSION_KEY,)).fetchone()
        schema_version = int(row["value"]) if row and row["value"] is not None else 0

        last_trace_row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (DB_SCHEMA_LAST_UPGRADE_TRACE_ID_KEY,)
        ).fetchone()
        last_error_row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (DB_SCHEMA_LAST_UPGRADE_ERROR_KEY,)
        ).fetchone()

        last_migration = None
        try:
            mig = conn.execute("""
                SELECT id, from_version, to_version, status, started_at, finished_at, error, trace_id
                FROM schema_migrations
                ORDER BY started_at DESC
                LIMIT 1
            """).fetchone()
            last_migration = dict(mig) if mig else None
        except Exception:
            last_migration = None

        database_path = app_config.get_database_path()
        backup_hint = {
            "database_path": database_path,
            "linux_example": f'cp "{database_path}" "{database_path}.backup"',
            "windows_example": f'copy "{database_path}" "{database_path}.backup"',
        }

        return jsonify(
            {
                "success": True,
                "upgrade": {
                    "schema_version": schema_version,
                    "target_version": DB_SCHEMA_VERSION,
                    "up_to_date": schema_version >= DB_SCHEMA_VERSION,
                    "last_upgrade_trace_id": (last_trace_row["value"] if last_trace_row else ""),
                    "last_upgrade_error": (last_error_row["value"] if last_error_row else ""),
                    "last_migration": last_migration,
                    "backup_hint": backup_hint,
                },
            }
        )
    finally:
        conn.close()
