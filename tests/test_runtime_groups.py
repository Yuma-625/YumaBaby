import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agent.groups import handle_group_admin, is_scan_group_command
from app.config import settings
from app.memory.store import MemoryStore


class RuntimeGroupAdminTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "m.db")
        self._patch = patch.object(settings, "QQ_PERSONA_ADMIN", "ADMIN")
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.store.close()
        self.tmp.cleanup()

    def test_add_normal_and_readonly(self):
        reply = handle_group_admin("加群 G12345678 正常 旁听", self.store, "ADMIN")
        self.assertIn("已加群", reply)
        row = self.store.get_runtime_group("G12345678")
        self.assertEqual(row["mode"], "normal")
        self.assertEqual(row["participation"], 1)

        handle_group_admin("加群 G87654321 只读", self.store, "ADMIN")
        row = self.store.get_runtime_group("G87654321")
        self.assertEqual(row["mode"], "readonly")
        self.assertEqual(row["participation"], 0)

    def test_reject_unknown(self):
        self.store.record_unknown_group("G99999999", "U1", "奶鱼", "在吗")
        reply = handle_group_admin("拒绝 G99999999", self.store, "ADMIN")
        self.assertIn("已拒绝", reply)
        self.assertEqual(self.store.get_unknown_group("G99999999")["status"], "rejected")

    def test_add_marks_unknown_added(self):
        self.store.record_unknown_group("G12345678", "U1", "奶鱼", "在吗")
        handle_group_admin("加群 G12345678 正常", self.store, "ADMIN")
        self.assertEqual(self.store.get_unknown_group("G12345678")["status"], "added")

    def test_list_groups(self):
        self.store.upsert_runtime_group("G12345678", "readonly")
        text = handle_group_admin("群列表", self.store, "ADMIN")
        self.assertIn("G12345678", text)
        self.assertIn("只读", text)

    def test_non_admin_ignored(self):
        self.assertIsNone(handle_group_admin("加群 G12345678 正常", self.store, "USER"))

    def test_invalid_openid(self):
        self.assertIn("格式不对", handle_group_admin("加群 xxx", self.store, "ADMIN"))

    def test_scan_command_detection(self):
        self.assertTrue(is_scan_group_command("扫一下最近新加的群"))
        self.assertTrue(is_scan_group_command("扫新群"))
        self.assertFalse(is_scan_group_command("群列表"))


if __name__ == "__main__":
    unittest.main()
