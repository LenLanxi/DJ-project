"""本地语音转写（faster-whisper）：无需 API Key，模型缓存后可离线使用。

与 voice_service.VoiceService 的关系
    VoiceService 走 OpenAI 兼容 /audio/transcriptions（需要 Key）；
    本模块是它的本地替代：输入同样的 16 位 PCM WAV 字节，输出同样的文本。

环境变量
    AI_LOCAL_STT_MODEL     模型名或本地目录，默认 small（中文兼顾速度与准确率）
    AI_LOCAL_STT_DIR       模型缓存目录，默认 <repo>/models/whisper
    AI_LOCAL_STT_COMPUTE   ctranslate2 计算类型，默认 int8（CPU 友好）
    AI_LOCAL_STT_LANG      识别语言，默认 zh
    HF_ENDPOINT            首次下载走镜像，例如 https://hf-mirror.com
"""
import io
import os
import re
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_DIR = os.getenv('AI_LOCAL_STT_DIR') or os.path.join(ROOT, 'models', 'whisper')
DEFAULT_MODEL = os.getenv('AI_LOCAL_STT_MODEL', 'small')
DEFAULT_COMPUTE = os.getenv('AI_LOCAL_STT_COMPUTE', 'int8')
DEFAULT_LANGUAGE = os.getenv('AI_LOCAL_STT_LANG', 'zh')

_lock = threading.Lock()
_models = {}

# whisper 有时输出繁体，4 组的关键词匹配用的是简体，这里做一层兜底转换
_TRAD_TO_SIMP = str.maketrans({
    '飛': '飞', '電': '电', '還': '还', '麼': '么', '後': '后', '轉': '转', '開': '开',
    '關': '关', '這': '这', '個': '个', '們': '们', '來': '来', '說': '说', '話': '话',
    '嗎': '吗', '現': '现', '對': '对', '於': '于', '裡': '里', '邊': '边', '進': '进',
    '著': '着', '沒': '没', '無': '无', '懸': '悬', '攝': '摄', '頭': '头', '視': '视',
    '頻': '频', '圖': '图', '傳': '传', '離': '离', '動': '动', '訊': '讯', '號': '号',
    '準': '准', '備': '备', '續': '续', '暫': '暂', '繼': '继',
})
INITIAL_PROMPT = ('以下是普通话指令，请用简体中文输出：起飞，降落，向前飞，向后飞，'
                  '向左飞，向右飞，上升，下降，跟着我，停下来，还有多少电。')


def resolve_model(name=None):
    """优先用本地目录（models/whisper/<name>），否则交给 faster-whisper 自行下载。"""
    name = name or DEFAULT_MODEL
    if os.path.isdir(name):
        return name
    local = os.path.join(DEFAULT_DIR, name)
    if os.path.isfile(os.path.join(local, 'model.bin')):
        return local
    return name


def to_simplified(text):
    return text.translate(_TRAD_TO_SIMP) if text else text


_TRIM = '，。、！？；：,.!?;: \t\r\n“”"\'（）()【】[]'
def clean_text(text):
    """去掉首尾标点（初始提示词会让 whisper 模仿句读，命令匹配不需要）。"""
    if not text:
        return ''
    return re.sub(r'\s+', ' ', text.strip().strip(_TRIM)).strip()


def installed():
    """faster-whisper 是否可用（只查包，不加载模型）。"""
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def model_cached(name=None):
    """模型文件是否已在本地缓存目录里（不联网判断）。"""
    name = name or DEFAULT_MODEL
    if os.path.isdir(name) and os.path.isfile(os.path.join(name, 'model.bin')):
        return True
    local = os.path.join(DEFAULT_DIR, name)
    if os.path.isfile(os.path.join(local, 'model.bin')):
        return True
    hub = os.path.join(DEFAULT_DIR, 'models--Systran--faster-whisper-' + name)
    return os.path.isdir(hub)


def _load(name, compute_type):
    from faster_whisper import WhisperModel
    resolved = resolve_model(name)
    with _lock:
        key = (resolved, compute_type)
        if key not in _models:
            os.makedirs(DEFAULT_DIR, exist_ok=True)
            _models[key] = WhisperModel(resolved, device='cpu', compute_type=compute_type,
                                        download_root=DEFAULT_DIR)
        return _models[key]


class LocalWhisper:
    """把 WAV 字节转成文本；第一次调用会加载（或下载）模型，之后常驻内存。"""

    def __init__(self, model=None, compute_type=None, language=None):
        self.name = model or DEFAULT_MODEL
        self.compute_type = compute_type or DEFAULT_COMPUTE
        self.language = language or DEFAULT_LANGUAGE
        self.last_info = {}

    def warmup(self):
        """提前加载模型，避免第一次说话时卡几秒。"""
        _load(self.name, self.compute_type)
        return self

    def transcribe(self, wav):
        model = _load(self.name, self.compute_type)
        # 传字节流让 faster-whisper 用 ffmpeg 解码并重采样到 16k，
        # 避免把 44.1k 的数组按 16k 解释（会导致语速/音调错误）
        segments, info = model.transcribe(
            io.BytesIO(wav), language=self.language, beam_size=1,
            vad_filter=True, condition_on_previous_text=False,
            initial_prompt=INITIAL_PROMPT if self.language.startswith('zh') else None)
        raw = ''.join(segment.text for segment in segments).strip()
        text = clean_text(to_simplified(raw))
        self.last_info = {'language': getattr(info, 'language', self.language),
                          'duration': round(float(getattr(info, 'duration', 0.0)), 2),
                          'raw': raw, 'converted': clean_text(raw) != raw}
        return text


def describe():
    if not installed():
        return '未安装（pip install faster-whisper）'
    state = '已缓存' if model_cached() else '未缓存（首次运行需下载）'
    return 'faster-whisper %s，模型=%s（%s），目录=%s' % (
        _version(), DEFAULT_MODEL, state, DEFAULT_DIR)


def _version():
    try:
        import importlib.metadata as metadata
        return metadata.version('faster-whisper')
    except Exception:
        return '?'
