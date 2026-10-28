"""V1.4 大模型复合指令意图拆解与评测基准自动化回归套件。

硬门禁指标要求（依据 PROMPT_AGY.md & V1.4 4.3.4）：
1. 核心动作拆解准确率 ≥ 95%
2. 参数提取与归一化准确率 ≥ 98%
3. 跨仓规划信封协议版本一致性 (lingxi-flight-handoff/v1)
"""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from lingxi_flight_control.handoff_contract import (
    CONTRACT_VERSION, ENVELOPE_FIELDS, BINDING_FIELDS, HandoffKind,
)
from lingxi_flight_control.intent_evals import decompose_intent

DATASET_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "evals_dataset_v14.json"


class IntentEvalsHarnessTests(unittest.TestCase):
    """大模型复合指令评测基准套件。"""

    @classmethod
    def setUpClass(cls):
        self_path = DATASET_PATH
        if not self_path.exists():
            raise FileNotFoundError(f"评测数据集不存在: {self_path}")
        with open(self_path, "r", encoding="utf-8") as f:
            cls.dataset = json.load(f)

    def test_evals_dataset_completeness_and_schema(self):
        """核验评测数据集样本量与 Schema 完整性。"""
        self.assertEqual(self.dataset.get("schema_version"), "lingxi-evals-v14/v1")
        scenarios = self.dataset.get("scenarios", [])
        self.assertGreaterEqual(len(scenarios), 50, "评测用例数量必须不低于 50 条")
        categories = {s["category"] for s in scenarios}
        expected_cats = {
            "sequential_compound", "parallel_compound",
            "implicit_parameters", "colloquial_dialect",
            "safety_boundary_and_clamp"
        }
        self.assertTrue(expected_cats.issubset(categories), f"缺失核心分类: {expected_cats - categories}")

    def test_intent_decomposition_and_parameter_accuracy(self):
        """执行全量 50+ 复合指令意图拆解，核验动作准确率 ≥ 95% 与参数准确率 ≥ 98%。"""
        scenarios = self.dataset["scenarios"]
        total_actions_expected = 0
        matched_actions = 0
        total_parameters_expected = 0
        matched_parameters = 0

        failed_cases = []

        for case in scenarios:
            cid = case["id"]
            user_input = case["input"]
            expected_actions = case["expected_actions"]
            expected_mode = case.get("execution_mode", "sequential")

            res = decompose_intent(user_input)
            observed_actions = res.get("actions", [])
            observed_mode = res.get("execution_mode", "sequential")

            # 核验执行模式
            if expected_mode != observed_mode:
                failed_cases.append((cid, user_input, f"模式不符: 期望 {expected_mode}, 实际 {observed_mode}"))

            # 比对动作序列
            total_actions_expected += len(expected_actions)
            for i, exp in enumerate(expected_actions):
                if i < len(observed_actions):
                    obs = observed_actions[i]
                    if obs.get("action") == exp["action"]:
                        matched_actions += 1
                    else:
                        failed_cases.append((cid, user_input, f"第{i+1}步动作不符: 期望 {exp['action']}, 实际 {obs.get('action')}"))

                    # 比对参数
                    exp_params = exp.get("parameters", {})
                    obs_params = obs.get("parameters", {})
                    total_parameters_expected += len(exp_params)
                    for p_key, p_val in exp_params.items():
                        if p_key in obs_params and obs_params[p_key] == p_val:
                            matched_parameters += 1
                        else:
                            failed_cases.append((cid, user_input, f"参数不符 [{p_key}]: 期望 {p_val}, 实际 {obs_params.get(p_key)}"))
                else:
                    failed_cases.append((cid, user_input, f"缺失第{i+1}步动作: 期望 {exp['action']}"))

        action_accuracy = matched_actions / total_actions_expected if total_actions_expected else 0
        param_accuracy = matched_parameters / total_parameters_expected if total_parameters_expected else 0

        # 输出评测日志断言硬门禁
        print(f"\n[Evals Result] 样本数: {len(scenarios)}, 动作期望: {total_actions_expected}, "
              f"动作命中: {matched_actions} ({action_accuracy*100:.2f}%), "
              f"参数期望: {total_parameters_expected}, 参数命中: {matched_parameters} ({param_accuracy*100:.2f}%)")

        self.assertGreaterEqual(
            action_accuracy, 0.95,
            f"核心动作拆解准确率未达标: {action_accuracy*100:.2f}% < 95%, 失败用例: {failed_cases[:5]}"
        )
        self.assertGreaterEqual(
            param_accuracy, 0.98,
            f"参数提取准确率未达标: {param_accuracy*100:.2f}% < 98%, 失败用例: {failed_cases[:5]}"
        )

    def test_handoff_contract_envelope_version_and_guards(self):
        """核验跨仓规划信封协议版本一致性与字段防御。"""
        self.assertEqual(CONTRACT_VERSION, "lingxi-flight-handoff/v1")
        self.assertIn("schema_version", ENVELOPE_FIELDS)
        self.assertIn("actor_id", BINDING_FIELDS)
        self.assertIn("drone_id", BINDING_FIELDS)
        self.assertIn("project_id", BINDING_FIELDS)
        self.assertIn("task_id", BINDING_FIELDS)
        self.assertIn("snapshot_version", BINDING_FIELDS)
        self.assertEqual(HandoffKind.TASK_DISPATCH.value, "task_dispatch")
        self.assertEqual(HandoffKind.FLIGHT_CONTROL.value, "flight_control")


if __name__ == "__main__":
    unittest.main()
