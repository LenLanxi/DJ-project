"""物理安全兜底：全局组合键 → 底层 priority 通道（pynput，Windows 低级键盘钩子）。

由应用层操作员控制（app/Hud.py）使用。
注册同一组组合键，避免"两套键、现场按错"。

    Ctrl+Shift+Alt+A   急停 / 悬停
    Ctrl+Shift+Alt+L   降落

不需要管理员权限；焦点在任何窗口都有效。依赖必须提前由项目环境准备；
缺少 pynput 明确报告，不在飞行现场安装包或改变环境。
"""
import threading
import time

HOTKEY_COOLDOWN_S = 1.0
HOTKEYS = (
    ('<ctrl>+<shift>+<alt>+a', 'stop', 'Ctrl+Shift+Alt+A'),
    ('<ctrl>+<shift>+<alt>+l', 'land', 'Ctrl+Shift+Alt+L'),
    ('<f12>', 'stop', 'F12'),
    ('<f11>', 'land', 'F11'),
)
HOTKEY_HELP = ('物理快捷键（全局）：Ctrl+Shift+Alt+A/F12=急停  Ctrl+Shift+Alt+L/F11=降落  '
               '[焦点在哪个窗口都有效]')


def _default_emit(tag, text):
    try:
        print('[%s] %s' % (tag, text), flush=True)
    except Exception:
        pass


def ensure_pynput(emit=None):
    """Import an already-installed dependency; never install at runtime."""
    emit = emit or _default_emit
    try:
        from pynput import keyboard
        return keyboard
    except Exception as exc:
        emit('快捷键', 'pynput 不可用（%s）：全局组合键未启用；飞行前请用 uv 同步项目依赖并重试' % exc)
        return None


class EmergencyHotkeys:
    """One listener owns both the global chords and the F11/F12 shortcuts."""

    def __init__(self, on_trigger, cooldown=HOTKEY_COOLDOWN_S, emit=None):
        if not callable(on_trigger):
            raise TypeError('on_trigger must be callable')
        self._on_trigger = on_trigger
        self._emit = emit or _default_emit
        self.cooldown = cooldown
        self.listener = None
        self._lock = threading.Lock()
        self._last = {}

    def allow(self, kind, now=None):
        """同一条安全指令在 cooldown 内只执行一次，防止按住组合键连发。"""
        now = time.monotonic() if now is None else now
        with self._lock:
            previous = self._last.get(kind)
            if previous is not None and now - previous < self.cooldown:
                return False
            self._last[kind] = now
            return True

    def fire(self, kind, combo):
        if not self.allow(kind):
            return False
        label = '急停/悬停' if kind == 'stop' else '降落'
        self._emit('物理快捷键', '触发%s（%s）—— 走 priority 通道' % (label, combo))
        try:
            self._on_trigger(kind, combo)
        except Exception as exc:
            self._emit('错误', '物理快捷键处理后异常：%s: %s' % (type(exc).__name__, exc))
        return True

    def start(self):
        keyboard = ensure_pynput(self._emit)
        if keyboard is None:
            raise RuntimeError('pynput_unavailable')
        self.listener = keyboard.GlobalHotKeys({
            combo: (lambda k=kind, c=human: self.fire(k, c))
            for combo, kind, human in HOTKEYS
        })
        self.listener.daemon = True
        self.listener.start()
        self._emit('快捷键', HOTKEY_HELP)

    def stop(self):
        if self.listener is not None:
            self.listener.stop()
            self.listener.join(1.0)
            if self.listener.is_alive():
                raise RuntimeError('hotkey_listener_still_running')
