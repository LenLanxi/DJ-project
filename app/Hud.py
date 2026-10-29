"""Operator hotkeys and optional visual feedback."""
import asyncio
import json
import time

EMERGENCY_HELP = ('全局 Ctrl+Shift+Alt+A 停止/悬停，Ctrl+Shift+Alt+L 降落；'
                  'F12/F11也可用。退出默认请求降落，须核对实际落地。')


def draw_follow_overlay(frame, snapshot):
    import cv2
    import numpy as np
    out = frame.copy()
    height, width = out.shape[:2]
    cv2.line(out, (width//2, 0), (width//2, height), (0, 255, 0), 1)
    color = (0, 255, 0) if snapshot['following'] else (0, 165, 255)
    obs = snapshot['observation']
    lines = []
    if obs and snapshot['target_visible']:
        sx = width / float(obs['width'])
        sy = height / float(obs['height'])
        try:
            quad = [(int(px*sx), int(py*sy)) for px, py in obs.get('corners') or ()]
        except (TypeError, ValueError, OverflowError):
            # HUD metadata must not disable operator/emergency control.
            quad = []
        if len(quad) >= 3:
            cv2.polylines(out, [np.array(quad, dtype=np.int32)], True, (0, 255, 0), 2)
        cx, cy = int((obs.get('center_x') or 0)*sx), int((obs.get('center_y') or 0)*sy)
        cv2.circle(out, (cx, cy), 5, (0, 0, 255), -1)
        lines.append('marker cx=%d offset=%+.0f sr=%.3f' % (cx, cx-width/2, obs.get('size_ratio') or 0))
    elif obs is not None:
        lines.append('waiting for marker: %s' % str(obs.get('reason') or '-')[:64])
    lines.append('RC L%+d F%+d U%+d Y%+d' % tuple(snapshot['requested_channels']))
    lines.append('follow: ' + snapshot['state'])
    for index, text in enumerate(lines):
        y = 24 + index*24
        cv2.putText(out, text, (8, y), cv2.FONT_HERSHEY_SIMPLEX, .55, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(out, text, (8, y), cv2.FONT_HERSHEY_SIMPLEX, .55, color, 1, cv2.LINE_AA)
    return out


async def operator_controls(runtime, args, log, device):
    """Global keys/HUD; never change foreground window or keyboard focus."""
    listener = None
    loop = asyncio.get_running_loop()
    def trigger(kind, combo):
        future = asyncio.run_coroutine_threadsafe(runtime.interrupt(kind == 'land'), loop)
        log({'event': 'stop_request_source', 'source': 'operator_hotkey', 'key': combo,
             'requested_land': kind == 'land'})
        def done(f):
            try: print('[热键] %s：%s' % (combo, json.dumps(f.result(), ensure_ascii=False)), flush=True)
            except Exception as exc: print('[热键] 失败：%s' % exc, flush=True)
        future.add_done_callback(done)
    try:
        from drone_sdk.emergency_hotkeys import EmergencyHotkeys
        listener = EmergencyHotkeys(trigger, emit=lambda tag, text: print('[%s] %s' % (tag, text), flush=True))
        listener.start()
        next_diagnostics = 0.
        show = args.show_video
        if show:
            import cv2
            from drone_sdk import controller as ctl
        while True:
            if time.monotonic() >= next_diagnostics:
                log(dict(device.diagnostics(), event='device_diagnostics'))
                next_diagnostics = time.monotonic()+2
            if show:
                try:
                    snapshot = device.follow.status()
                    packet = ctl.get_frame()
                    if packet.frame is not None and not packet.stale:
                        cv2.imshow('Companion live camera', draw_follow_overlay(packet.frame, snapshot))
                    if cv2.waitKey(1) & 0xFF in (ord('q'), 27):
                        cv2.destroyAllWindows()
                        show = False
                except Exception as exc:
                    log({'event': 'hud_failed', 'error': str(exc)})
                    show = False
                    try: cv2.destroyAllWindows()
                    except Exception: pass
            await asyncio.sleep(.05)
    finally:
        if listener: await asyncio.to_thread(listener.stop)
        if args.show_video:
            try:
                import cv2
                cv2.destroyAllWindows()
            except Exception: pass
