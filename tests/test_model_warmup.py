import io
import json
import unittest
from unittest.mock import patch

from tools.deploy.warm_flight_model import warm


class ModelWarmupTests(unittest.TestCase):
    def test_permanent_residency_uses_numeric_wire_value(self):
        from lingxi_flight_control import OllamaDecider, AgentCheckpoint
        decider = OllamaDecider(keep_alive=-1)
        body = decider.payload('synthetic', AgentCheckpoint('synthetic', {}, [], [], []), ())
        self.assertEqual(body['keep_alive'], -1)
        self.assertIs(type(body['keep_alive']), int)

    def test_readiness_requires_resident_gpu_and_preserves_permanent_keep_alive(self):
        replies = [{'version': 'fixture'}, {'done': True}, {'models': [
            {'name': 'qwen3.5:9b', 'context_length': 32768, 'size_vram': 100}]}]
        with patch('tools.deploy.warm_flight_model.urlopen', side_effect=[
                io.BytesIO(json.dumps(r).encode()) for r in replies]) as transport:
            warm('http://model.invalid')
        payload = json.loads(transport.call_args_list[1].args[0].data)
        self.assertEqual(payload['keep_alive'], -1)
        self.assertEqual(payload['options']['num_ctx'], 32768)
        self.assertNotIn('prompt', payload)
        self.assertNotIn('messages', payload)

    def test_missing_residency_fails_startup(self):
        with patch('tools.deploy.warm_flight_model.urlopen', side_effect=[
                io.BytesIO(json.dumps(r).encode()) for r in
                ({'version': 'fixture'}, {'done': True}, {'models': []})]):
            with self.assertRaisesRegex(RuntimeError, 'resident'):
                warm('http://model.invalid')
