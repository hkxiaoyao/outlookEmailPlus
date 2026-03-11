from __future__ import annotations

import logging
from typing import Any

from flask import jsonify, request

from outlook_web import config
from outlook_web.audit import log_audit
from outlook_web.db import get_db
from outlook_web.errors import build_error_payload
from outlook_web.repositories import accounts as accounts_repo
from outlook_web.repositories import groups as groups_repo
from outlook_web.security.auth import api_key_required, login_required
from outlook_web.services import email_delete as email_delete_service
from outlook_web.services import external_api as external_api_service
from outlook_web.services import graph as graph_service
from outlook_web.services import imap as imap_service
from outlook_web.services.imap_generic import get_email_detail_imap_generic, get_emails_imap_generic

_LOGGER = logging.getLogger("outlook_web.controllers.emails")

# IMAP 服务器配置
IMAP_SERVER_OLD = "outlook.office365.com"
IMAP_SERVER_NEW = "outlook.live.com"


# ==================== 邮件 API ====================


@login_required
def api_get_emails(email_addr: str) -> Any:
    """获取邮件列表（支持分页，不使用缓存）"""
    account = accounts_repo.get_account_by_email(email_addr)

    if not account:
        error_payload = build_error_payload(
            "ACCOUNT_NOT_FOUND",
            "账号不存在",
            "NotFoundError",
            404,
            f"email={email_addr}",
        )
        return jsonify({"success": False, "error": error_payload})

    folder = request.args.get("folder", "inbox")  # inbox, junkemail, deleteditems
    skip = int(request.args.get("skip", 0))
    top = int(request.args.get("top", 20))

    # PRD-00005 / FD-00005 / TDD-00005：按 account_type 路由分发（Outlook 链路保持原样，IMAP 走通用 IMAP 服务）
    account_type = (account.get("account_type") or "outlook").strip().lower()
    if account_type == "imap":
        result = get_emails_imap_generic(
            email_addr=email_addr,
            imap_password=account.get("imap_password", "") or "",
            imap_host=account.get("imap_host", "") or "",
            imap_port=account.get("imap_port", 993) or 993,
            folder=folder,
            provider=account.get("provider", "_default") or "_default",
            skip=skip,
            top=top,
        )
        return jsonify(result)

    # 获取分组代理设置
    proxy_url = ""
    if account.get("group_id"):
        group = groups_repo.get_group_by_id(account["group_id"])
        if group:
            proxy_url = group.get("proxy_url", "") or ""

    # 收集所有错误信息
    all_errors = {}

    # 1. 尝试 Graph API
    graph_result = graph_service.get_emails_graph(account["client_id"], account["refresh_token"], folder, skip, top, proxy_url)
    if graph_result.get("success"):
        emails = graph_result.get("emails", [])
        # 更新刷新时间
        db = get_db()
        db.execute(
            """
            UPDATE accounts
            SET last_refresh_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
            WHERE email = ?
        """,
            (email_addr,),
        )
        db.commit()

        # 格式化 Graph API 返回的数据
        formatted = []
        for e in emails:
            formatted.append(
                {
                    "id": e.get("id"),
                    "subject": e.get("subject", "无主题"),
                    "from": e.get("from", {}).get("emailAddress", {}).get("address", "未知"),
                    "date": e.get("receivedDateTime", ""),
                    "is_read": e.get("isRead", False),
                    "has_attachments": e.get("hasAttachments", False),
                    "body_preview": e.get("bodyPreview", ""),
                }
            )

        return jsonify(
            {
                "success": True,
                "emails": formatted,
                "method": "Graph API",
                "has_more": len(formatted) >= top,
            }
        )
    else:
        graph_error = graph_result.get("error")
        all_errors["graph"] = graph_error

        # 如果是代理错误，不再回退 IMAP
        if isinstance(graph_error, dict) and graph_error.get("type") in (
            "ProxyError",
            "ConnectionError",
        ):
            return jsonify(
                {
                    "success": False,
                    "error": "代理连接失败，请检查分组代理设置",
                    "details": all_errors,
                }
            )

    imap_new_result = imap_service.get_emails_imap_with_server(
        account["email"],
        account["client_id"],
        account["refresh_token"],
        folder,
        skip,
        top,
        IMAP_SERVER_NEW,
    )
    if imap_new_result.get("success"):
        return jsonify(
            {
                "success": True,
                "emails": imap_new_result.get("emails", []),
                "method": "IMAP (New)",
                "has_more": False,  # IMAP 分页暂未完全实现
            }
        )
    else:
        all_errors["imap_new"] = imap_new_result.get("error")

    # 3. 尝试旧版 IMAP (outlook.office365.com)
    imap_old_result = imap_service.get_emails_imap_with_server(
        account["email"],
        account["client_id"],
        account["refresh_token"],
        folder,
        skip,
        top,
        IMAP_SERVER_OLD,
    )
    if imap_old_result.get("success"):
        return jsonify(
            {
                "success": True,
                "emails": imap_old_result.get("emails", []),
                "method": "IMAP (Old)",
                "has_more": False,
            }
        )
    else:
        all_errors["imap_old"] = imap_old_result.get("error")

    return jsonify(
        {
            "success": False,
            "error": "无法获取邮件，所有方式均失败",
            "details": all_errors,
        }
    )


