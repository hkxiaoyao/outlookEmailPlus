from __future__ import annotations

from typing import Optional

_APP_INSTANCE = None


def create_app(*, autostart_scheduler: Optional[bool] = None):
    """
    应用工厂（迁移期实现）：
    - 统一装配入口，便于测试与后续 Blueprint/分层拆分
    - 控制 import-time 副作用：初始化/调度器启动放到 create_app 中受控执行
    - routes 采用 Blueprint 模块化注册（URL 不变）
    """
    global _APP_INSTANCE

    if _APP_INSTANCE is None:
        from pathlib import Path

        from flask import Flask
        from werkzeug.exceptions import HTTPException
        from werkzeug.middleware.proxy_fix import ProxyFix

        from outlook_web import config
        from outlook_web.db import init_db, register_db
        from outlook_web.middleware import (
            attach_trace_id_and_normalize_errors,
            ensure_trace_id,
            handle_exception,
            handle_http_exception,
        )
        from outlook_web.routes import (
            accounts,
            audit,
            emails,
            external_pool,
            groups,
            oauth,
            pages,
            scheduler,
            settings,
            system,
            tags,
            temp_emails,
        )
        from outlook_web.security.csrf import init_csrf

        # 初始化（DB/目录等）
        repo_root = Path(__file__).resolve().parents[1]
        templates_dir = repo_root / "templates"
        static_dir = repo_root / "static"

        # 确保目录存在
        templates_dir.mkdir(parents=True, exist_ok=True)
        static_dir.mkdir(parents=True, exist_ok=True)

        # 确保数据目录存在
        data_dir = config.get_database_path()
        if data_dir:
            import os

            os.makedirs(
                os.path.dirname(data_dir) if os.path.dirname(data_dir) else ".",
                exist_ok=True,
            )

        # 初始化数据库
        init_db()

        app = Flask(
            __name__,
            template_folder=str(templates_dir),
            static_folder=str(static_dir),
            static_url_path="/static",
        )

        # 注入版本号到所有模板（用于 UI 显示）
        from outlook_web import __version__ as APP_VERSION

        @app.context_processor
        def inject_app_version():
            return {"APP_VERSION": APP_VERSION}

        app.secret_key = config.require_secret_key()
        app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 7  # 7 天
        app.config["SESSION_COOKIE_HTTPONLY"] = True
        app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

        # ProxyFix 中间件（仅在配置启用时应用）
        # 注意：启用前必须配置 TRUSTED_PROXIES 环境变量
        if config.get_proxy_fix_enabled():
            app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

        # DB teardown（请求结束释放连接）
        register_db(app)

        # 配置日志（确保 outlook_web 命名空间的日志输出到 stderr）
        import logging
        import sys

        _log_handler = logging.StreamHandler(sys.stderr)
        _log_handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))
        _ow_logger = logging.getLogger("outlook_web")
        if not _ow_logger.handlers:
            _ow_logger.addHandler(_log_handler)
            _ow_logger.setLevel(logging.INFO)

        # CSRF（可选）
        _csrf, csrf_exempt, _generate_csrf = init_csrf(app)

        # trace_id + error 结构标准化（迁移到 middleware 模块）
        app.before_request(ensure_trace_id)
        app.after_request(attach_trace_id_and_normalize_errors)
        app.register_error_handler(HTTPException, handle_http_exception)
        app.register_error_handler(Exception, handle_exception)

        # Blueprint 路由注册（URL 不变）
        app.register_blueprint(pages.create_blueprint(csrf_exempt=csrf_exempt))
        app.register_blueprint(groups.create_blueprint())
        app.register_blueprint(tags.create_blueprint())
        app.register_blueprint(accounts.create_blueprint())
        app.register_blueprint(emails.create_blueprint())
        app.register_blueprint(temp_emails.create_blueprint(csrf_exempt=csrf_exempt))
        app.register_blueprint(oauth.create_blueprint())
        app.register_blueprint(settings.create_blueprint())
        app.register_blueprint(scheduler.create_blueprint())
        app.register_blueprint(system.create_blueprint())
        app.register_blueprint(audit.create_blueprint())
        app.register_blueprint(external_pool.create_blueprint(csrf_exempt=csrf_exempt))

        # 打印初始化信息
        print("=" * 60)
        print("Outlook 邮件 Web 应用已初始化")
        print(f"数据库文件: {config.get_database_path()}")
        print(f"GPTMail API: {config.get_gptmail_base_url()}")
        print("=" * 60)

        _APP_INSTANCE = app

    # 调度器启动控制
    if autostart_scheduler is None:
        from outlook_web.services import graph as graph_service
        from outlook_web.services import scheduler as scheduler_service

        if scheduler_service.should_autostart_scheduler():
            scheduler_service.init_scheduler(_APP_INSTANCE, graph_service.test_refresh_token_with_rotation)
    elif autostart_scheduler:
        from outlook_web.services import graph as graph_service
        from outlook_web.services import scheduler as scheduler_service

        scheduler_service.init_scheduler(_APP_INSTANCE, graph_service.test_refresh_token_with_rotation)

    return _APP_INSTANCE
