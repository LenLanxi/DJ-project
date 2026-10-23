"""Windows 离线语音合成（SAPI）：无需 API Key，作为 TTS 的降级路径。

与 voice_service.VoiceService 的关系
    VoiceService 走 OpenAI 兼容 /audio/speech（需要 Key）；
    本模块用系统自带的 System.Speech，声音来自 Windows 已安装语音包
    （中文需要中文语音包，例如 Microsoft Huihui）。

编码约定（重要）
    文本通过 stdin 传给 PowerShell 时不直接写中文：`PYTHONUTF8=1` 下 Python 用
    UTF-8 写 stdin，而 Windows PowerShell 5.1 按控制台代码页（cp936）解码，中文会
    变成乱码（表现为"念出来的声音识别成乱码"）。这里统一用 base64(UTF-8) 传输，
    与任何代码页都无关。
"""
import base64
import os
import subprocess

_PICK_ZH = ("$zh = $s.GetInstalledVoices() | Where-Object { "
            "$_.VoiceInfo.Culture.Name.StartsWith('zh') } | Select-Object -First 1;"
            "if ($zh) { $s.SelectVoice($zh.VoiceInfo.Name) };")

_DECODE = ('$b64 = [Console]::In.ReadToEnd();'
           '$text = [System.Text.Encoding]::UTF8.GetString('
           '[System.Convert]::FromBase64String($b64.Trim()));')

_SCRIPT = ('Add-Type -AssemblyName System.Speech;'
           '$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;'
           + _PICK_ZH + _DECODE +
           '$s.Rate = 1;'
           '$s.Speak($text)')

_FILE_SCRIPT = ('Add-Type -AssemblyName System.Speech;'
                '$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;'
                + _PICK_ZH +
                '$path = [Console]::In.ReadLine();' + _DECODE +
                '$s.Rate = 0;'
                '$s.SetOutputToWaveFile($path); $s.Speak($text); $s.Dispose()')


def available():
    return os.name == 'nt'


def _payload(text):
    return base64.b64encode((text or '').encode('utf-8')).decode('ascii')


def _run(script, stdin_text, timeout):
    if not available():
        raise RuntimeError('当前系统不支持 Windows SAPI')
    completed = subprocess.run(['powershell', '-NoProfile', '-Command', script],
                               input=stdin_text, text=True, capture_output=True,
                               timeout=timeout)
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or 'SAPI 调用失败').strip()
        raise RuntimeError(message[:200])
    return True


def speak(text, timeout=120):
    """把文本念出来；失败抛 RuntimeError（调用方决定是否静默）。"""
    text = (text or '').strip()
    if not text:
        raise ValueError('没有可播报的文本')
    return _run(_SCRIPT, _payload(text[:2000]), timeout)


def synthesize_to_wav(text, path, timeout=120):
    """把一句话合成到 WAV 文件（用于离线自检语音链路，不发声）。"""
    if not available():
        raise RuntimeError('当前系统不支持 Windows SAPI')
    _run(_FILE_SCRIPT, '%s\n%s' % (path, _payload(text)), timeout)
    if not os.path.exists(path) or os.path.getsize(path) < 1024:
        raise RuntimeError('SAPI 未生成有效的 WAV 文件')
    return path
