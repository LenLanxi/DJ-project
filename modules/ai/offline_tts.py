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
import threading

_PICK_ZH = ("$zh = $s.GetInstalledVoices() | Where-Object { "
            "$_.VoiceInfo.Culture.Name.StartsWith('zh') } | Select-Object -First 1;"
            "if ($zh) { $s.SelectVoice($zh.VoiceInfo.Name) };")

_DECODE = ('$b64 = [Console]::In.ReadToEnd();'
           '$text = [System.Text.Encoding]::UTF8.GetString('
           '[System.Convert]::FromBase64String($b64.Trim()));')

_SCRIPT = ('Add-Type -AssemblyName System.Speech;'
           '$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;'
           + _PICK_ZH + _DECODE +
           '$s.Rate = %d;'
           '$s.Speak($text)')

# 常驻播报进程：每句话都重开 PowerShell 要白等 4~5 秒（Add-Type 加载 System.Speech），
# 语音助手一轮一句根本受不了。这里起一个长生命周期进程，用 base64 行协议收发。
_WORKER_SCRIPT = ('Add-Type -AssemblyName System.Speech;'
                  '$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;'
                  + _PICK_ZH +
                  '$s.Rate = %d;'
                  'while ($true) {'
                  '  $line = [Console]::In.ReadLine();'
                  '  if ($null -eq $line) { break }'
                  '  $t = $line.Trim();'
                  '  if ($t -eq "QUIT") { break }'
                  '  if ($t.Length -gt 0) {'
                  '    $text = [System.Text.Encoding]::UTF8.GetString('
                  '      [System.Convert]::FromBase64String($t));'
                  '    $s.Speak($text)'
                  '  }'
                  '  [Console]::Out.WriteLine("DONE"); [Console]::Out.Flush()'
                  '}')

_WORKER = {'proc': None}


def _register_cleanup():
    import atexit
    atexit.register(close)

_FILE_SCRIPT = ('Add-Type -AssemblyName System.Speech;'
                '$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;'
                + _PICK_ZH +
                '$path = [Console]::In.ReadLine();' + _DECODE +
                '$s.Rate = 0;'
                '$s.SetOutputToWaveFile($path); $s.Speak($text); $s.Dispose()')

def available():
    return os.name == 'nt'


def speech_rate():
    """SAPI 语速 -10~10。默认 3：中文状态播报在 1 的时候慢得让人等。"""
    try:
        return max(-10, min(10, int(os.getenv('AI_SAPI_RATE', '3'))))
    except (TypeError, ValueError):
        return 3


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


def _start_worker():
    proc = subprocess.Popen(
        ['powershell', '-NoProfile', '-Command', _WORKER_SCRIPT % speech_rate()],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding='utf-8', errors='replace', bufsize=1)
    # 预热：先空跑一句，确认 SAPI 已加载完，后面第一句才不用等
    proc.stdin.write('\n')
    proc.stdin.flush()
    if proc.stdout.readline().strip() != 'DONE':
        proc.kill()
        raise RuntimeError('SAPI 常驻进程预热失败')
    return proc


def _speak_via_worker(text, timeout):
    proc = _WORKER.get('proc')
    if proc is None or proc.poll() is not None:
        proc = _start_worker()
        _WORKER['proc'] = proc
    proc.stdin.write(_payload(text) + '\n')
    proc.stdin.flush()
    watchdog = threading.Timer(timeout, proc.kill)
    watchdog.daemon = True
    watchdog.start()
    try:
        line = proc.stdout.readline()
    finally:
        watchdog.cancel()
    if line.strip() != 'DONE':
        raise RuntimeError('SAPI 常驻进程无响应')


def close():
    """退出时收掉常驻进程，避免留一个没有父进程的 PowerShell。"""
    proc = _WORKER.get('proc')
    _WORKER['proc'] = None
    if proc is None:
        return
    try:
        proc.stdin.write('QUIT\n')
        proc.stdin.flush()
        proc.wait(timeout=3)
    except Exception:                              # noqa: BLE001 - 清理阶段不抛
        try:
            proc.kill()
        except Exception:
            pass


def speak(text, timeout=120):
    """把文本念出来；失败抛 RuntimeError（调用方决定是否静默）。"""
    text = (text or '').strip()
    if not text:
        raise ValueError('没有可播报的文本')
    text = text[:2000]
    if available():
        try:
            return _speak_via_worker(text, timeout)
        except Exception:                          # noqa: BLE001 - 常驻进程挂了就退回一次性
            close()
    return _run(_SCRIPT % speech_rate(), _payload(text), timeout)


def synthesize_to_wav(text, path, timeout=120):
    """把一句话合成到 WAV 文件（用于离线自检语音链路，不发声）。"""
    if not available():
        raise RuntimeError('当前系统不支持 Windows SAPI')
    _run(_FILE_SCRIPT, '%s\n%s' % (path, _payload(text)), timeout)
    if not os.path.exists(path) or os.path.getsize(path) < 1024:
        raise RuntimeError('SAPI 未生成有效的 WAV 文件')
    return path


_register_cleanup()
