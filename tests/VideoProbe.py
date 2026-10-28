"""Reproducible in-memory H264 -> loopback UDP -> PyAV receive experiment.

Frame pixels identify the sender's recorded enqueue time. These measurements
exclude camera exposure and physical Wi-Fi and are not end-to-end flight age.
No device, external network, UI, cloud or extra dependency is used.
"""
import argparse
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import socket
import statistics
import threading
import time
from unittest.mock import patch

import av
import numpy as np


PROFILES={
    'small':{'width':256,'height':128,'rate':30,'frames':105,'requested_vbv_mbps':None},
    'large':{'width':960,'height':720,'rate':30,'frames':105,'requested_vbv_mbps':4},
}
CASES=[
    {'name':'Current','fifo':28672,'socket':None,'options':None,'format':None},
    {'name':'SmallQueue','fifo':512,'socket':65536,'options':None,'format':None},
    {'name':'MediumQueue','fifo':1024,'socket':65536,'options':None,'format':None},
    {'name':'SmallProbe','fifo':512,'socket':65536,'format':'h264',
     'options':{'probesize':'32768','analyzeduration':'0'}},
    {'name':'MediumProbe','fifo':1024,'socket':65536,'format':'h264',
     'options':{'probesize':'32768','analyzeduration':'0'}},
    {'name':'SmallNobuffer','fifo':512,'socket':65536,'format':'h264',
     'options':{'probesize':'32768','analyzeduration':'0','fflags':'nobuffer'}},
    {'name':'MediumNobuffer','fifo':1024,'socket':65536,'format':'h264',
     'options':{'probesize':'32768','analyzeduration':'0','fflags':'nobuffer'}},
]


def encode(profile):
    width,height=profile['width'],profile['height']
    encoder=av.CodecContext.create('libx264','w')
    encoder.width,encoder.height,encoder.pix_fmt=width,height,'yuv420p'
    encoder.time_base,encoder.framerate=Fraction(1,30),Fraction(30,1)
    encoder.thread_count=1
    params='keyint=15:scenecut=0:repeat-headers=1'
    if profile['requested_vbv_mbps']:
        params+=':vbv-maxrate=4000:vbv-bufsize=4000'
    encoder.options={'preset':'ultrafast','tune':'zerolatency','crf':'23','x264-params':params}
    encoder.open()
    rng=np.random.default_rng(7)
    packets=[]
    for number in range(profile['frames']):
        image=rng.integers(0,256,(height,width,3),dtype=np.uint8)
        for bit in range(8):
            image[:32,bit*32:(bit+1)*32,:]=240 if number>>bit&1 else 16
        frame=av.VideoFrame.from_ndarray(image,format='bgr24')
        frame.pts=number
        packets.append(b''.join(bytes(p) for p in encoder.encode(frame)))
    packets[-1]+=b''.join(bytes(p) for p in encoder.encode(None))
    return packets


