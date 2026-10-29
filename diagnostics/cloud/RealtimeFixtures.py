"""Reproducible synthetic media, never opens a microphone, speaker or camera."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import wave

import cv2
import numpy as np

FOLDER = Path(__file__).resolve().parents[2] / 'ScenarioAcceptance' / 'Evidence' / 'RealtimeComparison'


def fixture(name, folder=FOLDER):
    metadata = json.loads((folder / 'Fixtures.json').read_text(encoding='utf-8-sig'))['audio'][name]
    pcm = (folder / (name + '.pcm')).read_bytes()
    wav_path = folder / (name + '.wav')
    with wave.open(str(wav_path), 'rb') as stream:
        if (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) != (16000, 1, 2):
            raise ValueError('shared_fixture_format_mismatch')
        if stream.readframes(stream.getnframes()) != pcm:
            raise ValueError('shared_pcm_wav_mismatch')
    if (len(pcm) != metadata['samples'] * 2
            or metadata['sha256'] not in (hashlib.sha256(pcm).hexdigest(), hashlib.sha256(wav_path.read_bytes()).hexdigest())):
        raise ValueError('shared_fixture_hash_mismatch')
    return pcm, metadata


TEXTS = {
    'Status': '请查询模拟无人机的电量。不要操作飞机。',
    'Vision': '画面里的帐篷是什么颜色？只回答颜色。',
    'Speech': '请只回答一句话：测试收到。',
}


def synthesize(text):
    """Generate raw 16k mono PCM in memory; never opens a playback device."""
    command='''Add-Type -AssemblyName System.Speech
$synth = [System.Speech.Synthesis.SpeechSynthesizer]::new()
$stream = [System.IO.MemoryStream]::new()
try {
 $synth.SelectVoice('Microsoft Huihui Desktop')
 $format = [System.Speech.AudioFormat.SpeechAudioFormatInfo]::new(16000,[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,[System.Speech.AudioFormat.AudioChannel]::Mono)
 $synth.SetOutputToAudioStream($stream,$format)
 $synth.Speak($env:DRONE_QUERY_TEXT)
 $synth.SetOutputToNull()
 [Convert]::ToBase64String($stream.ToArray())
} finally { $synth.Dispose(); $stream.Dispose() }
'''
    result=subprocess.run(['pwsh','-NoProfile','-NonInteractive','-Command',command],
        env=dict(os.environ,DRONE_QUERY_TEXT=text),capture_output=True,check=True,timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    pcm=base64.b64decode(result.stdout.strip(),validate=True)
    if not pcm or len(pcm)%2: raise ValueError('invalid_synthetic_pcm')
    return pcm


def generate(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, COMPANION_FIXTURE_OUTPUT=str(output),
               COMPANION_FIXTURE_TEXT=json.dumps(TEXTS, ensure_ascii=False))
    command = '''Add-Type -AssemblyName System.Speech
$synth = [System.Speech.Synthesis.SpeechSynthesizer]::new()
try {
 $synth.SelectVoice('Microsoft Huihui Desktop')
 $format = [System.Speech.AudioFormat.SpeechAudioFormatInfo]::new(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
 $texts = $env:COMPANION_FIXTURE_TEXT | ConvertFrom-Json
 foreach ($entry in $texts.PSObject.Properties) {
  $target = Join-Path $env:COMPANION_FIXTURE_OUTPUT ($entry.Name + '.wav')
  $synth.SetOutputToWaveFile($target, $format)
  $synth.Speak([string]$entry.Value)
  $synth.SetOutputToNull()
 }
} finally { $synth.Dispose() }
'''
    subprocess.run(['pwsh', '-NoProfile', '-NonInteractive', '-Command', command],
                   env=env, check=True, capture_output=True,
                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    metadata = {'source': 'Microsoft Huihui Desktop offline synthetic TTS and geometric images',
                'no_physical_devices': True, 'audio': {}, 'images': {}}
    for name, text in TEXTS.items():
        path = output / (name+'.wav')
        with wave.open(str(path), 'rb') as stream:
            pcm = stream.readframes(stream.getnframes())
        samples = np.frombuffer(pcm, dtype='<i2')
        # Fixed threshold used for every provider; retain padding but do not count
        # it as speech. Metadata exposes the threshold and exact sample index.
        active = np.flatnonzero(np.abs(samples.astype(np.int32)) > 300)
        end = int(active[-1]+1) if active.size else 0
        (output/(name+'.pcm')).write_bytes(pcm)
        metadata['audio'][name] = dict(text=text, sample_rate=16000, channels=1,
            bits=16, samples=len(samples), speech_end_samples=end,
            speech_end_seconds=end/16000, seconds=len(samples)/16000,
            speech_end_threshold=300, sha256=hashlib.sha256(pcm).hexdigest())
    for name, color, expected in [('RedTent', (40,40,220), '红'), ('BlueTent', (220,70,30), '蓝')]:
        image = np.full((360,640,3), (225,225,215), np.uint8)
        image[260:] = (110,170,120)
        cv2.fillPoly(image, [np.array([[190,265],[350,80],[510,265]], np.int32)], color)
        cv2.fillPoly(image, [np.array([[320,265],[350,160],[385,265]], np.int32)], (35,35,35))
        cv2.imwrite(str(output/(name+'.jpg')), image, [cv2.IMWRITE_JPEG_QUALITY, 85])
        (output/(name+'.i420')).write_bytes(cv2.cvtColor(image,cv2.COLOR_BGR2YUV_I420).tobytes())
        metadata['images'][name] = dict(expected_color=expected,width=640,height=360,
            source='synthetic_triangle_tent_not_a_real_camera',
            sha256=hashlib.sha256((output/(name+'.jpg')).read_bytes()).hexdigest())
    (output/'Fixtures.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    return metadata


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    print(json.dumps(generate(args.output), ensure_ascii=False))