@login_required
def api_delete_emails() -> Any:
    """批量删除邮件（永久删除）"""
    data = request.json
    email_addr = data.get("email", "")
    message_ids = data.get("ids", [])

    if not email_addr or not message_ids:
        return jsonify({"success": False, "error": "参数不完整"})

    account = accounts_repo.get_account_by_email(email_addr)
    if not account:
        return jsonify({"success": False, "error": "账号不存在"})

    # PRD-00005：IMAP 账号不支持远程删除（避免误操作与跨厂商副作用）
    account_type = (account.get("account_type") or "outlook").strip().lower()
    if account_type == "imap":
        error_payload = build_error_payload(
            "IMAP_DELETE_NOT_SUPPORTED",
            "IMAP 邮箱不支持远程删除，请在邮箱客户端中操作",
            "NotSupportedError",
            400,
            f"email={email_addr}",
        )
        return jsonify({"success": False, "error": error_payload}), 400

    # 获取分组代理设置
    proxy_url = ""
    if account.get("group_id"):
        group = groups_repo.get_group_by_id(account["group_id"])
        if group:
            proxy_url = group.get("proxy_url", "") or ""

    response_data, method_used = email_delete_service.delete_emails_with_fallback(
        email_addr=email_addr,
        client_id=account["client_id"],
        refresh_token=account["refresh_token"],
        message_ids=message_ids,
        proxy_url=proxy_url,
        delete_emails_graph=graph_service.delete_emails_graph,
        delete_emails_imap=imap_service.delete_emails_imap,
        imap_server_new=IMAP_SERVER_NEW,
        imap_server_old=IMAP_SERVER_OLD,
    )

    if method_used == "graph":
        log_audit(
            "delete",
            "email",
            email_addr,
            f"删除邮件 {len(message_ids)} 封（Graph API）",
        )
    elif method_used == "imap_new":
        log_audit("delete", "email", email_addr, f"删除邮件 {len(message_ids)} 封（IMAP New）")
    elif method_used == "imap_old":
        log_audit("delete", "email", email_addr, f"删除邮件 {len(message_ids)} 封（IMAP Old）")

    return jsonify(response_data)


