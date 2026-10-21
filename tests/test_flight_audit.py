import contextlib
import io
import json
from types import SimpleNamespace
import unittest
from lingxi_flight_control.audit import gateway_event, digest, numbers, tool_parameters


class FlightAuditTests(unittest.TestCase):
    def test_gateway_audit_correlates_without_credentials_or_original_text(self):
        stream=io.StringIO()
        response=SimpleNamespace(status_code=200,payload={'status':'preview_required',
            'card':{'command_id':'cmd1','action':'move_forward','parameters':{'distance_m':10,
                'token':'private-token','longitude':113}},'trace_id':'trace1'})
        with contextlib.redirect_stdout(stream):
            gateway_event('/api/flight-control/agent/messages',
                b'{"text":"private instruction","request_id":"req1"}',
                {'Authorization':'private-token','X-Flight-Task':'task1','X-Agent-Session':'session1'},response,42)
        event=json.loads(stream.getvalue())
        self.assertNotIn('private',stream.getvalue())
        self.assertEqual(event['parameters'],{'distance_m':10})
        self.assertEqual(event['task_ref'],digest('task1'))
        self.assertEqual(event['command_id'],'cmd1')
        self.assertEqual(event['elapsed_ms'],42)

    def test_parameters_are_small_fixed_numeric_projection(self):
        self.assertEqual(numbers({'angle_deg':10,'value':float('nan'),
            'body':{'password':'secret'},'param':'secret'}),{'angle_deg':10})

    def test_registered_enums_are_auditable_without_raw_text_or_coordinates(self):
        self.assertEqual(tool_parameters('camera_live_quality',{'quality':'hd','token':'private'}),{'quality':'hd'})
        self.assertEqual(tool_parameters('camera_point_focus',{'lens':'zoom','x':0.2,'y':0.3}),{'lens':'zoom'})
        self.assertEqual(tool_parameters('camera_live_quality',{'quality':'private'}),{})

    def test_reader_filters_by_task_and_never_claims_device_completion(self):
        import importlib.util
        from pathlib import Path
        spec=importlib.util.spec_from_file_location('flight_audit_tool',Path(__file__).parents[1]/'tools/flight_audit.py')
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        rows=[json.dumps({'event':'flight_gateway','time':t,'task_ref':task,'private':'secret'})
              for t,task in [(10,'a'),(20,'a'),(20,'b')]]
        result=module.select(rows+['raw private log'],task_ref='a',after=15)
        self.assertEqual(result,[{'event':'flight_gateway','time':20,'task_ref':'a'}])
