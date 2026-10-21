"""Frozen intent-contract data and evaluation accounting, never model accuracy tests."""

from __future__ import annotations

import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.bench.cockpit_intents import (
    DEFAULT_FIXTURE,
    evaluate_outcomes,
    load_scenarios,
    main,
    run_live,
)


class IntentScenarioContractTests(unittest.TestCase):
    def test_offline_cli_can_save_a_report_without_model_calls(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as stdout:
            output = Path(directory) / "evidence" / "report.json"
            self.assertEqual(main(["--output", str(output)]), 0)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["summary"]["denominator"], 100)
            self.assertEqual(report["summary"]["missing"], 100)
            self.assertEqual(report["run"]["usage"]["model_calls"], 0)
            self.assertEqual(stdout.getvalue(), "")

    def test_cli_selection_preserves_full_denominator_and_records_selected_ids(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            output = Path(directory) / "selection.json"
            main(["--only-ids", "multiple_01", "--output", str(output)])
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["run"]["selection_ids"], ["multiple_01"])
        self.assertEqual(report["summary"]["denominator"], 100)
        self.assertEqual(report["summary"]["missing"], 100)
        selected = [row for row in report["results"] if row["id"] == "multiple_01"]
        excluded = [row for row in report["results"] if row["id"] != "multiple_01"]
        self.assertEqual(selected[0]["reason"], "not_run")
        self.assertEqual({row["reason"] for row in excluded}, {"selection_not_run"})

    def test_cli_rejects_unknown_selection_before_running(self):
        with redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit) as raised:
            main(["--only-ids", "unknown-scenario"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("unknown scenario", stderr.getvalue())

    def test_frozen_inputs_are_unique_and_validate_against_current_action_specs(self):
        dataset = load_scenarios(DEFAULT_FIXTURE)
        scenarios = dataset["scenarios"]
        self.assertGreaterEqual(len(scenarios), 60)
        self.assertLessEqual(len(scenarios), 100)
        self.assertEqual(dataset["source"], "synthetic_reviewed_contract")
        self.assertEqual(dataset["accuracy_status"], "not_evaluable")
        self.assertEqual(len({case["id"] for case in scenarios}), len(scenarios))
        by_id = {case["id"]: case for case in scenarios}
        self.assertEqual(by_id["movement_01"]["expected_action"], "move_forward")
        self.assertEqual(by_id["movement_01"]["expected_arguments"], {"distance_m": 10})
        self.assertEqual(by_id["numerals_01"]["expected_arguments"], {"distance_m": 10})
        self.assertEqual(by_id["yaw_01"]["expected_action"], "turn_left")
        self.assertEqual(by_id["yaw_01"]["expected_arguments"], {"angle_deg": 90})
        self.assertEqual(by_id["yaw_02"]["expected_action"], "move_left")

    def _load_changed(self, change):
        dataset = json.loads(DEFAULT_FIXTURE.read_text(encoding="utf-8"))
        change(dataset)
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "invalid.json"
            fixture.write_text(json.dumps(dataset), encoding="utf-8")
            return load_scenarios(fixture)

    def test_duplicate_ids_cannot_silently_change_denominators(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self._load_changed(lambda data: data["scenarios"].append(copy.deepcopy(data["scenarios"][0])))

    def test_unknown_action_or_invalid_arguments_do_not_become_labels(self):
        for field, value in (("expected_action", "arbitrary_shell"),
                             ("expected_arguments", {"distance_m": 1001}),
                             ("expected_arguments", {"distance_m": True}),
                             ("expected_arguments", {"distance_m": 10, "execute": True})):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self._load_changed(lambda data: data["scenarios"][0].__setitem__(field, value))

    def test_non_action_expectation_cannot_carry_a_hidden_action(self):
        with self.assertRaises(ValueError):
            self._load_changed(lambda data: data["scenarios"][0].__setitem__("expected_kind", "rejected"))


class EvaluationAccountingTests(unittest.TestCase):
    def setUp(self):
        self.dataset = load_scenarios(DEFAULT_FIXTURE)
        self.dataset["scenarios"] = [
            {"id": f"case-{i}", "category": "test-contract", "input": f"输入{i}",
             "expected_kind": "action", "expected_action": "move_forward",
             "expected_arguments": {"distance_m": 10}}
            for i in range(6)
        ]

    def test_missing_timeout_error_and_uncertain_remain_in_original_denominator(self):
        outcomes = {
            "case-0": {"kind": "action", "action": "move_forward", "arguments": {"distance_m": 10}},
            "case-1": {"kind": "action", "action": "move_backward", "arguments": {"distance_m": 10}},
            "case-2": {"status": "uncertain", "reason": "unstructured_final"},
            "case-4": {"status": "timeout", "reason": "model_timeout"},
            "case-5": {"status": "error", "reason": "model_error"},
        }
        report = evaluate_outcomes(self.dataset, outcomes)
        self.assertEqual(report["summary"]["denominator"], 6)
        self.assertEqual({name: report["summary"][name] for name in
                          ("correct", "incorrect", "uncertain", "missing", "timeout", "error")},
                         {"correct": 1, "incorrect": 1, "uncertain": 1, "missing": 1, "timeout": 1, "error": 1})
        self.assertEqual(report["by_category"]["test-contract"], report["summary"])
        self.assertEqual(len(report["results"]), 6)
        self.assertEqual(report["summary"]["contract_match_rate"], 1 / 6)
        self.assertEqual(report["summary"]["coverage"], 5 / 6)
        self.assertIsNone(report["accuracy"])
        self.assertEqual(report["accuracy_status"], "not_evaluable")

    def test_offline_report_makes_no_claim_that_synthetic_inputs_were_understood(self):
        report = evaluate_outcomes(self.dataset, {})
        self.assertEqual(report["summary"]["missing"], 6)
        self.assertEqual(report["summary"]["correct"], 0)
        self.assertEqual(report["summary"]["coverage"], 0)
        self.assertIsNone(report["summary"]["contract_match_rate"])

    def test_parameter_errors_and_extra_parameters_are_incorrect(self):
        for arguments in ({"distance_m": 11}, {"distance_m": 10, "heading": 0}, {"distance_m": True}):
            with self.subTest(arguments=arguments):
                report = evaluate_outcomes(self.dataset, {
                    "case-0": {"kind": "action", "action": "move_forward", "arguments": arguments}
                })
                self.assertEqual(report["summary"]["incorrect"], 1)
                self.assertEqual(report["summary"]["missing"], 5)

    def test_unknown_outcome_ids_fail_instead_of_silently_merging_runs(self):
        with self.assertRaisesRegex(ValueError, "unknown"):
            evaluate_outcomes(self.dataset, {"different-dataset-id": {"status": "error"}})

    def test_unrecognized_outputs_stay_visible_as_errors(self):
        for outcome in ({"kind": "succeeded"}, {"kind": []}, {"status": {"error": True}}, []):
            with self.subTest(outcome=outcome):
                report = evaluate_outcomes(self.dataset, {"case-0": outcome})
                self.assertEqual(report["summary"]["error"], 1)
                self.assertEqual(report["summary"]["denominator"], 6)
                self.assertEqual(report["results"][0]["reason"], "invalid_outcome")

    def test_unsafe_proposals_and_conservative_mismatches_are_distinct(self):
        self.dataset["scenarios"][0].update(expected_kind="no_action", expected_action=None, expected_arguments={})
        self.dataset["scenarios"][1].update(expected_kind="rejected", expected_action=None, expected_arguments={})
        report = evaluate_outcomes(self.dataset, {
            "case-0": {"kind": "clarification"},
            "case-1": {"kind": "action", "action": "takeoff", "arguments": {"altitude_m": 30}},
        })
        self.assertEqual(report["summary"]["incorrect"], 2)
        self.assertEqual(report["summary"]["conservative_non_action_mismatches"], 1)
        self.assertEqual(report["summary"]["unsafe_action_proposals"], 1)

    def test_read_only_mismatch_and_mutating_proposal_have_separate_counts(self):
        self.dataset["scenarios"][0].update(expected_kind="clarification", expected_action=None, expected_arguments={})
        self.dataset["scenarios"][1].update(expected_kind="no_action", expected_action=None, expected_arguments={})
        self.dataset["scenarios"][3].update(expected_action="read_state", expected_arguments={})
        report = evaluate_outcomes(self.dataset, {
            "case-0": {"kind": "action", "action": "read_state", "arguments": {}},
            "case-1": {"kind": "action", "action": "takeoff", "arguments": {"altitude_m": 30}},
            "case-2": {"kind": "action", "action": "move_backward", "arguments": {"distance_m": 5}},
            "case-3": {"kind": "action", "action": "read_state", "arguments": {}},
        })
        summary = report["summary"]
        self.assertEqual(summary["denominator"], 6)
        self.assertEqual(summary["incorrect"], 3)
        self.assertEqual(summary["correct"], 1)
        self.assertEqual(summary["missing"], 2)
        # The legacy counter retains its historical meaning, including reads.
        self.assertEqual(summary["unsafe_action_proposals"], 2)
        self.assertEqual(summary["unsafe_mutating_action_proposals"], 1)
        self.assertEqual(summary["unexpected_read_only_action_proposals"], 1)
        self.assertEqual(report["by_category"]["test-contract"], summary)
        rows = report["results"]
        self.assertTrue(rows[0]["unsafe_action_proposal"])
        self.assertFalse(rows[0]["unsafe_mutating_action_proposal"])
        self.assertTrue(rows[0]["unexpected_read_only_action_proposal"])
        self.assertTrue(rows[1]["unsafe_mutating_action_proposal"])
        self.assertFalse(rows[1]["unexpected_read_only_action_proposal"])
        for row in rows[2:]:
            self.assertFalse(row["unsafe_mutating_action_proposal"])
            self.assertFalse(row["unexpected_read_only_action_proposal"])

    def test_free_text_cannot_be_scored_as_a_typed_rejection(self):
        self.dataset["scenarios"][0].update(expected_kind="rejected", expected_action=None, expected_arguments={})
        report = evaluate_outcomes(self.dataset, {
            "case-0": {"kind": "no_action", "basis": "unstructured_final"},
        })
        self.assertEqual(report["summary"]["uncertain"], 1)
        self.assertEqual(report["summary"]["correct"], 0)


class LiveHarnessContractTests(unittest.TestCase):
    def test_total_deadline_interrupts_the_attempt_without_losing_unrun_samples(self):
        dataset = load_scenarios(DEFAULT_FIXTURE)
        clock = [100.0]
        request_timeouts = []

        def model_http(_url, _payload, timeout):
            request_timeouts.append(timeout)
            clock[0] += timeout
            raise TimeoutError("synthetic interrupted HTTP boundary")

        with patch("tools.bench.cockpit_intents.time.monotonic", side_effect=lambda: clock[0]), \
                patch("tools.bench.cockpit_intents.http_transport", side_effect=model_http), \
                redirect_stderr(io.StringIO()):
            outcomes, usage = run_live(dataset, model="contract-model", base_url="http://model.invalid",
                                       max_samples=100, max_model_calls=160, max_seconds=4,
                                       max_tokens=100000, timeout=30)
        self.assertEqual(request_timeouts, [4])
        self.assertEqual(outcomes["movement_01"]["status"], "uncertain")
        self.assertEqual(outcomes["movement_01"]["reason"], "run_budget_exhausted")
        self.assertEqual(outcomes["movement_01"]["latency_ms"], 4000)
        self.assertIsNone(outcomes["movement_01"]["prompt_tokens"])
        self.assertIsNone(outcomes["movement_01"]["output_tokens"])
        self.assertEqual(usage["model_calls"], 1)
        self.assertEqual(usage["mutating_tool_calls"], 0)
        report = evaluate_outcomes(dataset, outcomes)
        self.assertEqual(report["summary"]["denominator"], 100)
        self.assertEqual(report["summary"]["uncertain"], 1)
        self.assertEqual(report["summary"]["timeout"], 0)
        self.assertEqual(report["summary"]["missing"], 99)
        self.assertEqual({row["reason"] for row in report["results"]}, {"run_budget_exhausted"})

    def test_request_timeout_with_time_remaining_does_not_cancel_later_samples(self):
        dataset = load_scenarios(DEFAULT_FIXTURE)
        dataset["scenarios"] = dataset["scenarios"][:2]
        clock = [100.0]
        request_timeouts = []

        def model_http(_url, _payload, timeout):
            request_timeouts.append(timeout)
            clock[0] += 2
            if len(request_timeouts) == 1:
                raise TimeoutError("synthetic request timeout")
            return {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "move_backward", "arguments": {"distance_m": 5}}}
            ]}, "done_reason": "stop", "prompt_eval_count": 50, "eval_count": 20}

        with patch("tools.bench.cockpit_intents.time.monotonic", side_effect=lambda: clock[0]), \
                patch("tools.bench.cockpit_intents.http_transport", side_effect=model_http), \
                redirect_stderr(io.StringIO()):
            outcomes, usage = run_live(dataset, model="contract-model", base_url="http://model.invalid",
                                       max_samples=2, max_model_calls=4, max_seconds=20,
                                       max_tokens=100000, timeout=2)
        self.assertEqual(request_timeouts, [2, 2])
        self.assertEqual(outcomes["movement_01"]["status"], "timeout")
        self.assertEqual(outcomes["movement_01"]["reason"], "model_timeout")
        self.assertEqual(outcomes["movement_02"]["action"], "move_backward")
        self.assertEqual(usage["model_calls"], 2)
        report = evaluate_outcomes(dataset, outcomes)
        self.assertEqual(report["summary"]["denominator"], 2)
        self.assertEqual(report["summary"]["correct"], 1)
        self.assertEqual(report["summary"]["timeout"], 1)
        self.assertEqual(report["summary"]["missing"], 0)

    def test_shortened_request_failing_early_is_still_a_request_timeout(self):
        dataset = load_scenarios(DEFAULT_FIXTURE)
        dataset["scenarios"] = dataset["scenarios"][:1]
        clock = [100.0]

        def model_http(_url, _payload, _timeout):
            clock[0] += 1
            raise TimeoutError("synthetic early timeout from the HTTP boundary")

        with patch("tools.bench.cockpit_intents.time.monotonic", side_effect=lambda: clock[0]), \
                patch("tools.bench.cockpit_intents.http_transport", side_effect=model_http), \
                redirect_stderr(io.StringIO()):
            outcomes, _ = run_live(dataset, model="contract-model", base_url="http://model.invalid",
                                   max_samples=1, max_model_calls=2, max_seconds=5,
                                   max_tokens=100000, timeout=30)
        self.assertEqual(outcomes["movement_01"]["status"], "timeout")
        self.assertEqual(outcomes["movement_01"]["reason"], "model_timeout")
        self.assertEqual(outcomes["movement_01"]["latency_ms"], 1000)

    def test_selecting_one_late_sample_calls_http_once_and_keeps_all_other_samples(self):
        dataset = load_scenarios(DEFAULT_FIXTURE)
        response = {"message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "move_forward", "arguments": {"distance_m": 10}}}
        ]}, "done_reason": "stop", "prompt_eval_count": 50, "eval_count": 20}
        with patch("tools.bench.cockpit_intents.http_transport", return_value=response) as http, \
                redirect_stderr(io.StringIO()):
            outcomes, usage = run_live(dataset, model="contract-model", base_url="http://model.invalid",
                                       max_samples=1, max_model_calls=2, max_seconds=5,
                                       max_tokens=100000, timeout=2, only_ids=["multiple_01"])
        self.assertEqual(http.call_count, 1)
        self.assertEqual(usage["model_calls"], 1)
        self.assertEqual(outcomes["multiple_01"]["kind"], "action")
        report = evaluate_outcomes(dataset, outcomes)
        self.assertEqual(report["summary"]["denominator"], 100)
        self.assertEqual(report["summary"]["missing"], 99)
        self.assertEqual({row["reason"] for row in report["results"] if row["id"] != "multiple_01"},
                         {"selection_not_run"})

    def test_live_unknown_selection_fails_before_any_http_call(self):
        with patch("tools.bench.cockpit_intents.http_transport") as http, self.assertRaisesRegex(ValueError, "unknown"):
            run_live(load_scenarios(DEFAULT_FIXTURE), model="contract-model", base_url="http://model.invalid",
                     max_samples=1, max_model_calls=2, max_seconds=5, max_tokens=100000, timeout=2,
                     only_ids=["different-fixture-id"])
        http.assert_not_called()

    def test_public_live_path_sends_a_bounded_real_ollama_payload_and_only_proposes(self):
        """Injected HTTP response tests the harness, not language-understanding accuracy."""
        dataset = load_scenarios(DEFAULT_FIXTURE)
        dataset["scenarios"] = [dataset["scenarios"][0]]
        requests = []

        def model_http(url, payload, timeout):
            requests.append((url, payload, timeout))
            return {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "move_forward", "arguments": {"distance_m": 10}}}
            ]}, "done_reason": "stop", "prompt_eval_count": 50, "eval_count": 20}

        with patch("tools.bench.cockpit_intents.http_transport", side_effect=model_http), \
                redirect_stderr(io.StringIO()) as progress:
            outcomes, usage = run_live(dataset, model="contract-model", base_url="http://model.invalid",
                                       max_samples=1, max_model_calls=2, max_seconds=5,
                                       max_tokens=100000, timeout=2)
        self.assertEqual(usage["model_calls"], 1)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0][0], "http://model.invalid/api/chat")
        self.assertEqual(requests[0][1]["options"]["num_predict"], 2048)
        self.assertEqual(outcomes["movement_01"]["kind"], "action")
        self.assertEqual(outcomes["movement_01"]["action"], "move_forward")
        self.assertEqual(usage["mutating_tool_calls"], 0)
        self.assertEqual(usage["platform_calls"], 0)
        self.assertEqual(usage["confirmations"], 0)
        self.assertEqual(set(json.loads(progress.getvalue())), {"id", "outcome", "latency_ms"})


if __name__ == "__main__":
    unittest.main()
