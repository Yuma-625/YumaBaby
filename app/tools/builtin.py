"""内置工具：时间、网页抓取、记忆读写。

记忆工具依赖一个请求级的 scope（谁在说话），由 AgentService 通过
ContextVar 注入，模型不需要也不允许自己指定 scope。
"""

import base64
import html as html_lib
import ipaddress
import logging
import re
import socket
import time
from contextvars import ContextVar
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx

from app.config import settings
from app.memory.store import MemoryStore
from app.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

Scope = tuple[str, str]  # ("user" | "group", "12345")
_scope: ContextVar[Scope | None] = ContextVar("memory_scope", default=None)


def current_scope() -> Scope | None:
    return _scope.get()


def set_scope(scope: Scope):
    return _scope.set(scope)


def reset_scope(token) -> None:
    _scope.reset(token)


# --------------------------------------------------------------------- 时间


async def get_time() -> dict:
    try:
        tz = ZoneInfo(settings.TIMEZONE)
    except Exception:
        tz = ZoneInfo("UTC")
    now = datetime.now(tz)
    return {
        "time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "weekday": now.strftime("%A"),
        "timezone": str(tz),
    }


# --------------------------------------------------------------------- 网页


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "svg", "head"}
    _BREAK = {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}

    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)


def extract_text(html: str) -> str:
    """从 HTML 抽纯文本；不引第三方解析库。"""
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        return " ".join(html.split())
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line).strip()


