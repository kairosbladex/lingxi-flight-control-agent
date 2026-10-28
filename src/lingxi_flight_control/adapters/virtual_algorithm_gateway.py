"""虚拟 AI 算法生命周期网关 (VirtualAlgorithmGateway)。

依据 V1.4 2.1.2 规范：
算法绑定强制默认“实时+航点检测”（到达开启、离开关闭、去重防误关）。
外部平台当前仅暴露任务级 videoDetect 接口，本网关在内存与推流元数据层
精确维护 active_algorithms_set。
落实差集停用逻辑：离开某标注区域时，仅关闭该标注独有的算法；
若某算法在全局配置中已被勾选，或被其他活跃标注共享，则保留其开启状态（绝不误关）。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence, Set


class VirtualAlgorithmGateway:
    """管理虚拟细粒度 AI 算法的生命周期与差集启停逻辑。"""

    def __init__(self, global_algorithms: Sequence[str] | None = None):
        self._global_algorithms: set[str] = set(global_algorithms or [])
        self._active_algorithms: set[str] = set(self._global_algorithms)
        self._annotation_active_algorithms: dict[str, set[str]] = {}

    @property
    def active_algorithms(self) -> frozenset[str]:
        """当前处于运行状态的全部算法集合。"""
        return frozenset(self._active_algorithms)

    @property
    def global_algorithms(self) -> frozenset[str]:
        return frozenset(self._global_algorithms)

    def on_arrive_annotation(self, annotation_id: str,
                             algorithms: Sequence[str]) -> set[str]:
        """到达某静态标注点：激活该标注关联算法，返回新启动的增量算法。"""
        target_set = set(algorithms)
        self._annotation_active_algorithms[annotation_id] = target_set

        newly_started = target_set - self._active_algorithms
        self._active_algorithms.update(target_set)
        return newly_started

    def on_depart_annotation(self, annotation_id: str) -> set[str]:
        """离开某静态标注点：计算差集停用，绝不误关全局或其他活跃标注算法。"""
        departing_set = self._annotation_active_algorithms.pop(annotation_id, set())
        if not departing_set:
            return set()

        # 统计其他活跃标注仍在使用中的算法
        retained_by_others = set(self._global_algorithms)
        for other_id, other_set in self._annotation_active_algorithms.items():
            retained_by_others.update(other_set)

        # 仅停止当前标注独占且未被全局保留的算法
        to_stop = departing_set - retained_by_others
        self._active_algorithms.difference_update(to_stop)
        return to_stop

    def build_telemetry_metadata(self) -> dict[str, Any]:
        """生成下发推流元数据中绑定的算法标识。"""
        return {
            "active_algorithms": sorted(self._active_algorithms),
            "algorithm_count": len(self._active_algorithms),
            "video_detect_enabled": len(self._active_algorithms) > 0,
        }
