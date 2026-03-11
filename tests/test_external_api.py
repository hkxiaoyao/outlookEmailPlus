import json
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests._import_app import clear_login_attempts, import_web_app_module


class ExternalApiBaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = import_web_app_module()
        cls.app = cls.module.app

    def setUp(self):
        with self.app.app_context():
            clear_login_attempts()
            from outlook_web.db import get_db
            from outlook_web.repositories import settings as settings_repo

            db = get_db()
            db.execute("DELETE FROM audit_logs WHERE resource_type = 'external_api'")
            db.execute("DELETE FROM accounts WHERE email LIKE '%@extapi.test'")
            db.commit()
            settings_repo.set_setting("external_api_key", "")

    def _set_external_api_key(self, value: str):
        with self.app.app_context():
            from outlook_web.repositories import settings as settings_repo

            settings_repo.set_setting("external_api_key", value)

    def _insert_outlook_account(self, email_addr: str | None = None) -> str:
        email_addr = email_addr or f"{uuid.uuid4().hex}@extapi.test"
        with self.app.app_context():
            from outlook_web.db import get_db

            db = get_db()
            db.execute(
                """
                INSERT INTO accounts (email, password, client_id, refresh_token, group_id, status, account_type, provider)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (email_addr, "pw", "cid-test", "rt-test", 1, "active", "outlook", "outlook"),
            )
            db.commit()
        return email_addr

    def _insert_imap_account(self, email_addr: str | None = None) -> str:
        email_addr = email_addr or f"{uuid.uuid4().hex}@extapi.test"
        with self.app.app_context():
            from outlook_web.db import get_db

            db = get_db()
            db.execute(
                """
                INSERT INTO accounts (
                    email, password, client_id, refresh_token, group_id, status,
                    account_type, provider, imap_host, imap_port, imap_password
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (email_addr, "pw", "cid-test", "rt-test", 1, "active", "imap", "custom", "imap.test.com", 993, "imap-pass"),
            )
            db.commit()
        return email_addr

    @staticmethod
    def _auth_headers(value: str = "abc123"):
        return {"X-API-Key": value}

    def _login(self, client, password: str = "testpass123"):
        resp = client.post("/login", json={"password": password})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json().get("success"))

    @staticmethod
    def _utc_iso(minutes_delta: int = 0) -> str:
        dt = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=minutes_delta)
        return dt.isoformat().replace("+00:00", "Z")

    @classmethod
    def _graph_email(
        cls,
        message_id: str = "msg-1",
        subject: str = "Your verification code",
        sender: str = "noreply@example.com",
        received_at: str | None = None,
    ):
        return {
            "id": message_id,
            "subject": subject,
            "from": {"emailAddress": {"address": sender}},
            "receivedDateTime": received_at or cls._utc_iso(),
            "isRead": False,
            "hasAttachments": False,
            "bodyPreview": "Your code is 123456",
        }

    @classmethod
    def _graph_detail(
        cls,
        message_id: str = "msg-1",
        body_text: str = "Your code is 123456",
        html_text: str = "<p>Your code is 123456</p>",
        received_at: str | None = None,
    ):
        return {
            "id": message_id,
            "subject": "Your verification code",
            "from": {"emailAddress": {"address": "noreply@example.com"}},
            "toRecipients": [{"emailAddress": {"address": "user@outlook.com"}}],
            "receivedDateTime": received_at or cls._utc_iso(),
            "body": {"content": body_text if body_text else html_text, "contentType": "text" if body_text else "html"},
        }

    def _external_audit_logs(self):
        with self.app.app_context():
            from outlook_web.db import get_db

            db = get_db()
            rows = db.execute("""
                SELECT action, resource_id, details
                FROM audit_logs
                WHERE resource_type = 'external_api'
                ORDER BY id ASC
                """).fetchall()
        return [dict(row) for row in rows]


class ExternalApiAuthTests(ExternalApiBaseTest):
    def test_external_health_requires_api_key(self):
        client = self.app.test_client()
        self._set_external_api_key("abc123")

        resp = client.get("/api/external/health")

        self.assertEqual(resp.status_code, 401)
        data = resp.get_json()
        self.assertEqual(data.get("code"), "UNAUTHORIZED")

    def test_external_health_returns_403_when_api_key_not_configured(self):
        client = self.app.test_client()

        resp = client.get("/api/external/health", headers=self._auth_headers("abc123"))

        self.assertEqual(resp.status_code, 403)
        data = resp.get_json()
        self.assertEqual(data.get("code"), "API_KEY_NOT_CONFIGURED")

    def test_external_health_accepts_valid_api_key(self):
        client = self.app.test_client()
        self._set_external_api_key("abc123")

        resp = client.get("/api/external/health", headers=self._auth_headers("abc123"))

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("success"))
        self.assertEqual(data.get("code"), "OK")


