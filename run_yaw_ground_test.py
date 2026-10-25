"""三组 Yaw（偏航）跟随算法 · 真机地面调试台（拆桨叶，不起飞）。

一个进程内同时出现两个界面（不能开两个进程：Tello 的 8889/11111 端口只能被占用一次）：

    窗口 1（终端 · 主控台）
        · 每 2 秒打印一次真实状态：电量 / 高度 / 飞行状态 / 链路状态
        · 实时转发四组语音链路日志：[STT] 识别文本、[LLM] 回复文本、[TTS] 播报状态
        · 安全指令立即生效并打印 [急停响应]（急停=priority 零速度；降落=priority 降落）
        · 安全锁保留：地面下发的任何运动指令都会打印 [拦截] 未起飞（地面状态）

    窗口 2（OpenCV · 视觉台）
        · Tello 实时图传
        · 三组 MarkerTracker 逐帧识别 ArUco：绿色四边形边框 + 中心红点
        · 左上角叠加：图像 X 偏差（cx - 480）、三组算法输出的 Yaw 值（RC 与归一化）
          、状态机 reason、电量/高度/链路/fps

Yaw 通道来源（与现场一致，不是另写一套公式）
    用 r3_follow.config 载入跟随配置（默认取仓库里的 aircraft_A_ID0.json：
    control_profile=openpose_pid + steering_mode=yaw），构造 FollowController，
    再按 service.control_once() 的同一换算把 MotionRequest.yaw 变成 RC：
        (lr, fb, ud, yaw) = (round(right*100), round(forward*100), 0, round(yaw*100))
        yaw 夹紧到 ±round(yaw_limit*100)，lr/fb 夹紧到 ±round(forward_limit*100)
    yaw_limit / forward_limit 来自跟随配置（旧默认 0.20 / 0.30 → ±20 / ±30 RC；
    aircraft_A_ID0.json 现在是 0.50 / 1.00 → ±50 / ±100 RC）。改这两个数会同时抬高
    "算法自身输出上限" 和 "服务层钳位"，只改钳位是无效的（会被算法输出夹回去）。
    注意 RC 100 = 满杆（Tello 的最大飞行速度），不是 1 m/s。
    所以画面上的 Yaw 就是三组算法真正要下发的偏航量，可直观看出是否震荡。

用法
    python run_yaw_ground_test.py --props-removed
    python run_yaw_ground_test.py --props-removed --marker-id 0 --no-voice
    python run_yaw_ground_test.py --props-removed --follow-config modules\\follow\\configs\\aircraft_A_ID0.json
    python run_yaw_ground_test.py --self-check          # 离线自检，不连飞机、不碰声卡

依赖
    pynput（只用于全局组合键）：pip install pynput；缺失时脚本会自动尝试安装一次。
    不想用全局组合键：加 --no-hotkeys，窗口/终端的 s 键急停照旧可用。

环境变量（可选，不设也能跑）
    R3_FOLLOW_CONFIG   跟随配置文件路径，优先级高于仓库默认；显式 --follow-config 最高
    AI_STT_URL / AI_STT_MODEL / AI_STT_KEY      四组在线 STT（不配则回落本地 faster-whisper）
    AI_TTS_URL / AI_TTS_MODEL / AI_TTS_VOICE / AI_TTS_KEY   四组在线 TTS（不配则回落 Windows SAPI）
    AI_CHAT_URL / AI_API_KEY / AI_MODEL         配合 --live 使用真实大模型（不配则用离线样例）
    AI_VAD_MIN_DBFS / AI_VAD_MARGIN_DB          免按键监听的自适应门限

按键（窗口或终端都生效）
    q/ESC=退出   s=急停归零   c=重新标定参考尺寸   r=重置Yaw状态机
    p=暂停/恢复语音监听   b=立即打印一次状态

物理安全兜底（全局组合键，pynput 低级键盘钩子，无需管理员权限）
    Ctrl+Shift+Alt+A   急停/悬停：controller.stop_motion(priority=True)，速度强制归零
    Ctrl+Shift+Alt+L   降落     ：controller.land(priority=True)，绕过普通动作锁
    焦点在 OpenCV 窗口、终端或别的程序上都有效；触发时终端打印醒目的
    [物理快捷键] 触发急停/降落，1 秒内同一指令只执行一次（防按住连发）。
    缺少 pynput 时脚本会用同一个解释器自动 pip install 一次；装不上则退回 s 键方案。
"""
import argparse
import ctypes
import os
import subprocess
import sys
import threading
import time

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
for _extra in ('modules/ai', 'modules/follow'):
    _candidate = os.path.join(ROOT, _extra)
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import drone_sdk._driver as driver
from drone_sdk import ai_bridge, controller
from drone_sdk.contract import FrameResult
import run_ground_test as ground
from run_ground_test import (EventTail, KeyReader, StallWatchdog, StatusPoller, Throttle,
                             close_output, disable_quick_edit, emit, enable_ansi,
                             install_taps, reason_text)

ground.TAG_CODES.update({'Yaw': '96', '视觉': '96', 'STT': '95', 'LLM': '94',
                         'TTS': '92', '语音': '95', '急停响应': '91;1', '标定': '96',
                         '快捷键': '93', '物理快捷键': '97;41;1'})

WINDOW = 'Yaw Ground Test - 3rd group vision'
CONSOLE_TITLE = '窗口1 · Yaw 地面调试主控台（拆桨叶，不起飞）'
DEFAULT_FOLLOW_CONFIG = os.path.join(ROOT, 'modules', 'follow', 'configs', 'aircraft_A_ID0.json')
CHANNEL_CAPS = (30, 30, 0, 20)      # 旧默认（= yaw_limit 0.20 / forward_limit 0.30），现由配置推导
HELP_KEYS = 'q=退出  s=急停  c=重新标定  r=重置Yaw  p=语音暂停/恢复  b=状态'
HELP_KEYS_IMAGE = 'q=quit  s=stop  c=calibrate  r=reset-yaw  p=voice  b=status'
CV_FONT = cv2.FONT_HERSHEY_SIMPLEX

CSV_HEADER = ('t_wall,t_rel,frame_id,cx,dx,size_ratio,yaw_norm,yaw_rc,fb_rc,lr_rc,'
              'state,reason,detect_ms,fps,had_target')

# 全局物理快捷键：pynput 走 Windows 低级键盘钩子（WH_KEYBOARD_LL），不需要管理员权限，
# 焦点在 OpenCV 窗口、终端或任何别的程序上都有效。
HOTKEY_COOLDOWN_S = 1.0
HOTKEYS = (
    ('<ctrl>+<shift>+<alt>+a', 'stop', 'Ctrl+Shift+Alt+A'),
    ('<ctrl>+<shift>+<alt>+l', 'land', 'Ctrl+Shift+Alt+L'),
)
HOTKEY_HELP = ('物理快捷键（全局）：Ctrl+Shift+Alt+A=急停  Ctrl+Shift+Alt+L=降落  '
               '[焦点在哪个窗口都有效]')

