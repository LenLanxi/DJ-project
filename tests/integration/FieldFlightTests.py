"""Exercise the physical-test program with fake telemetry; never open hardware."""
import contextlib
import io
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


class FieldFlightTests(unittest.TestCase):
    def test_protocol_waits_for_late_takeoff_reply_and_confirms_landing(self):
        import FieldProtocol as protocol
        clock=types.SimpleNamespace(now=0.)
        sent=[]; replies=[]; state={'takeoff':None,'land':None}; events=[]
        class Log:
            path=Path('memory')
            def __call__(self,event): events.append(event)
            def close(self): pass
        class Socket:
            def bind(self,address): self.port=address[1]
            def close(self): pass
            def sendto(self,data,address):
                text=data.decode();sent.append((text,clock.now))
                if text=='takeoff':state['takeoff']=clock.now
                if text=='land' and state['land'] is None:state['land']=clock.now
                replies.append((clock.now+(6 if text=='takeoff' else .1),b'ok'))
            def recvfrom(self,size):
                if self.port==8889:
                    index=next(i for i,value in enumerate(replies) if value[0]<=clock.now)
                    return replies.pop(index)[1],('192.168.10.1',8889)
                airborne=(state['takeoff'] is not None and clock.now>=state['takeoff']+4
                          and (state['land'] is None or clock.now<state['land']+2))
                h,tof=(60,80) if airborne else (0,10)
                return f'h:{h};tof:{tof};bat:80;templ:40;temph:41;'.encode(),('192.168.10.1',8890)
        def select_ready(inputs,*args):
            clock.now+=.1
            ready=[value for value in inputs if value.port==8890 or any(t<=clock.now for t,_ in replies)]
            return ready,[],[]
        key_api=types.SimpleNamespace(windll=types.SimpleNamespace(user32=types.SimpleNamespace(GetAsyncKeyState=lambda key:0)))
        with patch.object(protocol.socket,'socket',side_effect=lambda *args:Socket()), \
             patch.object(protocol.select,'select',side_effect=select_ready), \
             patch.object(protocol,'time',types.SimpleNamespace(monotonic=lambda:clock.now)), \
             patch.object(protocol,'ctypes',key_api),patch.object(protocol,'SessionLog',return_value=Log()), \
             patch.object(sys,'argv',['FieldProtocol.py','--allow-takeoff','--send-up-20','--min-battery','0']), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(protocol.main(),0)
        takeoff=next(t for command,t in sent if command=='takeoff')
        rise=next(t for command,t in sent if command=='up 20')
        self.assertGreaterEqual(rise-takeoff,6)
        self.assertEqual(sum(command=='up 20' for command,_ in sent),1)
        self.assertTrue(any(event['event']=='protocol_landing_result' and event['ground_telemetry_confirmed'] for event in events))

    def test_rise_is_not_an_alternative_to_confirmed_takeoff(self):
        from FieldProtocol import rise_allowed
        for packet in ({'h':0,'tof':10},{'h':None,'tof':None},
                       {'h':0,'tof':8192},{'h':240,'tof':240}):
            self.assertFalse(rise_allowed(packet),packet)
        self.assertTrue(rise_allowed({'h':80,'tof':80}))