class ExternalApiMessageTests(ExternalApiBaseTest):
    @patch("outlook_web.services.graph.get_emails_graph")
    def test_external_latest_message_returns_filtered_latest_email(self, mock_get_emails_graph):
        email_addr = self._insert_outlook_account()
        self._set_external_api_key("abc123")
        newer = self._graph_email(message_id="msg-new", subject="Target mail", received_at=self._utc_iso())
        older = self._graph_email(message_id="msg-old", subject="Ignore mail", received_at=self._utc_iso(minutes_delta=-2))
        mock_get_emails_graph.return_value = {"success": True, "emails": [older, newer]}

        client = self.app.test_client()
        resp = client.get(
            f"/api/external/messages/latest?email={email_addr}&subject_contains=Target",
            headers=self._auth_headers(),
        )

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("success"))
        self.assertEqual(data.get("data", {}).get("id"), "msg-new")

    def test_external_messages_returns_account_not_found(self):
        client = self.app.test_client()
        self._set_external_api_key("abc123")

        resp = client.get(
            "/api/external/messages?email=missing@extapi.test",
            headers=self._auth_headers(),
        )

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json().get("code"), "ACCOUNT_NOT_FOUND")

    @patch("outlook_web.services.graph.get_emails_graph")
    def test_external_messages_returns_list_when_graph_succeeds(self, mock_get_emails_graph):
        email_addr = self._insert_outlook_account()
        self._set_external_api_key("abc123")
        mock_get_emails_graph.return_value = {"success": True, "emails": [self._graph_email()]}

        client = self.app.test_client()
        resp = client.get(
            f"/api/external/messages?email={email_addr}",
            headers=self._auth_headers(),
        )

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("success"))
        self.assertEqual(data.get("code"), "OK")
        self.assertEqual(len(data.get("data", {}).get("emails", [])), 1)

    @patch("outlook_web.services.imap.get_emails_imap_with_server")
    @patch("outlook_web.services.graph.get_emails_graph")
    def test_external_messages_falls_back_to_imap_when_graph_fails(self, mock_get_emails_graph, mock_get_emails_imap):
        email_addr = self._insert_outlook_account()
        self._set_external_api_key("abc123")
        mock_get_emails_graph.return_value = {"success": False, "error": "graph failed"}
        mock_get_emails_imap.return_value = {
            "success": True,
            "emails": [
                {
                    "id": "imap-1",
                    "subject": "IMAP Subject",
                    "from": "imap@example.com",
                    "date": "2026-03-08T12:00:00Z",
                    "is_read": False,
                    "has_attachments": False,
                    "body_preview": "preview",
                }
            ],
        }

        client = self.app.test_client()
        resp = client.get(
            f"/api/external/messages?email={email_addr}",
            headers=self._auth_headers(),
        )

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("success"))
        self.assertEqual(len(data.get("data", {}).get("emails", [])), 1)

    @patch("outlook_web.services.graph.get_email_raw_graph")
    @patch("outlook_web.services.graph.get_email_detail_graph")
    def test_external_message_detail_returns_message_content(self, mock_get_email_detail_graph, mock_get_email_raw_graph):
        email_addr = self._insert_outlook_account()
        self._set_external_api_key("abc123")
        mock_get_email_detail_graph.return_value = self._graph_detail()
        mock_get_email_raw_graph.return_value = "RAW MIME CONTENT"

        client = self.app.test_client()
        resp = client.get(
            f"/api/external/messages/msg-1?email={email_addr}",
            headers=self._auth_headers(),
        )

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("success"))
        self.assertIn("content", data.get("data", {}))
        self.assertIn("raw_content", data.get("data", {}))
        self.assertEqual(data.get("data", {}).get("raw_content"), "RAW MIME CONTENT")

    @patch("outlook_web.services.graph.get_email_raw_graph")
    @patch("outlook_web.services.graph.get_email_detail_graph")
    def test_external_message_raw_returns_raw_content_and_audits(self, mock_get_email_detail_graph, mock_get_email_raw_graph):
        email_addr = self._insert_outlook_account()
        self._set_external_api_key("abc123")
        mock_get_email_detail_graph.return_value = self._graph_detail(body_text="raw test")
        mock_get_email_raw_graph.return_value = "MIME-Version: 1.0\r\nraw test"

        client = self.app.test_client()
        resp = client.get(
            f"/api/external/messages/msg-1/raw?email={email_addr}",
            headers=self._auth_headers(),
        )

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("success"))
        self.assertEqual(data.get("data", {}).get("raw_content"), "MIME-Version: 1.0\r\nraw test")

        audit_logs = self._external_audit_logs()
        self.assertEqual(len(audit_logs), 1)
        self.assertIn("/api/external/messages/{message_id}/raw", audit_logs[0]["details"])


