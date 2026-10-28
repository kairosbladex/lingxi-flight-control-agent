"""Service-surface benchmarks exercise real service/loop/simulator, offline only."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.bench.cockpit_intents import DEFAULT_FIXTURE, REPOSITORY_ROOT, SERVICE_FIXTURE, load_scenarios, main


class ServiceIntentBenchTests(unittest.TestCase):
    def test_documented_script_entrypoint_works_with_only_src_on_pythonpath(self):
        result = subprocess.run([sys.executable, "tools/bench/cockpit_intents.py", "--surface", "service"],
            cwd=REPOSITORY_ROOT, env={**os.environ, "PYTHONPATH": "src"}, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["summary"]["denominator"], 102)

    def test_v2_keeps_v1_labels_and_uses_real_per_case_route_state(self):
        old = load_scenarios(DEFAULT_FIXTURE)
        current = load_scenarios(SERVICE_FIXTURE)
        by_id = {case["id"]: case for case in current["scenarios"]}
        for case in old["scenarios"]:
            if case["category"] == "out_of_range":
                continue
            self.assertEqual({key: by_id[case["id"]][key] for key in case}, case)
        for case_id, status in (("route_01", "running"), ("route_02", "paused"),
                                ("route_03", "paused"), ("route_06", "uploaded")):
            self.assertEqual(by_id[case_id]["initial_state"]["route_status"], status)
        self.assertEqual(old["context"]["route_status"], "paused")
        self.assertEqual(by_id["negation_01"]["expected_kind"], "no_action")
        self.assertEqual(by_id["negation_01"]["service_expected"]["status"], "final")
        self.assertEqual(by_id["injection_02"]["expected_kind"], "rejected")
        self.assertEqual(by_id["injection_02"]["service_expected"]["status"], "clarification")

    def test_service_offline_cli_has_its_own_frozen_denominator_and_no_model_calls(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()), \
                patch("tools.bench.cockpit_intents.http_transport") as http:
            output = Path(directory) / "report.json"
            self.assertEqual(main(["--surface", "service", "--output", str(output)]), 0)
            report = json.loads(output.read_text(encoding="utf-8"))
        http.assert_not_called()
        self.assertEqual(report["schema_version"], "cockpit-service-intent-report/v1")
        self.assertEqual(report["summary"]["denominator"], 102)
        self.assertEqual(report["summary"]["missing"], 102)
        self.assertEqual(report["action_coverage"]["declared_actions"], 66)
        # 冻结的 102 条场景集没有悬停用例：hover 是后加动作，保持数据集不变以便与历史报告可比。
        self.assertEqual(report["action_coverage"]["uncovered_actions"], [
            'ai_algorithm_set', 'hover', 'searchlight_brightness', 'speaker_speak', 'speaker_volume',
            'task_end_all', 'task_end_current'])
        self.assertEqual(report["run"]["surface"], "service")
        self.assertEqual(report["run"]["usage"]["confirmations"], 0)
        self.assertEqual(report["run"]["usage"]["mutating_tool_calls"], 0)

    def test_live_service_cli_reaches_real_preview_reads_and_early_policy(self):
        dataset = load_scenarios(SERVICE_FIXTURE)
        selected = ["movement_01", "missing_07", "negation_01", "injection_02", "inquiry_03",
                    "route_01", "route_02", "route_03", "route_06", "route_07", "payload_06"]
        by_input = {case["input"]: case for case in dataset["scenarios"]}
        requests = []

        def model_http(_url, payload, _timeout):
            current = json.loads(next(message["content"] for message in payload["messages"] if message["role"] == "user"))
            case = by_input[current["current_request"]]
            requests.append(case["id"])
            content = "本轮只读取状态。" if any(message["role"] == "tool" for message in payload["messages"]) else ""
            message = {"role": "assistant", "content": content}
            if not content:
                message["tool_calls"] = [{"function": {"name": case["expected_action"], "arguments": case["expected_arguments"]}}]
            return {"message": message, "done_reason": "stop", "prompt_eval_count": 50, "eval_count": 10}

        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                patch("tools.bench.cockpit_intents.http_transport", side_effect=model_http):
            output = Path(directory) / "report.json"
            self.assertEqual(main(["--surface", "service", "--live", "--only-ids", ",".join(selected),
                                   "--max-samples", "20", "--max-model-calls", "30", "--max-seconds", "30",
                                   "--max-tokens", "300000", "--output", str(output)]), 0)
            report = json.loads(output.read_text(encoding="utf-8"))
        rows = {row["id"]: row for row in report["results"]}
        self.assertEqual(report["summary"]["correct"], len(selected))
        self.assertEqual(report["summary"]["missing"], 102 - len(selected))
        for case_id in ("missing_07", "injection_02"):
            self.assertEqual(rows[case_id]["service_status"], "clarification")
            self.assertEqual(rows[case_id]["model_calls"], 0)
            self.assertNotIn(case_id, requests)
        self.assertEqual(rows["negation_01"]["service_status"], "final")
        self.assertEqual(rows["negation_01"]["model_calls"], 0)
        self.assertNotIn("negation_01", requests)
        self.assertEqual(rows["movement_01"]["preview_calls"], 1)
        self.assertEqual(rows["inquiry_03"]["read_only_tool_calls"], 0)
        self.assertEqual(rows["inquiry_03"]["model_calls"], 0)
        self.assertEqual(rows["inquiry_03"]["service_stage"], "direct_state_query")
        self.assertEqual(rows["inquiry_03"]["observed_action"], "read_state")
        for case_id in selected:
            self.assertTrue(rows[case_id]["state_unchanged"])
            self.assertEqual(rows[case_id]["confirmations"], 0)
            self.assertEqual(rows[case_id]["mutating_tool_calls"], 0)
        usage = report["run"]["usage"]
        self.assertEqual(usage["service_requests"], len(selected))
        self.assertEqual(usage["model_calls"], len(requests))
        self.assertEqual(usage["read_only_tool_calls"], 0)
        self.assertEqual(usage["confirmations"], 0)
        self.assertEqual(usage["mutating_tool_calls"], 0)

    def test_service_failure_keeps_denominator_and_unknown_usage(self):
        from tools.bench.cockpit_service_intents import evaluate_service_outcomes, run_service_live
        dataset = load_scenarios(SERVICE_FIXTURE)
        for response, failure, outcome in ((None, TimeoutError("synthetic timeout"), "timeout"),
                                           (None, ValueError("synthetic transport error"), "error"),
                                           ([], None, "error")):
            with self.subTest(outcome=outcome, response=response), redirect_stderr(io.StringIO()), \
                    patch("tools.bench.cockpit_intents.http_transport", return_value=response, side_effect=failure):
                results, usage = run_service_live(dataset, model="contract-model", base_url="http://model.invalid",
                    max_samples=1, max_model_calls=2, max_seconds=10, max_tokens=100000, timeout=2,
                    only_ids=["movement_01"])
            report = evaluate_service_outcomes(dataset, results)
            self.assertEqual(report["summary"]["denominator"], 102)
            self.assertEqual(report["summary"][outcome], 1)
            self.assertEqual(report["summary"]["missing"], 101)
            self.assertEqual(usage["model_calls"], 1)
            self.assertFalse(usage["token_usage_complete"])
            self.assertEqual(usage["unknown_usage_calls"], 1)
            self.assertIsNone(usage["prompt_tokens"])
            self.assertEqual(usage["confirmations"], 0)

    def test_service_typed_rejection_is_not_scored_as_a_preview(self):
        from tools.bench.cockpit_service_intents import evaluate_service_outcomes, run_service_live
        dataset = load_scenarios(SERVICE_FIXTURE)
        case = next(case for case in dataset["scenarios"] if case["id"] == "route_06")
        # Independent counterexample: actual simulator is paused, but execute needs uploaded.
        case["initial_state"]["route_status"] = "paused"
        response = {"message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "execute_route", "arguments": {}}}]}, "done_reason": "stop",
            "prompt_eval_count": 50, "eval_count": 10}
        with patch("tools.bench.cockpit_intents.http_transport", return_value=response), redirect_stderr(io.StringIO()):
            outcomes, usage = run_service_live(dataset, model="contract-model", base_url="http://model.invalid",
                max_samples=1, max_model_calls=2, max_seconds=10, max_tokens=100000, timeout=2, only_ids=["route_06"])
        report = evaluate_service_outcomes(dataset, outcomes)
        row = next(row for row in report["results"] if row["id"] == "route_06")
        self.assertEqual(row["outcome"], "incorrect")
        self.assertEqual(row["observed_kind"], "rejected")
        self.assertEqual(row["service_error_code"], "ROUTE_STATE_MISMATCH")
        self.assertEqual(usage["preview_calls"], 1)
        self.assertEqual(usage["confirmations"], 0)
        self.assertEqual(usage["mutating_tool_calls"], 0)

    def test_total_deadline_keeps_interrupted_and_unrun_service_samples_distinct(self):
        from tools.bench.cockpit_service_intents import evaluate_service_outcomes, run_service_live
        dataset = load_scenarios(SERVICE_FIXTURE)
        clock, timeouts = [100.0], []

        def interrupted_http(_url, _payload, timeout):
            timeouts.append(timeout)
            clock[0] += timeout
            raise TimeoutError("synthetic overall deadline")

        with patch("tools.bench.cockpit_intents.http_transport", side_effect=interrupted_http), \
                patch("tools.bench.cockpit_intents.time.monotonic", side_effect=lambda: clock[0]), \
                redirect_stderr(io.StringIO()):
            outcomes, usage = run_service_live(dataset, model="contract-model", base_url="http://model.invalid",
                max_samples=102, max_model_calls=180, max_seconds=4, max_tokens=2000000, timeout=30)
        report = evaluate_service_outcomes(dataset, outcomes)
        self.assertEqual(timeouts, [4])
        self.assertEqual(report["summary"]["denominator"], 102)
        self.assertEqual(report["summary"]["uncertain"], 1)
        self.assertEqual(report["summary"]["timeout"], 0)
        self.assertEqual(report["summary"]["missing"], 101)
        self.assertEqual(usage["service_requests"], 1)
        self.assertEqual(usage["model_calls"], 1)
        self.assertEqual(usage["confirmations"], 0)

    def test_grounding_blocks_a_guessed_distance_in_real_service(self):
        from tools.bench.cockpit_service_intents import evaluate_service_outcomes, run_service_live
        dataset = load_scenarios(SERVICE_FIXTURE)
        response = {"message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "move_forward", "arguments": {"distance_m": 5}}}]}, "done_reason": "stop",
            "prompt_eval_count": 50, "eval_count": 10}
        # The expected action stays 10m: this tests the real policy, never changes a label to fit it.
        with patch("tools.bench.cockpit_intents.http_transport", return_value=response), redirect_stderr(io.StringIO()):
            outcomes, usage = run_service_live(dataset, model="contract-model", base_url="http://model.invalid",
                max_samples=1, max_model_calls=2, max_seconds=10, max_tokens=100000, timeout=2, only_ids=["movement_01"])
        row = evaluate_service_outcomes(dataset, outcomes)["results"][0]
        self.assertEqual(row["outcome"], "incorrect")
        self.assertEqual(row["service_status"], "clarification")
        self.assertEqual(usage["preview_calls"], 0)
        self.assertEqual(usage["model_calls"], 1)
        self.assertEqual(usage["mutating_tool_calls"], 0)

    def test_model_output_truncation_remains_a_typed_failure_with_known_usage(self):
        from tools.bench.cockpit_service_intents import evaluate_service_outcomes, run_service_live
        dataset = load_scenarios(SERVICE_FIXTURE)
        response = {"message": {"role": "assistant", "content": ""}, "done_reason": "length",
                    "prompt_eval_count": 50, "eval_count": 10}
        with patch("tools.bench.cockpit_intents.http_transport", return_value=response), redirect_stderr(io.StringIO()):
            outcomes, usage = run_service_live(dataset, model="contract-model", base_url="http://model.invalid",
                max_samples=1, max_model_calls=2, max_seconds=10, max_tokens=100000, timeout=2, only_ids=["movement_01"])
        row = evaluate_service_outcomes(dataset, outcomes)["results"][0]
        self.assertEqual(row["outcome"], "error")
        self.assertEqual(row["service_error_code"], "MODEL_OUTPUT_TRUNCATED")
        self.assertEqual(row["reason"], "model_output_truncated")
        self.assertEqual(usage["known_prompt_tokens"], 50)
        self.assertTrue(usage["token_usage_complete"])

    def test_service_fixture_rejects_pseudo_state_before_any_model_request(self):
        dataset = json.loads(SERVICE_FIXTURE.read_text(encoding="utf-8"))
        dataset["scenarios"][0]["initial_state"]["control_owned"] = True
        with tempfile.TemporaryDirectory() as directory, patch("tools.bench.cockpit_intents.http_transport") as http:
            fixture = Path(directory) / "invalid.json"
            fixture.write_text(json.dumps(dataset), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "real simulator state"):
                load_scenarios(fixture)
        http.assert_not_called()


if __name__ == "__main__":
    unittest.main()
