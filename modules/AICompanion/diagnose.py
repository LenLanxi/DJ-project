"""Local-only diagnostics; prints presence, never key values; makes no network calls."""
import importlib.util
import os
import platform
import sys

print('Python:', platform.python_version())
print('运行环境:', platform.system())
print('Python版本合格:', sys.version_info >= (3, 10))
for name in ('numpy', 'sounddevice', 'cv2'):
    print(name + ':', '已安装' if importlib.util.find_spec(name) else '未安装（可选）')
for name in ('AI_CHAT_URL','AI_API_KEY','AI_MODEL','AI_STT_URL','AI_STT_MODEL',
             'AI_TTS_URL','AI_TTS_MODEL','AI_TTS_VOICE'):
    print(name + ':', '已设置' if os.getenv(name) else '未设置')
print('此诊断不代表模型可连接、麦克风可用或实机兼容。')
