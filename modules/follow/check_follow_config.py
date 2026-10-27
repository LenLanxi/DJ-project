"""Read-only Group 3 configuration check; never import the SDK or open devices.

Require an explicit file or R3_FOLLOW_CONFIG, so this check cannot accidentally
approve the backward-compatible legacy default. Success means configuration
checks passed, never that aircraft binding, camera accuracy or flight passed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from r3_follow.config import load_config
from r3_follow.pose_depth import PoseDepth
from r3_follow.mock_port import MockPort
from r3_follow.service import FollowService
from r3_follow import __version__


def check_files(paths=None, *, image_size=None, camera_id=None, expected_ids=None):
    if not paths:
        selected = os.environ.get('R3_FOLLOW_CONFIG')
        if not selected:
            return dict(ok=False, errors=['未指定配置；正式服务会回到 legacy，不能当作已启用新 PID。'],
                        configs=[], warnings=[], hardware_verified=False)
        paths = [selected]
    results, errors, warnings = [], [], []
    identities, targets = set(), set()
    for name in paths:
        item = dict(file=str(Path(name).absolute()), errors=[], warnings=[])
        results.append(item)
        try:
            raw = Path(name).read_bytes()
            c = load_config(name)
            item.update(sha256=hashlib.sha256(raw).hexdigest(),effective=c.to_dict())
            if c.instance_id in identities:
                item['errors'].append('多机配置重复使用 instance_id：'+c.instance_id)
            if c.target_id in targets:
                item['errors'].append('多机配置重复绑定标记 ID：'+str(c.target_id))
            identities.add(c.instance_id);targets.add(c.target_id)
            if c.control_profile != 'openpose_pid':
                item['errors'].append('此配置没有启用本次 openpose_pid 跟随计算。')
            if c.distance_mode == 'pose':
                pose = PoseDepth(c.camera_calibration,c.marker_side_m,camera_id=c.camera_id)
                data = json.loads(Path(c.camera_calibration).read_text(encoding='utf-8-sig'))
                source = str(data.get('source','')).lower()
                if any(v in source for v in ('synthetic','invented','simulation')):
                    item['errors'].append('虚拟相机标定不能用于真实相机。')
                if camera_id is not None and camera_id != c.camera_id:
                    item['errors'].append('待接相机名称与标定相机不一致。')
                if image_size is not None and tuple(image_size) != pose.size:
                    item['errors'].append('待接画面分辨率与相机标定不一致。')
                if camera_id is None or image_size is None:
                    item['warnings'].append('未提供实际相机名称和分辨率，尚未确认与标定匹配。')
                quality = data.get('view_conditioning')
                if not isinstance(quality,dict):
                    item['warnings'].append('旧标定没有多角度质量检查记录，需重新检查原照片。')
                item['warnings'].append('需用尺量距离核对测距误差；低重投影误差不能证明米数正确。')
                item['calibration_sha256'] = pose.sha256
            elif c.distance_mode == 'relative_size':
                item['warnings'].append('当前按标记大小保持相对距离；侧转仍可能暂停前后，不输出实测米数。')
            if c.vertical_follow:
                item['warnings'].append('真实高度及其采集时间未检查；缺失或过期时自动升降会被拦住。')
            if c.steering_mode == 'yaw':
                item['warnings'].append('yaw 模式只转向对准，左右平移请求为 0。')
            # Exercise the actual constructor/clamps without creating workers,
            # requests or importing any upstream SDK. No flight starts here.
            service = FollowService(MockPort(),log_directory=False,**c.to_dict())
            item['actual_service_rc_caps'] = list(service.rc_caps)
            item['actual_pid'] = service.controller.status()['openpose_control']
            item['source_version'] = __version__
            item['source_directory'] = str(Path(__file__).resolve().parent)
            item['actual_depth_brake'] = service.controller.status()['depth_brake']
            item['depth_brake_enabled'] = c.control_profile == 'openpose_pid' and c.distance_mode != 'apparent_pixels'
            if not item['depth_brake_enabled']:
                item['warnings'].append('本配置未启用前后趋势制动；需选 relative_size 或已实际标定的 pose。')
            service.close(wait=False)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            item['errors'].append(str(exc))
        errors.extend(f'{name}: {e}' for e in item['errors'])
        warnings.extend(f'{name}: {e}' for e in item['warnings'])
    if expected_ids is not None and targets != set(expected_ids):
        errors.append(f'选中 ID {sorted(targets)} 与要求 {sorted(set(expected_ids))} 不一致。')
    warnings.append('这是静态配置检查；三机设备绑定、控制权、网络、高度时效与物理响应尚未实测。')
    return dict(ok=not errors,errors=errors,warnings=warnings,configs=results,
                hardware_verified=False,configuration_scope_only=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',action='append',type=Path)
    p.add_argument('--image-size',type=int,nargs=2,metavar=('WIDTH','HEIGHT'))
    p.add_argument('--camera-id')
    p.add_argument('--expected-ids',type=int,nargs='+')
    p.add_argument('--output',type=Path)
    a=p.parse_args()
    if sys.version_info[:2]!=(3,11):raise SystemExit('Use Python 3.11')
    report=check_files(a.config,image_size=a.image_size,camera_id=a.camera_id,expected_ids=a.expected_ids)
    result=json.dumps(report,ensure_ascii=False,indent=2)
    if a.output:
        with a.output.open('x',encoding='utf8') as f:f.write(result)
    print(result)
    return 0 if report['ok'] else 2


if __name__=='__main__':raise SystemExit(main())
