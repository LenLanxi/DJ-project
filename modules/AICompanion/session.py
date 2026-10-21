"""Serial conversation worker plus separate priority interrupt worker.
Mock-only. Epoch prevents a cancelled model response from executing later.
"""
import threading
from concurrent.futures import ThreadPoolExecutor

class Session:
    def __init__(self, agent):
        if not getattr(agent.runner, 'supports_priority_interrupt', False):
            raise ValueError('公共SDK的阻塞中断尚未验证，请使用同步SdkDemo入口')
        self.agent = agent
        self.lock = threading.Lock()
        self.epoch = 0
        self.busy = False
        self.closed = False
        self.interrupting = False
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix='conversation')
        self.priority = ThreadPoolExecutor(max_workers=1, thread_name_prefix='interrupt')

    def submit(self, text):
        with self.lock:
            if self.closed or self.busy or self.interrupting:
                raise RuntimeError('当前正在处理，请等待；停止/降落通道仍可用')
            self.busy = True
            epoch = self.epoch
        return self.worker.submit(self._turn, text, epoch)

    def _turn(self, text, epoch):
        # Agent works on a per-turn proxy. Only commit history if still current.
        from .ai_agent import Agent
        outer = self
        class GuardedRunner:
            simulated = outer.agent.runner.simulated
            def get_status(self): return outer.agent.runner.get_status()
            def run(self, code):
                with outer.lock:
                    if epoch != outer.epoch or outer.closed:
                        return {'accepted': False, 'simulated': True, 'result': '本轮已取消，未执行迟到程序'}
                    # Mock calls are immediate; keep check+execution atomic.
                    # Do not hold this lock around future real blocking actions.
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
            self.agent.history.clear()
        def perform():
            try:
                with self.lock:
                    return self.agent.runner.run('drone.land()' if land else 'drone.stop_motion()')
            finally:
                with self.lock: self.interrupting = False
        return self.priority.submit(perform)

    def close(self):
        with self.lock:
            self.closed = True
            self.epoch += 1
        self.worker.shutdown(wait=True, cancel_futures=True)
        self.priority.shutdown(wait=True, cancel_futures=True)
