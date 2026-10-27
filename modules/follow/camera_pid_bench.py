"""Local webcam -> delivered R3 service -> MockPort; never imports a drone SDK."""
from pathlib import Path
import argparse
import hashlib
import json
import sys
import time
import threading
import uuid
from dataclasses import replace

HERE = Path(__file__).resolve().parent
PACKAGE = HERE.parents[1]
SOURCE = HERE
EXPECTED_VERSION = 'r3-motion-follow1'
EXPECTED_PROFILE = 'openpose_pid_marker_v2'


def verify_source():
    manifest=json.loads((PACKAGE/'文件校验_SHA256.json').read_text(encoding='utf-8'))
    checked=0
    for name,digest in manifest.items():
        if name.startswith('modules/follow/') and name.endswith('.py'):
            actual=hashlib.sha256((PACKAGE/name).read_bytes()).hexdigest()
            if actual!=digest:raise RuntimeError('交付源码校验不一致：'+name)
            checked+=1
    if checked<10:raise RuntimeError('交付源码清单不完整')
    return checked


sys.path.insert(0,str(SOURCE))
import cv2
import numpy as np
from r3_follow import __version__
from r3_follow.config import FollowConfig, load_config
from r3_follow.service import FollowService
from r3_follow.mock_port import MockPort
from r3_follow.service_ports import FrameSnapshot
from r3_follow.vision import synthetic_frame
from r3_follow.presentation import motion_text, distance_text, depth_status_text, braking_text
from r3_follow.physical_metrics import create_distance_calibration, estimate_distance


class WebcamMockPort(MockPort):
    """Fresh camera pixels, simulated motion only; capture time stays immutable."""
    def __init__(self, reader, monotonic=time.monotonic, wall=time.time):
        super().__init__(monotonic)
        self.reader=reader
        self.wall=wall
        self.epoch_offset=wall()-monotonic()
        self.frames={}

    def epoch_time(self):
        # Use the frame producer's clock mapping for age checks. Independent
        # Windows wall/monotonic tick phases can otherwise make fresh frames
        # appear a few milliseconds in the future.
        return self.epoch_offset+self.clock()

    def get_frame(self):
        latest,_=self.reader.snapshot()
        with self.lock:
            if latest is None:return self.snapshot
            frame_id,acquired,frame=latest
            if frame_id>self.snapshot.frame_id:
                self.snapshot=FrameSnapshot(frame,frame_id,self.epoch_offset+acquired,False)
                self.frames[frame_id]=frame
                while len(self.frames)>8:self.frames.pop(next(iter(self.frames)))
            packet=self.snapshot
            if self.clock()-(packet.timestamp-self.epoch_offset)>.5:
                return replace(packet,frame=None,stale=True)
            return packet

    def display_frame(self, observation):
        # A processed box belongs only to that processed image, never a newer one.
        with self.lock:
            matched=self.frames.get(observation.frame_id) if observation else None
        if matched is not None:
            return matched.copy(),True
        latest,_=self.reader.snapshot()
        return (latest[2].copy(),False) if latest else (None,False)


def make_service(reader, target_id=0, log_directory=False,
                 monotonic=time.monotonic, wall=time.time, *, steering_mode='yaw',
                 vertical_follow=False, distance_mode="relative_size", config=None):
    if __version__!=EXPECTED_VERSION:raise RuntimeError('后台版本不匹配')
    port=WebcamMockPort(reader,monotonic,wall)
    config=load_config(config) if config is not None else FollowConfig(f'camera-bench-ID{target_id}',target_id,
                        'DICT_4X4_50','openpose_pid',steering_mode=steering_mode,
                        vertical_follow=vertical_follow, distance_mode=distance_mode)
    service=FollowService(port,**config.to_dict(),recovery_window=3,
        log_directory=log_directory,monotonic=monotonic,wall_clock=port.epoch_time)
    status=service.get_follow_status()
    if (status['control_profile']!=EXPECTED_PROFILE
            or status['configured_control_profile']!='openpose_pid'):
        service.close();raise RuntimeError('未启用本次适配的开源PID，停止测试')
    service._record('camera_bench_context',actual_frame_source='local_webcam',
        device_port='MockPort_only_no_SDK',source_version=__version__,
        control_profile=EXPECTED_PROFILE,steering_mode=config.steering_mode,source_path=str(SOURCE),
        vertical_follow=config.vertical_follow, effective_config=config.to_dict(),
        recording_images=False,recording_audio=False,
        timestamp_origin='host_camera_read_complete_not_hardware_exposure')
    return port,service


