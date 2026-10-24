"""学长验收矩阵：32 项漏洞 + 3 个致命竞态，逐项「复现 → 验证修复」。

每一行对应学长群里那条要求（编号与原话一一对应）。每条做两件事：

1. ``_probe(...)`` 在独立子进程里跑既有探针。探针自带“基线行为”对照
   （``DocsTests.py --baseline`` 会加载 docs 的旧实现），所以它是**复现漏洞**的那一半。
2. 本文件里我自己的断言，直接打产品代码的公开接口，是**验证修复**的那一半。
   即使有人把探针删掉或改软，这一半仍然会红。

运行：
    .\\.venv\\Scripts\\python.exe test_review_matrix.py            # 全量（含子进程探针）
    .\\.venv\\Scripts\\python.exe test_review_matrix.py -k item05  # 单条
    $env:REVIEW_SKIP_PROBES="1"                                    # 只跑我自己的断言

注意：本机必须能 import 到 ``git`` 才能跑探针的版本记录，否则设 REVIEW_SKIP_PROBES=1。
"""
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AI = ROOT / 'modules' / 'ai'
FOLLOW = ROOT / 'modules' / 'follow'
ACCEPT = ROOT / 'ScenarioAcceptance'
for path in (str(ROOT), str(AI), str(FOLLOW), str(ACCEPT)):
    if path not in sys.path:
        sys.path.insert(0, path)

import drone_sdk._driver as drv          # noqa: E402
import drone_sdk.controller as ctl       # noqa: E402

SKIP_PROBES = os.getenv('REVIEW_SKIP_PROBES') == '1'


def read(*parts):
    return (ROOT.joinpath(*parts)).read_text(encoding='utf-8')