class ExternalApiVerificationTests(ExternalApiBaseTest):
    @patch("outlook_web.services.graph.get_email_raw_graph")
    @patch("outlook_web.services.graph.get_email_detail_graph")
    @patch("outlook_web.services.graph.get_emails_graph")
    def test_external_verification_code_returns_code(
        self,
        mock_get_emails_graph,
        mock_get_email_detail_graph,
        mock_get_email_raw_graph,
    ):
        email_addr = self._insert_outlook_account()
        self._set_external_api_key("abc123")
        mock_get_emails_graph.return_value = {"success": True, "emails": [self._graph_email()]}
        mock_get_email_detail_graph.return_value = self._graph_detail(body_text="Your code is 123456")
        mock_get_email_raw_graph.return_value = "RAW MIME CONTENT"

        client = self.app.test_client()
        resp = client.get(
            f"/api/external/verification-code?email={email_addr}",
            headers=self._auth_headers(),
        )

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("success"))
        self.assertEqual(data.get("data", {}).get("verification_code"), "123456")

    @patch("outlook_web.services.graph.get_emails_graph")
    def test_external_verification_code_defaults_to_recent_10_minutes(self, mock_get_emails_graph):
        email_addr = self._insert_outlook_account()
        self._set_external_api_key("abc123")
        mock_get_emails_graph.return_value = {
            "success": True,
            "emails": [self._graph_email(received_at=self._utc_iso(minutes_delta=-20))],
        }

        client = self.app.test_client()
        resp = client.get(
            f"/api/external/verification-code?email={email_addr}",
            headers=self._auth_headers(),
        )

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json().get("code"), "MAIL_NOT_FOUND")

    @patch("outlook_web.services.graph.get_email_raw_graph")
    @patch("outlook_web.services.graph.get_email_detail_graph")
    @patch("outlook_web.services.graph.get_emails_graph")
    def test_external_verification_link_returns_preferred_link(
        self,
        mock_get_emails_graph,
        mock_get_email_detail_graph,
        mock_get_email_raw_graph,
    ):
        email_addr = self._insert_outlook_account()
        self._set_external_api_key("abc123")
        mock_get_emails_graph.return_value = {
            "success": True,
            "emails": [self._graph_email(subject="Please verify your email")],
        }
        mock_get_email_detail_graph.return_value = self._graph_detail(
            body_text="Click https://example.com/verify?token=abc to continue",
        )
        mock_get_email_raw_graph.return_value = "RAW MIME CONTENT"

        client = self.app.test_client()
        resp = client.get(
            f"/api/external/verification-link?email={email_addr}",
            headers=self._auth_headers(),
        )

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("success"))
        self.assertIn("verify", data.get("data", {}).get("verification_link", ""))

    @patch("outlook_web.services.graph.get_emails_graph")
    def test_external_verification_link_defaults_to_recent_10_minutes(self, mock_get_emails_graph):
        email_addr = self._insert_outlook_account()
        self._set_external_api_key("abc123")
        mock_get_emails_graph.return_value = {
            "success": True,
            "emails": [self._graph_email(subject="Please verify your email", received_at=self._utc_iso(minutes_delta=-30))],
        }

        client = self.app.test_client()
        resp = client.get(
            f"/api/external/verification-link?email={email_addr}",
            headers=self._auth_headers(),
        )

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json().get("code"), "MAIL_NOT_FOUND")

    @patch("outlook_web.services.external_api.time.sleep")
    @patch("outlook_web.services.external_api.time.time")
    @patch("outlook_web.services.external_api.get_latest_message_for_external")
    def test_wait_for_message_only_returns_new_messages(self, mock_get_latest_message, mock_time, mock_sleep):
        from outlook_web.services import external_api as external_api_service

        mock_time.side_effect = [100, 100, 100]
        mock_get_latest_message.side_effect = [
            {"id": "old", "timestamp": 99, "method": "Graph API"},
            {"id": "new", "timestamp": 101, "method": "Graph API"},
        ]

        result = external_api_service.wait_for_message(email_addr="user@example.com", timeout_seconds=30, poll_interval=5)

        self.assertEqual(result.get("id"), "new")
        mock_sleep.assert_called_once_with(5)

    def test_external_wait_message_rejects_too_large_timeout(self):
        email_addr = self._insert_outlook_account()
        self._set_external_api_key("abc123")
        client = self.app.test_client()

        resp = client.get(
            f"/api/external/wait-message?email={email_addr}&timeout_seconds=999",
            headers=self._auth_headers(),
        )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json().get("code"), "INVALID_PARAM")


class ExternalApiSystemTests(ExternalApiBaseTest):
    def test_external_capabilities_returns_feature_list_and_audits(self):
        client = self.app.test_client()
        self._set_external_api_key("abc123")

        resp = client.get("/api/external/capabilities", headers=self._auth_headers())

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("success"))
        self.assertIn("service", data.get("data", {}))
        self.assertIn("version", data.get("data", {}))
        self.assertIn("features", data.get("data", {}))

        audit_logs = self._external_audit_logs()
        self.assertEqual(len(audit_logs), 1)
        self.assertIn("/api/external/capabilities", audit_logs[0]["details"])

    def test_external_health_audits_access(self):
        client = self.app.test_client()
        self._set_external_api_key("abc123")

        resp = client.get("/api/external/health", headers=self._auth_headers())

        self.assertEqual(resp.status_code, 200)
        audit_logs = self._external_audit_logs()
        self.assertEqual(len(audit_logs), 1)
        self.assertIn("/api/external/health", audit_logs[0]["details"])

    def test_external_account_status_returns_account_data_and_audits(self):
        email_addr = self._insert_outlook_account()
        self._set_external_api_key("abc123")
        client = self.app.test_client()

        resp = client.get(
            f"/api/external/account-status?email={email_addr}",
            headers=self._auth_headers(),
        )

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("success"))
        self.assertEqual(data.get("data", {}).get("email"), email_addr)
        self.assertTrue(data.get("data", {}).get("exists"))
        self.assertEqual(data.get("data", {}).get("account_type"), "outlook")
        self.assertEqual(data.get("data", {}).get("provider"), "outlook")
        self.assertIn("preferred_method", data.get("data", {}))
        self.assertIn("last_refresh_at", data.get("data", {}))
        self.assertTrue(data.get("data", {}).get("can_read"))

        audit_logs = self._external_audit_logs()
        self.assertEqual(len(audit_logs), 1)
        self.assertIn("/api/external/account-status", audit_logs[0]["details"])