@login_required
def api_get_email_detail(email_addr: str, message_id: str) -> Any:
    """获取邮件详情"""
    _LOGGER.info("email_detail_request email=%s message_id=%s", email_addr, message_id)
    account = accounts_repo.get_account_by_email(email_addr)

    if not account:
        _LOGGER.warning("email_detail_account_not_found email=%s", email_addr)
        return jsonify({"success": False, "error": "账号不存在"})

    account_type = (account.get("account_type") or "outlook").strip().lower()
    folder = request.args.get("folder", "inbox")
    _LOGGER.info("email_detail_type=%s provider=%s folder=%s", account_type, account.get("provider", "N/A"), folder)

    if account_type == "imap":
        detail = get_email_detail_imap_generic(
            email_addr=email_addr,
            imap_password=account.get("imap_password", "") or "",
            imap_host=account.get("imap_host", "") or "",
            imap_port=account.get("imap_port", 993) or 993,
            message_id=message_id,
            folder=folder,
            provider=account.get("provider", "_default") or "_default",
        )
        if detail:
            _LOGGER.info("email_detail_imap_ok email=%s subject=%s", email_addr, detail.get("subject", "?")[:40])
            return jsonify({"success": True, "email": detail})
        _LOGGER.warning("email_detail_imap_failed email=%s message_id=%s", email_addr, message_id)
        return jsonify({"success": False, "error": "获取邮件详情失败"})

    method = request.args.get("method", "graph")

    if method == "graph":
        # 获取分组代理设置
        proxy_url = ""
        if account.get("group_id"):
            group = groups_repo.get_group_by_id(account["group_id"])
            if group:
                proxy_url = group.get("proxy_url", "") or ""

        detail = graph_service.get_email_detail_graph(account["client_id"], account["refresh_token"], message_id, proxy_url)
        if detail:
            return jsonify(
                {
                    "success": True,
                    "email": {
                        "id": detail.get("id"),
                        "subject": detail.get("subject", "无主题"),
                        "from": detail.get("from", {}).get("emailAddress", {}).get("address", "未知"),
                        "to": ", ".join(
                            [r.get("emailAddress", {}).get("address", "") for r in detail.get("toRecipients", [])]
                        ),
                        "cc": ", ".join(
                            [r.get("emailAddress", {}).get("address", "") for r in detail.get("ccRecipients", [])]
                        ),
                        "date": detail.get("receivedDateTime", ""),
                        "body": detail.get("body", {}).get("content", ""),
                        "body_type": detail.get("body", {}).get("contentType", "text"),
                    },
                }
            )

    # 如果 Graph API 失败，尝试 IMAP
    detail = imap_service.get_email_detail_imap(
        account["email"],
        account["client_id"],
        account["refresh_token"],
        message_id,
        folder,
    )
    if detail:
        return jsonify({"success": True, "email": detail})

    return jsonify({"success": False, "error": "获取邮件详情失败"})


