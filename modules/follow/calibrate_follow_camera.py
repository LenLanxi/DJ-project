"""Calibrate from saved checkerboard photographs; never open a camera or SDK.

Use the aircraft's actual decoded resolution, unchanged crop/zoom and a flat
checkerboard. At least 15 distinct views are needed; holdout images estimate
reprojection fit only, not real depth accuracy. Output is never auto-activated.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path

import cv2
import numpy as np


def view_conditioning(board, corners, size):
    """Check five independent intrinsic constraints before nonlinear fitting.

    Each planar homography's first two columns must have equal norm and be
    orthogonal after applying the unknown inverse intrinsics. Stack those two
    linear constraints on the six symmetric-matrix entries. Frontal images
    translated/rescaled many times do not provide the required rank five.
    Coordinates and constraint rows are normalised to reduce unit sensitivity.
    This is a degeneracy guard, not proof of physical calibration accuracy.
    """
    constraints = []
    for pixels in corners:
        image = (pixels.reshape(-1, 2)-np.array(size)/2)/max(size)
        h, _ = cv2.findHomography(board[:, :2].astype(float), image, 0)
        if h is None or not np.isfinite(h).all():
            raise ValueError('Calibration homography cannot be estimated')
        def row(i, j):
            a, b = h[:, i], h[:, j]
            return np.array([a[0]*b[0], a[0]*b[1]+a[1]*b[0], a[1]*b[1],
                             a[2]*b[0]+a[0]*b[2], a[2]*b[1]+a[1]*b[2], a[2]*b[2]])
        for v in (row(0, 1), row(0, 0)-row(1, 1)):
            norm = np.linalg.norm(v)
            if not math.isfinite(norm) or norm < 1e-15:
                raise ValueError('Degenerate calibration view')
            constraints.append(v/norm)
    singular = np.linalg.svd(constraints, compute_uv=False)
    ratio = float(singular[-2]/singular[0]) if len(singular) == 6 else 0.
    if ratio < .005:
        raise ValueError('Calibration needs distinct tilted views on both axes; '
                         'frontal translations/scale changes cannot determine focal length')
    return dict(intrinsic_constraint_rank5_ratio=ratio, minimum_ratio=.005,
                singular_values=singular.tolist(), physical_accuracy_verified=False)


def calibrate(images, *, camera_id, columns, rows, square_m):
    if (not isinstance(camera_id,str) or not camera_id.strip()
            or type(columns) is not int or type(rows) is not int
            or not 4 <= columns <= 20 or not 4 <= rows <= 20
            or type(square_m) not in (int,float) or not math.isfinite(square_m)
            or not .005 <= square_m <= .1):
        raise ValueError('Provide camera_id, 4..20 inner corners per axis, measured square size 5..100 mm')
    board=np.zeros((columns*rows,3),np.float32)
    board[:,:2]=np.mgrid[0:columns,0:rows].T.reshape(-1,2)*square_m
    accepted=[];rejected=[];seen=set();size=None
    for path in images:
        raw=Path(path).read_bytes();digest=hashlib.sha256(raw).hexdigest()
        if digest in seen:
            rejected.append(dict(file=str(path),reason='duplicate_file'));continue
        seen.add(digest)
        frame=cv2.imdecode(np.frombuffer(raw,dtype=np.uint8),cv2.IMREAD_GRAYSCALE)
        if frame is None:
            rejected.append(dict(file=str(path),reason='unreadable_image'));continue
        dimensions=(frame.shape[1],frame.shape[0])
        if size is not None and dimensions!=size:
            raise ValueError('All calibration images must have the same resolution and crop')
        found,corners=cv2.findChessboardCornersSB(frame,(columns,rows))
        if not found:
            rejected.append(dict(file=str(path),reason='checkerboard_not_found'));continue
        size=dimensions
        normalized=corners.reshape(-1,2)/np.array(size)
        # Reject nearly identical views even when compressed bytes differ.
        if any(np.sqrt(np.mean((normalized-item['normalized'])**2))<.01 for item in accepted):
            rejected.append(dict(file=str(path),reason='near_duplicate_view'));continue
        accepted.append(dict(file=str(path),sha256=digest,corners=corners,normalized=normalized))
    if len(accepted)<15:
        raise ValueError(f'Need at least 15 distinct readable checkerboard views; got {len(accepted)}')
    centers=np.array([item['normalized'].mean(axis=0) for item in accepted])
    if np.ptp(centers[:,0])<.25 or np.ptp(centers[:,1])<.2:
        raise ValueError('Calibration views must cover left/right and upper/lower image regions')
    train=[item for i,item in enumerate(accepted) if i%4!=0]
    holdout=[item for i,item in enumerate(accepted) if i%4==0]
    conditioning = view_conditioning(board, [v['corners'] for v in train], size)
    rms,K,D,_,_=cv2.calibrateCamera([board]*len(train),[v['corners'] for v in train],size,None,None)
    if not math.isfinite(rms) or rms>1.5 or not np.isfinite(K).all() or not np.isfinite(D).all():
        raise ValueError('Training reprojection error is too large; inspect board, blur and coverage')
    errors=[]
    for item in holdout:
        ok,r,t=cv2.solvePnP(board,item['corners'],K,D,flags=cv2.SOLVEPNP_ITERATIVE)
        if not ok:raise ValueError('Holdout pose could not be solved')
        projected=cv2.projectPoints(board,r,t,K,D)[0]
        errors.append(float(np.sqrt(np.mean(np.sum((projected-item['corners'])**2,axis=2)))))
    if max(errors)>2:
        raise ValueError('Holdout reprojection error exceeds 2 pixels; collect better calibration images')
    return dict(schema='r3_camera_calibration_v1',camera_id=camera_id,image_size=list(size),
        camera_matrix=K.tolist(),dist_coeffs=D.reshape(-1).tolist(),rms_px=float(rms),
        holdout_rms_px=errors,view_conditioning=conditioning,
        source='saved_checkerboard_images',square_size_m=square_m,
        inner_corners=[columns,rows],training_views=len(train),holdout_views=len(holdout),
        images=[dict(file=v['file'],sha256=v['sha256']) for v in accepted],rejected=rejected,
        physical_depth_accuracy_verified=False,hardware_flight_verified=False)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--images',type=Path,required=True)
    p.add_argument('--camera-id',required=True)
    p.add_argument('--columns',type=int,required=True)
    p.add_argument('--rows',type=int,required=True)
    p.add_argument('--square-mm',type=float,required=True)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    files=sorted(f for f in args.images.iterdir() if f.suffix.lower() in ('.png','.jpg','.jpeg','.bmp'))
    result=calibrate(files,camera_id=args.camera_id,columns=args.columns,rows=args.rows,square_m=args.square_mm/1000)
    # No overwrite of a measured calibration or automatic flight config changes.
    with args.output.open('x',encoding='utf8') as stream:
        json.dump(result,stream,ensure_ascii=False,indent=2)
    print(f'Saved {args.output}; measured-distance check still required before pose-mode flight')


if __name__=='__main__':main()
