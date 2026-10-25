import argparse
import json
import re
from pathlib import Path
from ai_agent import Agent
from model_client import ModelClient


# 语音识别结果常带标点/语气词（"起飞。"），归一化后再匹配，
# 免得实飞现场因为一个句号被判成"听不清"。
_PUNCT = re.compile(r'[\s，。、！？!?,.;:：~…「」“”"\'（）()\[\]【】\-]+')

# 顺序即优先级（尤其"停止跟随"必须早于"跟随"）；长词写在前，避免被短词吃掉。
SCRIPTED_COMMANDS = (
    (('还有多少电', '还有多少电量', '电量'), 'get_battery()'),
    (('向前走30厘米',), 'move_forward(30)'),
    (('停止跟随', '别跟着', '结束跟随'), 'stop_follow()'),
    (('停在这里', '悬停', '急停', '停止', '停下', '停'), 'stop_motion()'),
    (('起飞', '开始起飞'), 'takeoff()'),
    (('跟着我', '跟随我', '开始跟随', '跟随'), 'start_follow()'),
    (('降落', '下来', '落地'), 'land()'),
)


def scripted_action(text):
    """把一句（可能带标点的）口语映射成 drone 调用；没有对应样例时返回 None。"""
    cleaned = _PUNCT.sub('', text or '')
    if not cleaned:
        return None
    for phrases, action in SCRIPTED_COMMANDS:      # 先精确匹配，再退化到包含匹配
        if cleaned in phrases:
            return action
    for phrases, action in SCRIPTED_COMMANDS:
        if any(phrase in cleaned for phrase in phrases):
            return action
    return None


class ScriptedClient:
    """Offline fixtures, explicitly NOT an AI model."""
    def complete(self, messages):
        request = json.loads(messages[-1]['content'])['request']
        action = scripted_action(request)
        if action is not None:
            return json.dumps({'kind': 'program', 'code': 'drone.' + action})
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
