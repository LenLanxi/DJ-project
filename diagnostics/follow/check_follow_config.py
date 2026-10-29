"""Read-only follow configuration check; never connect or open devices.

Require an explicit file or R3_FOLLOW_CONFIG, so this check cannot accidentally
approve an unintended configuration. Success means configuration
checks passed, never that aircraft binding, camera accuracy or flight passed.
"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'follow'
sys.path.insert(0, str(ROOT))

import argparse
import hashlib
import json
import os
from follow.r3_follow.config import load_config
from follow.r3_follow import __version__


def check_files(paths=None, *, expected_ids=None):
    if not paths:
        selected = os.environ.get('R3_FOLLOW_CONFIG')
        if not selected:
            return dict(ok=False, errors=['未指定配置文件。'],
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
            if c.vertical_follow:
                item['warnings'].append('真实高度及其采集时间未检查；缺失或过期时自动升降会被拦住。')
            item['source_version'] = __version__
            item['source_directory'] = str(SOURCE)
            item['depth_brake_enabled'] = True
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
    p.add_argument('--expected-ids',type=int,nargs='+')
    p.add_argument('--output',type=Path)
    a=p.parse_args()
    if sys.version_info[:2]!=(3,11):raise SystemExit('Use Python 3.11')
    report=check_files(a.config,expected_ids=a.expected_ids)
    result=json.dumps(report,ensure_ascii=False,indent=2)
    if a.output:
        with a.output.open('x',encoding='utf8') as f:f.write(result)
    print(result)
    return 0 if report['ok'] else 2


if __name__=='__main__':raise SystemExit(main())
