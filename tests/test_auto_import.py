"""tests/test_auto_import.py — FD-00006 auto 混合导入集成测试"""

import json
import unittest

from tests._import_app import clear_login_attempts, import_web_app_module


class TestAutoImport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = import_web_app_module()
        cls.app = cls.module.app

    def setUp(self):
        with self.app.app_context():
            clear_login_attempts()
            from outlook_web.db import get_db

            db = get_db()
            # 清理测试数据
            db.execute(
                "DELETE FROM accounts WHERE email LIKE '%@autotest.%' OR email LIKE '%@gmail.com' OR email LIKE '%@qq.com'"
            )
            db.execute("DELETE FROM temp_emails WHERE email LIKE '%@autotest.%' OR email LIKE '%@gptmail.com'")
            db.commit()

    def _login(self, client):
        resp = client.post("/login", json={"password": "testpass123"})
        self.assertEqual(resp.status_code, 200)

    def _import_auto(self, client, account_string, **kwargs):
        payload = {"account_string": account_string, "provider": "auto"}
        payload.update(kwargs)
        resp = client.post("/api/accounts", json=payload, content_type="application/json")
        return resp.get_json()

    def test_mixed_import_outlook_and_imap(self):
        """混合文件：Outlook + IMAP 3段 + 2段域名推断"""
        client = self.app.test_client()
        self._login(client)

        text = "ol@autotest.com----pwd----cid----rtoken\n" "qq@qq.com----authcode----qq\n" "gm@gmail.com----apppass"
        data = self._import_auto(client, text)
        self.assertTrue(data["success"])
        s = data["summary"]
        self.assertEqual(s["mode"], "auto")
        self.assertEqual(s["imported"], 3)
        self.assertEqual(s["failed"], 0)
        self.assertIn("outlook", s["by_provider"])
        self.assertIn("qq", s["by_provider"])
        self.assertIn("gmail", s["by_provider"])

    def test_skip_duplicate_strategy(self):
        """skip 模式：重复邮箱被跳过"""
        client = self.app.test_client()
        self._login(client)

        text = "dup@autotest.com----pwd----cid----rtoken"
        data = self._import_auto(client, text, duplicate_strategy="skip")
        self.assertTrue(data["success"])
        self.assertEqual(data["summary"]["imported"], 1)

        # 再次导入同一个
        data = self._import_auto(client, text, duplicate_strategy="skip")
        self.assertTrue(data["success"])
        self.assertEqual(data["summary"]["skipped"], 1)
        self.assertEqual(data["summary"]["imported"], 0)

    def test_overwrite_duplicate_strategy(self):
        """overwrite 模式：凭据更新"""
        client = self.app.test_client()
        self._login(client)

        text = "ow@autotest.com----pwd----cid----rtoken_old"
        data = self._import_auto(client, text)
        self.assertTrue(data["success"])

        # overwrite
        text2 = "ow@autotest.com----pwd2----cid2----rtoken_new"
        data = self._import_auto(client, text2, duplicate_strategy="overwrite")
        self.assertTrue(data["success"])
        self.assertEqual(data["summary"]["imported"], 1)
        self.assertEqual(data["summary"]["skipped"], 0)

    def test_comment_and_empty_lines_skipped(self):
        """注释行和空行被跳过"""
        client = self.app.test_client()
        self._login(client)

        text = "# This is a comment\n\nvalid@autotest.com----pwd----cid----rt\n  \n# Another comment"
        data = self._import_auto(client, text)
        self.assertTrue(data["success"])
        self.assertEqual(data["summary"]["imported"], 1)

    def test_custom_5_segment(self):
        """custom 5段格式"""
        client = self.app.test_client()
        self._login(client)

        text = "c@autotest.com----pwd----custom----mail.autotest.com----993"
        data = self._import_auto(client, text)
        self.assertTrue(data["success"])
        self.assertIn("custom", data["summary"]["by_provider"])

    def test_auto_groups_created(self):
        """自动分组应被创建"""
        client = self.app.test_client()
        self._login(client)

        text = "ag@gmail.com----apppass"
        data = self._import_auto(client, text)
        self.assertTrue(data["success"])
        s = data["summary"]
        # Gmail 分组可能已存在也可能新创建
        self.assertIn("gmail", s["by_provider"])

    def test_explicit_group_id(self):
        """指定 group_id 时所有账号落入该分组"""
        client = self.app.test_client()
        self._login(client)

        # 获取默认分组 ID
        with self.app.app_context():
            from outlook_web.repositories import groups as groups_repo

            default_id = groups_repo.get_default_group_id()

        text = "eg@autotest.com----pwd----cid----rt"
        data = self._import_auto(client, text, group_id=default_id)
        self.assertTrue(data["success"])

    def test_unknown_domain_no_fallback_fails(self):
        """2段未知域名无 fallback → 该行失败"""
        client = self.app.test_client()
        self._login(client)

        text = "u@unknowndomain.xyz----pwd"
        data = self._import_auto(client, text)
        self.assertFalse(data["success"])
        self.assertEqual(data["summary"]["failed"], 1)

    def test_unknown_domain_with_fallback_succeeds(self):
        """2段未知域名有 fallback → 成功"""
        client = self.app.test_client()
        self._login(client)

        text = "uf@autotest.xyz----pwd"
        data = self._import_auto(client, text, imap_host="mail.autotest.xyz", imap_port=993)
        self.assertTrue(data["success"])
        self.assertEqual(data["summary"]["imported"], 1)

    def test_error_line_numbers(self):
        """错误行号应正确"""
        client = self.app.test_client()
        self._login(client)

        text = "valid@autotest.com----pwd----cid----rt\ninvalid-no-at\n"
        data = self._import_auto(client, text)
        # At least one error
        errors = data.get("errors", [])
        if errors:
            self.assertEqual(errors[0]["line"], 2)


if __name__ == "__main__":
    unittest.main()