class ExternalApiRegressionTests(ExternalApiBaseTest):
    @patch("outlook_web.services.graph.get_emails_graph")
    def test_internal_email_list_api_still_works(self, mock_get_emails_graph):
        email_addr = self._insert_outlook_account()
        mock_get_emails_graph.return_value = {"success": True, "emails": [self._graph_email()]}

        client = self.app.test_client()
        self._login(client)
        resp = client.get(f"/api/emails/{email_addr}")

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("success"))
        self.assertIn("emails", data)

    @patch("outlook_web.services.graph.get_email_detail_graph")
    @patch("outlook_web.services.graph.get_emails_graph")
    def test_internal_extract_verification_api_still_works(self, mock_get_emails_graph, mock_get_email_detail_graph):
        email_addr = self._insert_outlook_account()
        mock_get_emails_graph.return_value = {"success": True, "emails": [self._graph_email()]}
        mock_get_email_detail_graph.return_value = self._graph_detail(body_text="Your code is 123456")

        client = self.app.test_client()
        self._login(client)
        resp = client.get(f"/api/emails/{email_addr}/extract-verification")

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("success"))
        self.assertEqual(data.get("data", {}).get("verification_code"), "123456")

    def test_internal_settings_api_still_returns_existing_fields(self):
        client = self.app.test_client()
        self._login(client)

        resp = client.get("/api/settings")

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("success"))
        self.assertIn("refresh_interval_days", data.get("settings", {}))
        self.assertIn("gptmail_api_key_set", data.get("settings", {}))


class ExternalApiSchemaValidationTests(ExternalApiBaseTest):
    """OpenAPI 返回字段抽样校验：确认核心接口的返回字段覆盖 OpenAPI schema required 字段"""

    @patch("outlook_web.services.graph.get_emails_graph")
    def test_messages_response_schema_has_required_fields(self, mock_get_emails_graph):
        email_addr = self._insert_outlook_account()
        self._set_external_api_key("abc123")
        mock_get_emails_graph.return_value = {"success": True, "emails": [self._graph_email()]}

        client = self.app.test_client()
        resp = client.get(f"/api/external/messages?email={email_addr}", headers=self._auth_headers())

        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        # 顶层统一响应结构
        for key in ("success", "code", "message", "data"):
            self.assertIn(key, body, f"顶层缺少字段: {key}")
        data = body["data"]
        self.assertIn("emails", data)
        self.assertIn("count", data)
        # MessageSummary required 字段
        if data["emails"]:
            msg = data["emails"][0]
            for key in ("id", "email_address", "from_address", "subject", "has_html", "timestamp", "created_at", "is_read"):
                self.assertIn(key, msg, f"MessageSummary 缺少字段: {key}")

    @patch("outlook_web.services.graph.get_email_raw_graph")
    @patch("outlook_web.services.graph.get_email_detail_graph")
    def test_message_detail_response_schema_has_required_fields(self, mock_detail, mock_raw):
        email_addr = self._insert_outlook_account()
        self._set_external_api_key("abc123")
        mock_detail.return_value = self._graph_detail()
        mock_raw.return_value = "RAW"

        client = self.app.test_client()
        resp = client.get(f"/api/external/messages/msg-1?email={email_addr}", headers=self._auth_headers())

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json().get("data", {})
        for key in ("id", "email_address", "from_address", "subject", "content", "html_content", "raw_content",
                     "timestamp", "created_at", "has_html"):
            self.assertIn(key, data, f"MessageDetail 缺少字段: {key}")

    @patch("outlook_web.services.graph.get_email_raw_graph")
    @patch("outlook_web.services.graph.get_email_detail_graph")
    @patch("outlook_web.services.graph.get_emails_graph")
    def test_verification_code_response_schema_has_required_fields(self, mock_list, mock_detail, mock_raw):
        email_addr = self._insert_outlook_account()
        self._set_external_api_key("abc123")
        mock_list.return_value = {"success": True, "emails": [self._graph_email()]}
        mock_detail.return_value = self._graph_detail(body_text="Your code is 123456")
        mock_raw.return_value = "RAW"

        client = self.app.test_client()
        resp = client.get(f"/api/external/verification-code?email={email_addr}", headers=self._auth_headers())

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json().get("data", {})
        for key in ("email", "verification_code", "matched_email_id", "from", "subject", "received_at"):
            self.assertIn(key, data, f"VerificationCodeData 缺少字段: {key}")
        # confidence 枚举校验
        self.assertIn(data.get("confidence"), ("high", "low"), "confidence 应为 high 或 low")

    @patch("outlook_web.services.graph.get_email_raw_graph")
    @patch("outlook_web.services.graph.get_email_detail_graph")
    @patch("outlook_web.services.graph.get_emails_graph")
    def test_verification_link_response_schema_has_required_fields(self, mock_list, mock_detail, mock_raw):
        email_addr = self._insert_outlook_account()
        self._set_external_api_key("abc123")
        mock_list.return_value = {
            "success": True,
            "emails": [self._graph_email(subject="Please verify")],
        }
        mock_detail.return_value = self._graph_detail(
            body_text="Click https://example.com/verify?token=abc to verify",
        )
        mock_raw.return_value = "RAW"

        client = self.app.test_client()
        resp = client.get(f"/api/external/verification-link?email={email_addr}", headers=self._auth_headers())

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json().get("data", {})
        for key in ("email", "verification_link", "matched_email_id", "from", "subject", "received_at"):
            self.assertIn(key, data, f"VerificationLinkData 缺少字段: {key}")

    def test_health_response_schema_has_required_fields(self):
        self._set_external_api_key("abc123")
        client = self.app.test_client()
        resp = client.get("/api/external/health", headers=self._auth_headers())

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json().get("data", {})
        for key in ("status", "service", "version", "server_time_utc", "database"):
            self.assertIn(key, data, f"HealthData 缺少字段: {key}")

    def test_capabilities_response_schema_has_required_fields(self):
        self._set_external_api_key("abc123")
        client = self.app.test_client()
        resp = client.get("/api/external/capabilities", headers=self._auth_headers())

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json().get("data", {})
        for key in ("service", "version", "features"):
            self.assertIn(key, data, f"CapabilitiesData 缺少字段: {key}")
        self.assertIsInstance(data["features"], list)

    def test_account_status_response_schema_has_required_fields(self):
        email_addr = self._insert_outlook_account()
        self._set_external_api_key("abc123")
        client = self.app.test_client()
        resp = client.get(f"/api/external/account-status?email={email_addr}", headers=self._auth_headers())

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json().get("data", {})
        for key in ("email", "exists"):
            self.assertIn(key, data, f"AccountStatusData 缺少字段: {key}")
        self.assertIn("status", data, "AccountStatusData 应返回 status 字段")


