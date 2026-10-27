import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from app.memory.store import MemoryStore
from app.tools.builtin import build_registry
from app.tools.sandbox import (
    edit_file,
    glob_files,
    grep_files,
    list_dir,
    read_file,
    write_file,
)


class SandboxFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "sandbox"
        self._patches = [
            patch.object(settings, "SANDBOX_ENABLED", True),
            patch.object(settings, "SANDBOX_ROOT", str(self.root)),
            patch.object(settings, "SANDBOX_MAX_READ_BYTES", 10000),
            patch.object(settings, "SANDBOX_MAX_WRITE_BYTES", 10000),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def test_write_read_list(self):
        write_file("a.txt", "hello 小弗")
        data = read_file("a.txt")
        self.assertEqual(data["content"], "hello 小弗")
        names = [item["name"] for item in list_dir(".")["entries"]]
        self.assertIn("a.txt", names)

    def test_overwrite_guard(self):
        write_file("a.txt", "one")
        with self.assertRaises(ValueError):
            write_file("a.txt", "two")
        write_file("a.txt", "two", overwrite=True)
        self.assertEqual(read_file("a.txt")["content"], "two")

    def test_edit(self):
        write_file("a.txt", "hello world")
        edit_file("a.txt", "world", "小弗")
        self.assertEqual(read_file("a.txt")["content"], "hello 小弗")

    def test_edit_missing_text(self):
        write_file("a.txt", "hello")
        with self.assertRaises(ValueError):
            edit_file("a.txt", "nope", "x")

    def test_glob_and_grep(self):
        write_file("src/a.py", "print('hello')\n")
        write_file("src/b.txt", "hello text\n")
        matches = glob_files("**/*.py")["matches"]
        self.assertEqual(matches, ["src/a.py"])
        hits = grep_files("hello", glob="**/*")["matches"]
        self.assertEqual(len(hits), 2)

    def test_blocks_traversal_and_absolute(self):
        for bad in ("../secret.txt", "..\\secret.txt", str(Path(self.tmp.name) / "x")):
            with self.assertRaises(ValueError):
                read_file(bad)

    def test_blocks_drive_path(self):
        with self.assertRaises(ValueError):
            write_file("C:/Windows/temp.txt", "x")

    def test_read_size_limit(self):
        write_file("big.txt", "x" * 200)
        with patch.object(settings, "SANDBOX_MAX_READ_BYTES", 50):
            with self.assertRaises(ValueError):
                read_file("big.txt")

    def test_binary_rejected(self):
        path = self.root / "bin.dat"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00\x01\x02")
        with self.assertRaises(ValueError):
            read_file("bin.dat")


class SandboxRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "m.db")
        self._patches = [
            patch.object(settings, "SANDBOX_ENABLED", True),
            patch.object(settings, "SANDBOX_ROOT", str(Path(self.tmp.name) / "sandbox")),
        ]
        for p in self._patches:
            p.start()
        self.registry = build_registry(self.store)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.store.close()
        self.tmp.cleanup()

    def test_schemas_can_be_filtered(self):
        names = [item["function"]["name"] for item in self.registry.schemas(allowed={"fs_read"})]
        self.assertEqual(names, ["fs_read"])

    def test_write_via_registry_uses_path_argument(self):
        import json

        result = asyncio.run(self.registry.run(
            "fs_write",
            json.dumps({"path": "notes/a.md", "content": "hello"}, ensure_ascii=False),
            allowed={"fs_write"},
        ))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(
            read_file("notes/a.md")["content"], "hello"
        )

    def test_disallowed_tool_is_rejected(self):
        result = asyncio.run(
            self.registry.run("fs_write", '{"path": "a.txt", "content": "x"}',
                              allowed={"fs_read"})
        )
        self.assertFalse(result.ok)
        self.assertIn("不允许", result.error)


if __name__ == "__main__":
    unittest.main()
