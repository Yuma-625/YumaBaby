import asyncio
import unittest

from app.agent.loop import AgentLoop
from app.tools.registry import ToolRegistry
from app.utils.llm import LLMResponse, ToolCall


class ScriptedLLM:
    """按顺序返回预置响应，方便断言循环行为。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.messages_seen: list[list[dict]] = []

    async def complete(self, messages, **kwargs):
        self.messages_seen.append(messages)
        return self.responses.pop(0) if self.responses else None


def make_registry():
    registry = ToolRegistry()

    async def echo(text: str):
        return {"echo": text}

    async def slow(seconds: float = 1.0):
        await asyncio.sleep(seconds)
        return {"slept": seconds}

    def double(x: float):
        return {"doubled": x * 2}

    registry.add(
        "echo", "回显文本",
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        echo,
    )
    registry.add(
        "slow", "故意慢",
        {"type": "object", "properties": {"seconds": {"type": "number"}}},
        slow,
        timeout=0.05,
    )
    registry.add(
        "double", "同步函数",
        {"type": "object", "properties": {"x": {"type": "number"}}, "required": ["x"]},
        double,
    )
    return registry


def call(name: str, args: str = "{}", call_id: str = "c1") -> LLMResponse:
    return LLMResponse(content=None, tool_calls=[ToolCall(id=call_id, name=name, arguments=args)])


def answer(text: str) -> LLMResponse:
    return LLMResponse(content=text)


class ToolRegistryTest(unittest.TestCase):
    def test_schema_shape(self):
        schemas = make_registry().schemas()
        self.assertEqual(schemas[0]["type"], "function")
        self.assertIn("parameters", schemas[0]["function"])
        self.assertEqual({s["function"]["name"] for s in schemas}, {"echo", "slow", "double"})

    def test_unknown_tool(self):
        result = asyncio.run(make_registry().run("nope", "{}"))
        self.assertFalse(result.ok)
        self.assertIn("未知工具", result.error)

    def test_missing_required(self):
        result = asyncio.run(make_registry().run("echo", "{}"))
        self.assertFalse(result.ok)
        self.assertIn("缺少必填参数", result.details[0])

    def test_wrong_type(self):
        result = asyncio.run(make_registry().run("echo", '{"text": 123}'))
        self.assertFalse(result.ok)
        self.assertIn("应为 string", result.details[0])

    def test_unknown_argument(self):
        result = asyncio.run(make_registry().run("echo", '{"text": "a", "extra": 1}'))
        self.assertFalse(result.ok)
        self.assertIn("未知参数", result.details[0])

    def test_invalid_json(self):
        result = asyncio.run(make_registry().run("echo", "{not json"))
        self.assertFalse(result.ok)
        self.assertIn("JSON", result.error)

    def test_timeout(self):
        result = asyncio.run(make_registry().run("slow", '{"seconds": 1}'))
        self.assertFalse(result.ok)
        self.assertIn("超时", result.error)

    def test_sync_handler(self):
        result = asyncio.run(make_registry().run("double", '{"x": 21}'))
        self.assertTrue(result.ok)
        self.assertEqual(result.result, {"doubled": 42})

    def test_integer_rejects_bool(self):
        registry = ToolRegistry()
        registry.add(
            "count", "计数",
            {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]},
            lambda n: {"n": n},
        )
        result = asyncio.run(registry.run("count", '{"n": true}'))
        self.assertFalse(result.ok)


class AgentLoopTest(unittest.TestCase):
    def test_tool_then_answer(self):
        llm = ScriptedLLM([call("echo", '{"text": "hi"}'), answer("说完了")])
        result = asyncio.run(AgentLoop(make_registry(), llm=llm).run("试试"))

        self.assertEqual(result.reply, "说完了")
        self.assertEqual(result.stopped, "done")
        self.assertEqual(len(result.trace), 1)
        self.assertTrue(result.trace[0]["ok"])
        # 第二次调用时，消息里应带 tool 结果
        tool_messages = [m for m in llm.messages_seen[1] if m["role"] == "tool"]
        self.assertEqual(len(tool_messages), 1)
        self.assertIn("hi", tool_messages[0]["content"])

    def test_plain_answer_without_tools(self):
        llm = ScriptedLLM([answer("直接回答")])
        result = asyncio.run(AgentLoop(make_registry(), llm=llm).run("你好"))
        self.assertEqual(result.reply, "直接回答")
        self.assertEqual(result.trace, [])

    def test_repeated_call_stops(self):
        same = '{"text": "x"}'
        llm = ScriptedLLM([
            call("echo", same), call("echo", same, "c2"), call("echo", same, "c3"),
        ])
        result = asyncio.run(AgentLoop(make_registry(), llm=llm).run("重复"))
        self.assertEqual(result.stopped, "repeated")
        self.assertEqual(len(result.trace), 1)

    def test_duplicate_gets_one_chance_to_recover(self):
        same = '{"text": "x"}'
        llm = ScriptedLLM([
            call("echo", same), call("echo", same, "c2"), answer("换了个说法"),
        ])
        result = asyncio.run(AgentLoop(make_registry(), llm=llm).run("重复"))
        self.assertEqual(result.stopped, "done")
        self.assertEqual(result.reply, "换了个说法")
        self.assertEqual(len(result.trace), 1)

    def test_argument_order_counts_as_repeat(self):
        llm = ScriptedLLM([
            call("echo", '{"text": "a"}'),
            call("echo", '{ "text" : "a" }', "c2"),
            call("echo", '{"text":"a"}', "c3"),
        ])
        result = asyncio.run(AgentLoop(make_registry(), llm=llm).run("重复"))
        self.assertEqual(result.stopped, "repeated")

    def test_max_steps_finalizes(self):
        llm = ScriptedLLM([
            call("echo", '{"text": "1"}', "c1"),
            answer("这是总结"),
        ])
        result = asyncio.run(
            AgentLoop(make_registry(), llm=llm, max_steps=1, finalize_on_limit=True).run("长任务")
        )
        self.assertEqual(result.stopped, "max_steps")
        self.assertEqual(result.reply, "这是总结")

    def test_llm_unavailable(self):
        llm = ScriptedLLM([])
        result = asyncio.run(AgentLoop(make_registry(), llm=llm).run("hi"))
        self.assertEqual(result.stopped, "llm_unavailable")

    def test_tool_error_is_fed_back_and_recovers(self):
        llm = ScriptedLLM([
            call("echo", "{}"),          # 缺参数
            answer("我换个方式回答"),
        ])
        result = asyncio.run(AgentLoop(make_registry(), llm=llm).run("坏参数"))
        self.assertEqual(result.reply, "我换个方式回答")
        self.assertFalse(result.trace[0]["ok"])
        tool_messages = [m for m in llm.messages_seen[1] if m["role"] == "tool"]
        self.assertIn("缺少必填参数", tool_messages[0]["content"])

    def test_history_is_passed(self):
        llm = ScriptedLLM([answer("好")])
        history = [{"role": "user", "content": "之前"}, {"role": "assistant", "content": "嗯"}]
        asyncio.run(AgentLoop(make_registry(), llm=llm).run("继续", history=history))
        roles = [m["role"] for m in llm.messages_seen[0]]
        self.assertEqual(roles, ["user", "assistant", "user"])

    def test_on_step_hook(self):
        seen = []
        llm = ScriptedLLM([call("double", '{"x": 2}'), answer("好了")])
        asyncio.run(
            AgentLoop(make_registry(), llm=llm, on_step=seen.append).run("乘以二")
        )
        self.assertEqual(seen[0]["tool"], "double")
        self.assertTrue(seen[0]["ok"])


if __name__ == "__main__":
    unittest.main()