STOP_WORDS = ('急停', '紧急停止', '刹停', '停下来', '停下', '停止', '别动', '不要动', '别飞')
LAND_WORDS = ('降落', '着陆', '落地', '下来')


def set_console_title(text):
    if os.name != 'nt':
        return
    try:
        ctypes.windll.kernel32.SetConsoleTitleW(text)
    except Exception:
        pass


def fmt(value, suffix=''):
    return '?' if value is None else '%s%s' % (value, suffix)


def resolve_follow_config(args):
    """显式参数 > R3_FOLLOW_CONFIG > 仓库默认配置 > FollowConfig()。"""
    from r3_follow.config import load_config
    if args.follow_config:
        path = args.follow_config
        if not os.path.isabs(path):
            path = os.path.join(ROOT, path)
        config = load_config(path)
        source = path
    elif os.environ.get('R3_FOLLOW_CONFIG'):
        config = load_config()
        source = os.environ['R3_FOLLOW_CONFIG']
    elif os.path.exists(DEFAULT_FOLLOW_CONFIG):
        config = load_config(DEFAULT_FOLLOW_CONFIG)
        source = DEFAULT_FOLLOW_CONFIG
    else:
        config = load_config()
        source = '(内置默认 FollowConfig)'
    emit('配置', '跟随配置=%s' % source)
    emit('配置', 'control_profile=%s  steering_mode=%s  target_id=%s  dictionary=%s' % (
        config.control_profile, config.steering_mode, config.target_id, config.dictionary))
    if config.steering_mode != 'yaw':
        emit('提示', 'terring_mode=%s 不是 yaw：Yaw 通道可能被分走或恒为 0，'
                     '验证偏航请用 steering_mode=yaw 的配置' % config.steering_mode)
    return config


def rc_caps(yaw_limit, forward_limit):
    """RC 通道上限，换算与 service.FollowService.rc_caps 完全一致（硬上限 ±100）。

    up_down 恒为 0：三组的律从不下达升降，这一条与预算无关。
    """
    return (min(100, round(forward_limit * 100)), min(100, round(forward_limit * 100)), 0,
            min(100, round(yaw_limit * 100)))


class YawSteering:
    """三组 FollowController 的薄封装：只做"喂观测 → 读速度"，不发任何指令。"""

    def __init__(self, config, marker_id, max_request_rate, yaw_limit, forward_limit,
                 recovery_window):
        from r3_follow.core import FollowController
        self.config = config
        self.marker_id = marker_id
        self.caps = rc_caps(yaw_limit, forward_limit)
        self.kwargs = dict(target_id=marker_id, recovery_window=recovery_window,
                           max_request_rate=max_request_rate, require_geometry=True,
                           steering_mode=config.steering_mode, yaw_limit=yaw_limit,
                           forward_limit=forward_limit,
                           control_profile=config.control_profile)
        self.controller = FollowController(**self.kwargs)
        self.reason = 'waiting_for_marker'
        self.reference_ready = False
        self.last_detect = ''
        self.seen = -1          # 已喂给控制器的最新识别帧号

    def reset(self):
        from r3_follow.core import FollowController
        self.controller = FollowController(**self.kwargs)
        self.reason = 'reset'
        self.reference_ready = False
        self.last_detect = ''
        emit('Yaw', '状态机已重置（参考尺寸需要重新标定）')

    def observe(self, observation, now):
        """只在有新观测时调用；负责标定与启动，之后交给 tick() 出速度。"""
        from r3_follow.core import State
        try:
            if not self.controller.observe(observation):
                return
        except Exception as exc:
            emit('错误', '3组 observe 异常：%s: %s' % (type(exc).__name__, exc))
            return
        if not observation.found:
            self.reason = observation.reason or 'no_marker'
            return
        controller_ = self.controller
        if controller_.reference_size is None:
            ok, reason = controller_.calibrate(now)
            if ok:
                self.reference_ready = True
                emit('标定', '参考尺寸已标定 reference_size=%.3f' % controller_.reference_size)
            else:
                self.reason = 'calibrate:%s' % reason
                if self.reason != self.last_detect:
                    self.last_detect = self.reason
                    emit('标定', '标定未通过（%s）：请把 ArUco 正对镜头、四边别被遮挡，' 
                                 '或按 c 重新标定' % reason)
            return
        if controller_.state not in (State.TRACKING, State.RECOVERING):
            ok, reason = controller_.start(now)
            if not ok:
                self.reason = 'start:%s' % reason

    def tick(self, now):
        motion = self.controller.tick(now)
        self.reason = motion.reason or str(self.controller.state)
        return motion


def to_channels(motion, caps=CHANNEL_CAPS):
    """MotionRequest → 4 路 RC（与 service.control_once() 同一公式与夹紧）。"""
    raw = (round(motion.right * 100), round(motion.forward * 100), 0,
           round(motion.yaw * 100))
    return tuple(max(-cap, min(cap, value)) for value, cap in zip(raw, caps))


class FrameGrabber(threading.Thread):
    """后台取真机帧；顺手把帧发布进 SDK，3组跟随入口读到的也是同一份画面。"""

    def __init__(self, publish=True):
        super().__init__(daemon=True, name='frame-grabber')
        self.publish = publish
        self._lock = threading.Lock()
        self._frame = None
        self._count = 0
        self._mono = 0.0
        self.published = 0
        self._publish_error = ''
        self._halt = threading.Event()

    def run(self):
        last_source = None
        while not self._halt.wait(1 / 30):
            try:
                frame = driver.get_frame()
            except Exception:
                frame = None
            if frame is None:
                continue
            metadata = driver.capture_metadata(frame)
            if metadata is False:          # 解码器正在换帧：不要拿半新半旧的时间戳
                continue
            now = time.monotonic()
            if metadata is not None:
                count, captured_at = metadata
            else:
                if frame is last_source:   # 旧读取器没有计数：同一对象就不再刷新
                    continue
                count = self._count + 1
                captured_at = time.time()
            last_source = frame
            with self._lock:
                self._frame = frame
                self._count = count
                self._mono = now
            if not self.publish:
                continue
            try:
                controller.publish_frame(FrameResult(frame, count, captured_at, False))
                self.published += 1
            except Exception as exc:
                if self._publish_error != str(exc):
                    self._publish_error = str(exc)
                    emit('错误', 'publish_frame 失败：%s' % exc)

    def latest(self):
        with self._lock:
            return self._frame, self._count, self._mono

    def stop(self):
        self._halt.set()


