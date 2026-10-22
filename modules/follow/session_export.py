"""Export one explicitly selected closed session. No upload or desktop calls."""
import argparse
import hashlib
import json
import tempfile
import time
import zipfile
from pathlib import Path

from log_report import read_rows, diagnostic_report, session_report, write_report


def export_session(path, destination=None):
    path = Path(path).resolve()
    if not path.name.startswith('demo_') or path.suffix != '.jsonl':
        raise ValueError('请选择demo_开头的会话日志')
    rows, _ = read_rows(path)
    if not any(r.get('event') == 'session_summary' for r in rows):
        raise ValueError('请先关闭测试窗口，再导出已结束的会话')
    linked, skipped = set(), []
    for row in rows:
        name = row.get('diagnostic_log')
        if not isinstance(name, str):
            continue
        if Path(name).name != name or not name.startswith('diagnostic_') or not name.endswith('.jsonl'):
            skipped.append(name)
            continue
        child = path.parent/name
        if not child.is_file() or not child.resolve().is_relative_to(path.parent):
            skipped.append(name)
            continue
        try:
            if diagnostic_report(child)['context'].get('session_log') != path.name:
                skipped.append(name)
                continue
        except (OSError, ValueError):
            skipped.append(name)
            continue
        linked.add(child)
    folder = Path(destination) if destination else path.parent/'exports'
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder/f'{path.stem}_反馈_{time.time_ns()}.zip'
    with tempfile.TemporaryDirectory() as td:
        snapshot = Path(td)
        for source in (path, *sorted(linked)):
            (snapshot/source.name).write_bytes(source.read_bytes())
            if source != path:
                report_txt = source.with_suffix('.txt')
                if report_txt.is_file():
                    (snapshot/report_txt.name).write_bytes(report_txt.read_bytes())
        summary = write_report(snapshot/path.name)
        report = session_report(snapshot/path.name)
        (snapshot/'测试条件请补充.txt').write_text(
            '标记介质（打印件/手机图）：\n光线：\n大致距离：\n操作步骤：\n异常现象：\n'
            '不需要另写报告，也不需要提供人脸视频。\n', encoding='utf-8-sig')
        files = sorted(p for p in snapshot.rglob('*') if p.is_file())
        metadata = dict(session=path.name, version=report['version'], mode=report['source'],
                        original_log=str(path), skipped_unverified_links=skipped,
                        files={p.relative_to(snapshot).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in files})
        (snapshot/'manifest.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
        with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED) as z:
            for p in (*files, snapshot/'manifest.json'):
                z.write(p,p.relative_to(snapshot).as_posix())
    return archive


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('log',type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    print(export_session(args.log,args.output))
