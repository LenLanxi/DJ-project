"""Frame replies retain evidence while cloud summaries omit binary media."""
import unittest
from unittest.mock import AsyncMock
from execution.ActionRunner import ActionRunner, ActionOutcome
from app.Results import tool_result
from types import SimpleNamespace

class PayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_frame_summary_omits_media_but_preserves_frame_identity(self):
        host = SimpleNamespace(dispatch=AsyncMock(return_value={
            'frame_id': 7, 'timestamp': 100., 'image_url': 'data:image/jpeg;base64,testOnly'}))
        outcome = ActionOutcome()
        await ActionRunner().run({'action':'get_frame','args':[]}, host, outcome)
        projected = tool_result(outcome.result)
        self.assertTrue(projected['ok'])
        self.assertEqual(projected['journal'][0]['result']['image_url'], '[payload omitted]')
        self.assertEqual(projected['journal'][0]['result']['frame_id'], 7)
        self.assertNotIn('testOnly', repr(projected))

    async def test_python_input_and_unexposed_methods_never_reach_host(self):
        host = SimpleNamespace(dispatch=AsyncMock())
        for request in ('import os', {'code':'drone.land()'},
                        {'action':'__getattribute__','args':[]},
                        {'action':'move_forward','args':[True]}):
            with self.subTest(request=request), self.assertRaises(ValueError):
                await ActionRunner().run(request, host, ActionOutcome())
        host.dispatch.assert_not_awaited()
