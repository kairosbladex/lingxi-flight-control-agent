#!/usr/bin/env python3
"""Bounded diagnostic pairs: change only think, propose once, never execute.

No prompt/response/hidden reasoning text is retained. This is a timing probe,
not an accuracy benchmark or a justification to change production defaults.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
import urllib.request
from uuid import uuid4

from lingxi_flight_control import AgentCheckpoint, OllamaDecider
from lingxi_flight_control.cockpit import COCKPIT_SYSTEM_PROMPT, build_proposal_registry
from lingxi_flight_control.ollama_decider import (
    DEFAULT_BASE_URL, DEFAULT_MODEL, build_tools_payload, http_transport,
)
from cockpit_intents import (
    DEFAULT_FIXTURE, REPOSITORY_ROOT, _EvaluationExecutor, _git_value, _sha256,
    _source_hashes, load_scenarios,
)


def source_hashes():
    path = Path(__file__)
    return {**_source_hashes(), str(path.relative_to(REPOSITORY_ROOT)):
            hashlib.sha256(path.read_bytes()).hexdigest()}


def metadata():
    result = {}
    for endpoint in ('version', 'tags', 'ps'):
        try:
            with urllib.request.urlopen(DEFAULT_BASE_URL + '/api/' + endpoint, timeout=5) as response:
                data = json.load(response)
            if endpoint == 'version':
                result['server_version'] = data.get('version')
            elif endpoint == 'tags':
                matches = [item for item in data.get('models', []) if item.get('name') == DEFAULT_MODEL]
                result['model_digest'] = matches[0].get('digest') if matches else None
            else:
                digest = result.get('model_digest')
                result['resident'] = (any(item.get('digest') == digest
                                          for item in data.get('models', [])) if digest else None)
                result['loaded_model_count'] = len(data.get('models', []))
        except Exception as exc:
            result[endpoint + '_error_type'] = type(exc).__name__
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output already exists; choose a new path to preserve prior evidence')
    data = load_scenarios(DEFAULT_FIXTURE)
    ids = ('movement_01', 'missing_01', 'yaw_06', 'limits_02')
    cases = [case for case in data['scenarios'] if case['id'] in ids]
    sources = source_hashes()
    run = {'run_id': str(uuid4()), 'started_at': datetime.now(timezone.utc).isoformat(),
           'code_sha': _git_value('rev-parse', 'HEAD'),
           'branch': _git_value('branch', '--show-current'),
           'dirty_worktree': bool(_git_value('status', '--porcelain')),
           'source_sha256': sources, 'actor': 'latency-probe-synthetic-actor',
           'provider': 'ollama', 'endpoint_sha256': _sha256(DEFAULT_BASE_URL),
           'tool_schema_sha256': _sha256(build_tools_payload(
               build_proposal_registry(_EvaluationExecutor({})).definitions())),
           'context_sha256': _sha256({key: data.get(key, {})
                                     for key in ('context', 'context_overrides')}),
           'model_options': {'temperature': 0, 'num_ctx': 32768, 'num_predict': 2048,
                             'max_attempts': 1, 'keep_alive': '30m', 'tool_selection': 'full'}}
    before = metadata()
    deadline = time.monotonic() + 300
    rows = []
    for index, case in enumerate(cases):
        # Alternating order limits (but does not eliminate) warm-cache bias.
        for variant in (('auto', 'off') if index % 2 == 0 else ('off', 'auto')):
            row = {'id': case['id'], 'variant': variant}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                rows.append({**row, 'status': 'missing', 'reason': 'run_budget_exhausted'})
                continue
            def transport(url, payload, timeout):
                response = http_transport(url, payload, min(timeout, max(.01, deadline - time.monotonic())))
                fields = ('done_reason', 'prompt_eval_count', 'eval_count', 'load_duration',
                          'prompt_eval_duration', 'eval_duration', 'total_duration')
                row['generation'] = {key: response.get(key) for key in fields}
                return response
            context = {**data['context'], **data.get('context_overrides', {}).get(case['id'], {})}
            executor = _EvaluationExecutor(context)
            text = json.dumps({'current_request': case['input'], 'original_intent': case['input'],
                               'conversation': [], 'state': executor.read_state()}, ensure_ascii=False)
            checkpoint = AgentCheckpoint(text, {}, [], [], [])
            brain = OllamaDecider(model=DEFAULT_MODEL, base_url=DEFAULT_BASE_URL,
                system_prompt=COCKPIT_SYSTEM_PROMPT, think=False if variant == 'off' else None,
                timeout=min(35, remaining), max_attempts=1, max_output_tokens=2048,
                transport=transport, keep_alive='30m')
            started = time.monotonic()
            try:
                decision = brain.decide(text, checkpoint, build_proposal_registry(executor).definitions())
                row.update(status='returned', kind=decision.kind.value,
                           tool=decision.tool_call.name if decision.tool_call else None,
                           arguments=dict(decision.tool_call.arguments) if decision.tool_call else None)
            except Exception as exc:
                row.update(status='error', error_type=type(exc).__name__)
            row['wall_ms'] = round((time.monotonic() - started) * 1000, 3)
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    after = metadata()
    run.update(finished_at=datetime.now(timezone.utc).isoformat(),
               source_changed_during_run=sources != source_hashes())
    report = {'schema_version': 'cockpit-latency-probe/v1', 'recorded_at': datetime.now(timezone.utc).isoformat(),
        'scope': 'single_decision_diagnostic_pairs_only', 'model': DEFAULT_MODEL, 'run': run,
        'before': before, 'after': after,
        'model_digest_unchanged': (before['model_digest'] == after['model_digest']
                                 if before.get('model_digest') and after.get('model_digest') else None),
        'fixture_sha256': data['fixture_sha256'],
        'prompt_sha256': hashlib.sha256(COCKPIT_SYSTEM_PROMPT.encode()).hexdigest(),
        'budgets': {'max_model_calls': 8, 'max_model_seconds': 300, 'request_timeout_sec': 35,
                    'output_tokens_per_call': 2048},
        'rows': rows, 'mutating_tool_calls': 0, 'platform_calls': 0, 'confirmations': 0,
        'limitations': ['Shared model service; no isolated hardware or independent flight truth.',
                       'One pair per input; single decisions do not prove full-loop latency or accuracy.',
                       'Missing duration fields remain unknown; hidden reasoning and response text are not saved.']}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf8')


if __name__ == '__main__':
    main()
