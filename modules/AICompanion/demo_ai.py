import argparse
import json
from pathlib import Path
from ai_agent import Agent
from model_client import ModelClient

class ScriptedClient:
    """Offline fixtures, explicitly NOT an AI model."""
    def complete(self, messages):
        request = json.loads(messages[-1]['content'])['request']
        mapping = {'还有多少电': 'get_battery()', '起飞': 'takeoff()',
                   '跟着我': 'start_follow()', '向前走30厘米': 'move_forward(30)',
                   '降落': 'land()'}
        if request in mapping:
            return json.dumps({'kind': 'program', 'code': 'drone.' + mapping[request]})
        return json.dumps({'kind': 'reply', 'text': '离线脚本没有这个样例，未调用真实AI。'})

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true', help='连接已配置模型；无人机仍然模拟')
    parser.add_argument('--scenario', action='store_true', help='自动运行演示')
    parser.add_argument('--log', help='可选JSONL日志路径，包含用户输入；默认不保存')
    args = parser.parse_args()
    try:
        agent = Agent(ModelClient() if args.live else ScriptedClient())
    except ValueError as exc:
        print(exc)
        return
    print('真实模型+模拟无人机' if args.live else '离线脚本+模拟无人机（不是AI）')
    def turn(text):
        try:
            record = agent.handle(text)
        except (ValueError, RuntimeError) as exc:
            print('失败：', exc)
            return
        print(json.dumps(record, ensure_ascii=False, indent=2))
        if args.log:
            with Path(args.log).open('a', encoding='utf-8') as f:
                f.write(json.dumps(record, ensure_ascii=False) + '\n')
    if args.scenario:
        for text in ('还有多少电', '起飞', '跟着我', '向前走30厘米', '降落'):
            turn(text)
    else:
        while True:
            try:
                text = input('你（退出结束）：')
            except (EOFError, KeyboardInterrupt):
                break
            if text == '退出':
                break
            turn(text)

if __name__ == '__main__':
    main()