@login_required
def api_extract_verification(email_addr: str) -> Any:
    """
    提取验证码和链接接口

    功能：从指定邮箱的最新邮件中提取验证码和链接

    实现策略（多 API 回退机制）：
    1. Graph API (inbox) - 优先从收件箱获取
    2. Graph API (junkemail) - 从垃圾邮件获取
    3. IMAP (新服务器) - Graph API 失败时回退
    4. IMAP (旧服务器) - 最后的回退方案
    """
    from outlook_web.services.verification_extractor import extract_verification_info

    # 获取账号信息
    account = accounts_repo.get_account_by_email(email_addr)

    if not account:
        error_payload = build_error_payload(
            "ACCOUNT_NOT_FOUND",
            "邮箱不存在",
            "NotFoundError",
            404,
            f"email={email_addr}",
        )
        return jsonify({"success": False, "error": error_payload}), 404

    # PRD-00005：IMAP 账号验证码提取走 IMAP（Generic）→ 详情 → extractor；Outlook 保持原 Graph→IMAP XOAUTH2 回退链
    account_type = (account.get("account_type") or "outlook").strip().lower()
    if account_type == "imap":
        emails_result = get_emails_imap_generic(
            email_addr=email_addr,
            imap_password=account.get("imap_password", "") or "",
            imap_host=account.get("imap_host", "") or "",
            imap_port=account.get("imap_port", 993) or 993,
            folder="inbox",
            provider=account.get("provider", "_default") or "_default",
            skip=0,
            top=1,
        )

        if not emails_result.get("success"):
            error_payload = build_error_payload(
                "EMAIL_FETCH_FAILED",
                "获取邮件失败",
                "IMAPError",
                500,
                emails_result,
            )
            return jsonify({"success": False, "error": error_payload}), 500

        emails = emails_result.get("emails") or []
        if not emails:
            error_payload = build_error_payload(
                "EMAIL_NOT_FOUND",
                "未找到邮件",
                "NotFoundError",
                404,
                f"email={email_addr}",
            )
            return jsonify({"success": False, "error": error_payload}), 404

        latest_email = emails[0]
        detail = get_email_detail_imap_generic(
            email_addr=email_addr,
            imap_password=account.get("imap_password", "") or "",
            imap_host=account.get("imap_host", "") or "",
            imap_port=account.get("imap_port", 993) or 993,
            message_id=latest_email.get("id") or "",
            folder="inbox",
            provider=account.get("provider", "_default") or "_default",
        )

        if not detail:
            error_payload = build_error_payload(
                "EMAIL_DETAIL_NOT_FOUND",
                "获取邮件详情失败",
                "NotFoundError",
                404,
                f"email={email_addr}",
            )
            return jsonify({"success": False, "error": error_payload}), 404

        # 构建邮件对象用于提取（避免把 HTML 放进 body 导致 extractor 不走 HTML->text）
        email_obj = {
            "subject": detail.get("subject", ""),
            "body": detail.get("body_text", ""),
            "body_html": detail.get("body_html", ""),
            "body_preview": latest_email.get("body_preview", ""),
        }

        try:
            result = extract_verification_info(email_obj)
            return jsonify({"success": True, "data": result, "message": "提取成功"})
        except ValueError as e:
            error_payload = build_error_payload(
                "VERIFICATION_NOT_FOUND",
                str(e),
                "NotFoundError",
                404,
                f"email={email_addr}",
            )
            return jsonify({"success": False, "error": error_payload}), 404
        except Exception as e:
            error_payload = build_error_payload("EXTRACT_ERROR", "提取失败", "ExtractError", 500, str(e))
            return jsonify({"success": False, "error": error_payload}), 500

    # 获取分组代理设置
    proxy_url = ""
    if account.get("group_id"):
        group = groups_repo.get_group_by_id(account["group_id"])
        if group:
            proxy_url = group.get("proxy_url", "") or ""

    # 收集邮件（同时从收件箱和垃圾邮件获取）
    emails = []
    graph_success = False

    # 1. 尝试 Graph API 从收件箱获取最新邮件
    try:
        inbox_result = graph_service.get_emails_graph(
            account["client_id"],
            account["refresh_token"],
            folder="inbox",
            skip=0,
            top=1,
            proxy_url=proxy_url,
        )
        if inbox_result.get("success"):
            emails.extend(inbox_result.get("emails", []))
            graph_success = True
    except Exception:
        pass

    # 2. 尝试 Graph API 从垃圾邮件获取最新邮件
    try:
        junk_result = graph_service.get_emails_graph(
            account["client_id"],
            account["refresh_token"],
            folder="junkemail",
            skip=0,
            top=1,
            proxy_url=proxy_url,
        )
        if junk_result.get("success"):
            emails.extend(junk_result.get("emails", []))
            graph_success = True
    except Exception:
        pass

    # 3. 如果 Graph API 失败，尝试 IMAP 回退
    if not graph_success or not emails:
        # 尝试新版 IMAP 服务器
        try:
            imap_new_result = imap_service.get_emails_imap_with_server(
                account["email"],
                account["client_id"],
                account["refresh_token"],
                folder="inbox",
                skip=0,
                top=1,
                server=IMAP_SERVER_NEW,
            )
            if imap_new_result.get("success"):
                emails.extend(imap_new_result.get("emails", []))
        except Exception:
            pass

        # 尝试旧版 IMAP 服务器
        try:
            imap_old_result = imap_service.get_emails_imap_with_server(
                account["email"],
                account["client_id"],
                account["refresh_token"],
                folder="inbox",
                skip=0,
                top=1,
                server=IMAP_SERVER_OLD,
            )
            if imap_old_result.get("success"):
                emails.extend(imap_old_result.get("emails", []))
        except Exception:
            pass

    if not emails:
        error_payload = build_error_payload("EMAIL_NOT_FOUND", "未找到邮件", "NotFoundError", 404, f"email={email_addr}")
        return jsonify({"success": False, "error": error_payload}), 404

    # 按时间排序，取最新的一封
    emails.sort(key=lambda x: x.get("receivedDateTime", "") or x.get("date", ""), reverse=True)
    latest_email = emails[0]

    # 获取邮件详情以获取完整内容
    email_detail = None

    # 尝试 Graph API 获取详情
    try:
        email_detail = graph_service.get_email_detail_graph(
            account["client_id"],
            account["refresh_token"],
            latest_email.get("id"),
            proxy_url,
        )
    except Exception:
        pass

    # 如果 Graph API 失败，尝试 IMAP 获取详情
    if not email_detail:
        try:
            email_detail = imap_service.get_email_detail_imap(
                account["email"],
                account["client_id"],
                account["refresh_token"],
                latest_email.get("id"),
                "inbox",
            )
        except Exception:
            pass

    # 构建邮件对象用于提取
    email_obj = {
        "subject": latest_email.get("subject", ""),
        "body_preview": latest_email.get("bodyPreview", "") or latest_email.get("body_preview", ""),
    }

    if email_detail:
        # Graph API 格式
        if "body" in email_detail:
            body_content = email_detail.get("body", {})
            email_obj["body"] = body_content.get("content", "") if body_content.get("contentType") == "text" else ""
            email_obj["body_html"] = body_content.get("content", "") if body_content.get("contentType") == "html" else ""
            email_obj["bodyContent"] = body_content.get("content", "")
            email_obj["bodyContentType"] = body_content.get("contentType", "text")
        # IMAP 格式
        elif "body" in email_detail or "body_html" in email_detail:
            email_obj["body"] = email_detail.get("body", "")
            email_obj["body_html"] = email_detail.get("body_html", "")

    try:
        # 尝试从邮件详情提取验证信息
        result = extract_verification_info(email_obj)

        return jsonify({"success": True, "data": result, "message": "提取成功"})

    except ValueError as e:
        # 未找到验证信息
        error_payload = build_error_payload(
            "VERIFICATION_NOT_FOUND",
            str(e),
            "NotFoundError",
            404,
            f"email={email_addr}",
        )
        return jsonify({"success": False, "error": error_payload}), 404

    except Exception as e:
        # 其他错误
        error_payload = build_error_payload("EXTRACT_ERROR", "提取失败", "ExtractError", 500, str(e))
        return jsonify({"success": False, "error": error_payload}), 500


