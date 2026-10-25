"""物理安全兜底：全局组合键 → 底层 priority 通道（pynput，Windows 低级键盘钩子）。

二组/四组共用：地面调试台（run_yaw_ground_test.py）与实飞入口（modules/ai/main.py）
注册同一组组合键，避免"两套键、现场按错"。

    Ctrl+Shift+Alt+A   急停 / 悬停
    Ctrl+Shift+Alt+L   降落

不需要管理员权限；焦点在任何窗口都有效。缺少 pynput 时尝试自动安装一次，
失败则只打印提示（调用方的 F12/F11 等备用通道不受影响）。
"""
import os
import subprocess
import sys
import threading
import time

HOTKEY_COOLDOWN_S = 1.0
HOTKEYS = (
    ('<ctrl>+<shift>+<alt>+a', 'stop', 'Ctrl+Shift+Alt+A'),
    ('<ctrl>+<shift>+<alt>+l', 'land', 'Ctrl+Shift+Alt+L'),
)
HOTKEY_HELP = ('物理快捷键（全局）：Ctrl+Shift+Alt+A=急停  Ctrl+Shift+Alt+L=降落  '
               '[焦点在哪个窗口都有效]')


def _default_emit(tag, text):
    try:
        print('[%s] %s' % (tag, text), flush=True)
    except Exception:
        pass


def ensure_pynput(emit=None):
    """没有 pynput 就现场装一次（同一个解释器）；返回 keyboard 模块或 None。"""
    emit = emit or _default_emit
    try:
        from pynput import keyboard
        return keyboard
    except Exception as exc:
        emit('快捷键', '缺少 pynput（%s），正在自动安装…' % exc)
    try:
        completed = subprocess.run(
            [sys.executable, '-m', 'pip', 'install', 'pynput'],
            capture_output=True, text=True, timeout=300)
        lines = [line for line in ((completed.stdout or '') +
                                   (completed.stderr or '')).splitlines() if line.strip()]
        emit('快捷键', 'pip install pynput %s %s' % (
            '成功' if completed.returncode == 0 else '失败（返回码 %s）' % completed.returncode,
            lines[-1] if lines else ''))
    except Exception as exc:
        emit('快捷键', '自动安装失败：%s: %s' % (type(exc).__name__, exc))
    try:
        import importlib
        importlib.invalidate_caches()
        from pynput import keyboard
        return keyboard
    except Exception as exc:
        emit('快捷键', 'pynput 仍不可用（%s）：全局组合键关闭，F12/F11 或 s 键仍可用' % exc)
        emit('提示', '手动安装：%s -m pip install pynput' % sys.executable)
        return None


def command_failed(result):
    return result is not None and not getattr(result, 'ok', True)


class EmergencyHotkeys(threading.Thread):
    """注册 Ctrl+Shift+Alt+A/L；on_trigger(kind, combo) 由调用方接到 priority 通道。"""

    def __init__(self, on_trigger, cooldown=HOTKEY_COOLDOWN_S, emit=None):
        super().__init__(daemon=True, name='emergency-hotkeys')
        if not callable(on_trigger):
            raise TypeError('on_trigger must be callable')
        self._on_trigger = on_trigger
        self._emit = emit or _default_emit
        self.cooldown = cooldown
        self.listener = None
        self.ready = False
        self.error = ''
        self.fired = []
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
            self.fired.append((kind, now))
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

    def run(self):
        keyboard = ensure_pynput(self._emit)
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
            self._emit('快捷键', HOTKEY_HELP)
        except Exception as exc:
            self.error = '%s: %s' % (type(exc).__name__, exc)
            self._emit('快捷键', '全局组合键注册失败（%s）：请用 F12/F11（或 s 键）急停' % self.error)

    def stop(self):
        listener = self.listener
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                pass
            if listener.is_alive():
                try:
                    listener.join(1.0)
                except Exception:
                    pass
        if self.is_alive():
            self.join(1.0)
