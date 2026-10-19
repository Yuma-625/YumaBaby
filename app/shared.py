"""跨模块共享的运行时对象。

放在独立模块里，避免路由/定时任务与 main 之间循环导入。
"""

from app.adapters.qq import QQAdapter

qq_adapter: QQAdapter | None = None
