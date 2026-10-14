"""Synthetic regression: raised request budgets must serialize persisted replies."""
import tempfile
import unittest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from app import create_app
from response_models import ResultMetadata

class ExtendedResponseTests(unittest.TestCase):
    def test_extended_budget_persisted_api_readback(self):
        with tempfile.TemporaryDirectory() as folder:
            app = create_app(directory=folder, start_worker=False)
            with TestClient(app) as client:
                posted = client.post('/api/v1/discoveries', json={'keywords':['synthetic'], 'limit':10, 'budget':{'seconds':1200,'max_turns':120}})
                self.assertEqual(posted.status_code, 202)
                ident = posted.json()['id']
                metadata = {'source':'web','outcome':'partial','blocked_reason':None,'rejected_count':0,'charged_turns':120,'reserved_turns':120,'budget_accounting':'full_cli_reservation','measured_model_iterations':None,'stop_reason':'turn_limit','evidence_validation':'verbatim_observed_tool_output','fit_reason_is_inference':True,'contact_permission_established':False}
                app.state.store.finish(ident,'partial',{'leads':[],'returned_count':0,'requested_count':10,'missing_count':10,'metadata':metadata})
                reply = client.get('/api/v1/discoveries/'+ident)
                self.assertEqual(reply.status_code,200)
                self.assertEqual(reply.json()['result']['metadata']['charged_turns'],120)
                with self.assertRaises(ValidationError):
                    ResultMetadata.model_validate({**metadata,'charged_turns':121})
                self.assertEqual(client.post('/api/v1/discoveries',json={'keywords':['synthetic'],'budget':{'seconds':1801}}).status_code,422)
                self.assertEqual(client.post('/api/v1/discoveries',json={'keywords':['synthetic'],'budget':{'max_turns':121}}).status_code,422)