# ==================== External Emails API ====================


def _parse_external_common_args(*, default_since_minutes: int | None = None) -> dict:
    """解析 external API 通用 query 参数（按 TDD-00008 做基础校验）。"""
    email_addr = (request.args.get("email") or "").strip()
    if not email_addr or "@" not in email_addr:
        raise external_api_service.InvalidParamError("email 参数无效")

    folder = (request.args.get("folder") or "inbox").strip().lower() or "inbox"
    if folder not in {"inbox", "junkemail", "deleteditems"}:
        raise external_api_service.InvalidParamError("folder 参数无效")

    def _int_arg(name: str, default: int) -> int:
        raw = request.args.get(name, None)
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except Exception as exc:
            raise external_api_service.InvalidParamError(f"{name} 参数无效") from exc

    skip = _int_arg("skip", 0)
    top = _int_arg("top", 20)
    if skip < 0:
        raise external_api_service.InvalidParamError("skip 参数无效")
    if top < 1 or top > 50:
        raise external_api_service.InvalidParamError("top 参数无效")

    since_minutes_raw = request.args.get("since_minutes", None)
    since_minutes = default_since_minutes
    if since_minutes_raw not in (None, ""):
        try:
            since_minutes = int(since_minutes_raw)
        except Exception as exc:
            raise external_api_service.InvalidParamError("since_minutes 参数无效") from exc
        if since_minutes < 1:
            raise external_api_service.InvalidParamError("since_minutes 参数无效")

    return {
        "email": email_addr,
        "folder": folder,
        "skip": skip,
        "top": top,
        "from_contains": (request.args.get("from_contains") or "").strip(),
        "subject_contains": (request.args.get("subject_contains") or "").strip(),
        "since_minutes": since_minutes,
    }


