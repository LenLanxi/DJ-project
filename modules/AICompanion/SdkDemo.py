"""Explicit synchronous integration entry; current public SDK runs fake flight."""
import argparse
import json

from .ai_agent import Agent
from .demo_ai import ScriptedClient
from .SdkRunner import SdkRunner


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', action='store_true', help='执行固定SDK桩联调场景')
    args = parser.parse_args()
    print('离线脚本（不是AI）+公共SDK；当前SDK为假飞行桩，不证明真机可用。')
    print('本入口同步执行，不支持阻塞动作的并发中断；尚未接入跟随。')
    agent = Agent(ScriptedClient(), runner=SdkRunner())

    def turn(text):
        print(json.dumps(agent.handle(text), ensure_ascii=False, indent=2))

    if args.scenario:
        for text in ('还有多少电', '起飞', '向前走30厘米', '降落'):
            turn(text)
    else:
        while True:
            try:
                text = input('SDK联调（退出结束）：').strip()
            except (EOFError, KeyboardInterrupt):
                break
            if text == '退出':
                break
            if text:
                turn(text)


if __name__ == '__main__':
    main()