def check_url(url: str) -> str | None:
    """返回拒绝原因；None 表示可以抓。防 SSRF：禁止私网/回环/链路本地。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "仅支持 http/https"
    host = parsed.hostname
    if not host:
        return "URL 缺少主机名"
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        return f"域名解析失败: {e}"

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return f"拒绝访问内网或保留地址: {ip}"
    return None


async def web_fetch(url: str, max_chars: int = 0) -> dict:
    """抓取网页正文。返回 url / status / text / truncated。"""
    max_chars = max_chars or settings.WEB_FETCH_MAX_CHARS
    max_chars = max(200, min(int(max_chars), 20000))
    current = url.strip()
    headers = {"User-Agent": "Mozilla/5.0 (compatible; YumaBaby/0.1)"}

    async with httpx.AsyncClient(timeout=settings.WEB_FETCH_TIMEOUT, follow_redirects=False) as client:
        for _ in range(4):
            reason = check_url(current)
            if reason:
                raise ValueError(reason)

            async with client.stream("GET", current, headers=headers) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        raise ValueError("重定向缺少 Location")
                    current = urljoin(current, location)
                    continue

                if resp.status_code >= 400:
                    raise ValueError(f"HTTP {resp.status_code}")

                ctype = (resp.headers.get("content-type") or "").lower()
                if not any(t in ctype for t in ("text/", "application/xhtml", "application/json")):
                    raise ValueError(f"不支持的内容类型: {ctype or 'unknown'}")

                chunks: list[bytes] = []
                size = 0
                truncated_bytes = False
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > settings.WEB_FETCH_MAX_BYTES:
                        truncated_bytes = True
                        break
                    chunks.append(chunk)

                encoding = resp.encoding or "utf-8"
                raw = b"".join(chunks).decode(encoding, errors="replace")
                text = extract_text(raw) if "html" in ctype else raw.strip()
                truncated = truncated_bytes or len(text) > max_chars
                return {
                    "url": current,
                    "status": resp.status_code,
                    "text": text[:max_chars],
                    "truncated": truncated,
                    "chars": min(len(text), max_chars),
                }

    raise ValueError("重定向次数过多")


# --------------------------------------------------------------------- 记忆


def _require_scope() -> Scope:
    scope = current_scope()
    if scope is None:
        raise ValueError("当前没有记忆作用域，无法读写记忆")
    return scope


async def remember(text: str, store: MemoryStore) -> dict:
    scope_type, scope_id = _require_scope()
    memory_id = store.remember(scope_type, scope_id, text, source="tool")
    return {"remembered": True, "id": memory_id, "text": text}


async def recall(query: str, limit: int, store: MemoryStore) -> dict:
    scope_type, scope_id = _require_scope()
    items = store.recall(scope_type, scope_id, query, limit=limit)
    return {
        "count": len(items),
        "memories": [{"text": i["text"], "created_at": i["created_at"]} for i in items],
    }


async def forget(keyword: str, store: MemoryStore) -> dict:
    scope_type, scope_id = _require_scope()
    removed = store.forget(scope_type, scope_id, keyword)
    return {"removed": removed}


# --------------------------------------------------------------------- 搜索

_TAG_RE = re.compile(r"<[^>]+>")
_search_cache: dict[str, tuple[float, list[dict]]] = {}
_SEARCH_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def _clean_text(value: str) -> str:
    return html_lib.unescape(_TAG_RE.sub("", value or "")).strip()


def _unwrap_bing_url(url: str) -> str:
    if "bing.com/ck/a" not in url:
        return url
    query = parse_qs(urlparse(url).query)
    token = (query.get("u") or [""])[0]
    if token.startswith("a1"):
        try:
            padded = token[2:] + "=" * (-len(token[2:]) % 4)
            return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
        except Exception:
            return url
    return url


def parse_bing_results(html_text: str, limit: int = 5) -> list[dict]:
    results = []
    blocks = re.split(r'<li class="b_algo"', html_text)[1:]
    for block in blocks:
        block = block.split("</li>", 1)[0]
        match = re.search(
            r'<h2[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S
        )
        if not match:
            continue
        url = _unwrap_bing_url(html_lib.unescape(match.group(1)))
        title = _clean_text(match.group(2))
        snippet_match = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        snippet = _clean_text(snippet_match.group(1)) if snippet_match else ""
        if title and url.startswith("http"):
            results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= limit:
            break
    return results


async def web_search(query: str, limit: int = 5) -> dict:
    """Bing 网页搜索，返回标题/链接/摘要。"""
    query = (query or "").strip()
    if not query:
        raise ValueError("搜索词不能为空")
    limit = max(1, min(int(limit), 8))

    cache_key = f"{query}|{limit}"
    cached = _search_cache.get(cache_key)
    now = time.monotonic()
    if cached and now - cached[0] < 300:
        return {"query": query, "cached": True, "results": cached[1]}

    url = "https://www.bing.com/search?" + urlencode(
        {"q": query, "setlang": "zh-CN", "count": limit}
    )
    async with httpx.AsyncClient(
        timeout=settings.WEB_FETCH_TIMEOUT, follow_redirects=True, headers=_SEARCH_UA
    ) as client:
        resp = await client.get(url)
        if resp.status_code >= 400:
            raise ValueError(f"搜索失败 HTTP {resp.status_code}")
        results = parse_bing_results(resp.text, limit)

    if results:
        _search_cache[cache_key] = (now, results)
        if len(_search_cache) > 100:
            _search_cache.clear()
    return {"query": query, "cached": False, "results": results}


def build_registry(store: MemoryStore) -> ToolRegistry:
    """把工具绑定到具体存储实例，避免用模块级全局。"""

    async def _remember(text: str) -> dict:
        return await remember(text, store)

    async def _recall(query: str = "", limit: int = 5) -> dict:
        return await recall(query, limit, store)

    async def _forget(keyword: str = "") -> dict:
        return await forget(keyword, store)

    registry = ToolRegistry()
    registry.add(
        "get_time",
        "获取当前日期、时间、星期。用户问“现在几点”“今天几号”“今天星期几”时调用。",
        {"type": "object", "properties": {}, "required": []},
        get_time,
    )
    registry.add(
        "web_fetch",
        "抓取一个网页并返回正文文本。用户给出 http/https 链接、或要求你看某个页面时调用。"
        "正文会被截断，不要用它访问内网地址。",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "http 或 https 开头的完整网址"},
                "max_chars": {"type": "integer", "description": "最多返回多少字符，默认 4000"},
            },
            "required": ["url"],
        },
        web_fetch,
        timeout=settings.WEB_FETCH_TIMEOUT + 5,
    )
    registry.add(
        "web_search",
        "联网搜索网页，返回标题、链接和摘要。用户问新闻、实时信息、"
        "你不知道或需要最新资料的问题时先调用它，再按需用 web_fetch 读具体页面。",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "limit": {"type": "integer", "description": "返回条数，默认 5，最多 8"},
            },
            "required": ["query"],
        },
        web_search,
        timeout=settings.WEB_FETCH_TIMEOUT + 5,
    )
    registry.add(
        "remember",
        "长期记住一条关于当前用户或当前群的信息。用户明确要求记住、或表达了稳定偏好时调用。"
        "只记事实，不要记一次性的事情。",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要记住的一句话，尽量简洁"},
            },
            "required": ["text"],
        },
        _remember,
    )
    registry.add(
        "recall",
        "检索之前记住的信息。需要回忆用户偏好、群规、以前说过的事时调用。",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词，留空则返回最近的记忆"},
                "limit": {"type": "integer", "description": "返回条数，默认 5"},
            },
            "required": [],
        },
        _recall,
    )
    registry.add(
        "forget",
        "删除记忆。用户要求忘掉某件事、或明确说不要记的时候调用。",
        {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "要删除的记忆关键词，留空删除全部"},
            },
            "required": [],
        },
        _forget,
    )
    return registry