def _external_error_response(exc: external_api_service.ExternalApiError):
    return jsonify(external_api_service.fail(exc.code, exc.message, data=exc.data)), exc.status


@api_key_required
def api_external_get_messages() -> Any:
    try:
        args = _parse_external_common_args()
        emails, method = external_api_service.list_messages_for_external(
            email_addr=args["email"],
            folder=args["folder"],
            skip=args["skip"],
            top=args["top"],
        )
        filtered = external_api_service.filter_messages(
            emails,
            from_contains=args["from_contains"],
            subject_contains=args["subject_contains"],
            since_minutes=args["since_minutes"],
        )

        external_api_service.audit_external_api_access(
            action="external_api_access",
            email_addr=args["email"] or "",
            endpoint="/api/external/messages",
            status="ok",
            details={"method": method, "count": len(filtered)},
        )

        return jsonify(external_api_service.ok({"emails": filtered, "count": len(filtered), "has_more": False}))
    except external_api_service.ExternalApiError as exc:
        external_api_service.audit_external_api_access(
            action="external_api_access",
            email_addr=(request.args.get("email") or "").strip(),
            endpoint="/api/external/messages",
            status="error",
            details={"code": exc.code},
        )
        return _external_error_response(exc)
    except Exception as exc:
        external_api_service.audit_external_api_access(
            action="external_api_access",
            email_addr=(request.args.get("email") or "").strip(),
            endpoint="/api/external/messages",
            status="error",
            details={"code": "INTERNAL_ERROR", "err": type(exc).__name__},
        )
        return jsonify(external_api_service.fail("INTERNAL_ERROR", "服务内部错误")), 500


@api_key_required
def api_external_get_latest_message() -> Any:
    try:
        args = _parse_external_common_args()
        latest = external_api_service.get_latest_message_for_external(
            email_addr=args["email"],
            folder=args["folder"],
            from_contains=args["from_contains"],
            subject_contains=args["subject_contains"],
            since_minutes=args["since_minutes"],
        )
        external_api_service.audit_external_api_access(
            action="external_api_access",
            email_addr=args["email"] or "",
            endpoint="/api/external/messages/latest",
            status="ok",
            details={"method": latest.get("method")},
        )
        return jsonify(external_api_service.ok(latest))
    except external_api_service.ExternalApiError as exc:
        external_api_service.audit_external_api_access(
            action="external_api_access",
            email_addr=(request.args.get("email") or "").strip(),
            endpoint="/api/external/messages/latest",
            status="error",
            details={"code": exc.code},
        )
        return _external_error_response(exc)
    except Exception as exc:
        external_api_service.audit_external_api_access(
            action="external_api_access",
            email_addr=(request.args.get("email") or "").strip(),
            endpoint="/api/external/messages/latest",
            status="error",
            details={"code": "INTERNAL_ERROR", "err": type(exc).__name__},
        )
        return jsonify(external_api_service.fail("INTERNAL_ERROR", "服务内部错误")), 500


