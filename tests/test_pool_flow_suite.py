import json
import unittest
import uuid

from tests._import_app import clear_login_attempts, import_web_app_module


class PoolFlowSuiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = import_web_app_module()
        cls.app = cls.module.app
        cls.client = cls.app.test_client()
        from outlook_web.db import create_sqlite_connection

        cls.create_conn = staticmethod(lambda: create_sqlite_connection())

    def setUp(self):
        with self.app.app_context():
            clear_login_attempts()
            from outlook_web.db import get_db
            from outlook_web.repositories import settings as settings_repo

            db = get_db()
            db.execute("DELETE FROM external_api_keys")
            db.execute("DELETE FROM external_api_consumer_usage_daily")
            db.execute("DELETE FROM audit_logs WHERE resource_type = 'external_api'")
            db.execute(
                "DELETE FROM account_claim_logs WHERE account_id IN (SELECT id FROM accounts WHERE email LIKE '%@poolflow.test')"
            )
            db.execute("DELETE FROM accounts WHERE email LIKE '%@poolflow.test'")
            db.commit()
            settings_repo.set_setting("external_api_key", "suite-key")
            settings_repo.set_setting("pool_external_enabled", "true")
            settings_repo.set_setting("external_api_public_mode", "false")
            settings_repo.set_setting("external_api_ip_whitelist", "[]")
            settings_repo.set_setting("external_api_disable_pool_claim_random", "false")
            settings_repo.set_setting("external_api_disable_pool_claim_release", "false")
            settings_repo.set_setting("external_api_disable_pool_claim_complete", "false")
            settings_repo.set_setting("external_api_disable_pool_stats", "false")

    @staticmethod
    def _auth_headers():
        return {"X-API-Key": "suite-key"}

    def _make_pool_account(self, *, provider: str = "outlook", pool_status: str = "available") -> dict:
        conn = self.create_conn()
        try:
            email_addr = f"flow_{uuid.uuid4().hex}@poolflow.test"
            conn.execute(
                """
                INSERT INTO accounts (
                    email, client_id, refresh_token, status,
                    account_type, provider, group_id, pool_status
                )
                VALUES (?, 'test_client', 'test_token', 'active', 'outlook', ?, 1, ?)
                """,
                (email_addr, provider, pool_status),
            )
            conn.commit()
            row = conn.execute(
                "SELECT id, email, pool_status, provider FROM accounts WHERE email = ?",
                (email_addr,),
            ).fetchone()
            return dict(row)
        finally:
            conn.close()

    def test_claim_complete_success_changes_status_to_used(self):
        self._make_pool_account()

        claim_resp = self.client.post(
            "/api/external/pool/claim-random",
            headers=self._auth_headers(),
            json={"caller_id": "suite_bot", "task_id": "success_flow"},
        )
        self.assertEqual(claim_resp.status_code, 200)
        claim_data = json.loads(claim_resp.data)
        self.assertTrue(claim_data["success"])

        complete_resp = self.client.post(
            "/api/external/pool/claim-complete",
            headers=self._auth_headers(),
            json={
                "account_id": claim_data["data"]["account_id"],
                "claim_token": claim_data["data"]["claim_token"],
                "caller_id": "suite_bot",
                "task_id": "success_flow",
                "result": "success",
                "detail": "manual suite success",
            },
        )
        self.assertEqual(complete_resp.status_code, 200)
        complete_data = json.loads(complete_resp.data)
        self.assertTrue(complete_data["success"])
        self.assertEqual(complete_data["data"]["pool_status"], "used")

        conn = self.create_conn()
        try:
            row = conn.execute(
                "SELECT pool_status, success_count, fail_count FROM accounts WHERE id = ?",
                (claim_data["data"]["account_id"],),
            ).fetchone()
            self.assertEqual(row["pool_status"], "used")
            self.assertEqual(row["success_count"], 1)
            self.assertEqual(row["fail_count"], 0)
        finally:
            conn.close()

    def test_claim_complete_failure_changes_status_to_cooldown(self):
        self._make_pool_account()

        claim_resp = self.client.post(
            "/api/external/pool/claim-random",
            headers=self._auth_headers(),
            json={"caller_id": "suite_bot", "task_id": "cooldown_flow"},
        )
        self.assertEqual(claim_resp.status_code, 200)
        claim_data = json.loads(claim_resp.data)
        self.assertTrue(claim_data["success"])

        complete_resp = self.client.post(
            "/api/external/pool/claim-complete",
            headers=self._auth_headers(),
            json={
                "account_id": claim_data["data"]["account_id"],
                "claim_token": claim_data["data"]["claim_token"],
                "caller_id": "suite_bot",
                "task_id": "cooldown_flow",
                "result": "verification_timeout",
                "detail": "manual suite timeout",
            },
        )
        self.assertEqual(complete_resp.status_code, 200)
        complete_data = json.loads(complete_resp.data)
        self.assertTrue(complete_data["success"])
        self.assertEqual(complete_data["data"]["pool_status"], "cooldown")

        conn = self.create_conn()
        try:
            row = conn.execute(
                "SELECT pool_status, success_count, fail_count, last_result FROM accounts WHERE id = ?",
                (claim_data["data"]["account_id"],),
            ).fetchone()
            self.assertEqual(row["pool_status"], "cooldown")
            self.assertEqual(row["success_count"], 0)
            self.assertEqual(row["fail_count"], 1)
            self.assertEqual(row["last_result"], "verification_timeout")
        finally:
            conn.close()

    def test_multiple_consecutive_claims_do_not_repeat_accounts(self):
        provider = f"suiteprov_{uuid.uuid4().hex}"
        created_ids = []
        for _ in range(3):
            created = self._make_pool_account(provider=provider)
            created_ids.append(created["id"])

        claimed_ids = []
        claimed_tokens = []
        for idx in range(3):
            resp = self.client.post(
                "/api/external/pool/claim-random",
                headers=self._auth_headers(),
                json={
                    "caller_id": "suite_bot",
                    "task_id": f"batch_claim_{idx}",
                    "provider": provider,
                },
            )
            self.assertEqual(resp.status_code, 200)
            data = json.loads(resp.data)
            self.assertTrue(data["success"])
            claimed_ids.append(data["data"]["account_id"])
            claimed_tokens.append(
                (
                    data["data"]["account_id"],
                    data["data"]["claim_token"],
                    f"batch_claim_{idx}",
                )
            )

        self.assertEqual(len(claimed_ids), len(set(claimed_ids)))
        self.assertTrue(set(claimed_ids).issubset(set(created_ids)))

        for account_id, claim_token, task_id in claimed_tokens:
            release_resp = self.client.post(
                "/api/external/pool/claim-release",
                headers=self._auth_headers(),
                json={
                    "account_id": account_id,
                    "claim_token": claim_token,
                    "caller_id": "suite_bot",
                    "task_id": task_id,
                    "reason": "suite cleanup",
                },
            )
            self.assertEqual(release_resp.status_code, 200)
            release_data = json.loads(release_resp.data)
            self.assertTrue(release_data["success"])
            self.assertEqual(release_data["data"]["pool_status"], "available")

        conn = self.create_conn()
        try:
            placeholders = ",".join(["?"] * len(created_ids))
            rows = conn.execute(
                f"SELECT id, pool_status, claim_token FROM accounts WHERE id IN ({placeholders})",
                created_ids,
            ).fetchall()
            self.assertEqual(len(rows), 3)
            for row in rows:
                self.assertEqual(row["pool_status"], "available")
                self.assertIsNone(row["claim_token"])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