class VisionWorker(threading.Thread):
    """三组识别单独跑：主线程只画图，检测再慢也不会卡住 OpenCV 窗口。"""

    def __init__(self, tracker):
        super().__init__(daemon=True, name='vision-worker')
        self.tracker = tracker
        self._lock = threading.Lock()
        self._pending = None
        self._observation = None
        self._obs_count = -1
        self.processed = 0
        self.detect_ms = 0.0
        self.error = ''
        self._halt = threading.Event()

    def submit(self, frame, frame_id, acquired_at):
        with self._lock:
            self._pending = (frame, frame_id, acquired_at)

    def run(self):
        while not self._halt.wait(0.002):
            with self._lock:
                item, self._pending = self._pending, None
            if item is None:
                continue
            frame, frame_id, acquired_at = item
            started = time.perf_counter()
            try:
                observation = self.tracker.detect(frame.copy(), frame_id, acquired_at)
            except Exception as exc:
                self.error = '%s: %s' % (type(exc).__name__, exc)
                observation = None
            elapsed = (time.perf_counter() - started) * 1000
            with self._lock:
                self._observation = observation
                self._obs_count = frame_id
                self.processed += 1
                self.detect_ms = elapsed

    def latest(self):
        with self._lock:
            return self._observation, self._obs_count, self.detect_ms

    def stop(self):
        self._halt.set()


def put_text(image, text, position, color, scale=0.56, thickness=1):
    """Hershey 字体没有描边，浅色画面上会看不清：先画一层黑底再上色。"""
    cv2.putText(image, text, position, CV_FONT, scale, (0, 0, 0), thickness + 2, cv2.LINE_AA)
    cv2.putText(image, text, position, CV_FONT, scale, color, thickness, cv2.LINE_AA)


def draw_visual(frame, observation, yaw_rc, yaw_norm, dx, state, reason, fps, snapshot,
                target_id, note='', yaw_cap=20):
    """视觉台叠加层。cv2 的 Hershey 字体不支持中文，图上只用 ASCII。"""
    image = frame.copy()
    height, width = image.shape[:2]
    center_x = width / 2.0
    cv2.line(image, (int(center_x), 0), (int(center_x), height), (90, 90, 90), 1)

    found = bool(observation is not None and observation.found)
    if found and observation.corners:
        points = np.array([[int(x), int(y)] for x, y in observation.corners], dtype=np.int32)
        cv2.polylines(image, [points], True, (0, 255, 0), 2)
        cv2.circle(image, (int(observation.center_x), int(observation.center_y)), 6,
                   (0, 0, 255), -1)
        cv2.circle(image, (int(observation.center_x), int(observation.center_y)), 7,
                   (255, 255, 255), 1)
        cv2.line(image, (int(center_x), int(observation.center_y)),
                 (int(observation.center_x), int(observation.center_y)), (0, 255, 255), 1)

    lines = [
        ('YAW GROUND TEST - NO TAKEOFF', (0, 0, 255), 2),
        ('X offset (cx-%d): %s px' % (int(center_x), '%+d' % dx if dx is not None else '--'),
         (0, 255, 255) if dx is None or abs(dx) > 24 else (0, 255, 0), 1),
        ('3rd-group Yaw: %s RC   yaw=%s' % (
            '%+d' % yaw_rc if yaw_rc is not None else '--',
            '%+.3f' % yaw_norm if yaw_norm is not None else '--'), (0, 255, 0), 1),
        ('state=%s  reason=%s' % (state or '-', reason or '-'), (220, 220, 220), 1),
        ('battery %s  height %s  link %s  fps %.1f  id %s %s' % (
            fmt(snapshot.get('battery'), '%'), fmt(snapshot.get('height_cm'), 'cm'),
            'ok' if snapshot.get('link_ok') else 'FAIL', fps, target_id,
            'FOUND' if found else 'lost'), (220, 220, 220), 1),
    ]
    for index, (text, color, thickness) in enumerate(lines):
        put_text(image, text, (10, 26 + index * 26), color, 0.56, thickness)

    # 偏航量条形指示：一眼看出是否在 0 附近来回震荡
    bar_top, bar_left, bar_len = 152, 10, 220
    cv2.rectangle(image, (bar_left, bar_top), (bar_left + bar_len, bar_top + 10),
                  (60, 60, 60), -1)
    middle = bar_left + bar_len // 2
    cv2.line(image, (middle, bar_top - 4), (middle, bar_top + 14), (210, 210, 210), 1)
    if yaw_rc is not None:
        ratio = max(-1.0, min(1.0, yaw_rc / float(yaw_cap or 20)))
        end = int(middle + ratio * (bar_len / 2))
        color = (0, 200, 0) if yaw_rc == 0 else (0, 180, 255)
        cv2.rectangle(image, (min(middle, end), bar_top + 3),
                      (max(middle, end), bar_top + 7), color, -1)
    put_text(image, 'yaw RC -%d..+%d (left..right)' % (yaw_cap, yaw_cap), (bar_left, bar_top + 28),
             (200, 200, 200), 0.42, 1)
    if note:
        put_text(image, note, (bar_left, bar_top + 50), (0, 200, 255), 0.44, 1)
    put_text(image, HELP_KEYS_IMAGE, (bar_left, height - 12), (255, 255, 0), 0.46, 1)
    return image


def detect_emergency(text):
    """语音安全指令：返回 'land' / 'stop' / ''。先判降落，避免"降落"被当成急停。"""
    if not text:
        return ''
    compact = ''.join(text.split())
    if any(word in compact for word in LAND_WORDS):
        return 'land'
    if any(word in compact for word in STOP_WORDS):
        return 'stop'
    return ''


_EMERGENCY_LOCK = threading.Lock()


def handle_emergency(kind, text, args, force_land=False):
    """安全指令：立即走 priority 通道并打印 [急停响应]，绝不交给大模型排队。

    force_land=True（物理快捷键）时，无论是否在地面都直接下发 land；
    默认（语音/单键）在地面只做零速度，避免反复向固件发无效 land。
    """
    with _EMERGENCY_LOCK:
        started = time.perf_counter()
        try:
            controller.stop_follow()
        except Exception as exc:
            emit('错误', '急停时停止跟随失败：%s' % exc)
        # snapshot() 只读内存状态，不走 UDP：急停路径上不能有任何网络等待
        flying = bool(driver.snapshot().get('flying'))
        if kind == 'land':
            if force_land or flying or not args.soft_land:
                result = controller.land('model', priority=True)
                emit('急停响应', '“%s”→ priority 降落已下发（%s）'
                     % (text, describe(result)))
                if not flying:
                    emit('急停响应', '当前在地面：固件对 land 可能回 error，'
                                    '[链路] 里出现 land 故障属预期；不想发就加 --soft-land')
            else:
                result = controller.stop_motion('model', priority=True)
                emit('急停响应', '“%s”→ 地面状态（--soft-land），只发 priority 零速度（%s）'
                     % (text, describe(result)))
        else:
            result = controller.stop_motion('model', priority=True)
            emit('急停响应', '“%s”→ priority 零速度已下发（%s）'
                 % (text, describe(result)))
        emit('急停响应', '安全指令耗时 %.1fms，控制权/速度已归零'
             % ((time.perf_counter() - started) * 1000))
        # 急停/降落把控制权抢到 model 名下：地面必须还回去，3组才能继续取回 follow 控制权
        if not flying:
            current = controller.get_status().control_owner
            if current == 'model':
                released = controller.release_control('model')
                emit('急停响应', '已释放急停期间的 model 控制权（%s），3组可继续下发 Yaw'
                     % describe(released))
        return result


