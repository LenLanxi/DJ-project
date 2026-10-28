"""Local readiness checks only. Flight readiness is checked by the connected runtime."""
import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

def inspect(config_path):
    from follow.r3_follow.config import load_config
    checks={}
    try:
        config=load_config(config_path)
        checks['follow_config']=config.control_profile=='openpose_pid'
    except Exception:
        checks['follow_config']=False
    checks['python']=sys.version_info[:2]==(3,11)
    checks['dependencies']=all(importlib.util.find_spec(n) is not None for n in ('cv2','numpy','av','djitellopy','websockets'))
    checks['rtc_credentials_present']=all(os.getenv(n) for n in ('RTC_APP_ID','RTC_APP_KEY','VOLCENGINE_ACCESS_KEY_ID','VOLCENGINE_SECRET_ACCESS_KEY','VOLC_S2S_APP_ID','VOLC_S2S_TOKEN','RTC_VLM_MODEL'))
    checks['node']=shutil.which('node') is not None
    checks['rtc_dependencies']=(ROOT/'web/node_modules/@volcengine/rtc').is_dir()
    checks['host_vision_configured']=bool((os.getenv('RTC_VLM_API_KEY') and os.getenv('RTC_VLM_MODEL')) or all(os.getenv(n) for n in ('AI_CHAT_URL','AI_API_KEY','AI_MODEL')))
    checks['docker_cli']=shutil.which('docker') is not None
    try:
        docker=subprocess.run(['docker','image','inspect','companion-sandbox:0.4'],capture_output=True,timeout=10)
        checks['sandbox_image']=docker.returncode==0
    except (OSError,subprocess.TimeoutExpired):
        checks['sandbox_image']=False
    return {'ok':all(checks.values()),'scope':'local_configuration_only','checks':checks,
            'drone_verified':False,'headset_verified':False,'cloud_verified':False}

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--follow-config',required=True)
    args=parser.parse_args(argv)
    result=inspect(args.follow_config)
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['ok'] else 2

if __name__=='__main__':raise SystemExit(main())
