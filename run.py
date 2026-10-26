import os
from pathlib import Path

import uvicorn

from app.config import settings
from app.utils.helpers import setup_logging

LOCK_FILE = Path(__file__).resolve().parent / "data" / "yumababy.lock"
_lock_handle = None


def acquire_single_instance():
    """用文件锁保证只跑一个实例，避免两个进程各回一次消息。"""
    global _lock_handle
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_FILE, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(
            f"已有实例在运行（{LOCK_FILE} 被占用），本次启动退出。\n"
            "如果确认没有别的实例，请先结束残留的 python 进程，或删除该锁文件。"
        )
        raise SystemExit(1)

    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    _lock_handle = handle
    return handle


if __name__ == "__main__":
    acquire_single_instance()
    setup_logging()
    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
    )
