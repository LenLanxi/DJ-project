import os
from http_transport import post_json

class ModelClient:
    def __init__(self):
        self.url = os.environ.get('AI_CHAT_URL', '')
        self.key = os.environ.get('AI_API_KEY', '')
        self.model = os.environ.get('AI_MODEL', '')
        self.last_usage = {}
        if not self.url.startswith('https://') or not self.key or not self.model:
            raise ValueError('请配置AI_CHAT_URL、AI_API_KEY、AI_MODEL')

    def complete(self, messages):
        result = post_json(self.url, self.key, {'model': self.model, 'messages': messages})
        try:
            content = result['choices'][0]['message']['content']
            if not isinstance(content, str): raise TypeError()
            self.last_usage = result.get('usage', {})
            return content
        except (KeyError, IndexError, TypeError, AttributeError):
            raise RuntimeError('模型返回格式不兼容') from None