@api_key_required
def api_external_get_message_detail(message_id: str) -> Any:
    try:
        args = _parse_external_common_args()
        detail = external_api_service.get_message_detail_for_external(
            email_addr=args["email"],
            message_id=message_id,
            folder=args["folder"],
        )
        external_api_service.audit_external_api_access(
            action="external_api_access",
            email_addr=args["email"] or "",
            endpoint="/api/external/messages/{message_id}",
            status="ok",
            details={"method": detail.get("method")},
        )
        return jsonify(external_api_service.ok(detail))
    except external_api_service.ExternalApiError as exc:
        external_api_service.audit_external_api_access(
            action="external_api_access",
            email_addr=(request.args.get("email") or "").strip(),
            endpoint="/api/external/messages/{message_id}",
            status="error",
            details={"code": exc.code},
        )
        return _external_error_response(exc)
    except Exception as exc:
        external_api_service.audit_external_api_access(
            action="external_api_access",
            email_addr=(request.args.get("email") or "").strip(),
            endpoint="/api/external/messages/{message_id}",
            status="error",
            details={"code": "INTERNAL_ERROR", "err": type(exc).__name__},
        )
        return jsonify(external_api_service.fail("INTERNAL_ERROR", "服务内部错误")), 500


@api_key_required
def api_external_get_message_raw(message_id: str) -> Any:
    try:
        args = _parse_external_common_args()
        detail = external_api_service.get_message_detail_for_external(
            email_addr=args["email"],
            message_id=message_id,
            folder=args["folder"],
        )
        external_api_service.audit_external_api_access(
            action="external_api_access",
            email_addr=args["email"] or "",
            endpoint="/api/external/messages/{message_id}/raw",
            status="ok",
            details={"method": detail.get("method")},
        )
        return jsonify(
            external_api_service.ok(
                {
                    "id": message_id,
                    "email_address": args["email"],
                    "raw_content": detail.get("raw_content", ""),
                    "method": detail.get("method", ""),
                }
            )
        )
    except external_api_service.ExternalApiError as exc:
        external_api_service.audit_external_api_access(
            action="external_api_access",
            email_addr=(request.args.get("email") or "").strip(),
            endpoint="/api/external/messages/{message_id}/raw",
            status="error",
            details={"code": exc.code},
        )
        return _external_error_response(exc)
    except Exception as exc:
        external_api_service.audit_external_api_access(
            action="external_api_access",
            email_addr=(request.args.get("email") or "").strip(),
            endpoint="/api/external/messages/{message_id}/raw",
            status="error",
            details={"code": "INTERNAL_ERROR", "err": type(exc).__name__},
        )
        return jsonify(external_api_service.fail("INTERNAL_ERROR", "服务内部错误")), 500


@api_key_required
def api_external_get_verification_code() -> Any:
    try:
        args = _parse_external_common_args(default_since_minutes=10)
        code_length = (request.args.get("code_length") or "").strip() or None
        code_regex = (request.args.get("code_regex") or "").strip() or None
        code_source = (request.args.get("code_source") or "all").strip().lower()
        if code_source not in {"subject", "content", "html", "all"}:
            raise external_api_service.InvalidParamError("code_source 参数无效")

        result = external_api_service.get_verification_result(
            email_addr=args["email"],
            folder=args["folder"],
            from_contains=args["from_contains"],
            subject_contains=args["subject_contains"],
            since_minutes=args["since_minutes"],
            code_regex=code_regex,
            code_length=code_length,
            code_source=code_source,
        )
        if not result.get("verification_code"):
            raise external_api_service.VerificationCodeNotFoundError("未找到符合条件的验证码邮件")

        external_api_service.audit_external_api_access(
            action="external_api_access",
            email_addr=args["email"] or "",
            endpoint="/api/external/verification-code",
            status="ok",
            details={"matched_email_id": result.get("matched_email_id"), "method": result.get("method")},
        )
        return jsonify(external_api_service.ok(result))
    except external_api_service.ExternalApiError as exc:
        external_api_service.audit_external_api_access(
            action="external_api_access",
            email_addr=(request.args.get("email") or "").strip(),
            endpoint="/api/external/verification-code",
            status="error",
            details={"code": exc.code},
        )
        return _external_error_response(exc)
    except ValueError:
        external_api_service.audit_external_api_access(
            action="external_api_access",
            email_addr=(request.args.get("email") or "").strip(),
            endpoint="/api/external/verification-code",
            status="error",
            details={"code": "INVALID_PARAM"},
        )
        return jsonify(external_api_service.fail("INVALID_PARAM", "参数错误")), 400
    except Exception:
        external_api_service.audit_external_api_access(
            action="external_api_access",
            email_addr=(request.args.get("email") or "").strip(),
            endpoint="/api/external/verification-code",
            status="error",
            details={"code": "INTERNAL_ERROR"},
        )
        return jsonify(external_api_service.fail("INTERNAL_ERROR", "服务内部错误")), 500


