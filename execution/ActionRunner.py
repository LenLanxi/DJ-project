"""Execute one validated action; model input is data and never Python source."""
import asyncio
from dataclasses import dataclass, field

from app.Tools import validate_action
from execution.Host import SDKActionError


@dataclass
class ActionOutcome:
    result: dict = field(default_factory=dict)
    executing: bool = True


class ActionRunner:
    def __init__(self, timeout=90):
        if not 0 < timeout <= 900:
            raise ValueError('action timeout must be between 0 and 900 seconds')
        self.timeout = timeout

    async def run(self, request, host, outcome):
        validate_action(request)
        journal = []
        outcome.result = result = {'ok': False, 'status': 'failed', 'action': request['action'],
                                   'journal': journal}

        async def dispatch(method, args):
            row = {'method': method, 'args': args}
            journal.append(row)
            try:
                value = await host.dispatch(method, args, {})
                row['result'] = value
                return value
            except SDKActionError as exc:
                row['result'] = exc.result
                raise
            except BaseException as exc:
                row['error'] = type(exc).__name__ + ': ' + str(exc)
                raise

        try:
            async with asyncio.timeout(self.timeout):
                result['action_result'] = await dispatch(request['action'], request['args'])
                if 'adjust_follow_reference' in request:
                    result['adjustment'] = await dispatch('adjust_follow_reference',
                                                         [request['adjust_follow_reference']])
            result.update(ok=True, status='completed')
        except asyncio.CancelledError:
            host.cancel()
            result.update(status='cancelled', reason='action_cancelled')
            raise
        except TimeoutError:
            host.cancel()
            result['reason'] = 'action_timeout; completion not confirmed'
        except SDKActionError as exc:
            result.update(reason=exc.result.get('reason', str(exc)), action_error=exc.result)
        except Exception as exc:
            result['reason'] = type(exc).__name__ + ': ' + str(exc)
        finally:
            outcome.executing = False
