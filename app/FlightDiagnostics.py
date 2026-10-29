"""Passive flight evidence. No device commands, control decisions or extra sockets."""
from collections import deque
from drone_sdk import _driver


class FlightDiagnostics:
    def __init__(self, log):
        self.log = log
        _, self.cursor, _ = _driver.events_since(0)
        self.history = deque(maxlen=100)
        self.commands = deque(maxlen=100)
        self.last_descent = float('-inf')
        self.error = None

    def flush(self):
        # Called by the existing HUD and at device shutdown, never in UDP send.
        try:
            rows, cursor, missed = _driver.events_since(self.cursor)
            if missed:
                self.log({'event': 'flight_diagnostic_gap', 'missing_events': missed,
                          'after_sequence': self.cursor})
            for row in rows:
                self.log({'event': 'flight_diagnostic', 'record': row})
                self.observe(row)
                self.cursor = row['sequence']
            self.cursor = cursor
            self.error = None
        except Exception as exc:
            self.error = type(exc).__name__ + ': ' + str(exc)

    def observe(self, row):
        now = row['monotonic']
        if row['event'] == 'wire_sent':
            self.commands.append(row)
            return
        if row['event'] != 'telemetry_received':
            return
        state = row['state']
        height = state.get('h')
        if type(height) not in (int, float):
            return
        while self.history and now - self.history[0]['monotonic'] > 3:
            self.history.popleft()
        peak = max(self.history, key=lambda item: item['state']['h'], default=None)
        if peak is not None and peak['state']['h'] - height >= 30 and now-self.last_descent >= 3:
            self.last_descent = now
            recent = [item for item in self.commands if 0 <= now-item['monotonic'] <= 20]
            self.log({'event': 'flight_descent_observed', 'cause': 'undetermined',
                'physical_descent_verified': False,
                'trigger': 'telemetry_height_drop_at_least_30cm_within_3s',
                'drop_cm': peak['state']['h']-height,
                'duration_s': now-peak['monotonic'], 'before': peak, 'after': row,
                'recent_wire_commands': recent,
                'note': 'Compare h/tof/baro, attitude, acceleration, battery and motor time. '
                        'Local send does not prove aircraft receipt; SDK exposes no descent reason code.'})
        self.history.append(row)