def describe(result):
    if result is None:
        return '无返回'
    if getattr(result, 'ok', False):
        return 'ok/%s' % (getattr(result, 'status', None) or 'accepted')
    return '失败/%s' % reason_text(getattr(result, 'reason', ''))


def ensure_pynput():
    """没有 pynput 就现场装一次（同一个解释器），失败则退回单键方案。"""
    try:
        from pynput import keyboard
        return keyboard
    except Exception as exc:
        emit('快捷键', '缺少 pynput（%s），正在自动安装…' % exc)
    try:
        completed = subprocess.run(
            [sys.executable, '-m', 'pip', 'install', 'pynput'],
            capture_output=True, text=True, timeout=300)
        output = (completed.stdout or '').strip().splitlines()
        failed = (completed.stderr or '').strip().splitlines()
        tail = [line for line in (output + failed) if line.strip()][-1:] or ['']
        emit('快捷键', 'pip install pynput %s %s' % (
            '成功' if completed.returncode == 0 else '失败（返回码 %s）' % completed.returncode,
            tail[0]))
    except Exception as exc:
        emit('快捷键', '自动安装失败：%s: %s' % (type(exc).__name__, exc))
    try:
        import importlib
        importlib.invalidate_caches()
        from pynput import keyboard
        return keyboard
    except Exception as exc:
        emit('快捷键', 'pynput 仍不可用（%s）：全局组合键关闭，终端/窗口的 s 键急停仍有效' % exc)
        emit('提示', '手动安装：%s -m pip install pynput' % sys.executable)
        return None


class CsvTelemetry:
    """每个控制 tick 追加一行遥测；只写文件，不参与任何控制或安全逻辑。"""

    def __init__(self, path=None):
        self.path = path
        self.rows = 0
        self.error = ''
        self._handle = None
        if not path:
            return
        try:
            directory = os.path.dirname(os.path.abspath(path))
            if directory:
                os.makedirs(directory, exist_ok=True)
            fresh = not os.path.exists(path) or os.path.getsize(path) == 0
            self._handle = open(path, 'a', encoding='utf-8', newline='')
            if fresh:
                self._handle.write(CSV_HEADER + '\n')
                self._handle.flush()
        except Exception as exc:
            self.error = '%s: %s' % (type(exc).__name__, exc)
            self._handle = None
            emit('错误', 'CSV 遥测无法打开：%s' % self.error)

    @property
    def active(self):
        return self._handle is not None

    def write(self, **fields):
        if self._handle is None:
            return
        try:
            self._handle.write(','.join(
                '' if fields.get(name) is None else str(fields.get(name))
                for name in CSV_HEADER.split(',')) + '\n')
            self.rows += 1
        except Exception as exc:
            if not self.error:
                self.error = '%s: %s' % (type(exc).__name__, exc)
                emit('错误', 'CSV 写入失败（后续不再写）：%s' % self.error)
            self._handle = None

    def close(self):
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass


class GlobalEmergencyHotkeys(threading.Thread):
    """物理安全兜底：全局组合键 → 底层 priority 通道（绕过普通动作锁）。"""

    def __init__(self, args, cooldown=HOTKEY_COOLDOWN_S):
        super().__init__(daemon=True, name='global-hotkeys')
        self.args = args
        self.cooldown = cooldown
        self.listener = None
        self.ready = False
        self.error = ''
        self._lock = threading.Lock()
        self._fired = {}

    def allow(self, kind, now=None):
        """同一条安全指令在 cooldown 内只执行一次，防止按住组合键连发。"""
        now = time.monotonic() if now is None else now
        with self._lock:
            previous = self._fired.get(kind)
            if previous is not None and now - previous < self.cooldown:
                return False
            self._fired[kind] = now
            return True

    def fire(self, kind, combo):
        if not self.allow(kind):
            return
        emit('物理快捷键', '触发%s（%s）—— 走 priority 通道'
             % ('急停/悬停' if kind == 'stop' else '降落', combo))
        try:
            handle_emergency(kind, '物理快捷键 %s' % combo, self.args,
                             force_land=True)
        except Exception as exc:
            emit('错误', '物理快捷键处理后异常：%s: %s' % (type(exc).__name__, exc))

    def run(self):
        keyboard = ensure_pynput()
        if keyboard is None:
            return
        try:
            self.listener = keyboard.GlobalHotKeys({
                combo: (lambda k=kind, c=human: self.fire(k, c))
                for combo, kind, human in HOTKEYS
            })
            self.listener.daemon = True
            self.listener.start()
            self.ready = True
            emit('快捷键', HOTKEY_HELP)
        except Exception as exc:
            self.error = '%s: %s' % (type(exc).__name__, exc)
            emit('快捷键', '全局组合键注册失败（%s）：仍可用终端/窗口的 s 键急停' % self.error)

    def stop(self):
        listener = self.listener
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                pass
        if self.is_alive():
            self.join(1.0)


