"""向量工具：float32 BLOB 打包 / 解包 / 余弦相似度。"""

import numpy as np


def pack_vector(vector: list[float] | np.ndarray) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def unpack_vector(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def cosine(a: list[float] | np.ndarray, b: list[float] | np.ndarray) -> float:
    va = np.asarray(a, dtype=np.float32)
    vb = np.asarray(b, dtype=np.float32)
    if va.size == 0 or vb.size == 0 or va.size != vb.size:
        return 0.0
    norm = float(np.linalg.norm(va) * np.linalg.norm(vb))
    if norm <= 0:
        return 0.0
    return float(np.dot(va, vb) / norm)


def similarity_score(cos: float) -> float:
    """把 [-1, 1] 的余弦压到 [0, 1]，便于和权重/时效项相加。"""
    return max(0.0, min(1.0, (cos + 1.0) / 2.0))
