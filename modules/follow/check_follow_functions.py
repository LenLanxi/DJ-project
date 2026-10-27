"""Record functional directions from generated ArUco pixels to MockPort.

No camera, GUI, SDK connection or physical motion. This complements the SDK
entry tests in tests/test_vertical_sdk.py; it is NOT a flight certificate.
"""
import argparse
import csv
import json
from pathlib import Path
import sys
from unittest.mock import patch

import cv2
from camera_pid_bench import make_service
from r3_follow import __version__
from r3_follow.vision import synthetic_frame


def run(output):
    if sys.version_info[:2] != (3,11):
        raise RuntimeError('Use Python 3.11')
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    cv2.setNumThreads(1)
    rows=[];checks=[]
    class Clock:
        now=100.
        def mono(self):return self.now
        def wall(self):return 1800000000.+self.now
    class Reader:
        latest=None
        def snapshot(self):return self.latest,''
    for marker_id in (0,1,2):
        for mode in ('yaw','lateral','hybrid'):
            clock,reader=Clock(),Reader()
            port,service=make_service(reader,marker_id,steering_mode=mode,vertical_follow=True,
                                     monotonic=clock.mono,wall=clock.wall,
                                     log_directory=output/'logs')
            def frame(scene,**kwargs):
                clock.now+=.1
                reader.latest=(max(1,service.last_received_frame_id+1),clock.now,
                    synthetic_frame(marker_id=marker_id,**kwargs))
                service.capture_once();service.control_once()
                obs=service.controller.observation
                rows.append(dict(target_id=marker_id,mode=mode,scene=scene,
                    frame_id=service.last_received_frame_id,time=round(clock.now,3),
                    found=bool(obs and obs.found),center_x=obs.center_x if obs else None,
                    center_y=obs.center_y if obs else None,size_ratio=obs.size_ratio if obs else None,
                    state=str(service.controller.state),left_right=port.velocity[0],
                    forward_back=port.velocity[1],up_down=port.velocity[2],yaw=port.velocity[3]))
            try:
                frame('reference')
                assert service.calibrate_reference().ok
                assert service.start_follow().ok
                cases=(('前进',dict(size=.18),lambda r:r[1]>0),
                       ('后退',dict(size=.32),lambda r:r[1]<0),
                       ('上升',dict(y=.3),lambda r:r[2]>0),
                       ('下降',dict(y=.7),lambda r:r[2]<0),
                       ('向右对准',dict(x=.7),lambda r:r[0]+r[3]>0),
                       ('向左对准',dict(x=.3),lambda r:r[0]+r[3]<0),
                       ('同时前进上升对准',dict(size=.18,y=.3,x=.7),
                            lambda r:r[1]>0 and r[2]>0 and r[0]+r[3]>0),
                       ('保持位置',dict(),lambda r:not any(r)))
                for name,options,expect in cases:
                    # No axis is allowed to retain previous movement when the
                    # current observation requests zero. Allow depth filter to settle.
                    for _ in range(10):frame(name,**options)
                    assert expect(port.velocity),(marker_id,mode,name,port.velocity)
                    checks.append(dict(target_id=marker_id,mode=mode,case=name,
                                       channels=port.velocity,passed=True))
                frame('遮挡',visible=False)
                assert port.velocity==(0,0,0,0)
                checks.append(dict(target_id=marker_id,mode=mode,case='遮挡归零',channels=port.velocity,passed=True))
                for _ in range(8):frame('恢复',y=.7)
                assert service.is_following() and port.velocity[2]<0
                checks.append(dict(target_id=marker_id,mode=mode,case='恢复后按新位置下降',channels=port.velocity,passed=True))
                service.stop_follow()
                for _ in range(4):frame('停止后仍看见目标',y=.3)
                assert port.velocity==(0,0,0,0) and not service.is_following()
                checks.append(dict(target_id=marker_id,mode=mode,case='停止后不自行启动',channels=port.velocity,passed=True))
            finally:service.close()
    with (output/'steps.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    result=dict(version=__version__,python=sys.version,checks=checks,steps=len(rows),
        passed=all(c['passed'] for c in checks),channels_order=['left_right','forward_back','up_down','yaw'],
        unit='RC channel value, not cm/s or m/s',camera_opened=False,desktop_opened=False,
        hardware_connected=False,input='generated ArUco images',output='MockPort requests',
        real_flight_verified=False)
    (output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:result[k] for k in ('version','steps','passed','real_flight_verified')},ensure_ascii=False))
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    with patch('socket.socket',side_effect=AssertionError('Network forbidden')), \
         patch('cv2.VideoCapture',side_effect=AssertionError('Camera forbidden')), \
         patch('tkinter.Tk',side_effect=AssertionError('Desktop forbidden')):
        run(args.output)