PHASES=[('正对固定','保持标记正对镜头、不移动',5),
        ('缓慢左右','缓慢左右移动，始终保留完整标记',5),
        ('缓慢拿远','保持正对，慢慢拿远一些',5),
        ('缓慢拿近','保持正对，慢慢拿回参考位置',5),
        ('侧转后转正','原地缓慢侧转，再转回正对',5),
        ('短遮挡恢复','遮住约1秒后立即露出，保持正对至结束',5)]

VERTICAL_PHASES=PHASES[:4]+[
        ('缓慢抬高','保持距离不变，把标记向画面上方移动；应显示飞机上升正值',5),
        ('缓慢放低','保持距离不变，把标记向画面下方移动；应显示飞机下降负值',5)]+PHASES[4:]


def displayed_distance(observation, calibration, status, *, now, camera_key, marker_key):
    """A visible but unconfirmed target must not become a confident metre label."""
    if calibration is not None:
        if (not status.get('observation_fresh') or status.get('recovering')
                or not status.get('distance_guard', {}).get('forward_allowed')):
            return {'status':'unavailable','distance_m':None,'unit':'m',
                    'reason':'目标尚未稳定确认或画面过期','is_estimate':True}
    return estimate_distance(observation,calibration,now=now,camera_key=camera_key,
        marker_key=marker_key,reference_size_ratio=status.get('reference_size'))


def phase_at(elapsed, vertical_follow=False):
    if elapsed<5:return -1,'准备','保持参考位置，等待倒计时',5-elapsed
    remaining=elapsed-5
    phases=VERTICAL_PHASES if vertical_follow else PHASES
    for index,(name,prompt,duration) in enumerate(phases):
        if remaining<duration:return index,name,prompt,duration-remaining
        remaining-=duration
    return len(phases),'完成','本轮已停止，日志已记录',0


def test_start_decision(status):
    if status['following']:
        return True, ''
    if status['recovering']:
        return False, '正在归零等待目标确认；请等状态显示“跟随已开启”后再开始短测。'
    return False, '短测没有开始：'+status['status_message']


def save_diagnostic_frame(reader, observation=None, directory=None, wall=time.time):
    """Save one explicitly requested raw snapshot; never draw another frame's result."""
    latest,error=reader.snapshot()
    if latest is None or latest[2] is None:
        return dict(ok=False,message='没有可保存的相机画面。'+(error or '请等待相机返回画面。'))
    frame_id,acquired,frame=latest
    try:
        ok,encoded=cv2.imencode('.png',frame.copy())
        if not ok:
            return dict(ok=False,message=f'第{frame_id}帧 PNG 编码失败，未保存文件。')
    except (cv2.error,ValueError,TypeError,AttributeError):
        return dict(ok=False,message=f'第{frame_id}帧 PNG 编码失败，未保存文件。')
    saved_at=wall()
    directory=Path(directory) if directory is not None else HERE/'logs'/'diagnostic_frames'
    stem=f'frame_{frame_id}_{uuid.uuid4().hex}'
    image_path,metadata_path=directory/(stem+'.png'),directory/(stem+'.json')
    detection_id=observation.frame_id if observation is not None else None
    record=dict(frame_id=frame_id,capture_monotonic=acquired,saved_at=saved_at,
        image_file=image_path.name,image_content='raw_reader_pixels_no_overlay',
        latest_detection_frame_id=detection_id,
        latest_detection_reason=observation.reason if observation is not None else None,
        latest_detection_found=observation.found if observation is not None else None,
        detection_matches_saved_frame=detection_id==frame_id)
    try:
        directory.mkdir(parents=True,exist_ok=True)
        image_path.write_bytes(encoded.tobytes())
        metadata_path.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    except OSError as error:
        for path in (image_path,metadata_path):
            try:path.unlink(missing_ok=True)
            except OSError:pass
        return dict(ok=False,message=f'第{frame_id}帧保存失败：{error}')
    detection_note=('尚无识别结果。' if detection_id is None else
        f'最近识别为第{detection_id}帧'+('，与保存帧一致。' if detection_id==frame_id else '，不是这张图的识别结果。'))
    return dict(ok=True,frame_id=frame_id,image_path=str(image_path),metadata_path=str(metadata_path),
        message=f'已保存第{frame_id}帧原始画面（含背景，无识别框）。{detection_note} 文件：{image_path.name}')