def run_case(case,profile,packets,*,stall):
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as temporary:
        temporary.bind(('127.0.0.1',0)); port=temporary.getsockname()[1]
    halt=threading.Event()
    sent={}
    sender_errors=[]
    def sender():
        try:
            with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as udp:
                halt.wait(.15)
                origin=time.monotonic()
                for number,data in enumerate(packets):
                    if halt.wait(max(0.,origin+number/profile['rate']-time.monotonic())): break
                    sent[number]=time.monotonic()
                    for offset in range(0,len(data),1200):
                        udp.sendto(data[offset:offset+1200],('127.0.0.1',port))
        except Exception as exc: sender_errors.append(type(exc).__name__)
    thread=threading.Thread(target=sender,daemon=True)
    result={'case':case['name'],'profile':profile,'stall_seconds':1.1 if stall else 0,
            'url_options':{key:case[key] for key in ('fifo','socket','options','format')},
            'fifo_bytes':188*case['fifo'],'decoded':[],'errors':[],
            'packet_max_bytes':max(map(len,packets)),
            'encoded_mean_mbps':round(sum(map(len,packets))*8/(len(packets)/profile['rate'])/1e6,3)}
    suffix=f'?overrun_nonfatal=0&fifo_size={case["fifo"]}'
    if case['socket']: suffix+=f'&buffer_size={case["socket"]}'
    started=time.monotonic()
    container=None
    thread.start()
    try:
        with av.logging.Capture(local=False) as messages:
            try:
                container=av.open(f'udp://127.0.0.1:{port}'+suffix,format=case['format'],
                                  options=case['options'],timeout=(4,1))
                result['open_ms']=round((time.monotonic()-started)*1000,3)
                for frame in container.decode(video=0):
                    pixels=frame.to_ndarray(format='bgr24')
                    at=time.monotonic()
                    number=sum(1<<bit for bit in range(8)
                        if pixels[4:28,bit*32+4:(bit+1)*32-4].mean()>128)
                    result['decoded'].append({'number':number,'received_ms':round((at-started)*1000,3),
                        'send_age_ms':round((at-sent[number])*1000,3) if number in sent else None,
                        'behind_latest_sent':max(sent)-number if sent else None})
                    if stall and len(result['decoded'])==2:
                        time.sleep(1.1)
                    if len(result['decoded'])>=45 or at-started>5: break
            except Exception as exc:
                result['errors'].append(type(exc).__name__+': '+str(exc)[:180])
            result['ffmpeg_log_tail']=[str(m[2])[:180] for m in messages[-4:]]
    finally:
        halt.set(); thread.join(2)
        if container is not None: container.close()
    result['sender_errors']=sender_errors
    result['sender_finished']=not thread.is_alive()
    ages=[f['send_age_ms'] for f in result['decoded'] if f['send_age_ms'] is not None]
    result['summary']={'count':len(result['decoded']),'max_send_age_ms':max(ages) if ages else None,
        'median_send_age_ms':statistics.median(ages) if ages else None,
        'first_frame_ms':result['decoded'][0]['received_ms'] if ages else None,
        'post_stall_stale_count':sum(a>500 for a in ages[2:]) if stall else None,
        'error_after_frames':len(ages) if result['errors'] else None}
    return result


