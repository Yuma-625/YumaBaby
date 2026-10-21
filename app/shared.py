"""跨模块共享的运行时对象。

放在独立模块里，避免路由/定时任务与 main 之间循环导入。
"""

from typing import Any

from app.adapters.qq import QQAdapter

qq_adapter: QQAdapter | None = None
agent_service: Any | None = None
memory_store: Any | None = None
