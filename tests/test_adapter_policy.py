import unittest
from unittest.mock import patch

from app.adapters.qq import QQAdapter
from app.config import settings


class GroupTriggerTest(unittest.TestCase):
    def setUp(self):
        self.adapter = QQAdapter(on_message=None)
        self.patches = [
            patch.object(settings, "QQ_BOT_UID", "999"),
            patch.object(settings, "QQ_AUTO_REPLY_KEYWORDS", ["小鱼"]),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_mention_or_keyword_default(self):
        with patch.object(settings, "GROUP_TRIGGER_MODE", "mention_or_keyword"):
            self.assertTrue(self.adapter._is_group_triggered(True, "hi"))
            self.assertTrue(self.adapter._is_group_triggered(False, "小鱼在吗"))
            self.assertFalse(self.adapter._is_group_triggered(False, "今天天气不错"))

    def test_mention_only(self):
        with patch.object(settings, "GROUP_TRIGGER_MODE", "mention"):
            self.assertTrue(self.adapter._is_group_triggered(True, "hi"))
            self.assertFalse(self.adapter._is_group_triggered(False, "小鱼在吗"))

    def test_keyword_only(self):
        with patch.object(settings, "GROUP_TRIGGER_MODE", "keyword"):
            self.assertFalse(self.adapter._is_group_triggered(True, "hi"))
            self.assertTrue(self.adapter._is_group_triggered(False, "小鱼在吗"))

    def test_all(self):
        with patch.object(settings, "GROUP_TRIGGER_MODE", "all"):
            self.assertTrue(self.adapter._is_group_triggered(False, "随便说点什么"))


class PrivateWhitelistTest(unittest.TestCase):
    def setUp(self):
        self.adapter = QQAdapter(on_message=None)

    def test_admin_is_always_allowed(self):
        with patch.object(settings, "QQ_PERSONA_ADMIN", "111"), \
             patch.object(settings, "QQ_PRIVATE_WHITELIST", []):
            self.assertTrue(self.adapter._is_private_allowed("111"))
            self.assertFalse(self.adapter._is_private_allowed("222"))

    def test_whitelist_allows_listed_user(self):
        with patch.object(settings, "QQ_PERSONA_ADMIN", "111"), \
             patch.object(settings, "QQ_PRIVATE_WHITELIST", ["222", "333"]):
            self.assertTrue(self.adapter._is_private_allowed("222"))
            self.assertTrue(self.adapter._is_private_allowed("111"))
            self.assertFalse(self.adapter._is_private_allowed("444"))

    def test_empty_config_denies_everyone(self):
        with patch.object(settings, "QQ_PERSONA_ADMIN", ""), \
             patch.object(settings, "QQ_PRIVATE_WHITELIST", []):
            self.assertFalse(self.adapter._is_private_allowed("111"))


if __name__ == "__main__":
    unittest.main()
