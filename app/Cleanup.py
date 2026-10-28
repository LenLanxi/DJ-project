"""Collect shutdown evidence without abandoning later cleanup steps."""
import asyncio

def cleanup_record(outcome, log, step, result=None, error=None):
    """Preserve all cleanup evidence; None is valid for void stop/close APIs."""
    failed = isinstance(result, dict) and result.get('ok') is False
    record = {'step': step, 'ok': not (failed or error is not None)}
    cleaner = getattr(log, 'clean', None)
    def clean(value):
        if not callable(cleaner):
            return value
        try:
            return cleaner(value)
        except Exception:
            return '[cleanup detail unavailable]'
    if result is not None:
        record['result'] = clean(result)
    if error is not None:
        record['error'] = clean(str(error) or type(error).__name__)
    outcome['steps'].append(record)
    outcome['ok'] = outcome['ok'] and record['ok']
    try:
        log(dict(record, event='cleanup_step'))
    except Exception:
        outcome['ok'] = False
    if not record['ok']:
        print('[清理] %s 未确认：%s' % (step, record.get('error', record.get('result'))), flush=True)
    return record['ok']


async def cleanup_step(outcome, log, step, operation, timeout):
    try:
        result = await asyncio.wait_for(operation(), timeout)
    except asyncio.CancelledError:
        if asyncio.current_task().cancelling():
            raise
        cleanup_record(outcome, log, step, error=RuntimeError('cleanup_operation_cancelled'))
        return None
    except Exception as exc:
        cleanup_record(outcome, log, step, error=exc)
        return None
    cleanup_record(outcome, log, step, result=result)
    return result