class ExternalApiRawFieldTrimTests(ExternalApiBaseTest):
    """验证 /messages/{id}/raw 仅返回裁剪后的字段"""

    @patch("outlook_web.services.graph.get_email_raw_graph")
    @patch("outlook_web.services.graph.get_email_detail_graph")
    def test_raw_endpoint_only_returns_trimmed_fields(self, mock_detail, mock_raw):
        email_addr = self._insert_outlook_account()
        self._set_external_api_key("abc123")
        mock_detail.return_value = self._graph_detail(body_text="body text here")
        mock_raw.return_value = "MIME-Version: 1.0\r\nraw content"

        client = self.app.test_client()
        resp = client.get(f"/api/external/messages/msg-1/raw?email={email_addr}", headers=self._auth_headers())

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json().get("data", {})
        allowed_keys = {"id", "email_address", "raw_content", "method"}
        actual_keys = set(data.keys())
        self.assertEqual(actual_keys, allowed_keys,
                         f"raw 接口应仅返回 {allowed_keys}，实际返回 {actual_keys}")
        self.assertEqual(data["raw_content"], "MIME-Version: 1.0\r\nraw content")
        # 不应包含详情字段
        self.assertNotIn("content", data)
        self.assertNotIn("html_content", data)
        self.assertNotIn("subject", data)


class ExternalApiWaitMessageHttpTests(ExternalApiBaseTest):
    """wait-message HTTP 层集成测试"""

    @patch("outlook_web.services.external_api.time.sleep")
    @patch("outlook_web.services.external_api.time.time")
    @patch("outlook_web.services.graph.get_emails_graph")
    def test_wait_message_http_only_returns_new_message(self, mock_get_emails_graph, mock_time, mock_sleep):
        email_addr = self._insert_outlook_account()
        self._set_external_api_key("abc123")

        # baseline_timestamp = int(time.time()) = 2000000000
        # old email timestamp (~1767225600) < baseline → 不匹配
        # new email timestamp (~2019686400) >= baseline → 命中
        mock_time.side_effect = [2000000000, 2000000000, 2000000000, 2000000000, 2000000000]
        old_email = self._graph_email(message_id="old-msg", received_at="2026-01-01T00:00:00Z")
        new_email = self._graph_email(message_id="new-msg", received_at="2034-01-01T00:00:00Z")
        mock_get_emails_graph.side_effect = [
            {"success": True, "emails": [old_email]},
            {"success": True, "emails": [new_email]},
        ]

        client = self.app.test_client()
        resp = client.get(
            f"/api/external/wait-message?email={email_addr}&timeout_seconds=30&poll_interval=5",
            headers=self._auth_headers(),
        )

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("success"))
        self.assertEqual(data.get("data", {}).get("id"), "new-msg")

    def test_wait_message_http_returns_400_for_invalid_timeout(self):
        email_addr = self._insert_outlook_account()
        self._set_external_api_key("abc123")
        client = self.app.test_client()

        resp = client.get(
            f"/api/external/wait-message?email={email_addr}&timeout_seconds=0",
            headers=self._auth_headers(),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json().get("code"), "INVALID_PARAM")

    def test_wait_message_http_returns_400_for_missing_email(self):
        self._set_external_api_key("abc123")
        client = self.app.test_client()

        resp = client.get(
            "/api/external/wait-message?timeout_seconds=10",
            headers=self._auth_headers(),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json().get("code"), "INVALID_PARAM")

    @patch("outlook_web.services.external_api.wait_for_message")
    def test_wait_message_http_unexpected_error_logs_audit(self, mock_wait_for_message):
        """wait-message 未预期异常也应写 external_api 审计日志"""
        email_addr = self._insert_outlook_account()
        self._set_external_api_key("abc123")
        mock_wait_for_message.side_effect = RuntimeError("boom")

        client = self.app.test_client()
        resp = client.get(
            f"/api/external/wait-message?email={email_addr}&timeout_seconds=10&poll_interval=5",
            headers=self._auth_headers(),
        )

        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json().get("code"), "INTERNAL_ERROR")

        audit_logs = self._external_audit_logs()
        self.assertGreaterEqual(len(audit_logs), 1)
        last_log = audit_logs[-1]
        details = json.loads(last_log["details"]) if isinstance(last_log["details"], str) else last_log["details"]
        self.assertEqual(details.get("code"), "INTERNAL_ERROR")
        self.assertEqual(details.get("err"), "RuntimeError")