class VoiceMonitor(threading.Thread):
    """四组语音链路：监听 → STT → （急停拦截）→ LLM → TTS，全程打印到主控台。"""

    def __init__(self, args):
        super().__init__(daemon=True, name='voice-monitor')
        self.args = args
        self.ready = False
        self.paused = threading.Event()
        self._halt = threading.Event()
        self._speaking = threading.Event()
        self.agent = None
        self.voice = None
        self.audio_io = None
        self.input_device = None
        self.output_device = None

    def _load(self):
        import audio_config
        import audio_io
        import local_stt
        from ai_agent import Agent
        from demo_ai import ScriptedClient
        from demo_text import Demo
        from model_client import ModelClient
        from program_runner import Runner
        from voice_service import VoiceService

        config = audio_config.load_audio_config(self.args.audio_config,
                                                self.args.input_device,
                                                self.args.output_device)
        audio_io.dependencies()            # 缺 PortAudio/numpy 时在这里就报清楚
        drone = ai_bridge.UnifiedDrone()
        health = driver.health()

        def real_status():
            status = ai_bridge.UnifiedDrone.get_status(drone)
            info = driver.health()
            status.update(battery=info['battery'], height_cm=info['height_cm'],
                          connected=info['connected'], flying=info['flying'],
                          simulated=False)
            return status

        drone.get_status = real_status
        drone.get_battery = lambda: driver.health()['battery']
        runner = Runner(Demo(drone), allow_simulated_sdk=True, allow_programs=False)
        self.agent = Agent(ModelClient() if self.args.live else ScriptedClient(), runner=runner)
        self.voice = VoiceService()
        self.audio_io = audio_io
        self.input_device = config.input_device
        self.output_device = config.output_device
        self.ready = True
        stt_api = bool(os.getenv('AI_STT_URL') and os.getenv('AI_STT_MODEL'))
        if not stt_api and not local_stt.installed():
            emit('语音', '未配置 AI_STT_* 且本地 faster-whisper 不可用：STT 环节会报错（视觉/Yaw 不受影响）')
        emit('语音', 'LLM=%s  STT=%s  TTS=%s  麦克风=%s  喇叭=%s' % (
            '真实模型' if self.args.live else '离线样例（不是AI）',
            '在线 API' if stt_api else '本地 faster-whisper',
            '在线 API' if os.getenv('AI_TTS_URL') else 'Windows SAPI 降级',
            self.input_device if self.input_device is not None else '系统默认',
            self.output_device if self.output_device is not None else '系统默认'))
        emit('语音', '当前电量 %s%%（真机地面状态，起飞被 SDK 闸门禁止）' % health['battery'])

    def run(self):
        try:
            self._load()
        except Exception as exc:
            emit('语音', '语音模块未启动：%s: %s' % (type(exc).__name__, exc))
            emit('提示', '装依赖：pip install -r modules/ai/requirements-audio.txt；'
                         '或加 --no-voice 只跑视觉 + Yaw')
            return
        emit('语音', '免按键监听已开启：直接说“急停 / 降落 / 跟着我 / 还有多少电”…')
        while not self._halt.is_set():
            if self.paused.is_set():
                self._halt.wait(0.05)
                continue
            try:
                wav, info = self.audio_io.listen_once(
                    self.input_device, silence_s=self.args.silence_s,
                    max_s=self.args.max_utterance, pause=self._speaking)
            except Exception as exc:
                emit('语音', '麦克风异常（%s: %s），1 秒后重试' % (type(exc).__name__, exc))
                self._halt.wait(1.0)
                continue
            if wav is None or self._halt.is_set():
                continue
            emit('语音', '捕获到 %.1f 秒音频（峰值 %.0f dBFS）'
                 % (info.get('speech_s', 0.0), info.get('peak_dbfs', -120.0)))
            try:
                text = self._transcribe(wav)
            except Exception as exc:
                emit('STT', '识别失败：%s: %s' % (type(exc).__name__, exc))
                continue
            if text:
                self.handle_text(text)

    def _transcribe(self, wav):
        text = ''
        try:
            text = self.voice.transcribe(wav)
            emit('STT', '识别文本：%s（在线 API）' % text)
        except Exception as exc:
            emit('STT', '在线识别不可用（%s），改试本地 faster-whisper' % exc)
            import local_stt
            if not local_stt.installed():
                raise
            engine = local_stt.LocalWhisper().warmup()
            text = engine.transcribe(wav)
            emit('STT', '识别文本：%s（本地 faster-whisper）' % text)
        return (text or '').strip()

    def handle_text(self, text):
        """一句话走完整条链路；自检也会直接调用它（不需要真麦克风）。"""
        if not text:
            return None
        kind = detect_emergency(text)
        if kind:
            handle_emergency(kind, text, self.args)
            return {'emergency': kind}
        if self.agent is None:
            emit('LLM', '语音链路未就绪，文本未交给模型：%s' % text)
            return None
        try:
            record = self.agent.handle(text)
        except Exception as exc:
            emit('LLM', '模型/执行失败：%s: %s' % (type(exc).__name__, exc))
            return None
        reply = str(record.get('reply', ''))
        emit('LLM', '回复：%s' % reply)
        plan = record.get('plan') or {}
        if plan.get('kind') == 'program':
            emit('LLM', '模型计划执行：%s（地面会被 [拦截]）' % plan.get('code'))
        self._speak(str(record.get('spoken') or reply))
        return record

    def _speak(self, text):
        text = (text or '').strip()
        if not text:
            emit('TTS', '跳过播报：没有可播报的文本')
            return
        self._speaking.set()
        try:
            try:
                wav = self.voice.synthesize(text[:2000])
                self.audio_io.play(wav, device=self.output_device)
                emit('TTS', '已播报（在线 API，%d 字节）' % len(wav))
                return
            except Exception as exc:
                emit('TTS', '在线合成不可用（%s），改试 Windows SAPI' % exc)
            import offline_tts
            if not offline_tts.available():
                emit('TTS', '未播报：系统不支持 Windows SAPI；文字结果仍有效')
                return
            offline_tts.speak(text[:2000])
            emit('TTS', '已播报（Windows SAPI 离线降级）')
        except Exception as exc:
            emit('TTS', '播报失败：%s: %s；文字结果仍有效' % (type(exc).__name__, exc))
        finally:
            self._speaking.clear()

    def toggle(self):
        if self.paused.is_set():
            self.paused.clear()
            emit('语音', '监听已恢复')
        else:
            self.paused.set()
            emit('语音', '监听已暂停（再按 p 恢复）')

    def stop(self):
        self._halt.set()
        self._speaking.set()          # 让正在跑的 listen_once 立刻返回
        if self.ready and self.audio_io is not None:
            try:
                self.audio_io.close()
            except Exception:
                pass
        if self.is_alive():
            self.join(1.0)


def send_velocity(channels, grant):
    """按 3组 的方式把速度送到 SDK 边界；地面必被 [拦截] 未起飞。"""
    if grant is None:
        fresh = controller.request_control('follow')
        if not fresh.ok:
            emit('拦截', '申请 follow 控制权失败：%s' % reason_text(fresh.reason))
            return None, None
        grant = fresh
    result = controller.update_velocity('follow', channels[0], channels[1], 0, channels[3],
                                        control_epoch=grant.control_epoch,
                                        command_epoch=grant.command_epoch)
    if not result.ok and result.reason in ('obsolete_control', 'obsolete_command', 'no_control'):
        fresh = controller.request_control('follow')
        grant = fresh if fresh.ok else None
        if grant is None:
            return result, None
        result = controller.update_velocity('follow', channels[0], channels[1], 0,
                                            channels[3],
                                            control_epoch=grant.control_epoch,
                                            command_epoch=grant.command_epoch)
    return result, grant


def status_line(snapshot, status, fps, dx, yaw_rc, yaw_norm, reason, detect_ms):
    return ('电量 %s  高度 %s  飞行=%s  链路=%s  控制权=%s  图传=%s  fps=%.1f  '
            'X偏差=%s  Yaw=%sRC(%s)  state=%s  识别=%.0fms' % (
                fmt(snapshot.get('battery'), '%'), fmt(snapshot.get('height_cm'), 'cm'),
                status.flight_state, 'ok' if snapshot.get('link_ok') else 'FAIL',
                status.control_owner, 'on' if snapshot.get('video') else 'off', fps,
                '%+d' % dx if dx is not None else '--',
                '%+d' % yaw_rc if yaw_rc is not None else '--',
                '%+.3f' % yaw_norm if yaw_norm is not None else '--',
                reason or '-', detect_ms))