def gui(camera, target_id, steering_mode='yaw', vertical_follow=False, config=None):
    verify_source()  # Also guard callers that import gui() instead of using CLI.
    if config is not None:
        config=load_config(config)
        if config.distance_mode == 'pose' and config.camera_id != f'local-webcam-{camera}':
            raise ValueError('Use a webcam-specific calibration; aircraft pose configuration must not open a webcam')
        target_id,steering_mode,vertical_follow=config.target_id,config.steering_mode,config.vertical_follow
    import tkinter as tk
    from tkinter import ttk
    from follow_demo import CameraReader  # only reuse camera reader, not old GUI/controller
    # Metadata writes must not race the session logger's background mkdir.
    (HERE/'logs').mkdir(parents=True,exist_ok=True)
    root=tk.Tk()
    root.title('R3 跟随响应测试 · 仅模拟运动')
    root.geometry(f'{min(1040,root.winfo_screenwidth()-70)}x{min(740,root.winfo_screenheight()-100)}')
    root.minsize(700,480)
    reader=CameraReader(camera)
    port,service=make_service(reader,target_id,HERE/'logs',steering_mode=steering_mode,
                              vertical_follow=vertical_follow,config=config)
    phases=VERTICAL_PHASES if vertical_follow else PHASES
    test_seconds=sum(p[2] for p in phases)
    service.start_background()
    service._record('bench_open',camera_index=camera)
    log_path=service.session_log.path
    info_path=log_path.with_suffix('.bench.json')
    state={'test_started':None,'phase':None,'finished':False,'aborted':False,'closing':False,
           'distance_calibration':None,'last_metric_frame':None}
    camera_key=f'local-camera-{camera}-session-{service.service_session_id}'
    metadata={'version':__version__,'profile':EXPECTED_PROFILE,'target_id':target_id,
        'source':'local_webcam','motion':'MockPort only; no aircraft','steering_mode':steering_mode,
        'camera_key':camera_key,'distance_calibration':None,'vertical_follow':vertical_follow,
        'session_log':str(log_path),'source_path':str(SOURCE),'phases':[],
        'images_saved':False,'hardware_connected':False,
        'pid_settings':service.controller._openpose.status(),
        'timestamp_origin':'host_camera_read_complete_not_hardware_exposure'}
    def save_metadata():
        metadata.update(finished=state['finished'],aborted=state['aborted'],
                        camera_metadata=reader.metadata,log_status=service.session_log.status())
        info_path.write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
    save_metadata()
    bar=ttk.Frame(root,padding=8);bar.pack(side='bottom',fill='x')
    note=tk.StringVar(value='先让标记正对镜头，看到绿框后记录参考大小，再开始。')
    phase_text=tk.StringVar(value=f'本轮记录：准备5秒＋测试{test_seconds}秒；程序会提示动作。')
    ttk.Label(root,text='本机摄像头 → 开源PID计算 → 模拟请求（未连接无人机）',
              font=('Microsoft YaHei',14,'bold')).pack(pady=(8,2))
    ttk.Label(root,text=f'{EXPECTED_PROFILE}  |  {__version__}  |  DICT_4X4_50 ID{target_id}',
              foreground='#075f51').pack()
    ttk.Label(root,text=f'距离计算：{service.config.distance_mode}；限幅：{service.rc_caps} RC；高度为模拟值').pack()
    ttk.Label(root,text='自动上下：'+('已开启（上限20 RC，保持标记在画面中部）' if vertical_follow
                                  else '未开启（旧配置兼容模式，上下请求为0）')).pack()
    ttk.Label(root,textvariable=phase_text,wraplength=950,font=('Microsoft YaHei',12,'bold')).pack(pady=6)
    body=ttk.Frame(root);body.pack(fill='both',expand=True,padx=8)
    picture=ttk.Label(body,anchor='center');picture.pack(side='left',fill='both',expand=True)
    # The essential controls stay in the bottom bar; only details scroll.
    panel_host=ttk.Frame(body,width=300);panel_host.pack(side='right',fill='y')
    canvas=tk.Canvas(panel_host,width=300,highlightthickness=0)
    scroll=ttk.Scrollbar(panel_host,orient='vertical',command=canvas.yview)
    scroll.pack(side='right',fill='y');canvas.pack(side='left',fill='both',expand=True)
    canvas.configure(yscrollcommand=scroll.set)
    panel=ttk.Frame(canvas,padding=8)
    window=canvas.create_window((0,0),window=panel,anchor='nw')
    panel.bind('<Configure>',lambda event:canvas.configure(scrollregion=canvas.bbox('all')))
    canvas.bind('<Configure>',lambda event:canvas.itemconfigure(window,width=event.width))
    message,values,details=(tk.StringVar() for _ in range(3))
    for var in (message,values,details):
        ttk.Label(panel,textvariable=var,wraplength=285,justify='left').pack(anchor='w',pady=8)
    physical=tk.StringVar(value=distance_text({}))
    ttk.Label(panel,textvariable=physical,wraplength=285).pack(anchor='w',pady=8)
    ttk.Separator(panel).pack(fill='x',pady=6)
    ttk.Label(panel,text='可选距离标定；不填也能测试。\n量摄像头到标记所在平面的距离。',wraplength=285).pack(anchor='w')
    measured=tk.StringVar(value='')
    marker_key=tk.StringVar(value=f'本轮实体标记-ID{target_id}')
    confirmed=tk.BooleanVar(value=False)
    ttk.Label(panel,text='已量参考距离（米）：').pack(anchor='w')
    ttk.Entry(panel,textvariable=measured,width=20).pack(anchor='w')
    ttk.Label(panel,text='实体标记名称（换大小后需重标定）：',wraplength=285).pack(anchor='w')
    ttk.Entry(panel,textvariable=marker_key,width=25).pack(anchor='w')
    ttk.Checkbutton(panel,text='已实际测量，标记静止且正对',variable=confirmed).pack(anchor='w')
    ttk.Label(panel,text='填好后点“1 记录参考大小”。\n米数只作条件估计，不改变PID；\nRC不会被当成米/秒。',wraplength=285).pack(anchor='w',pady=5)
    ttk.Label(bar,textvariable=note,wraplength=950).pack(anchor='w')
    buttons=ttk.Frame(bar);buttons.pack(fill='x',pady=4)
    def action(name):
        if state['test_started'] is not None and name!='stop':
            note.set('分段测试期间请按提示操作标记；要提前结束请点停止。');return
        if name=='stop' and state['test_started'] is not None:
            state['aborted']=True;state['test_started']=None
            service._record('bench_test_aborted')
            phase_text.set('测试提前结束；日志保留。')
        physical_record=None
        if name=='calibrate':
            # Bind the measured reference to exactly the frame being calibrated.
            with service.lock:
                if measured.get().strip():
                    try:distance_m=float(measured.get())
                    except ValueError:
                        note.set('参考距离请填米数，例如1.5；未测量就留空。');return
                    physical_record=create_distance_calibration(service.controller.observation,distance_m,
                        now=service.clock(),camera_key=camera_key,marker_key=marker_key.get().strip(),
                        front_facing_confirmed=confirmed.get())
                    if physical_record['status']!='recorded':
                        note.set('米数未记录：'+physical_record['reason']);return
                result=service.calibrate_reference()
                if result.ok:
                    state['distance_calibration']=(physical_record['calibration'] if physical_record else None)
                    metadata['distance_calibration']=state['distance_calibration']
                    service._record('bench_distance_calibration',calibration=state['distance_calibration'],
                                    source='manual_distance_entry',used_for_control=False)
        else:result={'start':service.start_follow,
                'pause':service.pause_follow,'resume':service.resume_follow,
                'stop':service.stop_follow}[name]()
        success={'calibrate':'参考大小已记录。现在点“2 开始”。','start':f'跟随计算已开启，可点“3 开始{test_seconds}秒分段测试”。',
                 'pause':'已暂停。','resume':'已恢复。','stop':'已停止；新绿框不会自动重新开始。'}
        failed={'new_valid_frame_required':'还没有可靠的新画面，请等绿框出现。',
                'Fresh valid target required':'目标没有稳定识别，请正对镜头后再试。',
                'A fresh valid target is required':'目标没有稳定识别，请正对镜头后再试。',
                'Front-facing marker geometry required for reference':'请把标记摆正，再记录参考大小。',
                'Calibrate reference size first':'请先点“1 记录参考大小”。'}
        note.set(success[name] if result.ok else '操作未成功：'+failed.get(result.reason,result.reason))
        service._record('bench_button',name=name,status=result.status,reason=result.reason)
        save_metadata()
    for caption,name in [('1 记录参考大小','calibrate'),('2 开始','start'),('暂停','pause'),('恢复','resume'),('停止','stop')]:
        ttk.Button(buttons,text=caption,command=lambda n=name:action(n)).pack(side='left',expand=True,fill='x',padx=3)
    def begin_test():
        if state['test_started'] is not None:return
        status=service.get_follow_status()
        allowed,why=test_start_decision(status)
        if not allowed:
            note.set(why)
            service._record('bench_test_rejected',reason=why,state=status['state'],
                            last_halt_reason=status['last_halt_reason'])
            save_metadata();return
        state.update(test_started=time.monotonic(),phase=None,finished=False,aborted=False)
        metadata['test_started_monotonic']=state['test_started'];metadata['phases']=[]
        service._record('bench_test_start',phases=phases,preparation_seconds=5,
                        vertical_follow=vertical_follow)
        save_metadata()
    ttk.Button(bar,text=f'3 开始{test_seconds}秒分段测试（另有5秒准备）',command=begin_test).pack(fill='x',pady=3)
    def save_current_frame():
        with service.lock:observation=service.controller.observation
        result=save_diagnostic_frame(reader,observation)
        note.set(result['message'])
        if result['ok']:
            metadata['images_saved']=True
            metadata.setdefault('diagnostic_frames',[]).append({key:result[key] for key in
                ('frame_id','image_path','metadata_path')})
            service._record('bench_diagnostic_frame_saved',frame_id=result['frame_id'],
                image_path=result['image_path'],metadata_path=result['metadata_path'])
            save_metadata()
    ttk.Button(bar,text='保存1张当前画面（含背景）',command=save_current_frame).pack(fill='x',pady=3)
    ttk.Label(bar,text='日志自动保存：'+str(log_path),wraplength=960,foreground='#555').pack(anchor='w')
    def update():
        if state['closing']:return
        status=service.get_follow_status()
        latest,error=reader.snapshot()
        with service.lock:obs=service.controller.observation
        frame,matched=port.display_frame(obs if status['observation_fresh'] else None)
        if frame is not None:
            if matched and obs.found and status['observation_fresh']:
                cv2.polylines(frame,[np.array(obs.corners,dtype=np.int32)],True,(0,210,0),2)
                cv2.circle(frame,(round(obs.center_x),round(obs.center_y)),4,(0,0,255),-1)
            width,height=frame.shape[1],frame.shape[0]
            factor=min(max(240,body.winfo_width()-310)/width,max(150,body.winfo_height()-10)/height,1)
            frame=cv2.resize(frame,(max(1,int(width*factor)),max(1,int(height*factor))))
            ok,encoded=cv2.imencode('.png',frame)
            if ok:
                photo=tk.PhotoImage(data=encoded.tobytes());picture.configure(image=photo);picture.image=photo
        elif error:picture.configure(text=error)
        message.set(status['status_message'])
        channels=status['requested_channels']
        values.set(motion_text(channels,steering_mode))
        vision=status['last_vision'];age=status['observation_age_ms']
        details.set(f'帧号：{status["frame_id"]}\n识别耗时：{vision.get("detection_ms",0):.1f} ms\n'
            f'识别结果帧龄：{age:.1f} ms\n' if age is not None else '等待相机新画面\n')
        details.set(details.get()+(depth_status_text(status['distance_guard'])+'\n')+
            braking_text(status['depth_brake'])+'\n'+
            ('相机：'+error if error else '')+
            (f'\n日志错误：{service.session_log.error}' if service.session_log.error else ''))
        distance_result=displayed_distance(obs,state['distance_calibration'],status,now=service.clock(),
            camera_key=camera_key,marker_key=marker_key.get().strip())
        physical.set(distance_text(distance_result))
        if obs is not None and state['last_metric_frame']!=obs.frame_id:
            state['last_metric_frame']=obs.frame_id
            if state['distance_calibration'] is not None:
                service._record('bench_distance_estimate',frame_id=obs.frame_id,
                    estimate=distance_result,used_for_control=False)
        if state['test_started'] is not None and not status['active']:
            state.update(test_started=None,aborted=True,finished=False)
            service._record('bench_test_aborted',reason='following_became_inactive',
                state=status['state'],last_halt_reason=status['last_halt_reason'])
            note.set('短测中断：'+status['status_message'])
            phase_text.set('本轮已中断，日志已保存；本轮不会标记为完成。')
            save_metadata()
        if state['test_started'] is not None:
            elapsed=time.monotonic()-state['test_started'];index,name,prompt,left=phase_at(elapsed,vertical_follow)
            if index!=state['phase']:
                state['phase']=index
                row={'index':index,'name':name,'phase_at_monotonic':time.monotonic(),'elapsed':elapsed}
                metadata['phases'].append(row);service._record('bench_phase',**row)
            phase_text.set(f'{name}：{prompt}（约{max(0,int(left)+1)}秒）')
            if index==len(phases):
                service.stop_follow()
                service._record('bench_test_completed')
                state.update(test_started=None,finished=True)
                note.set('本轮完成并已停止。可以关闭窗口，再回复“测完了”。')
                phase_text.set(f'{test_seconds}秒分段测试完成，已停止。');save_metadata()
        root.after(100,update)
    def close():
        state['closing']=True
        if state['test_started'] is not None:
            state['aborted']=True;service._record('bench_test_aborted',reason='window_closed')
        service.close();reader.done.set();reader.thread.join(timeout=.3)
        save_metadata();root.destroy()
    root.protocol('WM_DELETE_WINDOW',close)
    try:
        update();root.mainloop()
    finally:
        if not state['closing']:close()


