"""降级池：按权重和实时表现挑模型，表现差的少挑。"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Dict, List


@dataclass
class ModelStats:
    """一个候选的实时表现，只存三个数字，不存历史。"""

    weight: float = 1.0                # 配置里的权重，越大越想命中
    first_token_seconds: float = 0.0   # 首 token 耗时，0 表示还没跑过
    error_rate: float = 0.0            # 失败比例，1 表示每次都挂

    def record_first_token(self, seconds: float) -> None:
        """记首 token 耗时：新的占一成，老的占九成。"""
        old = self.first_token_seconds
        self.first_token_seconds = seconds if old == 0 else old * 0.9 + seconds * 0.1

    def record_result(self, failed: bool) -> None:
        """记这次成没成：新的占一成，老的占九成。"""
        self.error_rate = self.error_rate * 0.9 + (1.0 if failed else 0.0) * 0.1

    def score(self, fast_seconds: float) -> float:
        """得分 = 权重 × 健康度三次方。保底留权重的 2%，免得掉下去就再也抽不中。"""
        # 没跑过或者够快都算满分，慢了按倍数扣
        speed = 1.0 if self.first_token_seconds <= fast_seconds else fast_seconds / self.first_token_seconds
        health = (1.0 - self.error_rate) * speed
        return max(self.weight * health ** 3, self.weight * 0.02)


class ModelPool:
    """保存每个候选的表现，按得分抽样决定尝试顺序。"""

    def __init__(self) -> None:
        self._stats: Dict[str, ModelStats] = {}

    def stats(self, name: str, weight: float = 1.0) -> ModelStats:
        """取这个候选的表现记录，没有就新建。"""
        current = self._stats.get(name)
        if current is None:
            current = self._stats[name] = ModelStats(weight=weight)
        return current

    def order(self, names: List[str], fast_seconds: float) -> List[str]:
        """按得分抽样排出尝试顺序，抽中的不再参与后面的抽样。"""
        remaining = list(names)
        result: List[str] = []
        while remaining:
            scores = [self.stats(name).score(fast_seconds) for name in remaining]
            chosen = random.choices(remaining, weights=scores, k=1)[0]
            result.append(chosen)
            remaining.remove(chosen)
        return result

    def snapshot(self, fast_seconds: float) -> List[tuple]:
        """当前每个候选的表现和命中概率，给日志和排查用。"""
        names = sorted(self._stats)
        scores = [self.stats(name).score(fast_seconds) for name in names]
        total = sum(scores) or 1.0
        return [(name, self.stats(name), score / total) for name, score in zip(names, scores)]