# ---------------------------------------------------------------------------
# TC-AUTH-03: 错误 API Key → 401 UNAUTHORIZED
# ---------------------------------------------------------------------------
class ExternalApiWrongKeyTests(ExternalApiBaseTest):
    """TC-AUTH-03"""

    def test_wrong_api_key_returns_401(self):
        self._set_external_api_key("correct-key-123")
        client = self.app.test_client()

        resp = client.get("/api/external/health", headers=self._auth_headers("wrong-key-456"))

        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.get_json().get("code"), "UNAUTHORIZED")


# ---------------------------------------------------------------------------
# TC-MSG-04 ~ TC-MSG-15: 消息接口参数校验、过滤、回退、错误路径
# ---------------------------------------------------------------------------
class ExternalApiMessageParamTests(ExternalApiBaseTest):
    """TC-MSG-04, TC-MSG-05, TC-MSG-06, TC-MSG-07, TC-MSG-08"""

    def test_invalid_folder_returns_400(self):
        """TC-MSG-04: folder 参数非法 → 400"""
        email_addr = self._insert_outlook_account()
        self._set_external_api_key("abc123")
        client = self.app.test_client()

        resp = client.get(
            f"/api/external/messages?email={email_addr}&folder=spam",
            headers=self._auth_headers(),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json().get("code"), "INVALID_PARAM")

    def test_top_param_zero_returns_400(self):
        """TC-MSG-05: top=0 越界"""
        email_addr = self._insert_outlook_account()
        self._set_external_api_key("abc123")
        client = self.app.test_client()

        resp = client.get(
            f"/api/external/messages?email={email_addr}&top=0",
            headers=self._auth_headers(),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json().get("code"), "INVALID_PARAM")

    def test_top_param_too_large_returns_400(self):
        """TC-MSG-05: top=999 越界"""
        email_addr = self._insert_outlook_account()
        self._set_external_api_key("abc123")
        client = self.app.test_client()

        resp = client.get(
            f"/api/external/messages?email={email_addr}&top=999",
            headers=self._auth_headers(),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json().get("code"), "INVALID_PARAM")

    @patch("outlook_web.services.graph.get_emails_graph")
    def test_from_contains_filter(self, mock_get_emails_graph):
        """TC-MSG-06: from_contains 过滤"""
        email_addr = self._insert_outlook_account()
        self._set_external_api_key("abc123")
        mock_get_emails_graph.return_value = {
            "success": True,
            "emails": [
                self._graph_email(message_id="m1", sender="openai@example.com", subject="OpenAI Code"),
                self._graph_email(message_id="m2", sender="google@example.com", subject="Google Code"),
            ],
        }

        client = self.app.test_client()
        resp = client.get(
            f"/api/external/messages?email={email_addr}&from_contains=openai",
            headers=self._auth_headers(),
        )

        self.assertEqual(resp.status_code, 200)
        emails = resp.get_json().get("data", {}).get("emails", [])
        self.assertEqual(len(emails), 1)
        self.assertIn("openai", emails[0].get("from_address", "").lower())

    @patch("outlook_web.services.graph.get_emails_graph")
    def test_since_minutes_filter(self, mock_get_emails_graph):
        """TC-MSG-08: since_minutes 过滤"""
        email_addr = self._insert_outlook_account()
        self._set_external_api_key("abc123")
        mock_get_emails_graph.return_value = {
            "success": True,
            "emails": [
                self._graph_email(message_id="new", received_at=self._utc_iso(minutes_delta=-2)),
                self._graph_email(message_id="old", received_at=self._utc_iso(minutes_delta=-60)),
            ],
        }

        client = self.app.test_client()
        resp = client.get(
            f"/api/external/messages?email={email_addr}&since_minutes=10",
            headers=self._auth_headers(),
        )

        self.assertEqual(resp.status_code, 200)
        emails = resp.get_json().get("data", {}).get("emails", [])
        self.assertEqual(len(emails), 1)
        self.assertEqual(emails[0].get("id"), "new")