@api_key_required
def api_external_get_verification_link() -> Any:
    try:
        args = _parse_external_common_args(default_since_minutes=10)
        result = external_api_service.get_verification_result(
            email_addr=args["email"],
            folder=args["folder"],
            from_contains=args["from_contains"],
            subject_contains=args["subject_contains"],
            since_minutes=args["since_minutes"],
        )
        if not result.get("verification_link"):
            raise external_api_service.VerificationLinkNotFoundError("未找到符合条件的验证链接邮件")

        external_api_service.audit_external_api_access(
            action="external_api_access",
            email_addr=args["email"] or "",
            endpoint="/api/external/verification-link",
            status="ok",
            details={"matched_email_id": result.get("matched_email_id"), "method": result.get("method")},
        )
        return jsonify(external_api_service.ok(result))
    except external_api_service.ExternalApiError as exc:
        external_api_service.audit_external_api_access(
            action="external_api_access",
            email_addr=(request.args.get("email") or "").strip(),
            endpoint="/api/external/verification-link",
            status="error",
            details={"code": exc.code},
        )
        return _external_error_response(exc)
    except Exception:
        external_api_service.audit_external_api_access(
            action="external_api_access",
            email_addr=(request.args.get("email") or "").strip(),
            endpoint="/api/external/verification-link",
            status="error",
            details={"code": "INTERNAL_ERROR"},
        )
        return jsonify(external_api_service.fail("INTERNAL_ERROR", "服务内部错误")), 500


@api_key_required
def api_external_wait_message() -> Any:
    try:
        args = _parse_external_common_args()
        timeout_seconds = request.args.get("timeout_seconds", "30")
        poll_interval = request.args.get("poll_interval", "5")
        result = external_api_service.wait_for_message(
            email_addr=args["email"],
            timeout_seconds=int(timeout_seconds),
            poll_interval=int(poll_interval),
            folder=args["folder"],
            from_contains=args["from_contains"],
            subject_contains=args["subject_contains"],
            since_minutes=args["since_minutes"],
        )

        external_api_service.audit_external_api_access(
            action="external_api_access",
            email_addr=args["email"] or "",
            endpoint="/api/external/wait-message",
            status="ok",
            details={"matched_email_id": result.get("id"), "method": result.get("method")},
        )
        return jsonify(external_api_service.ok(result))
    except external_api_service.ExternalApiError as exc:
        external_api_service.audit_external_api_access(
            action="external_api_access",
            email_addr=(request.args.get("email") or "").strip(),
            endpoint="/api/external/wait-message",
            status="error",
            details={"code": exc.code},
        )
        return _external_error_response(exc)
    except Exception as exc:
        external_api_service.audit_external_api_access(
            action="external_api_access",
            email_addr=(request.args.get("email") or "").strip(),
            endpoint="/api/external/wait-message",
            status="error",
            details={"code": "INTERNAL_ERROR", "err": type(exc).__name__},
        )
        return jsonify(external_api_service.fail("INTERNAL_ERROR", "服务内部错误")), 500
