import unittest

from outlook_web.services import verification_extractor as extractor


class VerificationExtractorOptionsTests(unittest.TestCase):
    def _require_new_api(self):
        func = getattr(extractor, "extract_verification_info_with_options", None)
        self.assertTrue(callable(func), "缺少 extract_verification_info_with_options()")
        return func

    def test_extract_with_default_options_returns_code(self):
        func = self._require_new_api()
        email = {
            "subject": "Your verification code",
            "body": "Your code is 123456",
            "body_html": "<p>Your code is 123456</p>",
        }

        result = func(email)

        self.assertEqual(result.get("verification_code"), "123456")

    def test_extract_with_code_length_prefers_specified_length(self):
        func = self._require_new_api()
        email = {
            "subject": "Your code",
            "body": "short 1234 and target 654321",
            "body_html": "",
        }

        result = func(email, code_length="6-6")

        self.assertEqual(result.get("verification_code"), "654321")

    def test_extract_with_code_regex_supports_alphanumeric_code(self):
        func = self._require_new_api()
        email = {
            "subject": "OTP",
            "body": "Use AB12CD to continue",
            "body_html": "",
        }

        result = func(email, code_regex=r"\b[A-Z0-9]{6}\b")

        self.assertEqual(result.get("verification_code"), "AB12CD")

    def test_extract_with_code_source_subject_only(self):
        func = self._require_new_api()
        email = {
            "subject": "Code 778899",
            "body": "no code here",
            "body_html": "",
        }

        result = func(email, code_source="subject")

        self.assertEqual(result.get("verification_code"), "778899")

    def test_extract_with_preferred_link_keywords_returns_verify_link_first(self):
        func = self._require_new_api()
        email = {
            "subject": "Please verify your email",
            "body": "Open https://example.com/home or https://example.com/verify?token=abc",
            "body_html": "",
        }

        result = func(email)

        self.assertIn("verify", result.get("verification_link", ""))


class VerificationExtractorEdgeCaseTests(unittest.TestCase):
    """TC-EXT-01 ~ TC-EXT-04: 提取器参数化与边界测试"""

    def _require_new_api(self):
        func = getattr(extractor, "extract_verification_info_with_options", None)
        self.assertTrue(callable(func), "缺少 extract_verification_info_with_options()")
        return func

    def test_code_source_html_only_from_html(self):
        """TC-EXT-01: code_source=html 仅从 HTML 提取"""
        func = self._require_new_api()
        email = {
            "subject": "No code here",
            "body": "No code in plain text either",
            "body_html": "<p>Your verification code is 998877</p>",
        }

        result = func(email, code_source="html")

        self.assertEqual(result.get("verification_code"), "998877")
        self.assertEqual(result.get("match_source"), "html")

    def test_code_source_html_ignores_body(self):
        """TC-EXT-01 补充: code_source=html 不应从 body 提取"""
        func = self._require_new_api()
        email = {
            "subject": "Code 112233",
            "body": "Body code 445566",
            "body_html": "<p>No code here at all just text</p>",
        }

        result = func(email, code_source="html")

        # HTML 里没有验证码，不应从 body 提取
        self.assertIsNone(result.get("verification_code"))

    def test_code_length_format_invalid_abc(self):
        """TC-EXT-02: code_length='abc' 格式非法"""
        func = self._require_new_api()
        email = {"subject": "Code", "body": "123456", "body_html": ""}

        with self.assertRaises(ValueError):
            func(email, code_length="abc")

    def test_code_length_format_invalid_reversed(self):
        """TC-EXT-02: code_length='8-4' 反序非法"""
        func = self._require_new_api()
        email = {"subject": "Code", "body": "123456", "body_html": ""}

        with self.assertRaises(ValueError):
            func(email, code_length="8-4")

    def test_code_length_format_invalid_single_number(self):
        """TC-EXT-02 补充: code_length='6' 格式非法（需要 min-max 格式）"""
        func = self._require_new_api()
        email = {"subject": "Code", "body": "123456", "body_html": ""}

        with self.assertRaises(ValueError):
            func(email, code_length="6")

    def test_multiple_candidates_prefers_keyword_adjacent(self):
        """TC-EXT-03: 多个候选验证码时优先关键词邻近值"""
        func = self._require_new_api()
        email = {
            "subject": "Verify your account",
            "body": (
                "Some unrelated info about your purchase of item number 999999 from our store last week. "
                "Now here is the important part: Your verification code is 123456. "
                "Please ignore reference number 654321 above."
            ),
            "body_html": "",
        }

        result = func(email)

        # 应优先提取靠近 "verification code" 关键词的 123456
        self.assertEqual(result.get("verification_code"), "123456")
        self.assertEqual(result.get("confidence"), "high")

    def test_multiple_links_prefers_verify_confirm(self):
        """TC-EXT-04: 多个链接时优先 verify/confirm"""
        func = self._require_new_api()
        email = {
            "subject": "Action required",
            "body": (
                "Visit https://example.com/unsubscribe for opt-out. "
                "Or click https://example.com/confirm?token=xyz to confirm. "
                "Also see https://example.com/help for support."
            ),
            "body_html": "",
        }

        result = func(email)

        self.assertIn("confirm", result.get("verification_link", ""))

    def test_no_high_priority_link_falls_back_to_first(self):
        """TC-VER-08 补充: 无高优先级关键字时回退首个链接"""
        func = self._require_new_api()
        email = {
            "subject": "Links",
            "body": "See https://example.com/page1 or https://example.com/page2",
            "body_html": "",
        }

        result = func(email)

        # 没有 verify/confirm 类关键字，应返回第一个链接
        self.assertIsNotNone(result.get("verification_link"))
        self.assertIn("example.com", result.get("verification_link", ""))

    def test_code_regex_invalid_raises_valueerror(self):
        """补充: 非法正则直接抛 ValueError"""
        func = self._require_new_api()
        email = {"subject": "Code", "body": "123456", "body_html": ""}

        with self.assertRaises(ValueError):
            func(email, code_regex="[unclosed")

    def test_confidence_high_for_keyword_match(self):
        """补充: 关键词匹配时 confidence=high"""
        func = self._require_new_api()
        email = {
            "subject": "Your OTP code",
            "body": "Your OTP code is 778899",
            "body_html": "",
        }

        result = func(email)

        self.assertEqual(result.get("verification_code"), "778899")
        self.assertEqual(result.get("confidence"), "high")

    def test_confidence_low_for_fallback(self):
        """补充: 无关键词匹配时 confidence=low"""
        func = self._require_new_api()
        email = {
            "subject": "Random email",
            "body": "Some text 445566 end",
            "body_html": "",
        }

        result = func(email)

        if result.get("verification_code"):
            self.assertEqual(result.get("confidence"), "low")


if __name__ == "__main__":
    unittest.main()