class ExternalApiMessageErrorTests(ExternalApiBaseTest):
    """TC-MSG-10, TC-MSG-13, TC-MSG-14, TC-MSG-15"""

    @patch("outlook_web.services.graph.get_emails_graph")
    def test_latest_message_not_found(self, mock_get_emails_graph):
        """TC-MSG-10: 最新邮件不存在 → 404"""
        email_addr = self._insert_outlook_account()
        self._set_external_api_key("abc123")
        mock_get_emails_graph.return_value = {"success": True, "emails": []}

        client = self.app.test_client()
        resp = client.get(
            f"/api/external/messages/latest?email={email_addr}",
            headers=self._auth_headers(),
        )

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json().get("code"), "MAIL_NOT_FOUND")

    @patch("outlook_web.services.imap.get_email_detail_imap_with_server")
    @patch("outlook_web.services.graph.get_email_raw_graph")
    @patch("outlook_web.services.graph.get_email_detail_graph")
    def test_detail_graph_fail_imap_fallback(self, mock_detail_graph, mock_raw_graph, mock_detail_imap):
        """TC-MSG-13: 详情 Graph 失败后 IMAP 回退成功"""
        email_addr = self._insert_outlook_account()
        self._set_external_api_key("abc123")
        mock_detail_graph.return_value = None
        mock_raw_graph.return_value = None
        mock_detail_imap.return_value = {
            "id": "msg-1",
            "subject": "IMAP Detail Subject",
            "from": "sender@test.com",
            "date": self._utc_iso(),
            "body": "IMAP body content",
            "html": "<p>IMAP body content</p>",
        }

        client = self.app.test_client()
        resp = client.get(
            f"/api/external/messages/msg-1?email={email_addr}",
            headers=self._auth_headers(),
        )

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json().get("data", {})
        self.assertIn("content", data)
        self.assertIn("IMAP", data.get("method", ""))

    @patch("outlook_web.services.imap.get_emails_imap_with_server")
    @patch("outlook_web.services.graph.get_emails_graph")
    def test_all_upstream_fail_returns_502(self, mock_graph, mock_imap):
        """TC-MSG-14: Graph + IMAP 全部失败 → 502 UPSTREAM_READ_FAILED"""
        email_addr = self._insert_outlook_account()
        self._set_external_api_key("abc123")
        mock_graph.return_value = {"success": False, "error": "graph error"}
        mock_imap.return_value = {"success": False, "error": "imap error"}

        client = self.app.test_client()
        resp = client.get(
            f"/api/external/messages?email={email_addr}",
            headers=self._auth_headers(),
        )

        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.get_json().get("code"), "UPSTREAM_READ_FAILED")

    @patch("outlook_web.services.graph.get_emails_graph")
    def test_proxy_error_returns_502(self, mock_graph):
        """TC-MSG-15: Graph 代理错误 → 502 PROXY_ERROR"""
        email_addr = self._insert_outlook_account()
        self._set_external_api_key("abc123")
        mock_graph.return_value = {
            "success": False,
            "error": {"type": "ProxyError", "message": "Proxy connection failed"},
        }

        client = self.app.test_client()
        resp = client.get(
            f"/api/external/messages?email={email_addr}",
            headers=self._auth_headers(),
        )

        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.get_json().get("code"), "PROXY_ERROR")


# ---------------------------------------------------------------------------
# TC-VER-04, TC-VER-06, TC-VER-09, TC-VER-12: 验证码/链接错误路径
# ---------------------------------------------------------------------------
class ExternalApiVerificationErrorTests(ExternalApiBaseTest):
    """TC-VER-04, TC-VER-06, TC-VER-09, TC-VER-12"""

    @patch("outlook_web.services.graph.get_email_raw_graph")
    @patch("outlook_web.services.graph.get_email_detail_graph")
    @patch("outlook_web.services.graph.get_emails_graph")
    def test_invalid_code_regex_returns_400(self, mock_list, mock_detail, mock_raw):
        """TC-VER-04: 非法正则 → 400 INVALID_PARAM"""
        email_addr = self._insert_outlook_account()
        self._set_external_api_key("abc123")
        mock_list.return_value = {"success": True, "emails": [self._graph_email()]}
        mock_detail.return_value = self._graph_detail(body_text="Code is 123456")
        mock_raw.return_value = "RAW"

        client = self.app.test_client()
        resp = client.get(
            f"/api/external/verification-code?email={email_addr}&code_regex=[invalid",
            headers=self._auth_headers(),
        )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json().get("code"), "INVALID_PARAM")

    @patch("outlook_web.services.graph.get_email_raw_graph")
    @patch("outlook_web.services.graph.get_email_detail_graph")
    @patch("outlook_web.services.graph.get_emails_graph")
    def test_no_verification_code_returns_404(self, mock_list, mock_detail, mock_raw):
        """TC-VER-06: 邮件存在但无验证码 → 404 VERIFICATION_CODE_NOT_FOUND"""
        email_addr = self._insert_outlook_account()
        self._set_external_api_key("abc123")
        mock_list.return_value = {"success": True, "emails": [self._graph_email()]}
        mock_detail.return_value = self._graph_detail(body_text="Hello, this is a normal email with no code.")
        mock_raw.return_value = "RAW"

        client = self.app.test_client()
        resp = client.get(
            f"/api/external/verification-code?email={email_addr}",
            headers=self._auth_headers(),
        )

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json().get("code"), "VERIFICATION_CODE_NOT_FOUND")

    @patch("outlook_web.services.graph.get_email_raw_graph")
    @patch("outlook_web.services.graph.get_email_detail_graph")
    @patch("outlook_web.services.graph.get_emails_graph")
    def test_no_verification_link_returns_404(self, mock_list, mock_detail, mock_raw):
        """TC-VER-09: 邮件存在但无验证链接 → 404 VERIFICATION_LINK_NOT_FOUND"""
        email_addr = self._insert_outlook_account()
        self._set_external_api_key("abc123")
        mock_list.return_value = {"success": True, "emails": [self._graph_email()]}
        mock_detail.return_value = self._graph_detail(body_text="No links here at all.")
        mock_raw.return_value = "RAW"

        client = self.app.test_client()
        resp = client.get(
            f"/api/external/verification-link?email={email_addr}",
            headers=self._auth_headers(),
        )

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json().get("code"), "VERIFICATION_LINK_NOT_FOUND")

    @patch("outlook_web.services.external_api.time.sleep")
    @patch("outlook_web.services.external_api.time.time")
    @patch("outlook_web.services.graph.get_emails_graph")
    def test_wait_message_timeout_returns_404(self, mock_graph, mock_time, mock_sleep):
        """TC-VER-12: 等待超时 → 404 MAIL_NOT_FOUND"""
        email_addr = self._insert_outlook_account()
        self._set_external_api_key("abc123")
        # time.time() 模拟: baseline=100, start=100, 第1次循环检查=100, 第2次=200(超时)
        mock_time.side_effect = [100, 100, 100, 200]
        mock_graph.return_value = {"success": True, "emails": []}

        client = self.app.test_client()
        resp = client.get(
            f"/api/external/wait-message?email={email_addr}&timeout_seconds=10&poll_interval=5",
            headers=self._auth_headers(),
        )

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json().get("code"), "MAIL_NOT_FOUND")


