"""Bounded local propagation of a cloud-selected, textured image region.

Historical frames retain their real decoder IDs/timestamps. Only current()
with its default fresh check is suitable for the navigation observation path.
This is short-term optical flow, not identity recognition, obstacle avoidance,
metric positioning, or a guarantee that every occlusion will be detected.

OpenCV primitives (no additional dependencies):
https://docs.opencv.org/4.x/d4/dee/tutorial_optical_flow.html
https://docs.opencv.org/4.x/d9/d0c/group__calib3d.html
"""
from __future__ import annotations

import math
import time
import uuid

import cv2
import numpy as np
from navigation.Observation import SEED_MAX_AGE


class TargetTrackingError(RuntimeError):
    """A failed tracker stays invalid until explicitly seeded again."""


class TargetTracker:
    def __init__(self, *, clock=time.time, max_source_age=1.0,
                  max_semantic_age=SEED_MAX_AGE, max_gap=.3, max_updates=100,
                 max_dimension=960):
        for value, maximum in ((max_source_age,1.0),(max_semantic_age,8.),(max_gap,.3)):
            if type(value) not in (int,float) or not math.isfinite(value) or not 0 < value <= maximum:
                raise ValueError('invalid_target_tracker_time_budget')
        if type(max_updates) is not int or not 1 <= max_updates <= 160:
            raise ValueError('invalid_target_tracker_sample_budget')
        if type(max_dimension) is not int or not 320 <= max_dimension <= 960:
            raise ValueError('invalid_target_tracker_resolution_budget')
        self.clock=clock
        self.max_source_age=float(max_source_age)
        self.max_semantic_age=float(max_semantic_age)
        self.max_gap=float(max_gap)
        self.max_updates=max_updates
        self.max_dimension=max_dimension
        self.alive=False
        self.failure='target_not_seeded'

    def _fail(self, code):
        self.alive=False
        self.failure=code
        raise TargetTrackingError(code)

    def _packet(self, packet):
        if not isinstance(packet,dict):self._fail('invalid_target_packet')
        fid,stamp=packet.get('frame_id'),packet.get('timestamp')
        if type(fid) is not int or fid < 0:self._fail('invalid_target_frame_identity')
        if type(stamp) not in (int,float) or not math.isfinite(stamp):
            self._fail('invalid_target_frame_timestamp')
        age=self.clock()-stamp
        if packet.get('stale') or not 0 <= age <= self.max_semantic_age:
            self._fail('target_source_frame_expired')
        source=packet.get('source','sdk_camera')
        if not isinstance(source,str) or not source or len(source)>128:
            self._fail('invalid_target_source')
        frame=packet.get('frame')
        if (not isinstance(frame,np.ndarray) or frame.dtype!=np.uint8
                or frame.ndim!=3 or frame.shape[2]!=3
                or min(frame.shape[:2])<48 or frame.shape[0]*frame.shape[1]>8_500_000):
            self._fail('invalid_target_image')
        h,w=frame.shape[:2]
        scale=min(1.,self.max_dimension/max(h,w))
        if scale<1:frame=cv2.resize(frame,(round(w*scale),round(h*scale)),interpolation=cv2.INTER_AREA)
        else:frame=frame.copy()
        gray=cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY)
        identity={k:packet[k] for k in ('frame_id','timestamp','source','simulated') if k in packet}
        identity.setdefault('source',source)
        return frame,gray,identity

    def _bounds(self, quad, shape):
        h,w=shape[:2]
        if not np.isfinite(quad).all():self._fail('target_transform_nonfinite')
        left,top=quad.min(axis=0);right,bottom=quad.max(axis=0)
        if min(left,top)<1 or right>w-1 or bottom>h-1:
            self._fail('target_left_field_of_view')
        if right-left<24 or bottom-top<24 or (right-left)*(bottom-top)>.75*w*h:
            self._fail('target_box_out_of_bounds')
        return (float(left),float(top),float(right-left),float(bottom-top))

    @staticmethod
    def _crop(gray, box, inset=0):
        x,y,w,h=box
        x0,y0=int(math.ceil(x))+inset,int(math.ceil(y))+inset
        x1,y1=int(math.floor(x+w))-inset,int(math.floor(y+h))-inset
        return gray[y0:y1,x0:x1]

    def _unique(self, gray, box):
        # Local appearance can reject a second near-identical region. It cannot
        # establish semantic uniqueness of differently textured same-color
        # objects; cloud selection must already have rejected that ambiguity.
        x,y,w,h=box
        template=self._crop(gray,box,2)
        if min(template.shape)<20 or float(template.std())<6:
            self._fail('target_texture_insufficient')
        scores=cv2.matchTemplate(gray,template,cv2.TM_CCOEFF_NORMED)
        tw,th=template.shape[1],template.shape[0]
        # Suppress overlapping placements of this same physical patch.
        cx,cy=x+w/2,y+h/2
        xx=np.arange(scores.shape[1])+tw/2
        yy=np.arange(scores.shape[0])+th/2
        overlap=(np.abs(xx[None,:]-cx)<.8*w)&(np.abs(yy[:,None]-cy)<.8*h)
        scores[overlap]=-1.
        second=float(scores.max())
        if second>.92:self._fail('target_appearance_ambiguous')
        return second

    def _coverage(self, points, box):
        x,y,w,h=box
        flat=points.reshape(-1,2)
        if len(flat)<12:self._fail('target_features_lost')
        hull=cv2.convexHull(flat.astype(np.float32))
        coverage=float(cv2.contourArea(hull)/(w*h))
        cells=np.floor((flat-np.array([x,y]))/np.array([w,h])*4).astype(int)
        occupied=len({tuple(np.clip(cell,0,3)) for cell in cells})
        if coverage<.16 or occupied<6:self._fail('target_feature_coverage_low')
        return coverage,occupied

    def seed(self, packet, bbox, *, require_fresh=False):
        self.alive=False
        frame,gray,identity=self._packet(packet)
        if (not isinstance(bbox,(list,tuple)) or len(bbox)!=4
                or any(type(v) not in (int,float) or not math.isfinite(v) for v in bbox)):
            self._fail('invalid_target_bbox')
        x,y,w,h=map(float,bbox)
        if min(x,y)<0 or min(w,h)<=0 or x+w>1 or y+h>1:
            self._fail('invalid_target_bbox')
        height,width=gray.shape
        quad=np.array([[x*width,y*height],[(x+w)*width,y*height],
                       [(x+w)*width,(y+h)*height],[x*width,(y+h)*height]],dtype=np.float32)
        box=self._bounds(quad,gray.shape)
        second=self._unique(gray,box)
        mask=np.zeros_like(gray)
        px,py,pw,ph=box
        inset=max(4,int(min(pw,ph)*.08))
        cv2.rectangle(mask,(math.ceil(px)+inset,math.ceil(py)+inset),
                      (math.floor(px+pw)-inset,math.floor(py+ph)-inset),255,-1)
        points=cv2.goodFeaturesToTrack(gray,maxCorners=120,qualityLevel=.025,
            minDistance=5,mask=mask,blockSize=5)
        if points is None or len(points)<16:self._fail('target_texture_insufficient')
        coverage,occupied=self._coverage(points,box)
        self.gray=gray
        self.seed_gray=gray.copy()
        self.seed_frame=frame.copy()
        self.seed_box=box
        self.seed_quad=quad.copy()
        self.quad=quad
        self.points=points.astype(np.float32)
        self.seed_count=len(points)
        self.identity=identity
        self.seed_identity=identity.copy()
        self.matrix=np.eye(3,dtype=np.float64)
        self.track_id=uuid.uuid4().hex
        self.updates=0
        self.quality={'features':len(points),'inlier_fraction':1.,'coverage':coverage,
                      'occupied_cells':occupied,'appearance_ncc':1.,'appearance_good_fraction':1.,
                      'second_region_score':second}
        self.alive=True
        self.failure=None
        return self.current(require_fresh=require_fresh)

    def _appearance(self, frame, gray, matrix):
        # Always compare to the cloud-seeded template, never successively
        # replace it with a new patch that could drift onto the background.
        inverse=np.linalg.inv(matrix)
        height,width=self.seed_gray.shape
        aligned=cv2.warpAffine(gray,inverse[:2],(width,height),flags=cv2.INTER_LINEAR)
        patch=self._crop(aligned,self.seed_box,5).astype(np.float32)
        reference=self._crop(self.seed_gray,self.seed_box,5).astype(np.float32)
        if patch.shape!=reference.shape or min(patch.shape)<12:self._fail('target_appearance_invalid')
        a=reference-reference.mean();b=patch-patch.mean()
        divisor=math.sqrt(float((a*a).sum()*(b*b).sum()))
        ncc=float((a*b).sum()/divisor) if divisor>0 else -1.
        # Compensate only a global brightness offset; no patch replacement.
        difference=np.abs((patch-reference)-np.median(patch-reference))
        good=float(np.mean(difference<=30))
        if ncc<.76 or good<.86:self._fail('target_appearance_changed')
        # Texture cells must continue to agree. This rejects a large cover
        # even when surviving feature points still fit an affine transform.
        cell_matches=[]
        for row in range(4):
            for col in range(4):
                yy=slice(row*patch.shape[0]//4,(row+1)*patch.shape[0]//4)
                xx=slice(col*patch.shape[1]//4,(col+1)*patch.shape[1]//4)
                r=reference[yy,xx];p=patch[yy,xx]
                if r.std()<6:continue
                r=r-r.mean();p=p-p.mean()
                d=math.sqrt(float((r*r).sum()*(p*p).sum()))
                cell_matches.append(float((r*p).sum()/d) if d>0 else -1.)
        if len(cell_matches)<4 or np.mean(np.array(cell_matches)>=.55)<.85:
            self._fail('target_partial_occlusion_or_drift')
        aligned_color=cv2.warpAffine(frame,inverse[:2],(width,height),flags=cv2.INTER_LINEAR)
        color=np.median(np.abs(self._crop(aligned_color,self.seed_box,5).astype(np.float32)-
                               self._crop(self.seed_frame,self.seed_box,5).astype(np.float32)),axis=(0,1))
        if float(color.max())>35:self._fail('target_color_changed')
        return ncc,good

    def update(self, packet, *, require_fresh=False):
        if not self.alive:raise TargetTrackingError(self.failure or 'target_not_seeded')
        try:
            return self._update(packet,require_fresh=require_fresh)
        except TargetTrackingError:raise
        except (cv2.error,ValueError,np.linalg.LinAlgError,OverflowError,TypeError):
            self._fail('target_tracking_computation_failed')

    def _update(self, packet, *, require_fresh):
        frame,gray,identity=self._packet(packet)
        if identity['source']!=self.identity['source'] or identity.get('simulated')!=self.identity.get('simulated'):
            self._fail('target_source_changed')
        if gray.shape!=self.gray.shape:self._fail('target_resolution_changed')
        fid,stamp=identity['frame_id'],identity['timestamp']
        if fid==self.identity['frame_id']:
            if stamp!=self.identity['timestamp']:self._fail('target_duplicate_timestamp_changed')
            if not np.array_equal(gray,self.gray):self._fail('target_duplicate_content_changed')
            return self.current(require_fresh=require_fresh)
        if fid<self.identity['frame_id']:self._fail('target_frame_identity_reversed')
        gap=stamp-self.identity['timestamp']
        if not 0 < gap <= self.max_gap+1e-6:self._fail('target_frame_gap')
        if self.clock()-self.seed_identity['timestamp']>self.max_semantic_age:
            self._fail('target_semantic_expired')
        if self.updates>=self.max_updates:self._fail('target_sample_budget')
        params=dict(winSize=(21,21),maxLevel=3,
            criteria=(cv2.TERM_CRITERIA_EPS|cv2.TERM_CRITERIA_COUNT,30,.01),minEigThreshold=1e-4)
        forward,found,error=cv2.calcOpticalFlowPyrLK(self.gray,gray,self.points,None,**params)
        if forward is None or found is None:self._fail('target_optical_flow_lost')
        backward,back_found,_=cv2.calcOpticalFlowPyrLK(gray,self.gray,forward,None,**params)
        if backward is None or back_found is None:self._fail('target_optical_flow_lost')
        fb=np.linalg.norm(backward.reshape(-1,2)-self.points.reshape(-1,2),axis=1)
        valid=(found.ravel()==1)&(back_found.ravel()==1)&np.isfinite(fb)&(fb<=1.)
        if error is not None:valid &= np.isfinite(error.ravel())&(error.ravel()<=30)
        old=self.points.reshape(-1,2)[valid];new=forward.reshape(-1,2)[valid]
        if len(old)<12:self._fail('target_features_lost')
        affine,inliers=cv2.estimateAffine2D(old,new,method=cv2.RANSAC,ransacReprojThreshold=1.8,
                                          maxIters=1200,confidence=.99,refineIters=10)
        if affine is None or inliers is None or not np.isfinite(affine).all():
            self._fail('target_motion_inconsistent')
        mask=inliers.ravel().astype(bool);fraction=float(mask.mean())
        if fraction<.8 or mask.sum()<max(12,math.ceil(self.seed_count*.55)):
            self._fail('target_inliers_insufficient')
        linear=affine[:,:2];singular=np.linalg.svd(linear,compute_uv=False)
        rotation=math.degrees(math.atan2(linear[1,0],linear[0,0]))
        if (np.linalg.det(linear)<=0 or singular.min()<.8 or singular.max()>1.25
                or singular.max()/singular.min()>1.35 or abs(rotation)>15):
            self._fail('target_scale_or_rotation_jump')
        step=np.vstack((affine,[0.,0.,1.]))
        matrix=step@self.matrix
        quad=cv2.transform(self.quad.reshape(1,-1,2),affine).reshape(-1,2)
        box=self._bounds(quad,gray.shape)
        previous=self._bounds(self.quad,self.gray.shape)
        distance=np.linalg.norm(quad.mean(axis=0)-self.quad.mean(axis=0))
        if distance>max(12.,.35*max(previous[2:])):self._fail('target_motion_jump')
        cumulative_scale=math.sqrt(abs(float(np.linalg.det(matrix[:2,:2]))))
        if not .45<=cumulative_scale<=2.2:self._fail('target_cumulative_scale_budget')
        # Surviving original features must cover the target, not one tiny
        # unoccluded corner or an unrelated background cluster.
        points=new[mask].reshape(-1,1,2).astype(np.float32)
        coverage,occupied=self._coverage(points,box)
        ncc,good=self._appearance(frame,gray,matrix)
        second=self._unique(gray,box)
        self.points=points;self.gray=gray;self.quad=quad;self.matrix=matrix
        self.identity=identity;self.updates+=1
        self.quality={'features':len(points),'inlier_fraction':fraction,'coverage':coverage,
                      'occupied_cells':occupied,'appearance_ncc':ncc,'appearance_good_fraction':good,
                      'second_region_score':second,'forward_backward_max':float(fb[valid][mask].max())}
        return self.current(require_fresh=require_fresh)

    def current(self, *, require_fresh=True):
        if not self.alive:raise TargetTrackingError(self.failure or 'target_not_seeded')
        now=self.clock();age=now-self.identity['timestamp']
        semantic_age=now-self.seed_identity['timestamp']
        if not 0<=semantic_age<=self.max_semantic_age:self._fail('target_semantic_expired')
        if not 0<=age<=self.max_semantic_age:self._fail('target_source_frame_expired')
        fresh=age<=self.max_source_age
        if require_fresh and not fresh:self._fail('target_current_frame_expired')
        x,y,w,h=self._bounds(self.quad,self.gray.shape)
        height,width=self.gray.shape
        bbox=[x/width,y/height,w/width,h/height]
        return {**self.identity,'bbox':bbox,'visible':True,'center_x':(x+w/2)/width,
            'width_fraction':w/width,'fresh':fresh,
            'target_tracking':{'track_id':self.track_id,'seed_frame_id':self.seed_identity['frame_id'],
                'semantic_timestamp':self.seed_identity['timestamp'],'quality':dict(self.quality),
                'updates':self.updates,'source_age_seconds':age,'semantic_age_seconds':semantic_age,
                'max_semantic_age_seconds':self.max_semantic_age,'method':'seed_appearance_LK_FB_RANSAC_affine',
                'identity_verified':False,'hardware_verified':False}}
