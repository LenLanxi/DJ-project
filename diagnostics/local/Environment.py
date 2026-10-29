"""Local-only diagnostics; prints presence, never key values; makes no network calls."""
import importlib.util
import os
import platform
import sys

print('Python:', platform.python_version())
print('运行环境:', platform.system())
print('Python版本合格:', sys.version_info[:2] == (3, 11))
for name in ('numpy', 'cv2', 'av', 'djitellopy', 'websockets'):
    print(name + ':', '已安装' if importlib.util.find_spec(name) else '未安装')
print('豆包凭据路径:', '已设置' if os.getenv('DOUBAO_CREDENTIALS_FILE') else '未设置，可通过启动参数指定')
print('此诊断不代表模型可连接、麦克风可用或实机兼容。')
