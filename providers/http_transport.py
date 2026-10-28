"""Bounded HTTPS POST; never redirects credentials or retries a request."""
import json
import urllib.request
import urllib.error
from urllib.parse import urlsplit

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None

def post(url, key, body, content_type='application/json', limit=2000000, *, timeout=30):
    u = urlsplit(url)
    if u.scheme != 'https' or not u.hostname or u.username or u.password or u.fragment:
        raise ValueError('服务地址须为不含账号密码及片段的完整HTTPS地址')
    if not key:
        raise ValueError('缺少API密钥')
    request = urllib.request.Request(url, data=body, headers={
        'Content-Type': content_type, 'Authorization': 'Bearer ' + key})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            data = response.read(limit + 1)
        if len(data) > limit:
            raise RuntimeError('服务响应过大')
        return data
    except urllib.error.HTTPError as exc:
        raise RuntimeError('服务HTTP错误%d；未重试' % exc.code) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RuntimeError('服务连接失败或超时；未重试') from None

def post_json(url, key, value, *, timeout=None):
    try:
        body = json.dumps(value).encode()
        return json.loads(post(url, key, body) if timeout is None else post(url, key, body, timeout=timeout))
    except (json.JSONDecodeError, UnicodeError):
        raise RuntimeError('服务返回的JSON无效') from None