def run_decoder(profile,packets,*,stall,processing_delay=0.):
    """Run the actual production decoder; substitute neither AV nor UDP data.

    The optional container wrapper pauses iteration once, reproducing host
    scheduling/decode starvation. Its close() stays on the decoder thread.
    """
    import sys
    root=Path(__file__).resolve().parents[1]
    sys.path.insert(0,str(root))
    from drone_sdk import _driver as driver
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as temporary:
        temporary.bind(('127.0.0.1',0)); port=temporary.getsockname()[1]
    halt=threading.Event()
    sent={}
    published=[]
    conversion_times=[]
    sender_errors=[]
    def sender():
        try:
            with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as udp:
                halt.wait(.15)
                origin=time.monotonic()
                for number,data in enumerate(packets):
                    if halt.wait(max(0.,origin+number/profile['rate']-time.monotonic())): break
                    sent[number]=time.monotonic()
                    for offset in range(0,len(data),1200):
                        udp.sendto(data[offset:offset+1200],('127.0.0.1',port))
        except Exception as exc: sender_errors.append(type(exc).__name__)
    class RecordingDecoder(driver._VideoDecoder):
        def __setattr__(self,name,value):
            if name=='_frame' and value is not None:
                at=time.monotonic()
                number=sum(1<<bit for bit in range(8)
                    if value[4:28,bit*32+4:(bit+1)*32-4].mean()>128)
                published.append({'number':number,
                    'send_age_ms':round((at-sent[number])*1000,3) if number in sent else None})
            super().__setattr__(name,value)
    paused=False
    original=av.open
    class Frame:
        def __init__(self,inner): self.inner=inner
        def to_ndarray(self,**kwargs):
            started=time.monotonic()
            image=self.inner.to_ndarray(**kwargs)
            if processing_delay: time.sleep(processing_delay)
            conversion_times.append((time.monotonic()-started)*1000)
            return image
    class Container:
        def __init__(self,inner): self.inner=inner
        def demux(self,**kwargs):
            class Packet:
                def __init__(self,inner): self.inner=inner
                def decode(self): return [Frame(frame) for frame in self.inner.decode()]
            nonlocal paused
            count = 0
            for packet in self.inner.demux(**kwargs):
                yield Packet(packet)
                count += 1
                if stall and not paused and count == 2:
                    paused = True
                    time.sleep(1.1)
        def close(self): self.inner.close()
    def opened(*args,**kwargs): return Container(original(*args,**kwargs))
    decoder=RecordingDecoder(f'udp://127.0.0.1:{port}',open_timeout=2,read_timeout=.5)
    thread=threading.Thread(target=sender,daemon=True)
    with av.logging.Capture(local=False) as messages, patch('av.open',new=opened):
        decoder.start(); thread.start()
        try:
            thread.join(len(packets)/profile['rate']+2)
            time.sleep(.1)
        finally:
            halt.set(); thread.join(2); decoder.stop(timeout=3)
        logs=[str(m[2])[:180] for m in messages[-4:]]
    ages=[p['send_age_ms'] for p in published if p['send_age_ms'] is not None]
    return {'case':'ProductionDecoder','profile':profile,'stall_seconds':1.1 if stall else 0,
        'driver_lf_sha256':hashlib.sha256(Path(driver.__file__).read_bytes().replace(b'\r\n',b'\n')).hexdigest(),
        'url_options':driver.VIDEO_URL_OPTIONS,'av_options':driver.VIDEO_DECODE_OPTIONS,
        'fixture_timeouts_seconds':[2,.5],'processing_delay_seconds':processing_delay,
        'decoded':published,'stats':decoder.stats(),
        'sender_errors':sender_errors,'sender_finished':not thread.is_alive(),
        'decoder_finished':not decoder.is_alive(),'ffmpeg_log_tail':logs,
        'summary':{'count':len(published),'max_send_age_ms':max(ages) if ages else None,
                   'unknown_source_count':sum(p['send_age_ms'] is None for p in published),
                   'published_stale_count':sum(a>500 for a in ages),
                   'reconnects':decoder.reconnects,
                   'conversion_max_ms':round(max(conversion_times),3) if conversion_times else None,
                   'processing_rejected':decoder.processing_rejections > 0,
                   'gap_rejected':decoder.gap_rejections > 0}}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',choices=PROFILES,action='append')
    parser.add_argument('--case',choices=[c['name'] for c in CASES],action='append')
    parser.add_argument('--output',required=True)
    parser.add_argument('--decoder',action='store_true',help='Exercise the actual production decoder, including gap rejection')
    parser.add_argument('--slow-processing-ms',type=float,default=0,
                        help='Decoder fixture only: inject local per-frame array processing delay')
    args=parser.parse_args()
    evidence={'av_version':av.__version__,'library_versions':av.library_versions,'samples':[],
        'sources':['https://ffmpeg.org/ffmpeg-protocols.html#udp',
                   'https://ffmpeg.org/ffmpeg-formats.html#Format-Options'],
        'scope':'generated pixels and local UDP send times; not camera or aircraft latency'}
    output=Path(args.output); output.parent.mkdir(parents=True,exist_ok=True)
    for name in args.profile or PROFILES:
        packets=encode(PROFILES[name])
        if args.decoder:
            for stall in ((False,) if args.slow_processing_ms else (False,True)):
                sample=run_decoder(PROFILES[name],packets,stall=stall,processing_delay=args.slow_processing_ms/1000)
                evidence['samples'].append(sample)
                output.write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
                print(json.dumps({'profile':name,**sample['summary']},ensure_ascii=False),flush=True)
            continue
        for case in CASES:
            if args.case and case['name'] not in args.case: continue
            for stall in (False,True):
                sample=run_case(case,PROFILES[name],packets,stall=stall)
                evidence['samples'].append(sample)
                output.write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
                print(json.dumps({'profile':name,'case':case['name'],'stall':stall,
                    **sample['summary'],'errors':sample['errors']},ensure_ascii=False),flush=True)


if __name__=='__main__': main()
