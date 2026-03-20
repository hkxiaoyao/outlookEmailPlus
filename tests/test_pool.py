import json
import unittest

from tests._import_app import import_web_app_module


class PoolRepositoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = import_web_app_module()
        from outlook_web.db import create_sqlite_connection
        from outlook_web.repositories import pool as pool_repo

        cls.pool_repo = pool_repo
        cls.create_conn = staticmethod(lambda: create_sqlite_connection())

    def _make_account(self, conn, email_suffix="", pool_status="available"):
        import secrets

        email = f"pool_test_{secrets.token_hex(4)}{email_suffix}@example.com"
        conn.execute(
            """
            INSERT INTO accounts (email, client_id, refresh_token, status, pool_status)
            VALUES (?, 'test_client', 'test_token', 'active', ?)
            """,
            (email, pool_status),
        )
        conn.commit()
        row = conn.execute("SELECT id FROM accounts WHERE email = ?", (email,)).fetchone()
        return row["id"]

    def test_claim_and_complete_success(self):
        conn = self.create_conn()
        try:
            self._make_account(conn)
            result = self.pool_repo.claim_atomic(conn, caller_id="reg_bot", task_id="task_001", lease_seconds=60)
            self.assertIsNotNone(result)
            self.assertTrue(result["claim_token"].startswith("clm_"))
            claimed_id = result["id"]

            row = conn.execute(
                "SELECT pool_status, claim_token FROM accounts WHERE id = ?",
                (claimed_id,),
            ).fetchone()
            self.assertEqual(row["pool_status"], "claimed")

            new_status = self.pool_repo.complete(
                conn,
                account_id=claimed_id,
                claim_token=result["claim_token"],
                caller_id="reg_bot",
                task_id="task_001",
                result="success",
                detail="注册成功",
            )
            self.assertEqual(new_status, "used")

            row2 = conn.execute(
                "SELECT pool_status, success_count FROM accounts WHERE id = ?",
                (claimed_id,),
            ).fetchone()
            self.assertEqual(row2["pool_status"], "used")
            self.assertEqual(row2["success_count"], 1)
        finally:
            conn.close()

    def test_claim_and_release(self):
        conn = self.create_conn()
        try:
            account_id = self._make_account(conn)
            result = self.pool_repo.claim_atomic(conn, caller_id="reg_bot", task_id="task_002", lease_seconds=60)
            self.assertIsNotNone(result)

            self.pool_repo.release(
                conn,
                account_id=account_id,
                claim_token=result["claim_token"],
                caller_id="reg_bot",
                task_id="task_002",
                reason="任务取消",
            )

            row = conn.execute(
                "SELECT pool_status, claim_token FROM accounts WHERE id = ?",
                (account_id,),
            ).fetchone()
            self.assertEqual(row["pool_status"], "available")
            self.assertIsNone(row["claim_token"])
        finally:
            conn.close()

    def test_claim_result_maps_to_cooldown(self):
        conn = self.create_conn()
        try:
            self._make_account(conn)
            result = self.pool_repo.claim_atomic(conn, caller_id="reg_bot", task_id="task_003", lease_seconds=60)
            self.assertIsNotNone(result)
            claimed_id = result["id"]

            before = conn.execute("SELECT fail_count FROM accounts WHERE id = ?", (claimed_id,)).fetchone()["fail_count"]

            new_status = self.pool_repo.complete(
                conn,
                account_id=claimed_id,
                claim_token=result["claim_token"],
                caller_id="reg_bot",
                task_id="task_003",
                result="verification_timeout",
                detail=None,
            )
            self.assertEqual(new_status, "cooldown")

            row = conn.execute(
                "SELECT pool_status, fail_count FROM accounts WHERE id = ?",
                (claimed_id,),
            ).fetchone()
            self.assertEqual(row["pool_status"], "cooldown")
            self.assertEqual(row["fail_count"], before + 1)
        finally:
            conn.close()

    def test_no_available_account_returns_none(self):
        conn = self.create_conn()
        try:
            result = self.pool_repo.claim_atomic(
                conn,
                caller_id="reg_bot",
                task_id="task_none",
                lease_seconds=60,
                provider="nonexistent_provider_xyz",
            )
            self.assertIsNone(result)
        finally:
            conn.close()

    def test_expire_stale_claims(self):
        conn = self.create_conn()
        try:
            account_id = self._make_account(conn)
            conn.execute(
                """
                UPDATE accounts SET
                    pool_status = 'claimed',
                    claimed_by = 'reg_bot:task_exp',
                    claim_token = 'clm_expired_test',
                    lease_expires_at = '2000-01-01T00:00:00Z',
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                (account_id,),
            )
            conn.commit()

            expired_count = self.pool_repo.expire_stale_claims(conn)
            self.assertGreaterEqual(expired_count, 1)

            row = conn.execute("SELECT pool_status FROM accounts WHERE id = ?", (account_id,)).fetchone()
            self.assertEqual(row["pool_status"], "cooldown")
        finally:
            conn.close()

    def test_recover_cooldown(self):
        conn = self.create_conn()
        try:
            account_id = self._make_account(conn, pool_status="cooldown")
            conn.execute(
                "UPDATE accounts SET updated_at = '2000-01-01T00:00:00' WHERE id = ?",
                (account_id,),
            )
            conn.commit()

            recovered = self.pool_repo.recover_cooldown(conn, cooldown_seconds=3600)
            self.assertGreaterEqual(recovered, 1)

            row = conn.execute("SELECT pool_status FROM accounts WHERE id = ?", (account_id,)).fetchone()
            self.assertEqual(row["pool_status"], "available")
        finally:
            conn.close()

    def test_get_stats_shape(self):
        conn = self.create_conn()
        try:
            stats = self.pool_repo.get_stats(conn)
            self.assertIn("pool_counts", stats)
            self.assertNotIn("today", stats)
            self.assertNotIn("overall", stats)
            self.assertIn("available", stats["pool_counts"])
            self.assertIn("claimed", stats["pool_counts"])
        finally:
            conn.close()

    def test_get_stats_ignores_accounts_without_pool_status(self):
        conn = self.create_conn()
        try:
            import secrets

            email = f"pool_null_status_{secrets.token_hex(4)}@example.com"
            conn.execute(
                """
                INSERT INTO accounts (email, client_id, refresh_token, status, pool_status)
                VALUES (?, 'test_client', 'test_token', 'active', NULL)
                """,
                (email,),
            )
            conn.commit()

            stats = self.pool_repo.get_stats(conn)

            self.assertEqual(
                set(stats["pool_counts"].keys()),
                {"available", "claimed", "used", "cooldown", "frozen", "retired"},
            )
            self.assertNotIn("not_in_pool", stats["pool_counts"])
        finally:
            conn.close()

    def test_complete_network_error_returns_available_and_increments_fail_count(self):
        conn = self.create_conn()
        try:
            account_id = self._make_account(conn)
            result = self.pool_repo.claim_atomic(conn, caller_id="reg_bot", task_id="task_ne", lease_seconds=60)
            self.assertIsNotNone(result)
            claimed_id = result["id"]

            before = conn.execute("SELECT fail_count FROM accounts WHERE id = ?", (claimed_id,)).fetchone()["fail_count"]

            new_status = self.pool_repo.complete(
                conn,
                account_id=claimed_id,
                claim_token=result["claim_token"],
                caller_id="reg_bot",
                task_id="task_ne",
                result="network_error",
                detail=None,
            )
            self.assertEqual(new_status, "available")

            row = conn.execute(
                "SELECT pool_status, fail_count FROM accounts WHERE id = ?",
                (claimed_id,),
            ).fetchone()
            self.assertEqual(row["pool_status"], "available")
            self.assertEqual(row["fail_count"], before + 1)
        finally:
            conn.close()

    def test_complete_provider_blocked_returns_frozen(self):
        conn = self.create_conn()
        try:
            account_id = self._make_account(conn)
            result = self.pool_repo.claim_atomic(conn, caller_id="reg_bot", task_id="task_pb", lease_seconds=60)
            self.assertIsNotNone(result)
            claimed_id = result["id"]

            before = conn.execute("SELECT fail_count FROM accounts WHERE id = ?", (claimed_id,)).fetchone()["fail_count"]

            new_status = self.pool_repo.complete(
                conn,
                account_id=claimed_id,
                claim_token=result["claim_token"],
                caller_id="reg_bot",
                task_id="task_pb",
                result="provider_blocked",
                detail="IP 被封",
            )
            self.assertEqual(new_status, "frozen")

            row = conn.execute(
                "SELECT pool_status, fail_count FROM accounts WHERE id = ?",
                (claimed_id,),
            ).fetchone()
            self.assertEqual(row["pool_status"], "frozen")
            self.assertEqual(row["fail_count"], before + 1)
        finally:
            conn.close()

    def test_complete_credential_invalid_returns_retired(self):
        conn = self.create_conn()
        try:
            account_id = self._make_account(conn)
            result = self.pool_repo.claim_atomic(conn, caller_id="reg_bot", task_id="task_ci", lease_seconds=60)
            self.assertIsNotNone(result)
            claimed_id = result["id"]

            before = conn.execute("SELECT fail_count FROM accounts WHERE id = ?", (claimed_id,)).fetchone()["fail_count"]

            new_status = self.pool_repo.complete(
                conn,
                account_id=claimed_id,
                claim_token=result["claim_token"],
                caller_id="reg_bot",
                task_id="task_ci",
                result="credential_invalid",
                detail=None,
            )
            self.assertEqual(new_status, "retired")

            row = conn.execute(
                "SELECT pool_status, fail_count FROM accounts WHERE id = ?",
                (claimed_id,),
            ).fetchone()
            self.assertEqual(row["pool_status"], "retired")
            self.assertEqual(row["fail_count"], before + 1)
        finally:
            conn.close()

    def test_claim_log_written_on_claim(self):
        conn = self.create_conn()
        try:
            account_id = self._make_account(conn)
            result = self.pool_repo.claim_atomic(conn, caller_id="log_bot", task_id="log_task_001", lease_seconds=60)
            self.assertIsNotNone(result)
            claimed_id = result["id"]
            log_row = conn.execute(
                """
                SELECT * FROM account_claim_logs
                WHERE account_id = ? AND action = 'claim'
                ORDER BY created_at DESC LIMIT 1
                """,
                (claimed_id,),
            ).fetchone()
            self.assertIsNotNone(log_row)
            self.assertEqual(log_row["caller_id"], "log_bot")
            self.assertEqual(log_row["task_id"], "log_task_001")
            self.assertEqual(log_row["claim_token"], result["claim_token"])
            self.assertIsNone(log_row["result"])
        finally:
            conn.close()

    def test_complete_log_written_on_complete(self):
        conn = self.create_conn()
        try:
            account_id = self._make_account(conn)
            result = self.pool_repo.claim_atomic(conn, caller_id="log_bot", task_id="log_task_002", lease_seconds=60)
            self.assertIsNotNone(result)
            claimed_id = result["id"]
            self.pool_repo.complete(
                conn,
                account_id=claimed_id,
                claim_token=result["claim_token"],
                caller_id="log_bot",
                task_id="log_task_002",
                result="success",
                detail="ok",
            )
            log_row = conn.execute(
                """
                SELECT * FROM account_claim_logs
                WHERE account_id = ? AND action = 'complete'
                ORDER BY created_at DESC LIMIT 1
                """,
                (claimed_id,),
            ).fetchone()
            self.assertIsNotNone(log_row)
            self.assertEqual(log_row["result"], "success")
            self.assertEqual(log_row["detail"], "ok")
        finally:
            conn.close()

    def test_expire_stale_claims_increments_fail_count(self):
        conn = self.create_conn()
        try:
            account_id = self._make_account(conn)
            conn.execute(
                """
                UPDATE accounts SET
                    pool_status = 'claimed',
                    claimed_by = 'bot:task_exp2',
                    claim_token = 'clm_exp2_test',
                    lease_expires_at = '2000-01-01T00:00:00Z',
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                (account_id,),
            )
            conn.commit()

            self.pool_repo.expire_stale_claims(conn)

            row = conn.execute(
                "SELECT pool_status, fail_count FROM accounts WHERE id = ?",
                (account_id,),
            ).fetchone()
            self.assertEqual(row["pool_status"], "cooldown")
            self.assertEqual(row["fail_count"], 1)
        finally:
            conn.close()

    def test_exclude_recent_minutes_skips_recently_claimed_account(self):
        import time as _time

        conn = self.create_conn()
        try:
            account_id = self._make_account(conn)
            result1 = self.pool_repo.claim_atomic(conn, caller_id="bot", task_id="excl_t1", lease_seconds=60)
            self.assertIsNotNone(result1)
            claimed_id = result1["id"]
            self.pool_repo.release(
                conn,
                account_id=claimed_id,
                claim_token=result1["claim_token"],
                caller_id="bot",
                task_id="excl_t1",
                reason="test",
            )

            conn.execute(
                "UPDATE accounts SET pool_status = 'used' WHERE pool_status = 'available' AND id != ?",
                (claimed_id,),
            )
            conn.commit()

            result2 = self.pool_repo.claim_atomic(
                conn,
                caller_id="bot",
                task_id="excl_t2",
                lease_seconds=60,
                exclude_recent_minutes=60,
            )
            self.assertIsNone(result2)
        finally:
            conn.close()

    def test_multiple_claim_tokens_are_unique(self):
        conn = self.create_conn()
        try:
            tokens = set()
            for i in range(5):
                account_id = self._make_account(conn)
                result = self.pool_repo.claim_atomic(conn, caller_id="bot", task_id=f"uniq_t{i}", lease_seconds=60)
                self.assertIsNotNone(result)
                tokens.add(result["claim_token"])
                self.pool_repo.complete(
                    conn,
                    account_id=result["id"],
                    claim_token=result["claim_token"],
                    caller_id="bot",
                    task_id=f"uniq_t{i}",
                    result="success",
                    detail=None,
                )
            self.assertEqual(len(tokens), 5, "5 次 claim 应产生 5 个不同的 token")
        finally:
            conn.close()

    def test_concurrent_claim_unique(self):
        """并发场景：多个 claim 不应领到同一个邮箱（P0-6）"""
        conn = self.create_conn()
        try:
            import secrets

            iso_provider = f"conc_iso_{secrets.token_hex(4)}"
            email = f"pool_conc_{secrets.token_hex(4)}@example.com"
            conn.execute(
                """
                INSERT INTO accounts (email, client_id, refresh_token, status, pool_status, provider)
                VALUES (?, 'test_client', 'test_token', 'active', 'available', ?)
                """,
                (email, iso_provider),
            )
            conn.commit()

            r1 = self.pool_repo.claim_atomic(
                conn,
                caller_id="bot_a",
                task_id="conc_1",
                lease_seconds=60,
                provider=iso_provider,
            )
            self.assertIsNotNone(r1)

            r2 = self.pool_repo.claim_atomic(
                conn,
                caller_id="bot_b",
                task_id="conc_2",
                lease_seconds=60,
                provider=iso_provider,
            )
            self.assertIsNone(r2, "只有 1 个可用邮箱时，第二个 claim 应返回 None")
        finally:
            conn.close()

    def test_release_log_written_on_release(self):
        """release 动作应写入 claim log（P0-6）"""
        conn = self.create_conn()
        try:
            account_id = self._make_account(conn)
            result = self.pool_repo.claim_atomic(conn, caller_id="log_bot", task_id="log_rel_001", lease_seconds=60)
            self.assertIsNotNone(result)
            claimed_id = result["id"]

            self.pool_repo.release(
                conn,
                account_id=claimed_id,
                claim_token=result["claim_token"],
                caller_id="log_bot",
                task_id="log_rel_001",
                reason="任务取消",
            )

            log_row = conn.execute(
                """
                SELECT * FROM account_claim_logs
                WHERE account_id = ? AND action = 'release'
                ORDER BY created_at DESC LIMIT 1
                """,
                (claimed_id,),
            ).fetchone()
            self.assertIsNotNone(log_row, "release 应写入 claim log")
            self.assertEqual(log_row["caller_id"], "log_bot")
            self.assertEqual(log_row["task_id"], "log_rel_001")
            self.assertEqual(log_row["claim_token"], result["claim_token"])
            self.assertEqual(log_row["result"], "manual_release")
            self.assertEqual(log_row["detail"], "任务取消")
        finally:
            conn.close()

    def test_expire_log_written_on_expire(self):
        """expire（租约超时）动作应写入 claim log（P0-6）"""
        conn = self.create_conn()
        try:
            account_id = self._make_account(conn)
            token = "clm_expire_log_test"
            conn.execute(
                """
                UPDATE accounts SET
                    pool_status = 'claimed',
                    claimed_by = 'exp_bot:exp_task',
                    claim_token = ?,
                    lease_expires_at = '2000-01-01T00:00:00Z',
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                (token, account_id),
            )
            conn.commit()

            self.pool_repo.expire_stale_claims(conn)

            log_row = conn.execute(
                """
                SELECT * FROM account_claim_logs
                WHERE account_id = ? AND action = 'expire'
                ORDER BY created_at DESC LIMIT 1
                """,
                (account_id,),
            ).fetchone()
            self.assertIsNotNone(log_row, "expire 应写入 claim log")
            self.assertEqual(log_row["caller_id"], "exp_bot")
            self.assertEqual(log_row["task_id"], "exp_task")
            self.assertEqual(log_row["claim_token"], token)
            self.assertEqual(log_row["result"], "lease_expired")
        finally:
            conn.close()

    def test_expire_affects_stats(self):
        """expire 后 stats 中 cooldown 应增加、claimed 应减少（P0-6）"""
        conn = self.create_conn()
        try:
            account_id = self._make_account(conn)
            conn.execute(
                """
                UPDATE accounts SET
                    pool_status = 'claimed',
                    claimed_by = 'stat_bot:stat_task',
                    claim_token = 'clm_stat_test',
                    lease_expires_at = '2000-01-01T00:00:00Z',
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                (account_id,),
            )
            conn.commit()

            stats_before = self.pool_repo.get_stats(conn)
            claimed_before = stats_before["pool_counts"]["claimed"]
            cooldown_before = stats_before["pool_counts"]["cooldown"]

            self.pool_repo.expire_stale_claims(conn)

            stats_after = self.pool_repo.get_stats(conn)
            claimed_after = stats_after["pool_counts"]["claimed"]
            cooldown_after = stats_after["pool_counts"]["cooldown"]

            self.assertLess(claimed_after, claimed_before, "expire 后 claimed 数量应减少")
            self.assertGreater(cooldown_after, cooldown_before, "expire 后 cooldown 数量应增加")
        finally:
            conn.close()


class PoolServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = import_web_app_module()
        from outlook_web.db import create_sqlite_connection
        from outlook_web.services import pool as pool_service

        cls.pool_service = pool_service
        cls.create_conn = staticmethod(lambda: create_sqlite_connection())

    def _make_account(self, pool_status="available"):
        import secrets

        conn = self.create_conn()
        try:
            email = f"svc_test_{secrets.token_hex(4)}@example.com"
            conn.execute(
                """
                INSERT INTO accounts (email, client_id, refresh_token, status, pool_status)
                VALUES (?, 'test_client', 'test_token', 'active', ?)
                """,
                (email, pool_status),
            )
            conn.commit()
            row = conn.execute("SELECT id FROM accounts WHERE email = ?", (email,)).fetchone()
            return row["id"]
        finally:
            conn.close()

    def test_claim_random_returns_account(self):
        self._make_account()
        result = self.pool_service.claim_random(caller_id="bot", task_id="t1")
        self.assertIn("claim_token", result)
        self.assertIn("lease_expires_at", result)

    def test_claim_random_caller_id_validation(self):
        with self.assertRaises(self.pool_service.PoolServiceError) as ctx:
            self.pool_service.claim_random(caller_id="", task_id="t1")
        self.assertEqual(ctx.exception.error_code, "caller_id_empty")

    def test_claim_random_no_account_200(self):
        with self.assertRaises(self.pool_service.PoolServiceError) as ctx:
            self.pool_service.claim_random(
                caller_id="bot",
                task_id="t_no_acct",
                provider="provider_that_does_not_exist_xyz",
            )
        self.assertEqual(ctx.exception.http_status, 200)
        self.assertEqual(ctx.exception.error_code, "no_available_account")

    def test_complete_claim_invalid_result(self):
        account_id = self._make_account()
        with self.assertRaises(self.pool_service.PoolServiceError) as ctx:
            self.pool_service.complete_claim(
                account_id=account_id,
                claim_token="clm_fake",
                caller_id="bot",
                task_id="t2",
                result="not_a_valid_result",
            )
        self.assertEqual(ctx.exception.error_code, "invalid_result")

    def test_release_claim_token_mismatch(self):
        self._make_account()
        result = self.pool_service.claim_random(caller_id="bot", task_id="t_rel")
        claimed_id = result["id"]
        with self.assertRaises(self.pool_service.PoolServiceError) as ctx:
            self.pool_service.release_claim(
                account_id=claimed_id,
                claim_token="clm_wrong_token",
                caller_id="bot",
                task_id="t_rel",
            )
        self.assertEqual(ctx.exception.error_code, "token_mismatch")

    def test_release_account_not_found(self):
        """release 对不存在的 account_id 应返回 account_not_found (400)"""
        with self.assertRaises(self.pool_service.PoolServiceError) as ctx:
            self.pool_service.release_claim(
                account_id=999999,
                claim_token="clm_any",
                caller_id="bot",
                task_id="t_release_anf",
            )
        self.assertEqual(ctx.exception.error_code, "account_not_found")
        self.assertEqual(ctx.exception.http_status, 400)

    def test_release_not_claimed(self):
        """release 对 pool_status != claimed 的账号应返回 not_claimed (409)"""
        account_id = self._make_account(pool_status="available")
        with self.assertRaises(self.pool_service.PoolServiceError) as ctx:
            self.pool_service.release_claim(
                account_id=account_id,
                claim_token="clm_any",
                caller_id="bot",
                task_id="t_release_nc",
            )
        self.assertEqual(ctx.exception.error_code, "not_claimed")
        self.assertEqual(ctx.exception.http_status, 409)

    def test_release_caller_mismatch(self):
        """release 使用正确 token 但不同 caller_id:task_id 应返回 caller_mismatch (403)"""
        self._make_account()
        result = self.pool_service.claim_random(caller_id="bot_a", task_id="t_cm")
        claimed_id = result["id"]
        with self.assertRaises(self.pool_service.PoolServiceError) as ctx:
            self.pool_service.release_claim(
                account_id=claimed_id,
                claim_token=result["claim_token"],
                caller_id="bot_b",
                task_id="t_cm",
            )
        self.assertEqual(ctx.exception.error_code, "caller_mismatch")
        self.assertEqual(ctx.exception.http_status, 403)

    def test_complete_account_not_found(self):
        """complete 对不存在的 account_id 应返回 account_not_found (400)"""
        with self.assertRaises(self.pool_service.PoolServiceError) as ctx:
            self.pool_service.complete_claim(
                account_id=999999,
                claim_token="clm_any",
                caller_id="bot",
                task_id="t_comp_anf",
                result="success",
            )
        self.assertEqual(ctx.exception.error_code, "account_not_found")
        self.assertEqual(ctx.exception.http_status, 400)

    def test_complete_not_claimed(self):
        """complete 对 pool_status != claimed 的账号应返回 not_claimed (409)"""
        account_id = self._make_account(pool_status="available")
        with self.assertRaises(self.pool_service.PoolServiceError) as ctx:
            self.pool_service.complete_claim(
                account_id=account_id,
                claim_token="clm_any",
                caller_id="bot",
                task_id="t_comp_nc",
                result="success",
            )
        self.assertEqual(ctx.exception.error_code, "not_claimed")
        self.assertEqual(ctx.exception.http_status, 409)

    def test_complete_token_mismatch(self):
        """complete 使用错误 token 应返回 token_mismatch (403)"""
        self._make_account()
        result = self.pool_service.claim_random(caller_id="bot", task_id="t_comp_tm")
        claimed_id = result["id"]
        with self.assertRaises(self.pool_service.PoolServiceError) as ctx:
            self.pool_service.complete_claim(
                account_id=claimed_id,
                claim_token="clm_wrong_token",
                caller_id="bot",
                task_id="t_comp_tm",
                result="success",
            )
        self.assertEqual(ctx.exception.error_code, "token_mismatch")
        self.assertEqual(ctx.exception.http_status, 403)

    def test_complete_caller_mismatch(self):
        """complete 使用正确 token 但不同 caller_id:task_id 应返回 caller_mismatch (403)"""
        self._make_account()
        result = self.pool_service.claim_random(caller_id="bot_a", task_id="t_comp_cm")
        claimed_id = result["id"]
        with self.assertRaises(self.pool_service.PoolServiceError) as ctx:
            self.pool_service.complete_claim(
                account_id=claimed_id,
                claim_token=result["claim_token"],
                caller_id="bot_b",
                task_id="t_comp_cm",
                result="success",
            )
        self.assertEqual(ctx.exception.error_code, "caller_mismatch")
        self.assertEqual(ctx.exception.http_status, 403)

    def test_ownership_check_order_release(self):
        """验证 release 校验顺序：not_claimed 应优先于 token_mismatch（TD §5.4）"""
        account_id = self._make_account(pool_status="available")
        with self.assertRaises(self.pool_service.PoolServiceError) as ctx:
            self.pool_service.release_claim(
                account_id=account_id,
                claim_token="clm_wrong",
                caller_id="bot_wrong",
                task_id="t_wrong",
            )
        self.assertEqual(ctx.exception.error_code, "not_claimed")

    def test_ownership_check_order_complete(self):
        """验证 complete 校验顺序：token_mismatch 应优先于 caller_mismatch（TD §5.4）"""
        self._make_account()
        result = self.pool_service.claim_random(caller_id="bot_a", task_id="t_order")
        claimed_id = result["id"]
        with self.assertRaises(self.pool_service.PoolServiceError) as ctx:
            self.pool_service.complete_claim(
                account_id=claimed_id,
                claim_token="clm_wrong",
                caller_id="bot_b",
                task_id="t_order",
                result="success",
            )
        self.assertEqual(ctx.exception.error_code, "token_mismatch")


if __name__ == "__main__":
    unittest.main()
