import asyncio
import tempfile
import unittest
from pathlib import Path

from app.memory.store import MemoryStore
from app.tools.builtin import (
    build_registry,
    check_url,
    extract_text,
    parse_bing_results,
    reset_scope,
    set_scope,
)


class UrlGuardTest(unittest.TestCase):
    def test_blocks_loopback(self):
        self.assertIn("内网", check_url("http://127.0.0.1:8000/api"))

    def test_blocks_private_ranges(self):
        for url in ("http://10.0.0.5/", "http://192.168.1.1/", "http://172.16.0.1/"):
            self.assertIsNotNone(check_url(url), url)

    def test_blocks_metadata_endpoint(self):
        self.assertIn("内网", check_url("http://169.254.169.254/latest/meta-data"))

    def test_blocks_non_http_scheme(self):
        self.assertIn("http", check_url("file:///etc/passwd"))

    def test_allows_public_ip(self):
        self.assertIsNone(check_url("https://93.184.216.34/"))

    def test_hostname_resolution_failure(self):
        self.assertIsNotNone(check_url("http://no-such-host.invalid/"))


class HtmlExtractTest(unittest.TestCase):
    def test_drops_script_and_style(self):
        html = "<html><head><style>.a{}</style></head><body><script>alert(1)</script><p>正文</p></body></html>"
        text = extract_text(html)
        self.assertIn("正文", text)
        self.assertNotIn("alert", text)
        self.assertNotIn(".a{}", text)

    def test_keeps_paragraph_breaks(self):
        text = extract_text("<p>第一段</p><p>第二段</p>")
        self.assertEqual(text, "第一段\n第二段")


class BingParseTest(unittest.TestCase):
    HTML = """
    <ol id="b_results">
      <li class="b_algo">
        <h2><a href="https://example.com/a">第一条标题</a></h2>
        <div class="b_caption"><p>第一条摘要内容</p></div>
      </li>
      <li class="b_algo">
        <h2><a href="https://example.com/b">第二条标题</a></h2>
        <p>第二条摘要</p>
      </li>
    </ol>
    """

    def test_parses_title_url_snippet(self):
        results = parse_bing_results(self.HTML, limit=5)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["title"], "第一条标题")
        self.assertEqual(results[0]["url"], "https://example.com/a")
        self.assertIn("第一条摘要", results[0]["snippet"])

    def test_respects_limit(self):
        self.assertEqual(len(parse_bing_results(self.HTML, limit=1)), 1)

    def test_empty_page(self):
        self.assertEqual(parse_bing_results("<html></html>"), [])


class MemoryToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "m.db")
        self.registry = build_registry(self.store)
        self.token = set_scope(("user", "10001"))

    def tearDown(self):
        reset_scope(self.token)
        self.store.close()
        self.tmp.cleanup()

    def test_remember_then_recall(self):
        r1 = asyncio.run(self.registry.run("remember", '{"text": "用户喜欢猫"}'))
        self.assertTrue(r1.ok, r1.error)

        r2 = asyncio.run(self.registry.run("recall", '{"query": "猫"}'))
        self.assertTrue(r2.ok)
        self.assertEqual(r2.result["memories"][0]["text"], "用户喜欢猫")

    def test_forget(self):
        asyncio.run(self.registry.run("remember", '{"text": "临时信息"}'))
        r = asyncio.run(self.registry.run("forget", '{"keyword": "临时"}'))
        self.assertEqual(r.result["removed"], 1)

    def test_remember_requires_scope(self):
        reset_scope(self.token)
        r = asyncio.run(self.registry.run("remember", '{"text": "x"}'))
        self.assertFalse(r.ok)
        self.assertIn("作用域", r.error)
        self.token = set_scope(("user", "10001"))

    def test_tools_do_not_leak_scope_parameter(self):
        schema = next(s for s in self.registry.schemas() if s["function"]["name"] == "remember")
        self.assertEqual(list(schema["function"]["parameters"]["properties"]), ["text"])


if __name__ == "__main__":
    unittest.main()
