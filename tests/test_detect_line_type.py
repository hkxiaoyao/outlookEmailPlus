"""tests/test_detect_line_type.py — FD-00006 行类型识别单元测试"""

import unittest

from tests._import_app import import_web_app_module


class TestDetectLineType(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = import_web_app_module()
        cls.app = cls.module.app

    def _detect(self, line, fallback_host="", fallback_port=993):
        with self.app.app_context():
            from outlook_web.controllers.accounts import _detect_line_type

            return _detect_line_type(line, fallback_host, fallback_port)

    # --- Outlook ≥4 段 ---
    def test_outlook_4_parts(self):
        r = self._detect("user@outlook.com----pwd----client123----refresh_tok")
        self.assertEqual(r["type"], "outlook")
        self.assertEqual(r["provider"], "outlook")
        self.assertEqual(r["fields"]["email"], "user@outlook.com")
        self.assertEqual(r["fields"]["client_id"], "client123")
        self.assertEqual(r["fields"]["refresh_token"], "refresh_tok")

    def test_outlook_token_contains_separator(self):
        r = self._detect("u@o.com----p----cid----part1----part2----part3")
        self.assertEqual(r["type"], "outlook")
        self.assertEqual(r["fields"]["refresh_token"], "part1----part2----part3")

    # --- Custom 5 段 ---
    def test_custom_5_parts(self):
        r = self._detect("user@corp.com----pwd123----custom----mail.corp.com----993")
        self.assertEqual(r["type"], "imap")
        self.assertEqual(r["provider"], "custom")
        self.assertEqual(r["fields"]["imap_host"], "mail.corp.com")
        self.assertEqual(r["fields"]["imap_port"], 993)

    def test_custom_5_parts_case_insensitive(self):
        r = self._detect("u@c.com----p----Custom----h.com----995")
        self.assertEqual(r["type"], "imap")
        self.assertEqual(r["provider"], "custom")

    # --- IMAP 3 段 ---
    def test_imap_3_parts_known_provider(self):
        r = self._detect("user@qq.com----authcode----qq")
        self.assertEqual(r["type"], "imap")
        self.assertEqual(r["provider"], "qq")
        self.assertEqual(r["fields"]["imap_host"], "imap.qq.com")

    def test_imap_3_parts_unknown_provider(self):
        r = self._detect("user@x.com----pwd----unknownprov")
        self.assertEqual(r["type"], "error")
        self.assertIn("未知", r["error"])

    def test_imap_3_parts_custom_not_allowed(self):
        r = self._detect("u@x.com----p----custom")
        self.assertEqual(r["type"], "error")

    # --- 2 段：域名推断 ---
    def test_2_parts_gmail_inferred(self):
        r = self._detect("user@gmail.com----apppassword")
        self.assertEqual(r["type"], "imap")
        self.assertEqual(r["provider"], "gmail")
        self.assertEqual(r["fields"]["imap_host"], "imap.gmail.com")

    def test_2_parts_unknown_with_fallback(self):
        r = self._detect("user@corp.com----pwd", fallback_host="mail.corp.com", fallback_port=995)
        self.assertEqual(r["type"], "imap")
        self.assertEqual(r["provider"], "custom")
        self.assertEqual(r["fields"]["imap_host"], "mail.corp.com")
        self.assertEqual(r["fields"]["imap_port"], 995)

    def test_2_parts_unknown_no_fallback(self):
        r = self._detect("user@corp.com----pwd")
        self.assertEqual(r["type"], "error")

    # --- 1 段：GPTMail ---
    def test_gptmail_1_part(self):
        r = self._detect("temp@gptmail.com")
        self.assertEqual(r["type"], "gptmail")
        self.assertEqual(r["provider"], "gptmail")
        self.assertEqual(r["fields"]["email"], "temp@gptmail.com")

    def test_1_part_invalid_email(self):
        r = self._detect("not-an-email")
        self.assertEqual(r["type"], "error")


if __name__ == "__main__":
    unittest.main()