def self_check():
    from unittest.mock import patch
    class Clock:
        now=100.
        def mono(self):return self.now
        def wall(self):return 1800000000.+self.now
    class Reader:
        latest=None
        def snapshot(self):return self.latest,''
    clock,reader=Clock(),Reader()
    port,service=make_service(reader,monotonic=clock.mono,wall=clock.wall)
    checks={}
    def feed(seq,x=.5,side=110):
        clock.now+=.1
        reader.latest=(seq,clock.now,synthetic_frame(x=x,size=side/640))
        service.capture_once();service.control_once()
    try:
        with patch('cv2.VideoCapture',side_effect=AssertionError('camera forbidden')),patch('socket.socket',side_effect=AssertionError('network forbidden')):
            feed(1)
            packet=port.get_frame();clock.now+=.05
            checks['cached_frame_time_unchanged']=port.get_frame().timestamp==packet.timestamp
            checks['exact_adapted_profile']=service.get_follow_status()['control_profile']==EXPECTED_PROFILE
            checks['identity_work_budget_profile']=(
                service.tracker.last_diagnostics.get('recognition_profile')
                == 'v020_identity_current_frame_reuse')
            assert service.calibrate_reference().ok and service.start_follow().ok
            for i in range(2,7):feed(i,x=.7,side=90)
            checks['yaw_and_depth_actual_nonzero']=port.velocity[3]>0 and port.velocity[1]>0
            obs=service.controller.observation
            reader.latest=(7,clock.now+.01,synthetic_frame(x=.2))
            rendered,matched=port.display_frame(obs)
            checks['overlay_matches_processed_frame']=matched and np.array_equal(rendered,port.frames[obs.frame_id])
            service.stop_follow();feed(8,x=.7)
            checks['fresh_green_cannot_restart']=not service.is_following() and port.velocity==(0,0,0,0)
            clock.now+=.6
            checks['real_stale_not_refreshed']=port.get_frame().stale and port.get_frame().frame is None
            checks['phase_boundaries']=[phase_at(t)[0] for t in (0,5,10,15,20,25,30,35)]==[-1,0,1,2,3,4,5,6]
            for t in (0,5,10,15,20,25,30,35):
                index,name,_,_=phase_at(t)
                service._record('bench_phase',index=index,name=name,
                                phase_at_monotonic=clock.now,elapsed=t)
            checks['phase_events_record_without_conflicting_keys']=sum(e['event']=='bench_phase' for e in service.events)==8
            allowed,why=test_start_decision(dict(following=False,recovering=True))
            checks['recovery_rejection_explains_wait']=not allowed and '归零等待' in why
            allowed,why=test_start_decision(dict(following=False,recovering=False,status_message='需重新开始'))
            checks['stopped_rejection_explains_actual_state']=not allowed and '需重新开始' in why
            checks['tracking_can_start_test']=test_start_decision(dict(following=True))[0]
    finally:service.close()
    clock,reader=Clock(),Reader()
    up_port,up_service=make_service(reader,monotonic=clock.mono,wall=clock.wall,vertical_follow=True)
    try:
        def vertical_feed(seq,y=.5,visible=True):
            clock.now+=.1
            reader.latest=(seq,clock.now,synthetic_frame(y=y,visible=visible))
            up_service.capture_once();up_service.control_once()
        vertical_feed(1)
        assert up_service.calibrate_reference().ok and up_service.start_follow().ok
        checks['vertical_explicitly_enabled']=up_service.get_follow_status()['vertical_follow']
        for seq in range(2,7):vertical_feed(seq,.3)
        checks['upward_actual_nonzero']=up_port.velocity[2]>0
        for seq in range(7,13):vertical_feed(seq,.7)
        checks['downward_actual_negative']=up_port.velocity[2]<0
        vertical_feed(13,visible=False)
        checks['loss_zero_includes_vertical']=up_port.velocity==(0,0,0,0)
        up_service.stop_follow();vertical_feed(14,.3)
        checks['stop_blocks_vertical_restart']=up_port.velocity==(0,0,0,0) and not up_service.is_following()
        checks['vertical_test_phase_boundaries']=[phase_at(t,True)[0] for t in (25,30,35,40,45)]==[4,5,6,7,8]
    finally:up_service.close()
    assert all(checks.values()),checks
    report=dict(python=sys.version,profile=EXPECTED_PROFILE,source=EXPECTED_VERSION,
        checks=checks,camera_opened=False,desktop_opened=False,passed=True)
    (HERE/'self_check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--camera',type=int,default=0)
    parser.add_argument('--target-id',type=int,choices=(0,1,2),default=0)
    parser.add_argument('--steering-mode',choices=('yaw','lateral','hybrid'),default='yaw',
                        help='lateral/hybrid are explicit uncalibrated offline candidates')
    parser.add_argument('--self-check',action='store_true')
    parser.add_argument('--config',type=Path,help='加载指定三组配置；覆盖目标、模式和上下开关，仍只用MockPort')
    parser.add_argument('--vertical-follow',action='store_true',help='启用原开源升降PID；只发给MockPort')
    args=parser.parse_args()
    if sys.version_info[:2]!=(3,11):raise SystemExit('请使用本项目Python3.11启动入口')
    verify_source()
    cv2.setNumThreads(1)
    if args.self_check:self_check()
    else:gui(args.camera,args.target_id,args.steering_mode,args.vertical_follow,args.config)
