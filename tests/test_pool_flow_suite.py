import json
import unittest
import uuid

from tests._import_app import import_web_app_module


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
            from outlook_web.db import get_db
            from outlook_web.repositories import settings as settings_repo

            db = get_db()
            db.execute("DELETE FROM external_api_keys")
            db.execute("DELETE FROM external_api_rate_limits")
            db.commit()
            settings_repo.set_setting("external_api_key", "abc123")
            settings_repo.set_setting("pool_external_enabled", "true")
            settings_repo.set_setting("external_api_public_mode", "false")
            settings_repo.set_setting("external_api_ip_whitelist", "[]")
            settings_repo.set_setting("external_api_rate_limit_per_minute", "60")
            settings_repo.set_setting("external_api_disable_pool_claim_random", "false")
            settings_repo.set_setting(
                "external_api_disable_pool_claim_release", "false"
            )
            settings_repo.set_setting(
                "external_api_disable_pool_claim_complete", "false"
            )
            settings_repo.set_setting("external_api_disable_pool_stats", "false")

    @staticmethod
    def _auth_headers():
        return {"X-API-Key": "abc123"}

    def _make_pool_account(
        self, *, provider: str = "outlook", pool_status: str = "available"
    ) -> dict:
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
        # PR#27: success → cooldown（而非旧版的 used）
        self.assertEqual(complete_data["data"]["pool_status"], "cooldown")

        conn = self.create_conn()
        try:
            row = conn.execute(
                "SELECT pool_status, success_count, fail_count FROM accounts WHERE id = ?",
                (claim_data["data"]["account_id"],),
            ).fetchone()
            self.assertEqual(row["pool_status"], "cooldown")
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

    def test_claim_response_includes_email_domain_and_claimed_at(self):
        """PR#27: claim-random 响应应包含 email_domain 和 claimed_at 字段。"""
        self._make_pool_account()
        resp = self.client.post(
            "/api/external/pool/claim-random",
            headers=self._auth_headers(),
            json={"caller_id": "domain_bot", "task_id": "domain_check"},
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(data["success"])
        self.assertIn("email_domain", data["data"])
        self.assertIn("claimed_at", data["data"])
        self.assertIsNotNone(data["data"]["claimed_at"])

    def test_claim_with_project_key_prevents_same_project_reuse(self):
        """PR#27: 同 caller_id + project_key 下，同一账号不应被再次领取。"""
        provider = f"proj_{uuid.uuid4().hex}"
        acct = self._make_pool_account(provider=provider)
        account_id = acct["id"]

        # 第一次领取
        resp1 = self.client.post(
            "/api/external/pool/claim-random",
            headers=self._auth_headers(),
            json={
                "caller_id": "proj_bot",
                "task_id": "proj_task_1",
                "provider": provider,
                "project_key": "project_alpha",
            },
        )
        self.assertEqual(resp1.status_code, 200)
        data1 = json.loads(resp1.data)
        self.assertTrue(data1["success"])
        self.assertEqual(data1["data"]["account_id"], account_id)

        # 标记完成（让账号回到 cooldown 状态）
        self.client.post(
            "/api/external/pool/claim-complete",
            headers=self._auth_headers(),
            json={
                "account_id": account_id,
                "claim_token": data1["data"]["claim_token"],
                "caller_id": "proj_bot",
                "task_id": "proj_task_1",
                "result": "success",
            },
        )

        # 手动把账号调回 available（绕过 cooldown，模拟恢复）
        conn = self.create_conn()
        try:
            conn.execute(
                "UPDATE accounts SET pool_status = 'available' WHERE id = ?",
                (account_id,),
            )
            conn.commit()
        finally:
            conn.close()

        # 同 project_key 再次领取 → 应该拿不到（同 caller+project 的账号已被排除）
        resp2 = self.client.post(
            "/api/external/pool/claim-random",
            headers=self._auth_headers(),
            json={
                "caller_id": "proj_bot",
                "task_id": "proj_task_2",
                "provider": provider,
                "project_key": "project_alpha",
            },
        )
        self.assertEqual(resp2.status_code, 200)
        data2 = json.loads(resp2.data)
        # 池里只有这一个账号，同 project 应排除，预期 no_available_account
        self.assertFalse(data2["success"])
        self.assertEqual(data2["code"], "NO_AVAILABLE_ACCOUNT")

    def test_claim_with_different_project_key_allows_reuse(self):
        """PR#27: 不同 project_key 下，同一账号可以被再次领取。"""
        provider = f"proj2_{uuid.uuid4().hex}"
        acct = self._make_pool_account(provider=provider)
        account_id = acct["id"]

        # 第一次领取（project_beta）
        resp1 = self.client.post(
            "/api/external/pool/claim-random",
            headers=self._auth_headers(),
            json={
                "caller_id": "proj_bot",
                "task_id": "pb_task_1",
                "provider": provider,
                "project_key": "project_beta",
            },
        )
        self.assertEqual(resp1.status_code, 200)
        data1 = json.loads(resp1.data)
        self.assertTrue(data1["success"])

        # 完成 + 恢复 available
        self.client.post(
            "/api/external/pool/claim-complete",
            headers=self._auth_headers(),
            json={
                "account_id": account_id,
                "claim_token": data1["data"]["claim_token"],
                "caller_id": "proj_bot",
                "task_id": "pb_task_1",
                "result": "success",
            },
        )
        conn = self.create_conn()
        try:
            conn.execute(
                "UPDATE accounts SET pool_status = 'available' WHERE id = ?",
                (account_id,),
            )
            conn.commit()
        finally:
            conn.close()

        # 第二次领取（project_gamma，不同 project_key）→ 应该能拿到
        resp2 = self.client.post(
            "/api/external/pool/claim-random",
            headers=self._auth_headers(),
            json={
                "caller_id": "proj_bot",
                "task_id": "pg_task_1",
                "provider": provider,
                "project_key": "project_gamma",
            },
        )
        self.assertEqual(resp2.status_code, 200)
        data2 = json.loads(resp2.data)
        self.assertTrue(data2["success"])
        self.assertEqual(data2["data"]["account_id"], account_id)

        # 清理
        self.client.post(
            "/api/external/pool/claim-release",
            headers=self._auth_headers(),
            json={
                "account_id": account_id,
                "claim_token": data2["data"]["claim_token"],
                "caller_id": "proj_bot",
                "task_id": "pg_task_1",
            },
        )


if __name__ == "__main__":
    unittest.main()