def run(args, throttle):
    config = resolve_follow_config(args)
    marker_id = config.target_id if args.marker_id is None else args.marker_id
    yaw_limit = config.yaw_limit if args.yaw_limit is None else args.yaw_limit
    forward_limit = config.forward_limit if args.forward_limit is None else args.forward_limit
    max_rate = config.max_request_rate if args.max_request_rate is None else args.max_request_rate

    try:
        health = driver.connect(args.address, timeout=args.timeout)
    except Exception as exc:
        ground.connect_failed(exc)
        return 1
    if not health:
        emit('错误', '连接返回空结果')
        return 1
    emit('连接', '电量=%s%%  高度=%scm  链路=%s  图传后端=%s' % (
        health['battery'], health['height_cm'], 'ok' if health['link_ok'] else 'FAIL',
        driver.VIDEO_BACKEND))
    controller.configure_takeoff(authorized=False, min_battery=args.battery_min)

    if not driver.start_video(wait=args.frame_timeout):
        emit('错误', '图传未出真实画面：%s' % driver.video_stats())
        emit('提示', '先跑 python test_live_view.py --props-removed 定位图传问题')
        driver.disconnect()
        return 1
    emit('图传', '真实画面已就绪（backend=%s shape=%s）' % (
        driver.VIDEO_BACKEND, driver.video_stats().get('shape')))

    from r3_follow.vision import MarkerTracker
    tracker = MarkerTracker(target_id=marker_id, processing_budget_ms=args.detect_budget_ms)
    steering = YawSteering(config, marker_id, max_rate or None,
                           yaw_limit, forward_limit, args.recovery_window)
    emit('Yaw', 'Yaw 通道已就绪：profile=%s mode=%s ramp=%s' % (
        config.control_profile, config.steering_mode,
        '%s/s' % max_rate if max_rate else 'off'))
    emit('Yaw', 'RC 预算：lr/fb=±%d  yaw=±%d（yaw_limit=%.2f forward_limit=%.2f；' 
                '100=满杆，不是 1m/s）' % (steering.caps[0], steering.caps[3],
                                          yaw_limit, forward_limit))

    grabber = FrameGrabber()
    vision = VisionWorker(tracker)
    poller, keys = StatusPoller(), KeyReader()
    watchdog = StallWatchdog(limit=3.0)
    started_wall = time.time()
    voice = None if args.no_voice else VoiceMonitor(args)
    hotkeys = None if args.no_hotkeys else GlobalEmergencyHotkeys(args)
    for thread in (grabber, vision, poller, keys, watchdog):
        thread.start()
    if hotkeys is not None:
        hotkeys.start()
    if voice is not None:
        voice.start()

    telemetry = CsvTelemetry(args.log_csv)
    if args.log_csv:
        emit('遥测', 'CSV=%s%s' % (args.log_csv,
                                  '' if telemetry.active else '（打开失败）'))

    snapshot = poller.refresh()
    emit('状态', status_line(snapshot, controller.get_status(), 0.0, None, None, None,
                             'startup', 0.0))
    emit('按键', HELP_KEYS)

    grant = None
    last_count = -1
    last_sent = 0.0
    sent_count = 0
    last_show = 0.0
    fps = 0.0
    fps_ref_count, fps_ref_time = grabber.latest()[1], time.time()
    next_status = time.time() + args.status_interval
    deadline = time.time() + args.seconds if args.seconds else None
    window_created = False
    observation = None
    yaw_rc = yaw_norm = dx = None
    reason = 'startup'
    note = '' if config.steering_mode == 'yaw' else 'NOTE: steering_mode is %s' % config.steering_mode

    try:
        while True:
            watchdog.tick('loop')
            now_wall = time.time()
            frame, count, captured_mono = grabber.latest()
            if frame is not None and count != last_count:
                last_count = count
                vision.submit(frame, count, captured_mono)

            now_mono = time.monotonic()
            fresh_observation, obs_count, detect_ms = vision.latest()
            if fresh_observation is not None and obs_count != steering.seen:
                steering.seen = obs_count
                observation = fresh_observation
                steering.observe(observation, now_mono)

            motion = steering.tick(now_mono)
            channels = to_channels(motion, steering.caps)
            yaw_norm = motion.yaw
            yaw_rc = channels[3]
            reason = steering.reason
            # 只在"这一帧是新鲜的、并且真的看见了码"时才显示偏差，避免显示过期数字
            if (observation is not None and observation.found
                    and 0 <= now_mono - observation.acquired_at <= steering.controller.max_age):
                dx = int(round(observation.center_x - observation.width / 2.0))
            else:
                dx = None

            telemetry.write(
                t_wall='%.3f' % now_wall, t_rel='%.3f' % (now_wall - started_wall),
                frame_id=getattr(observation, 'frame_id', '') if observation else '',
                cx=('%.1f' % observation.center_x) if observation is not None
                   and observation.found else '', dx=dx,
                size_ratio=('%.4f' % observation.size_ratio) if observation is not None
                           and observation.found else '',
                yaw_norm='%.4f' % yaw_norm, yaw_rc=yaw_rc, fb_rc=channels[1],
                lr_rc=channels[0], state=str(steering.controller.state), reason=reason,
                detect_ms='%.1f' % detect_ms, fps='%.1f' % fps,
                had_target=int(dx is not None))

            if any(channels) and now_wall - last_sent >= args.send_interval:
                result, grant = send_velocity(channels, grant)
                last_sent = now_wall
                sent_count += 1
                if result is not None and result.ok and any(channels):
                    emit('警告', '非零速度在地面被放行（不应发生）：%s' % (channels,))

            if now_wall >= next_status:
                next_status = now_wall + args.status_interval
                status = controller.get_status()
                snapshot = poller.refresh()
                frames_now = grabber.latest()[1]
                span = max(1e-6, now_wall - fps_ref_time)
                fps = (frames_now - fps_ref_count) / span
                fps_ref_count, fps_ref_time = frames_now, now_wall
                emit('状态', status_line(snapshot, status, fps, dx, yaw_rc, yaw_norm,
                                         reason, detect_ms))
                emit('Yaw', '状态机=%s  参考尺寸=%s  识别帧=%d  已发速度=%d  已发布帧=%d%s' % (
                    steering.controller.state,
                    '已标定' if steering.controller.reference_size else '未标定',
                    vision.processed, sent_count, grabber.published,
                    '' if not vision.error else '  识别错误=%s' % vision.error))
                current = snapshot.get('battery')
                if isinstance(current, int) and current <= args.battery_min:
                    emit('电量', '电量降至 %d%%（阈值 %d%%），提前安全收工'
                         % (current, args.battery_min))
                    break

            if not args.no_window:
                if frame is not None and now_wall - last_show > 1 / 20.0:
                    last_show = now_wall
                    cv2.imshow(WINDOW, draw_visual(frame, observation, yaw_rc, yaw_norm, dx,
                                                   str(steering.controller.state), reason, fps,
                                                   poller.snapshot(), marker_id, note,
                                                   steering.caps[3]))
                    window_created = True
                pressed = cv2.waitKey(15) & 0xFF
                key = '' if pressed == 255 else chr(pressed).lower()
                if window_created and cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
                    emit('状态', '视觉台窗口已关闭，准备退出')
                    break
            else:
                key = ''
                time.sleep(0.05)

            key = key or keys.get(0.0)
            if key:
                watchdog.tick('key:%s' % key)
                if key in ('q', '\x1b'):
                    break
                if key == 's':
                    handle_emergency('stop', '键盘 s', args)
                elif key == 'b':
                    emit('状态', status_line(poller.refresh(), controller.get_status(), fps, dx,
                                             yaw_rc, yaw_norm, reason, detect_ms))
                elif key == 'c':
                    ok, why = steering.controller.calibrate(time.monotonic())
                    emit('标定', '手动重新标定%s：%s' % ('成功' if ok else '失败', why))
                elif key == 'r':
                    steering.reset()
                elif key == 'p' and voice is not None:
                    voice.toggle()

            if deadline and time.time() >= deadline:
                emit('状态', '到达 --seconds 设定时长，收工')
                break
        return 0
    except KeyboardInterrupt:
        emit('状态', '收到 Ctrl+C，开始清理')
        return 0
    except Exception as exc:
        emit('错误', '主循环异常，安全收工：%s: %s' % (type(exc).__name__, exc))
        return 1
    finally:
        emit('状态', '清理：停快捷键 / 停语音 / 停识别 / 释放控制权 / 停跟随 / 关图传 / 断开')
        telemetry.close()
        if args.log_csv:
            emit('遥测', 'CSV 已写入 %d 行 → %s' % (telemetry.rows, args.log_csv))
        if hotkeys is not None:
            hotkeys.stop()
        if not args.no_window:
            try:
                cv2.destroyAllWindows()
            except Exception:
                pass
        if voice is not None:
            voice.stop()
        for thread in (vision, grabber, keys, watchdog, poller):
            thread.stop()
        controller.stop_follow()
        current = controller.get_status().control_owner
        if current != 'none':
            released = controller.release_control(current)
            emit('清理', '释放控制权 %s → %s' % (current, describe(released)))
        try:
            driver.stop_video()
        except Exception as exc:
            emit('清理', '关闭图传失败（继续断开）：%s' % exc)
        try:
            driver.disconnect()
        except Exception as exc:
            emit('清理', '断开失败（请手动给飞机断电）：%s' % exc)
        emit('清理', '已断开真机（本次没有执行任何起飞指令）')


