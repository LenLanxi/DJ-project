"""Paired holdout validation. Uses only generated images and two saved videos."""
import argparse, sys, time, json, hashlib, csv, importlib.util
from pathlib import Path
from collections import Counter
import cv2
import numpy as np
BASE=Path(__file__).resolve().parent; ROOT=BASE.parent
sys.path.insert(0,str(BASE))
D=cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)

def make(rng, marker_id=0):
    size=float(rng.uniform(38,135));angle=rng.uniform(-.65,.65)
    points=np.float32([[-.5,-.5],[.5,-.5],[.5,.5],[-.5,.5]])*size
    rot=np.array([[np.cos(angle),-np.sin(angle)],[np.sin(angle),np.cos(angle)]])
    points=(points@rot.T+np.array([180,130])+rng.uniform(-.12,.12,(4,2))*size).astype(np.float32)
    mark=cv2.aruco.generateImageMarker(D,marker_id,144)
    img=cv2.warpPerspective(mark,cv2.getPerspectiveTransform(np.float32([[0,0],[143,0],[143,143],[0,143]]),points),(360,260),borderValue=235)
    k=int(rng.choice([1,3,5,7,9,13,17]))
    kernel=np.zeros((k,k),np.float32)
    direction=int(rng.integers(0,3))
    if direction==0:kernel[k//2,:]=1/k
    elif direction==1:kernel[:,k//2]=1/k
    else:np.fill_diagonal(kernel,1/k)
    img=cv2.filter2D(img,-1,kernel)
    grad=np.linspace(rng.uniform(.4,.9),rng.uniform(.65,1.15),360)[None,:]
    img=np.clip(img*grad+rng.normal(0,rng.uniform(1,4),img.shape),0,255).astype(np.uint8)
    return img,points,{'size':size,'blur':k,'direction':direction}
from r3_follow.vision import MarkerTracker
from video_replay import paired_summary
def load_baseline(path):
    spec=importlib.util.spec_from_file_location('r3_follow._comparison_baseline',path)
    baseline=importlib.util.module_from_spec(spec);spec.loader.exec_module(baseline)
    return baseline.MarkerTracker()
def save_image(path,image):
    ok,data=cv2.imencode('.png',image)
    if not ok:raise RuntimeError('encode failed')
    path.write_bytes(data.tobytes())
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--mode',choices=('synthetic','screen'),required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--seed',type=int,default=2026102157)
    parser.add_argument('--stride',type=int,default=1)
    parser.add_argument('--baseline',type=Path,default=BASE/'replay_baselines/vision_v011/vision.py')
    args=parser.parse_args()
    if args.stride<1:raise SystemExit('stride must be positive')
    if args.output.exists():raise SystemExit('Use a fresh output folder')
    args.output.mkdir(parents=True);cv2.setNumThreads(1)
    if sys.version_info[:2]!=(3,11):raise SystemExit('Python 3.11 required')
    old,new=load_baseline(args.baseline),MarkerTracker()
    report={'mode':args.mode,'python':sys.version,'cv2':cv2.__version__,'seed':args.seed,'stride':args.stride,
        'old_sha256':hashlib.sha256(args.baseline.read_bytes()).hexdigest(),
        'new_sha256':hashlib.sha256((BASE/'r3_follow/vision.py').read_bytes()).hexdigest(),
        'camera_opened':False,'desktop_opened':False,'hardware_connected':False,
        'limits':'Synthetic and screen-recording pixels only; not raw camera accuracy. Existing screen overlays retained.'}
    (args.output/'baseline.py.txt').write_bytes(args.baseline.read_bytes())
    (args.output/'candidate.py.txt').write_bytes((BASE/'r3_follow/vision.py').read_bytes())
    rows=[]; saved=Counter()
    def compare(img,i,t,kind,points=None):
        results={}
        for name,tracker in ((('old',old),('new',new)) if i%2 else (('new',new),('old',old))):
            begin=time.perf_counter(); obs=tracker.detect(img,i,t,source='offline-validation')
            results[name]=(obs,(time.perf_counter()-begin)*1000)
        a,ams=results['old'];b,bms=results['new']
        row=dict(index=i,pts=t,kind=kind,baseline_found=a.found,current_found=b.found,
                 baseline_ms=ams,current_ms=bms,method=new.last_diagnostics['detector_method'],
                 wrong_position=bool(b.found and points is not None and np.linalg.norm(np.array([b.center_x,b.center_y])-points.mean(0))>=12),
                 center_x=b.center_x,center_y=b.center_y,reason=b.reason,
                 sha256=hashlib.sha256(img.tobytes()).hexdigest())
        category='rescue' if b.found and not a.found else 'regression' if a.found and not b.found else 'both_missing' if not b.found else 'both_found'
        if category in ('rescue','regression') and saved[kind+category]<12:
            prefix=f'{kind}_{category}_{i:05d}'
            save_image(args.output/(prefix+'.png'),img)
            (args.output/(prefix+'.json')).write_text(json.dumps({'expected_corners':None if points is None else points.tolist(),
                'detected_corners':b.corners,'row':row}),encoding='utf8')
            saved[kind+category]+=1
        rows.append(row);return row
    if args.mode=='synthetic':
        rng=np.random.default_rng(report['seed'])
        for i in range(600):
            img,pts,_=make(rng);compare(img,i,float(i),'positive',pts)
        print('holdout positives finished',flush=True)
        for i in range(800):
            if i<500:img,_,_=make(rng,int(rng.integers(1,50)));kind='wrong_id'
            elif i<600:img=rng.integers(0,256,(260,360),np.uint8);kind='noise'
            elif i<700:
                img=np.full((260,360),235,np.uint8);kind='rectangles'
                for _ in range(10):
                    x,y=rng.integers(0,250,2);w,h=rng.integers(10,70,2)
                    cv2.rectangle(img,(int(x),int(y)),(int(x+w),int(y+h)),int(rng.integers(0,255)),2)
            else:
                img=np.full((260,360),235,np.uint8);img[90:150,150:210]=cv2.aruco.generateImageMarker(D,0,60)
                img[110:140,150:210]=235;kind='occluded'
            compare(img,600+i,float(600+i),kind)
            if i%200==199:print('negative',i+1,flush=True)
    else:
        report['roi']=[24,175,960,720];report['resize']=[640,480];report['recordings']={}
        for name in ('marker_v08_141912','marker_v09_145140'):
            path=ROOT/'tmp'/(name+'.mp4');cap=cv2.VideoCapture(str(path))
            if not cap.isOpened():raise RuntimeError('Cannot open supplied video file')
            total=int(cap.get(cv2.CAP_PROP_FRAME_COUNT));fps=cap.get(cv2.CAP_PROP_FPS);clip=[]
            for i in range(total):
                ok,screen=cap.read()
                if not ok:raise RuntimeError('Incomplete file decode')
                if i%args.stride:continue
                img=cv2.resize(screen[175:895,24:984],(640,480),interpolation=cv2.INTER_AREA)
                pts=cap.get(cv2.CAP_PROP_POS_MSEC)/1000
                clip.append(compare(img,i,pts,name))
                if i%500==0:print(name,i+1,'/',total,flush=True)
            cap.release()
            report['recordings'][name]=(paired_summary(clip,clip[-1]['pts']+1/fps) if args.stride==1 else {
                'decoded_file_frames':total,'tested_frames':len(clip),
                'baseline_found':sum(r['baseline_found'] for r in clip),
                'current_found':sum(r['current_found'] for r in clip),
                'note':'Regularly sampled recording frames. No continuous-loss-duration claim.'})
            report['recordings'][name]['file_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    groups={}
    for kind in sorted({r['kind'] for r in rows}):
        g=[r for r in rows if r['kind']==kind]
        groups[kind]={'count':len(g),'baseline_found':sum(r['baseline_found'] for r in g),
            'current_found':sum(r['current_found'] for r in g),
            'rescued':sum(r['current_found'] and not r['baseline_found'] for r in g),
            'regressions':sum(r['baseline_found'] and not r['current_found'] for r in g),
            'wrong_position':sum(r['wrong_position'] for r in g),
            'baseline_ms_p95':float(np.percentile([r['baseline_ms'] for r in g],95)),
            'current_ms_p95':float(np.percentile([r['current_ms'] for r in g],95))}
    report['groups']=groups
    with (args.output/'frames.csv').open('w',newline='',encoding='utf-8-sig') as f:
        writer=csv.DictWriter(f,fieldnames=rows[0]);writer.writeheader();writer.writerows(rows)
    (args.output/'summary.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    print(json.dumps(groups,indent=2),flush=True)
if __name__=='__main__':main()
