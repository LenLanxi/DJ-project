import json
import math
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

import cv2
from continuous_follow_sim import run, SCENARIOS, MATRIX
from image_loop_lab import render, service_loop
from r3_follow.vision import MarkerTracker
from session_export import export_session


class PreintegrationTests(unittest.TestCase):
    def test_parameter_matrix_and_stress_invariants(self):
        for case in (*SCENARIOS,*MATRIX):
            with self.subTest(case=case.name):
                rows, report = run(case)
                self.assertEqual(report['nontracking_nonzero'],0)
                self.assertLessEqual(report['max_request'],.3)
                self.assertTrue(all(math.isfinite(r[k]) for r in rows for k in
                                    ('right','forward','x_error','size_error','velocity_x','velocity_z')))
                if case.delay > .5:
                    self.assertEqual(rows[-1]['state'],'frame_stale')

    def test_generated_truth_and_determinism(self):
        a, truth=render(); b,_=render()
        self.assertEqual(a.tobytes(),b.tobytes())
        self.assertEqual(truth['category'],'visible_target')
        self.assertTrue(MarkerTracker().detect(a,0,0).found)
        for effect in ('wrong_id','out_of_view','occluded'):
            frame, truth=render(effect=effect)
            self.assertFalse(truth['full_target_visible'])
            if effect != 'occluded':
                self.assertFalse(MarkerTracker().detect(frame,0,0).found)

    def test_rendered_service_loops_and_handoffs(self):
        cv2.setNumThreads(1)
        expected={'clean':'tracking','loss':'tracking','wrong_id':'target_lost',
                  'freeze':'frame_stale','handoff':'tracking','late_completion':'stopped',
                  'action_failed':'stopped','control_busy':'stopped','send_failure':'stopped'}
        with patch('socket.socket',side_effect=AssertionError('offline')), \
             patch('cv2.VideoCapture',side_effect=AssertionError('no camera')):
            for case, final in expected.items():
                with self.subTest(case=case):
                    rows, events=service_loop(case,steps=55)
                    self.assertEqual(rows[-1]['state'],final)
                    for row in rows:
                        if row['state']!='tracking':
                            self.assertEqual((row['right'],row['forward']),(0,0))
                    motions=[e for e in events if e['event']=='motion_request']
                    for prev,cur in zip(motions,motions[1:]):
                        if cur['channels'] != (0,0,0,0) and cur['time']-prev['time'] < .1-1e-8:
                            self.fail('Nonzero requests exceed 10Hz')

    def test_export_closed_session_only_and_no_unrelated_files(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'demo_1.jsonl'
            p.write_text('{"event":"session_start"}\n',encoding='utf-8')
            with self.assertRaises(ValueError):export_session(p)
            p.write_text(json.dumps(dict(event='session_summary',app_version='0.10',source='synthetic'))+'\n',encoding='utf-8')
            (p.parent/'demo_2.jsonl').write_text('private',encoding='utf-8')
            archive=export_session(p)
            with zipfile.ZipFile(archive) as z:
                self.assertIn('demo_1.jsonl',z.namelist())
                self.assertNotIn('demo_2.jsonl',z.namelist())
                self.assertIn('manifest.json',z.namelist())
                self.assertEqual(json.loads(z.read('manifest.json'))['version'],'0.10')

    def test_export_rejects_wrong_owner_and_path_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td)
            (folder/'diagnostic_2.jsonl').write_text(json.dumps(dict(test_context={'session_log':'demo_2.jsonl'}))+'\n',encoding='utf-8')
            rows=[dict(event='short_test_start',diagnostic_log='../private.jsonl'),
                  dict(event='short_test_start',diagnostic_log='diagnostic_2.jsonl'),dict(event='session_summary')]
            p=folder/'demo_1.jsonl';p.write_text('\n'.join(map(json.dumps,rows)),encoding='utf-8')
            with zipfile.ZipFile(export_session(p)) as z:
                self.assertNotIn('diagnostic_2.jsonl',z.namelist())
                self.assertEqual(len(json.loads(z.read('manifest.json'))['skipped_unverified_links']),2)


if __name__ == '__main__':
    unittest.main()
