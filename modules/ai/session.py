"""Serial conversation worker plus separate priority interrupt worker.
Mock-only. Epoch prevents a cancelled model response from executing later.
"""
import threading
from concurrent.futures import ThreadPoolExecutor

class Session:
    def __init__(self, agent):
        self.agent = agent
        self.lock = threading.Lock()
        self.epoch = 0
        self.busy = False
        self.closed = False
        self.interrupting = False
        # 移植自 v0.4 gateway.py：取消标志必须在任何阻塞调用之前置位。
        self.cancelled = threading.Event()
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix='conversation')
        self.priority = ThreadPoolExecutor(max_workers=1, thread_name_prefix='interrupt')

    def submit(self, text):
        with self.lock:
            if self.closed or self.busy or self.interrupting:
                raise RuntimeError('当前正在处理，请等待；停止/降落通道仍可用')
            self.busy = True
            epoch = self.epoch
            self.cancelled.clear()  # 新一轮开始，清掉上一轮的取消标志
        return self.worker.submit(self._turn, text, epoch)

    def _turn(self, text, epoch):
        # Agent works on a per-turn proxy. Only commit history if still current.
        from ai_agent import Agent
        outer = self
        class GuardedRunner:
            def __init__(self): self.demo = outer.agent.runner.demo
            def run(self, code):
                with outer.lock:                      # 锁内只做检查
                    if (epoch != outer.epoch or outer.closed
                            or outer.cancelled.is_set()):
                        return {'accepted': False, 'simulated': True, 'result': '本轮已取消，未执行迟到程序'}
                # 锁已释放：真机阻塞动作在锁外执行，急停线程不会被挡住。
                drone = self.demo.drone
                if hasattr(drone, 'turn_guard'):
                    with drone.turn_guard(lambda: epoch != outer.epoch or outer.closed or outer.cancelled.is_set()):
                        return outer.agent.runner.run(code)
                return outer.agent.runner.run(code)
        turn = Agent(self.agent.client, GuardedRunner(), self.agent.vision)
        turn.history = list(self.agent.history)
        try:
            record = turn.handle(text)
            with self.lock:
                if epoch != self.epoch or self.closed:
                    return {'request': text, 'reply': '本轮结果已作废，不播报旧动作结果',
                            'cancelled': True, 'execution': {'accepted': False, 'simulated': True}}
                self.agent.history = turn.history
            return record
        finally:
            with self.lock: self.busy = False

    def interrupt(self, land=False):
        with self.lock:
            if self.closed: raise RuntimeError('会话已关闭')
            if self.interrupting: raise RuntimeError('正在处理上一条中断请求')
            self.interrupting = True
            self.epoch += 1
            # 移植自 v0.4 gateway.interrupt：先置取消标志，再等待任何东西。
            self.cancelled.set()
            self.agent.history.clear()

        def perform():
            try:
                # v0.4 顺序：先停跟随（内部 0 速度 + 释放控制权）→ 再接管 → 再急停。
                self._stop_follow_safely()
                return self.agent.runner.run('drone.land()' if land else 'drone.stop_motion()')
            finally:
                with self.lock: self.interrupting = False

        return self.priority.submit(perform)

    def _stop_follow_safely(self):
        """跟随时控制权在 follow 手里，不先释放就抢不到 model。"""
        drone = getattr(getattr(self.agent.runner, 'demo', None), 'drone', None)
        if drone is None:
            return
        try:
            drone.stop_follow()
        except (RuntimeError, OSError, ValueError):
            pass

    def close(self):
        with self.lock:
            self.closed = True
            self.epoch += 1
            self.cancelled.set()
        # Closing a turn is also a device-control boundary. Invalidate follow
        # and send stop before waiting for a previously blocking worker.
        drone = getattr(getattr(self.agent.runner, 'demo', None), 'drone', None)
        if drone is not None:
            state = drone.get_status()
            if state.get('following') or state.get('owner') == 'model':
                self._stop_follow_safely()
                self.agent.runner.run('drone.stop_motion()')
        self.worker.shutdown(wait=True, cancel_futures=True)
        self.priority.shutdown(wait=True, cancel_futures=True)