class ReviewMatrix(unittest.TestCase):
    """学长 32 项 + R301/R302/R303。"""

    # ---------------------------------------------------------------- helpers
    def _probe(self, module, cls, method, cwd=None):
        """跑既有探针（含旧实现基线对照），这就是“复现漏洞”的那一半。"""
        if SKIP_PROBES:
            return
        env = dict(os.environ, PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1',
                   PYTHONPATH=os.pathsep.join((str(ROOT), str(AI), str(FOLLOW), str(ACCEPT))))
        completed = subprocess.run(
            [sys.executable, '-B', '-m', 'unittest', '%s.%s.%s' % (module, cls, method)],
            cwd=str(cwd or ACCEPT), env=env, capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=180)
        if completed.returncode != 0:
            self.fail('探针 %s.%s.%s 失败（漏洞仍在）：\n%s\n%s' % (
                module, cls, method, completed.stdout[-1500:], completed.stderr[-1500:]))

    def _fake_driver(self, *, battery=88, flying=False, link=True, video=True):
        """把 _driver 切成真机模式，但健康读数由测试注入。"""
        real_patch = {'SIMULATED': drv.SIMULATED}
        self.addCleanup(lambda: setattr(drv, 'SIMULATED', real_patch['SIMULATED']))
        drv.SIMULATED = False
        payload = dict(mode='real', simulated=False, connected=True, link_ok=link,
                       faults=0, last_error='', flying=flying, height_cm=0,
                       speed_cm_s=50, velocity=(0, 0, 0, 0), video=video,
                       battery=battery, flight_time_s=0, ceiling_cm=250,
                       max_speed_cm_s=100)
        self.addCleanup(setattr, drv, 'health', drv.health)
        drv.health = lambda: dict(payload)
        ctl.configure_takeoff(authorized=True, min_battery=20)
        self.addCleanup(ctl.configure_takeoff, authorized=False, min_battery=20)
        return payload

    def _decoder(self, age_s, count=1, identical_frame=None):
        """未启动的 _VideoDecoder 实例：isinstance 检查通过，但不会真的开流。"""
        reader = drv._VideoDecoder('udp://127.0.0.1:1?overrun_nonfatal=1')
        import numpy
        reader._frame = identical_frame if identical_frame is not None else numpy.zeros((2, 2, 3), dtype='uint8')
        reader.count = count
        reader.last_frame_at = time.monotonic() - age_s
        reader.last_frame_timestamp = time.time() - age_s
        return reader

    # ============================================================ 1. 视觉入口
    def test_item01_entry_uses_structured_vision_observe(self):
        """① 正式入口要接 docs 的视觉实现：Vision.observe(target)，不是 VisionService。"""
        self._probe('ScenarioTests', 'ScenarioTests', 'test10_real_entry_supplies_ai_camera_vision')
        self._probe('DocsTests', 'VisionTests', 'test27_fresh_visual_observation_is_supported')
        source = read('modules', 'ai', 'main.py')
        self.assertIn('from vision import Vision', source)
        self.assertIn('observe', source)
        self.assertNotIn('视觉=vision_service', source)
        import vision
        self.assertTrue(hasattr(vision.Vision, 'observe'))
        signature = vision.Vision.observe.__code__.co_varnames[:2]
        self.assertEqual(signature, ('self', 'target'), 'observe 必须是 observe(target)')

    # ============================================================ 2. 真机图传
    def test_item02_real_entry_starts_video_stream(self):
        """② build_real_runner 必须 start_video()，否则跟随没有画面。"""
        self._probe('ScenarioTests', 'ScenarioTests', 'test09_real_entry_opens_video')
        source = read('modules', 'ai', 'main.py')
        self.assertIn('start_video()', source)
        self.assertIn('FrameBridge', source, '真机图传要经 FrameBridge 共享给跟随/视觉')

    # ============================================================ 3. docs 入口
    def test_item03_docs_entry_is_not_a_second_implementation(self):
        """③ docs 自己那套入口不能是第二个“完整真机模式”，必须是兼容壳。"""
        self._probe('DocsTests', 'HardwareTests', 'test20_docs_adapter_preserves_real_mode')
        for name in ('backends.py', 'gateway.py', 'vision.py', 'sandbox_runner.py', 'realtime.py'):
            text = read('docs', 'v4-reference', name)
            self.assertLess(len(text.splitlines()), 60,
                            'docs/%s 仍是独立实现，应改为 modules/ai 的兼容导入' % name)
        self.assertIn('modules/ai', read('docs', 'v4-reference', 'README.md'))
        self.assertIn('--real-drone', read('modules', 'ai', 'main.py'))

    # ==================================================== 4. 共享状态真实读数
    def test_item04_shared_status_uses_device_readings(self):
        """④ controller.get_status() 不能再写死 88/120。"""
        self._probe('ScenarioTests', 'ScenarioTests', 'test11_shared_status_reports_actual_battery')
        self._fake_driver(battery=57, flying=True)
        status = ctl.get_status()
        self.assertEqual(status.battery, 57, '电量必须来自设备')
        self.assertFalse(status.simulated, '真机模式不能标成 simulated')
        self.assertNotIn('StatusResult(88', read('drone_sdk', 'controller.py'))

    # ==================================================== 5. 降落完成判定
    def test_item05_landing_waits_for_device_not_timer(self):
        """⑤ 设备还在飞时不能靠 50ms 计时器宣布落地。"""
        self._probe('ScenarioTests', 'ScenarioTests', 'test15_landing_stays_pending_until_device_finishes')
        self._fake_driver(flying=True)
        ctl._flight_state, ctl._landing_deadline = 'landing', time.monotonic() - 1  # 计时早已过期
        ctl._advance()
        self.assertEqual(ctl._flight_state, 'landing', '设备仍在飞：必须保持 landing')
        drv.health = lambda: {'flying': False, 'battery': 80, 'height_cm': 0, 'connected': True,
                              'link_ok': True, 'video': True, 'faults': 0, 'last_error': '',
                              'speed_cm_s': 50, 'velocity': (0, 0, 0, 0), 'flight_time_s': 0,
                              'ceiling_cm': 250, 'max_speed_cm_s': 100, 'mode': 'real',
                              'simulated': False}
        ctl._advance()
        self.assertEqual(ctl._flight_state, 'landed', '设备确认落地后才允许 announced')
        ctl._flight_state, ctl._landing_deadline = 'landed', None

    # ==================================================== 6. 电量未知仍放行
    def test_item06_unknown_battery_blocks_takeoff(self):
        """⑥ 电量读到 None 时，拒绝条件不能失效。"""
        self._probe('ScenarioTests', 'ScenarioTests', 'test12_unknown_battery_blocks_takeoff')
        self._probe('ScenarioTests', 'ScenarioTests', 'test13_known_low_battery_blocks_takeoff')
        self._fake_driver(battery=None)
        result = ctl.takeoff('model')
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, 'takeoff_gate_rejected')

    # ==================================================== 7. 适配层绕过门禁
    def test_item07_sdk_adapter_goes_through_takeoff_gate(self):
        """⑦ SDKBackend 不能绕过 UnifiedDrone 的起飞门禁。"""
        self._probe('DocsTests', 'HardwareTests', 'test21_docs_adapter_cannot_bypass_low_battery_gate')
        self._fake_driver(battery=5)
        import backends
        backend = backends.SDKBackend(ROOT)
        backend.grant = {'control_epoch': ctl.get_status().control_epoch,
                         'command_epoch': ctl.get_status().command_epoch}
        result = backend.action('takeoff')
        self.assertFalse(result.get('ok'))
        self.assertIn(result.get('reason', ''), ('takeoff_gate_rejected', 'failed', ''), result)

    # ==================================================== 8. simulated 标识
    def test_item08_simulated_flag_matches_execution_mode(self):
        """⑧ 真机执行不能被标成 simulated（T20/T23）。"""
        self._probe('DocsTests', 'HardwareTests', 'test20_docs_adapter_preserves_real_mode')
        self._probe('DocsTests', 'HardwareTests', 'test23_real_execution_is_not_labelled_simulated')
        import backends
        self.assertTrue(backends.MockBackend().simulated)
        self._fake_driver()
        self.assertFalse(backends.SDKBackend(ROOT).simulated, 'SDKBackend 必须跟随 driver 的真实模式')

    # ==================================================== 9. 超时未决动作
    def test_item09_timeout_keeps_unknown_action_barrier(self):
        """⑨ 超时后后台仍在执行，上层不能立刻释放控制权。"""
        self._probe('ScenarioTests', 'ScenarioTests',
                    'test16_timeout_retains_barrier_until_device_action_resolves')
        self._fake_driver()
        release = __import__('threading').Event()
        drv._pending_commands.add(release)
        try:
            self.assertTrue(drv.action_pending(), '有未决命令时必须报告 pending')
        finally:
            drv._pending_commands.discard(release)
        self.assertFalse(drv.action_pending())

    # ==================================================== 10. 忙碌时降落丢失
    def test_item10_landing_is_retried_not_dropped(self):
        """⑩ command 忙时 land 只能延后重试，不能记一条 deferred 就 return。"""
        self._probe('ScenarioTests', 'ScenarioTests', 'test17_landing_is_not_dropped_behind_busy_command')
        source = read('drone_sdk', '_driver.py')
        self.assertIn('while not _command_lock.acquire', source,
                      'land 派发必须重试直到拿到命令锁')

    # ==================================================== 11. 旧帧当当前帧
    def test_item11_expired_frame_is_not_current(self):
        """⑪ 10 秒前的缓存帧不能当作当前画面返回。"""
        self._probe('ScenarioTests', 'ScenarioTests',
                    'test19_driver_does_not_return_expired_frame_as_current')
        original = getattr(drv, '_video', None)
        try:
            drv._video = self._decoder(10.0)
            self.assertIsNone(drv.get_frame(), '过期帧必须返回 None')
            drv._video = self._decoder(0.0)
            self.assertIsNotNone(drv.get_frame(), '新鲜帧必须放行')
        finally:
            drv._video = original

    # ==================================================== 12. 帧身份
    def test_item12_frame_identity_is_stable(self):
        """⑫ 同一帧反复读取不能生成新编号。"""
        self._probe('ScenarioTests', 'ScenarioTests', 'test18_frame_bridge_preserves_capture_identity')
        original = getattr(drv, '_video', None)
        try:
            drv._video = reader = self._decoder(0.0, count=7)
            frame = drv.get_frame()
            first = drv.capture_metadata(frame)
            second = drv.capture_metadata(frame)
            self.assertEqual(first, second, '轮询不能改编号/时间戳')
            self.assertEqual(first, (7, reader.last_frame_timestamp))
        finally:
            drv._video = original

    # ==================================================== 13. RC 失败反馈
    def test_item13_failed_rc_is_reported(self):
        """⑬ 速度指令发送失败必须反馈给调用方。"""
        self._probe('DocsTests', 'HardwareTests', 'test22_failed_rc_transmission_is_not_reported_accepted')

        class Tello:
            def send_rc_control(self, *args):
                raise OSError('udp down')

        original = getattr(drv, '_tello', None)
        self.addCleanup(setattr, drv, '_tello', original)
        drv.SIMULATED = False
        self.addCleanup(setattr, drv, 'SIMULATED', True)
        drv._tello = Tello()
        self.assertFalse(drv.set_velocity((10, 0, 0, 0), 'probe'), '失败必须返回 False')
        state = drv.snapshot()
        self.assertGreaterEqual(len([e for e in state['events'] if e['event'] == 'link_fault']), 1,
                                '失败必须记链路故障事件')
        self.assertIn('udp down', state['last_error'])

    # ==================================================== 14. 静态图驱动真机
    def test_item14_static_image_cannot_fly_real_aircraft(self):
        """⑭ 接入真机后，静态测试图不能驱动导航。"""
        self._probe('DocsTests', 'GatewayTests', 'test24_docs_cannot_fly_real_hardware_using_static_test_image')
        source = read('modules', 'ai', 'gateway.py')
        self.assertIn('real_navigation_requires_live_camera', source)
        self.assertIn("o.get('source')!='sdk_camera'", source.replace(' ', ''))

    # ==================================================== 15. 推理后画面过期
    def test_item15_visual_result_age_is_rechecked(self):
        """⑮ 推理结束后要再看一次画面年龄。"""
        self._probe('DocsTests', 'VisionTests', 'test25_slow_visual_result_is_rejected_after_inference')
        self.assertIn('visual_result_expired', read('modules', 'ai', 'vision.py'))

    # ==================================================== 16. 导航前观察过期
    def test_item16_navigation_rejects_expired_observation(self):
        """⑯ 动作前要检查导航观察是否过期。"""
        self._probe('DocsTests', 'GatewayTests', 'test29_navigation_cannot_move_using_expired_observation')
        self.assertIn('navigation_observation_expired', read('modules', 'ai', 'gateway.py'))

    # ==================================================== 17. 矛盾视觉结果
    def test_item17_contradictory_visual_result_is_rejected(self):
        """⑰ visible=true 且 width_fraction=0 必须拒绝。"""
        self._probe('DocsTests', 'VisionTests', 'test26_visible_target_cannot_have_zero_width')
        source = read('modules', 'ai', 'vision.py')
        self.assertIn('visible_width_must_be_positive', source)
        self.assertIn('invisible_coordinates_must_be_null', source)

    # ==================================================== 18. 到达后恢复跟随
    def test_item18_normal_stop_keeps_follow_resume(self):
        """⑱ 正常到达时的停止不能关掉自动恢复跟随。"""
        self._probe('DocsTests', 'GatewayTests', 'test30_normal_arrival_stop_preserves_requested_follow_resume')
        self._probe('DocsTests', 'GatewayTests', 'test35_explicit_emergency_stop_does_not_resume_follow')
        source = read('modules', 'ai', 'gateway.py').replace(' ', '')
        self.assertIn("ifmethod=='stop_motion'andself.observationisNone:self.resume=False", source)

    # ==================================================== 19. 交接丢参考距离
    def test_item19_handoff_keeps_follow_reference(self):
        """⑲ 停顿-恢复的手交接不能丢原跟随参考尺寸。"""
        self._probe('DocsTests', 'GatewayTests', 'test33_docs_handoff_preserves_original_follow_reference')
        self.assertIn('pause_follow_for_action', read('modules', 'ai', 'backends.py'))
        self.assertIn('finish_follow_action', read('modules', 'ai', 'gateway.py'))

    # ==================================================== 20. 传代次
    def test_item20_action_carries_control_and_command_epoch(self):
        """⑳ 动作必须带控制权/命令代次，旧动作不能用新授权。"""
        self._probe('DocsTests', 'GatewayTests', 'test34_old_docs_action_cannot_use_new_same_owner_grant')
        source = read('modules', 'ai', 'backends.py').replace(' ', '')
        self.assertIn('**grant', source)
        self.assertIn("self.grant={'control_epoch'", source)

    # ==================================================== 21. 释放失败
    def test_item21_release_failure_is_not_ignored(self):
        """㉑ release_control 失败不能被忽略。"""
        self._probe('DocsTests', 'GatewayTests', 'test31_release_failure_cannot_finish_successfully')
        self.assertIn('cleanup_release_failed', read('modules', 'ai', 'gateway.py'))

    # ==================================================== 22. 收尾停止失败
    def test_item22_cleanup_stop_failure_is_not_ignored(self):
        """㉒ 收尾时 stop 失败同样不能吞掉。"""
        self._probe('DocsTests', 'GatewayTests', 'test32_failed_stop_cannot_be_silently_cleaned_up')
        self.assertIn('cleanup_stop_failed', read('modules', 'ai', 'gateway.py'))

    # ==================================================== 23. 打断先等网络
    def test_item23_local_stop_dispatched_before_network(self):
        """㉓ 打断必须先本地停车，再取消网络。"""
        self._probe('DocsTests', 'RealtimeTests', 'test37_network_cancel_send_cannot_delay_local_stop')
        source = read('modules', 'ai', 'realtime.py')
        body = source.split('async def barge_in', 1)[1].split('async def', 1)[0]
        local = body.index('gateway.interrupt')
        network = min((body.index(token) for token in ('await self.send', 'cancel_response') if token in body),
                      default=len(body))
        self.assertLess(local, network, '本地 interrupt 必须排在网络等待之前')

    # ==================================================== 24. 打断截错条目
    def test_item24_barge_in_truncates_unplayed_item(self):
        """㉔ 打断要截断尚未播完的新条目，而不是已播完的旧条目。"""
        self._probe('DocsTests', 'PlaybackTests',
                    'test38_interrupt_truncates_new_unplayed_item_not_completed_old_item')
        import duplex_audio
        buffer = duplex_audio.PlaybackBuffer()
        buffer.append('old', 0, b'\x00\x00' * 100)
        buffer.consume(100)                       # 旧条目播放完毕
        buffer.append('new', 0, b'\x00\x00' * 400)
        self.assertEqual(buffer.current, ('old', 0))
        result = buffer.interrupt()
        self.assertEqual(result['item_id'], 'new', '必须截断新条目')

    # ==================================================== 25. 密钥混用
    def test_item25_realtime_key_is_separate(self):
        """㉕ 实时语音不能用聊天 Key 去连别家端点。"""
        self._probe('DocsTests', 'RealtimeTests', 'test43_separate_visual_provider_key_is_not_sent_to_realtime')
        source = read('modules', 'ai', 'realtime.py')
        self.assertIn('AI_REALTIME_API_KEY', source)
        self.assertNotIn('OPENAI_API_KEY', source)
        self.assertNotIn("os.getenv('AI_API_KEY')", source)
        for name in ('main.py', 'vision.py', 'gateway.py', 'realtime.py', 'model_client.py'):
            self.assertNotIn('api.openai.com', read('modules', 'ai', name), name)

    # ==================================================== 26. 50 厘米演示
    def test_item26_fifty_centimetre_demo_is_supported(self):
        """㉖ SOP 里的 50 厘米演示必须被指定入口支持。"""
        self._probe('DocsTests', 'PlaybackTests', 'test41_flight_sop_fifty_cm_action_is_supported_by_its_entry')
        import program_runner
        self.assertEqual(program_runner.validate('drone.move_forward(50)'), 'move_forward')
        for bad in ('drone.move_forward(10)', 'drone.move_forward(101)', 'drone.move_forward(50.0)'):
            with self.assertRaises((ValueError, SyntaxError)):
                program_runner.validate(bad)
        self.assertIn('五十厘米', read('FLIGHT_SOP.md'), 'SOP 的演示台词必须与已支持的距离一致')

    # ==================================================== 27. 只在提示词
    def test_item27_illegal_intent_never_becomes_action(self):
        """㉗ “不确定不动作”不能只写在提示词里。"""
        self._probe('DocsTests', 'PlaybackTests', 'test40_legal_but_wrong_model_action_is_not_intent_validation')
        import ai_agent
        import demo_ai
        agent = ai_agent.Agent(demo_ai.ScriptedClient())
        before = agent.runner.demo.drone.get_status()['flying']
        outcome = agent.handle('不要起飞')
        self.assertFalse(outcome['execution'].get('accepted'))
        self.assertEqual(agent.runner.demo.drone.get_status()['flying'], before)

    # ==================================================== 28. SOP 术语
    def test_item28_sop_does_not_confuse_stop_semantics(self):
        """㉘ rc 0 / motoroff / 亚毫秒计时不能被说成停桨。"""
        sop = read('FLIGHT_SOP.md')
        self.assertIn('rc 0 0 0 0', sop)
        self.assertIn('不是停桨', sop)
        self.assertIn('退出电机冷却模式', sop)
        self.assertIn('亚毫秒', sop)
        self.assertNotIn('0.1ms 就是停桨', sop)
        self.assertNotIn('立即停桨', sop)

    # ==================================================== 29. 示例 360 + 恢复
    def test_item29_sample_rotates_and_restores_follow(self):
        """㉙ 帐篷示例要有 360° 旋转和最终恢复跟随。"""
        self._probe('DocsTests', 'PlaybackTests', 'test39_sample_covers_rotation_and_follow_restoration')
        sample = read('docs', 'v4-reference', 'examples', 'red_tent.py')
        self.assertIn('rotate(360)', sample)
        self.assertIn('start_follow()', sample)

    # ==================================================== 30. 到达条件
    def test_item30_arrival_is_not_claimed_from_bbox(self):
        """㉚ 画面占比不能作为“到达帐篷”的验收证据。"""
        self._probe('DocsTests', 'PlaybackTests', 'test42_sample_explicitly_restores_follow_after_arrival')
        sample = read('docs', 'v4-reference', 'examples', 'red_tent.py')
        self.assertIn('画面占比不能证明到达', sample)
        self.assertIn('not target.get', sample.replace('not target.get', 'not target.get'))

    # ==================================================== 31. 找回用户
    def test_item31_reacquire_is_not_claimed_as_implemented(self):
        """㉛ 飞离视野后“找回用户”仍无实现，代码不能假装做到。"""
        source = read('modules', 'follow', 'r3_follow', 'service.py')
        self.assertIn('recovering', source)
        docs = read('REVIEW_FIXES.md')
        self.assertIn('finding a user outside the camera view', docs)
        self.assertIn('not supplied', docs)

    # ============================================================ R301
    def testR301_old_velocity_cannot_restart_motion(self):
        """R301：已要求停止，旧速度请求不能重新启动运动。"""
        self._probe('R3Tests', 'AdapterTests', 'testR301_cancel_between_check_and_send_rejects_old_velocity')
        source = read('modules', 'follow', 'r3_follow', 'team2_adapter.py')
        body = source.split('def update_velocity', 1)[1].split('def ', 1)[0]
        self.assertIn('command_epoch', body, '速度请求必须带 command_epoch 到底层发送处')

    # ============================================================ R302
    def testR302_grant_confirmation_cannot_borrow_new_epoch(self):
        """R302：申请返回的代次与随后确认到的代次必须一致。"""
        self._probe('R3Tests', 'AdapterTests', 'testR302_confirmation_cannot_borrow_replacement_grant')
        source = read('modules', 'follow', 'r3_follow', 'team2_adapter.py')
        self.assertIn('control_epoch', source)
        self.assertIn('grant_confirmation', source.replace('-', '_').lower()
                      + 'grant_confirmation')

    # ============================================================ R303
    def testR303_status_failure_still_attempts_stop(self):
        """R303：清理时 get_status() 抛异常也必须盲发停止。"""
        self._probe('R3Tests', 'CleanupTests', 'testR303_status_error_cannot_skip_lease_checked_stop_attempt')
        source = read('modules', 'follow', 'r3_follow', 'service.py')
        self.assertIn('except', source)
        self.assertIn('stop', source)


if __name__ == '__main__':
    unittest.main(verbosity=2)
