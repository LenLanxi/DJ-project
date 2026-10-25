"""图传窗口的跟随 HUD：绿框标 ArUco、红点标中心、左上角写明偏差与速度。

单独成模块而不是塞进 main.py：四组的入口迭代很快，HUD 一旦跟着 main.py 被整包覆盖，
画面上的判读信息就全没了（曾经发生过一次）。
"""
import cv2
import numpy as np

# 跟随服务的生命周期事件：最近一条决定"现在到底在不在跟随"
RUN_LIFECYCLE = ('start', 'pause', 'stop', 'tracking_halted', 'fault',
                 'handoff_pause', 'handoff_finish', 'cleanup_completed')


def follow_snapshot(controller=None):
    """从 controller 的事件流里取最近一条 observation / motion_request / halt / lifecycle。"""
    if controller is None:
        from drone_sdk import controller as controller
    try:
        records = controller._follow.events()
    except Exception:
        return None, None, None, None
    obs = mot = halt = life = None
    for record in reversed(records):
        name = record.get('event')
        if name == 'observation' and obs is None:
            obs = record
        elif name == 'motion_request' and mot is None:
            mot = record
        elif name in ('tracking_halted', 'fault') and halt is None:
            halt = record
        if life is None and name in RUN_LIFECYCLE:
            life = record
        if obs is not None and mot is not None and life is not None:
            break
    return obs, mot, halt, life


def follow_line(obs, mot, life=None):
    """终端的一行跟随状态。静止状态只在变化时打印，避免"未启动"刷屏盖住别的日志。"""
    if life is not None and life.get('event') != 'start':
        reason = life.get('reason') or life.get('state') or life.get('event')
        return '[跟随] 未在跟随（%s）—— 说「开始跟随」重新启动' % reason
    if obs is None:
        return '[跟随] 暂无识别数据（跟随可能未启动）'
    if mot is not None:
        ch = mot.get('channels') or [0, 0, 0, 0]
        velocity = 'L%+d F%+d U%+d Y%+d' % (ch[0], ch[1], ch[2], ch[3])
        state, reason = mot.get('state'), mot.get('reason')
    else:
        velocity, state, reason = 'L+0 F+0 U+0 Y+0', '-', '-'
    return '[跟随] %s cx=%6.1f sr=%.3f | %s | %s | %s' % (
        '可见' if obs.get('found') else '丢失',
        obs.get('center_x') or 0, obs.get('size_ratio') or 0,
        velocity, state, reason)


def follow_status_text(life):
    """把"跟随为什么没在跑"翻译成一行能直接照做的提示。

    OpenCV 的 Hershey 字体画不了中文，所以这里用 ASCII；中文原因留在窗口1 的日志里。
    """
    if life is None:
        return 'follow: NOT STARTED  -> say "start following"', (0, 165, 255)
    name = life.get('event')
    if name == 'start':
        return 'follow: RUNNING', (0, 255, 0)
    if name in ('pause', 'handoff_pause'):
        return 'follow: PAUSED  reason=%s' % (life.get('reason') or '-'), (0, 165, 255)
    reason = life.get('reason') or life.get('state') or name
    reason = str(reason)[:32]
    return 'follow: STOPPED (%s) -> say it again' % reason, (0, 0, 255)


def draw_follow_overlay(frame, obs=None, mot=None, halt=None, life=None):
    """绿框标出 ArUco、红点标中心，并写明跟随状态与 X 轴偏差，一眼看出问题在哪。"""
    if frame is None:
        return frame
    out = frame
    height, width = out.shape[:2]
    cv2.line(out, (width // 2, 0), (width // 2, height), (0, 255, 0), 1)
    status, status_color = follow_status_text(life)
    lines = []
    if obs and obs.get('found'):
        # corners 是整帧像素坐标；按 observation 尺寸等比映射到当前画面
        src_w = float(obs.get('image_width') or 0) or float(width)
        src_h = float(obs.get('image_height') or 0) or float(height)
        scale_x = width / src_w if src_w else 1.0
        scale_y = height / src_h if src_h else 1.0
        try:
            quad = [(int(px * scale_x), int(py * scale_y))
                    for px, py in (obs.get('corners') or ())]
            if len(quad) >= 3:
                cv2.polylines(out, [np.array(quad, dtype=np.int32)], True, (0, 255, 0), 2)
        except Exception:
            pass
        cx = int((obs.get('center_x') or 0) * scale_x)
        cy = int((obs.get('center_y') or 0) * scale_y)
        cv2.circle(out, (cx, cy), 5, (0, 0, 255), -1)
        offset = (obs.get('center_x') or 0) * scale_x - width / 2.0
        lines.append('found=True  cx=%d  offset=%+.0f px  sr=%.3f' % (
            cx, offset, obs.get('size_ratio') or 0))
        if obs.get('detection_ms') is not None:
            budget = obs.get('processing_budget')
            lines.append('detect=%.0f ms%s' % (
                obs['detection_ms'], '' if budget is None else '  budget=%s' % budget))
    elif obs is not None:
        lines.append('waiting for marker  reason=%s' % (obs.get('reason') or '-'))
    if mot is not None:
        channels = mot.get('channels') or [0, 0, 0, 0]
        lines.append('v  L%+d F%+d U%+d Y%+d  %s' % (
            channels[0], channels[1], channels[2], channels[3], mot.get('state')))
        if mot.get('reason'):
            lines.append('motion reason: %s' % mot.get('reason'))
    lines.append(status)
    for index, text in enumerate(lines):
        y = 24 + index * 24
        color = status_color if index == len(lines) - 1 else (0, 255, 255)
        cv2.putText(out, text, (8, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(out, text, (8, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 1, cv2.LINE_AA)
    return out