# ---------------------------------------------------------------------------
# TC-SYS-04: account-status 账号不存在
# ---------------------------------------------------------------------------
class ExternalApiSystemErrorTests(ExternalApiBaseTest):
    """TC-SYS-04"""

    def test_account_status_not_found(self):
        """TC-SYS-04: account-status 账号不存在 → 404 ACCOUNT_NOT_FOUND"""
        self._set_external_api_key("abc123")
        client = self.app.test_client()

        resp = client.get(
            "/api/external/account-status?email=nonexist@nowhere.test",
            headers=self._auth_headers(),
        )

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json().get("code"), "ACCOUNT_NOT_FOUND")


# ---------------------------------------------------------------------------
# TC-REG-02, TC-REG-05: 旧接口回归补充
# ---------------------------------------------------------------------------
class ExternalApiRegressionExtendedTests(ExternalApiBaseTest):
    """TC-REG-02, TC-REG-05"""

    @patch("outlook_web.services.graph.get_email_detail_graph")
    @patch("outlook_web.services.graph.get_emails_graph")
    def test_internal_email_detail_still_works(self, mock_list, mock_detail):
        """TC-REG-02: 旧邮件详情接口仍可用"""
        email_addr = self._insert_outlook_account()
        mock_list.return_value = {"success": True, "emails": [self._graph_email()]}
        mock_detail.return_value = self._graph_detail(body_text="detail body")

        client = self.app.test_client()
        self._login(client)

        resp = client.get(f"/api/email/{email_addr}/msg-1")

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("success"))

    def test_settings_put_old_fields_only(self):
        """TC-REG-05: PUT /api/settings 只修改旧字段不影响 external_api_key"""
        self._set_external_api_key("my-secret-key")
        client = self.app.test_client()
        self._login(client)

        resp = client.put("/api/settings", json={"refresh_interval_days": 7})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json().get("success"))

        # external_api_key 不应被清空
        with self.app.app_context():
            from outlook_web.repositories import settings as settings_repo
            key = settings_repo.get_external_api_key()
            self.assertTrue(key, "external_api_key 不应被清空")


# ---------------------------------------------------------------------------
# TC-AUD-02, TC-AUD-03: 审计日志错误路径与敏感信息脱敏
# ---------------------------------------------------------------------------
class ExternalApiAuditTests(ExternalApiBaseTest):
    """TC-AUD-02, TC-AUD-03"""

    @patch("outlook_web.services.graph.get_emails_graph")
    def test_failed_api_call_also_logs_audit(self, mock_graph):
        """TC-AUD-02: 失败调用也写审计日志"""
        email_addr = self._insert_outlook_account()
        self._set_external_api_key("abc123")
        mock_graph.return_value = {"success": True, "emails": []}

        client = self.app.test_client()
        # 触发 MAIL_NOT_FOUND
        resp = client.get(
            f"/api/external/messages/latest?email={email_addr}",
            headers=self._auth_headers(),
        )
        self.assertEqual(resp.status_code, 404)

        audit_logs = self._external_audit_logs()
        self.assertGreaterEqual(len(audit_logs), 1)
        last_log = audit_logs[-1]
        details = json.loads(last_log["details"]) if isinstance(last_log["details"], str) else last_log["details"]
        self.assertEqual(details.get("code"), "MAIL_NOT_FOUND")

    def test_audit_logs_do_not_contain_api_key(self):
        """TC-AUD-03: 审计日志不包含明文 API Key"""
        self._set_external_api_key("super-secret-api-key-12345")
        client = self.app.test_client()

        resp = client.get("/api/external/health", headers=self._auth_headers("super-secret-api-key-12345"))
        self.assertEqual(resp.status_code, 200)

        audit_logs = self._external_audit_logs()
        for log in audit_logs:
            details_str = json.dumps(log) if isinstance(log, dict) else str(log)
            self.assertNotIn("super-secret-api-key-12345", details_str,
                             "审计日志不应包含明文 API Key")


if __name__ == "__main__":
    unittest.main()
