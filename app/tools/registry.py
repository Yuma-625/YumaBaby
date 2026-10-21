"""工具注册表：把 Python 函数暴露成 LLM 可调用的 JSON Schema 工具。

设计目标只有三条：

1. 模型传错参数时**不崩**，而是把结构化错误回传，让它自己改；
2. 每个工具都有**超时**，不会把整个循环拖死；
3. 执行结果统一包成 ``{"ok": bool, ...}``，便于回填和审计。
"""

import asyncio
import inspect
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

HANDLER = Callable[..., Any] | Callable[..., Awaitable[Any]]

_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list,),
}


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: HANDLER
    timeout: float = 10.0


@dataclass
class ToolResult:
    """执行结果。``ok=False`` 时 ``error`` 一定有值。"""

    ok: bool
    result: Any = None
    error: str | None = None
    details: list[str] = field(default_factory=list)
    duration_ms: int = 0

    def to_dict(self) -> dict:
        data: dict[str, Any] = {"ok": self.ok}
        if self.ok:
            data["result"] = self.result
        else:
            data["error"] = self.error
            if self.details:
                data["details"] = self.details
        return data


class ToolRegistry:
    def __init__(self):
        self._specs: dict[str, ToolSpec] = {}

    # ------------------------------------------------------------------ 注册

    def add(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: HANDLER,
        *,
        timeout: float = 10.0,
    ) -> None:
        if not name or not name.replace("_", "").isalnum():
            raise ValueError(f"工具名不合法: {name!r}")
        if name in self._specs:
            raise ValueError(f"工具已注册: {name}")
        self._specs[name] = ToolSpec(
            name=name,
            description=description,
            parameters=parameters,
            handler=handler,
            timeout=timeout,
        )

    def __contains__(self, name: str) -> bool:
        return name in self._specs

    @property
    def names(self) -> list[str]:
        return list(self._specs)

    def schemas(self) -> list[dict[str, Any]]:
        """OpenAI / DeepSeek 的 tools 参数格式。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            }
            for spec in self._specs.values()
        ]

    # ------------------------------------------------------------------ 执行

    async def run(self, name: str, arguments: str | dict[str, Any] | None) -> ToolResult:
        started = time.perf_counter()

        def finish(result: ToolResult) -> ToolResult:
            result.duration_ms = int((time.perf_counter() - started) * 1000)
            return result

        spec = self._specs.get(name)
        if spec is None:
            return finish(ToolResult(False, error=f"未知工具: {name}"))

        try:
            args = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
        except ValueError as e:
            return finish(ToolResult(False, error=f"arguments 不是合法 JSON: {e}"))

        errors = validate_arguments(spec.parameters, args)
        if errors:
            return finish(ToolResult(False, error="参数校验失败", details=errors))

        try:
            if inspect.iscoroutinefunction(spec.handler):
                value = await asyncio.wait_for(spec.handler(**args), spec.timeout)
            else:
                value = await asyncio.wait_for(
                    asyncio.to_thread(spec.handler, **args), spec.timeout
                )
            return finish(ToolResult(True, result=value))
        except asyncio.TimeoutError:
            logger.warning(f"工具超时: {name}")
            return finish(ToolResult(False, error=f"执行超时（>{spec.timeout}s）"))
        except (ValueError, TypeError) as e:
            # 参数/输入问题属于预期错误，不打完整堆栈
            logger.warning(f"工具输入不合法 [{name}]: {e}")
            return finish(ToolResult(False, error=f"{type(e).__name__}: {e}"))
        except Exception as e:
            logger.exception(f"工具执行失败: {name}")
            return finish(ToolResult(False, error=f"{type(e).__name__}: {e}"))


def validate_arguments(schema: dict[str, Any], args: Any) -> list[str]:
    """按 JSON Schema 的 required/properties 做最小校验。"""
    if not isinstance(args, dict):
        return ["arguments 必须是 JSON 对象"]

    errors: list[str] = []
    properties = schema.get("properties", {}) or {}
    required = schema.get("required", []) or []

    for key in required:
        if key not in args:
            errors.append(f"缺少必填参数: {key}")

    for key, value in args.items():
        prop = properties.get(key)
        if prop is None:
            errors.append(f"未知参数: {key}")
            continue
        expected_type = prop.get("type")
        if not expected_type:
            continue
        expected = _JSON_TYPES.get(expected_type)
        if expected is None:
            continue
        # bool 是 int 的子类，整数/数字参数要显式排除
        if expected_type in ("integer", "number") and isinstance(value, bool):
            errors.append(f"参数 {key} 应为 {expected_type}，收到 boolean")
            continue
        if not isinstance(value, expected):
            errors.append(
                f"参数 {key} 应为 {expected_type}，收到 {type(value).__name__}"
            )

    return errors