def self_check(args):
    """离线自检：识别、X 偏差、Yaw 符号/限幅、丢码归零、地面拦截、叠加层、急停识别。"""
    from r3_follow.config import FollowConfig
    from r3_follow.vision import MarkerTracker, synthetic_frame

    checks = []
    tracker = MarkerTracker(target_id=0)
    frame = synthetic_frame(x=.70, size=.20, visible=True, marker_id=0)
    observation = tracker.detect(frame, 1, 100.0)
    checks.append(('三组 MarkerTracker 能识别合成 ArUco', bool(observation.found)))
    checks.append(('识别到的是目标 id=0', observation.target_id == 0))
    expected = int(round(observation.center_x - frame.shape[1] / 2.0))
    checks.append(('X 偏差 = cx - width/2（合成帧约为 +128）', 100 <= expected <= 150))

    config = FollowConfig(instance_id='selfcheck', target_id=0, dictionary='DICT_4X4_50',
                          control_profile='openpose_pid', physical_distance_m=None,
                          steering_mode='yaw')
    steering = YawSteering(config, 0, None, .2, .3, 0.0)
    now, motion = 100.0, None
    for seq in range(2, 10):
        observation = tracker.detect(synthetic_frame(x=.70, size=.20, marker_id=0), seq, now)
        steering.observe(observation, now)
        motion = steering.tick(now)
        now += 0.05
    found_obs = observation
    checks.append(('目标在画面右侧 → Yaw 向右（>0）', motion is not None and motion.yaw > 0))
    channels = to_channels(motion, rc_caps(.2, .3)) if motion is not None else (0, 0, 0, 0)
    checks.append(('旧预算下 Yaw 通道在 ±20 RC 内', 0 < channels[3] <= 20))

    # RC 预算：配置里的两个 limit 必须同时抬高算法输出与服务钳位，且不能超过 SDK 的 ±100
    checks.append(('预算换算 0.5/1.0 → (100,100,0,50)', rc_caps(.5, 1.0) == (100, 100, 0, 50)))
    checks.append(('预算换算 0.2/0.3 → (30,30,0,20)（旧默认不变）',
                   rc_caps(.2, .3) == (30, 30, 0, 20)))
    fast = YawSteering(config, 0, None, .5, 1.0, 0.0)
    now_fast, fast_motion = 200.0, None
    for seq in range(2, 14):
        # 第 1 帧按大尺寸标定，之后目标变"小"→ 深度误差拉满，检查前后通道也能超过旧上限
        size = .40 if seq == 2 else .20
        observation = tracker.detect(synthetic_frame(x=.90, size=size, marker_id=0), seq, now_fast)
        fast.observe(observation, now_fast)
        fast_motion = fast.tick(now_fast)
        now_fast += 0.05
    raised = to_channels(fast_motion, fast.caps) if fast_motion is not None else (0, 0, 0, 0)
    checks.append(('放宽后 Yaw 真的能超过旧的 ±20（未被静默夹回）', abs(raised[3]) > 20))
    checks.append(('放宽后前后通道能超过旧的 ±30', abs(raised[1]) > 30))
    checks.append(('升降通道恒为 0（三组从不下达高度指令）', raised[2] == 0))
    checks.append(('任何预算下 RC 都不超过 ±100',
                   all(abs(value) <= 100 for value in raised)))

    lost = tracker.detect(synthetic_frame(visible=False), 20, now)
    steering.observe(lost, now)
    motion = steering.tick(now + 0.01)
    checks.append(('丢码后 Yaw 归零', motion is not None and motion.yaw == 0))

    driver.set_simulated_delay(0)
    owner = controller.get_status().control_owner
    if owner != 'none':
        controller.release_control(owner)
    grant = controller.request_control('follow')
    blocked = controller.update_velocity('follow', 0, 0, 0, 12,
                                         control_epoch=grant.control_epoch,
                                         command_epoch=grant.command_epoch)
    checks.append(('地面非零速度被 is_landed 拦截',
                   blocked.ok is False and blocked.reason == 'is_landed'))
    if grant.ok:
        controller.release_control('follow', control_epoch=grant.control_epoch)

    image = draw_visual(synthetic_frame(x=.70, size=.20), found_obs, -12, -0.12, expected,
                        'tracking', 'self_check', 12.0,
                        dict(battery=66, height_cm=0, link_ok=True, owner='none', video=True),
                        0, '', 50)
    checks.append(('视觉台叠加层绘制不抛异常且尺寸不变',
                   image is not None and image.shape == synthetic_frame().shape))

    checks.append(('“急停”识别为 stop', detect_emergency('急停') == 'stop'))
    checks.append(('“降落”识别为 land', detect_emergency('降落') == 'land'))
    checks.append(('普通指令不误判', detect_emergency('跟着我') == ''))

    # 物理全局快捷键：不真正挂钩键盘（自检不接管用户输入），只校验解析、映射与防连发
    hotkey_map = {combo: kind for combo, kind, _human in HOTKEYS}
    checks.append(('快捷键映射含 Ctrl+Shift+Alt+A=stop / +L=land',
                   hotkey_map.get('<ctrl>+<shift>+<alt>+a') == 'stop'
                   and hotkey_map.get('<ctrl>+<shift>+<alt>+l') == 'land'))
    keyboard = ensure_pynput()
    checks.append(('pynput 可用（全局组合键依赖）', keyboard is not None))
    if keyboard is not None:
        try:
            parsed = all(keyboard.HotKey.parse(combo) for combo in hotkey_map)
        except Exception as exc:
            parsed = False
            emit('错误', 'pynput 解析组合键失败：%s' % exc)
        checks.append(('pynput 能解析两个组合键', parsed))
    guard = GlobalEmergencyHotkeys(args, cooldown=1.0)
    checks.append(('同一安全指令 1 秒内只执行一次（防按住连发）',
                   guard.allow('stop', 100.0) and not guard.allow('stop', 100.5)
                   and guard.allow('stop', 101.5) and guard.allow('land', 100.0)))

    failures = [name for name, ok in checks if not ok]
    for name, ok in checks:
        emit('PASS' if ok else 'FAIL', name)

    try:
        import local_stt
        import offline_tts
        import audio_io
        audio_io.dependencies()
        emit('提示', '语音依赖可用：本地 STT=%s  离线 TTS=%s（真机跑时会自动使用）'
             % ('on' if local_stt.installed() else 'off',
                'on' if offline_tts.available() else 'off'))
    except Exception as exc:
        emit('提示', '语音依赖不可用（不影响视觉/Yaw 自检）：%s' % exc)
    emit('汇总', '%d/%d 通过' % (len(checks) - len(failures), len(checks)))
    return 0 if not failures else 1


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='三组 Yaw 跟随算法真机地面调试（终端主控台 + OpenCV 视觉台）')
    parser.add_argument('--self-check', action='store_true', help='离线自检，不连飞机、不碰声卡')
    parser.add_argument('--props-removed', action='store_true', help='确认真机桨叶已拆下（真机必填）')
    parser.add_argument('--follow-config', default=None,
                        help='跟随配置 JSON；默认 R3_FOLLOW_CONFIG 或 modules/follow/configs/aircraft_A_ID0.json')
    parser.add_argument('--marker-id', type=int, default=None, help='ArUco 目标 id（默认取跟随配置）')
    parser.add_argument('--address', default=None, help='飞机 IP，默认 192.168.10.1')
    parser.add_argument('--timeout', type=float, default=driver.RESPONSE_TIMEOUT_S)
    parser.add_argument('--frame-timeout', type=float, default=12.0, help='等真实画面的秒数')
    parser.add_argument('--seconds', type=float, default=0, help='运行 N 秒后自动退出（0=一直跑）')
    parser.add_argument('--status-interval', type=float, default=2.0, help='状态打印间隔（秒）')
    parser.add_argument('--battery-min', type=int, default=10, help='电量低于该值自动收工')
    parser.add_argument('--send-interval', type=float, default=0.5, help='速度下发周期（秒）')
    parser.add_argument('--max-request-rate', type=float, default=None,
                        help='速度变化率上限（每秒；默认取配置，旧默认 0.8，0=关闭渐变）')
    parser.add_argument('--yaw-limit', type=float, default=None,
                        help='Yaw 归一化上限（默认取跟随配置，旧默认 0.20 → ±20 RC）')
    parser.add_argument('--forward-limit', type=float, default=None,
                        help='前后平移归一化上限（默认取跟随配置，旧默认 0.30 → ±30 RC）')
    parser.add_argument('--recovery-window', type=float, default=3.0, help='短暂丢码恢复窗口（秒）')
    parser.add_argument('--detect-budget-ms', type=float, default=120.0,
                        help='三组识别单帧时间预算（毫秒，与线上服务一致）')
    parser.add_argument('--log-csv', default=None, metavar='PATH',
                        help='把每个控制 tick 追加写入 CSV（t/cx/dx/yaw/fb/state…）；只写文件，不影响控制')
    parser.add_argument('--no-window', action='store_true', help='不弹视觉台窗口（只留主控台）')
    parser.add_argument('--no-voice', action='store_true', help='不启动四组语音监听')
    parser.add_argument('--live', action='store_true', help='语音理解用真实大模型（需 AI_* 环境变量）')
    parser.add_argument('--audio-config', default=None, help='audio_config.json 路径')
    parser.add_argument('--input-device', type=int, default=None, help='麦克风设备索引')
    parser.add_argument('--output-device', type=int, default=None, help='喇叭设备索引')
    parser.add_argument('--silence-s', type=float, default=1.5, help='静音多久算一句话说完')
    parser.add_argument('--max-utterance', type=float, default=10.0, help='单句最长录音秒数')
    parser.add_argument('--soft-land', action='store_true',
                        help='地面状态下收到“降落”只做零速度（物理快捷键 Ctrl+Shift+Alt+L 始终真发 land）')
    parser.add_argument('--no-hotkeys', action='store_true',
                        help='不注册 pynput 全局组合键（仍可用窗口/终端的 s 键急停）')
    parser.add_argument('--color', action='store_true', help='强制开启 ANSI 颜色')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    enable_ansi(args.color)
    disable_quick_edit()
    if args.self_check:
        try:
            return self_check(args)
        finally:
            close_output()
    if not args.props_removed:
        print('拒绝连接：地面调试必须先拆下桨叶。', flush=True)
        print('  离线自检：python run_yaw_ground_test.py --self-check', flush=True)
        print('  真机调试：python run_yaw_ground_test.py --props-removed', flush=True)
        return 2
    if args.marker_id is not None and not 0 <= args.marker_id < 50:
        print('--marker-id 必须是 0~49（DICT_4X4_50）', flush=True)
        return 2
    if not 1 <= args.battery_min <= 100:
        print('--battery-min 必须在 1~100 之间', flush=True)
        return 2
    for name, value in (('--yaw-limit', args.yaw_limit),
                        ('--forward-limit', args.forward_limit)):
        if value is not None and not 0 < value <= 1:
            print('%s 必须在 (0, 1] 之间（1.0 = ±100 RC）' % name, flush=True)
            return 2
    if args.max_request_rate is not None and args.max_request_rate < 0:
        print('--max-request-rate 不能为负（0=关闭渐变）', flush=True)
        return 2
    set_console_title(CONSOLE_TITLE)
    throttle = Throttle(1.0)
    install_taps(throttle)
    tail = EventTail(throttle)
    tail.start()
    emit('启动', 'Yaw 地面调试台启动：窗口1=本终端，窗口2=OpenCV 视觉台（不会起飞）')
    try:
        return run(args, throttle)
    finally:
        tail.stop()
        close_output()


if __name__ == '__main__':
    sys.exit(main())
