"""Actual local Node HTTP/client/Python WebSocket contract, without cloud/devices.

Only the RTC media engine, cloud invoker and aircraft are fixtures. Production
SessionController, RTCClient and RTCBridge exchange their real wire formats.
"""
import asyncio
import json
from pathlib import Path
import shutil
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'modules' / 'ai'))
sys.path.insert(0, str(ROOT / 'tests'))
from RTCBridge import RTCBridge
from RTCBridgeTests import Camera, Runtime


NODE = r"""
'use strict';
const assert = require('node:assert/strict');
const {once} = require('node:events');
const {createService,loadConfig} = require('./RTCService');
const {Client,decodeTLV} = require('./RTCClient');
const bridgeUrl=process.argv[1];
const actions=[];
const config=loadConfig({RTC_APP_ID:'123456789012345678901234',RTC_APP_KEY:'fake',
 VOLCENGINE_ACCESS_KEY_ID:'fake',VOLCENGINE_SECRET_ACCESS_KEY:'fake',
 VOLC_S2S_APP_ID:'fake',VOLC_S2S_TOKEN:'fake',RTC_VLM_MODEL:'fake',
 RTC_BRIDGE_URL:bridgeUrl,RTC_SESSION_SECONDS:'30'});
const service=createService({config,invoke:async(action)=>{
 actions.push(action);return{ResponseMetadata:{Action:action,RequestId:'fixture'}};}});
const replies=[];
const track={requestFrame(){},stop(){}};
const engine={on(){},async setAudioPlaybackDevice(){},async startAudioCapture(){},
 async setVideoSourceType(){},async setExternalVideoTrack(){},async joinRoom(){},
 async publishStream(){},async leaveRoom(){},
 async sendUserBinaryMessage(botId,raw){replies.push({botId,...decodeTLV(raw)});}};
globalThis.Image=class{constructor(){this.naturalWidth=16;this.naturalHeight=16;}
 set src(_){queueMicrotask(()=>this.onload());}};
const canvas={width:16,height:16,getContext(){return{fillRect(){},drawImage(){}};},
 captureStream(){return{getVideoTracks(){return[track];},getTracks(){return[track];}};}};
let client;
async function until(predicate){const deadline=Date.now()+5000;
 while(!predicate()){if(Date.now()>deadline)throw Error('contract_timeout');
 await new Promise(resolve=>setTimeout(resolve,10));}}
(async()=>{
 try{
 service.server.listen(0,'127.0.0.1');await once(service.server,'listening');
 const base='http://127.0.0.1:'+service.server.address().port;
 client=new Client({sdk:{createEngine(){return engine;},destroyEngine(){}},canvas,
  fetch:(url,options)=>fetch(base+url,options),WebSocket});
 await client.connect('fixtureInput','fixtureOutput');
 assert.equal(service.controller.session.status,'submitted');
 const identity=client.session;
 client.rtcMessage({type:'subv',value:{data:[{userId:identity.userId,
  paragraph:1,text:'check status',definite:true}]}});
 await until(()=>Number.isInteger(client.epoch));
 client.rtcMessage({type:'tool',value:{tool_calls:[{id:'contractCall',
  function:{name:'run_program',arguments:JSON.stringify({code:'print(drone.get_status())'})}}]}});
 await until(()=>replies.length===1);
 assert.equal(replies[0].type,'func');
 assert.equal(replies[0].botId,identity.botId);
 assert.equal(replies[0].value.ToolCallID,'contractCall');
 const result=JSON.parse(replies[0].value.Content);
 assert.equal(result.ok,true);assert.equal(result.status,'completed');
 assert.equal(result.flight_state,'landed');
 // Same native call must not execute again after an identical retransmission.
 client.rtcMessage({type:'tool',value:{tool_calls:[{id:'contractCall',
  function:{name:'run_program',arguments:JSON.stringify({code:'print(drone.get_status())'})}}]}});
 await new Promise(resolve=>setTimeout(resolve,50));
 assert.equal(replies.length,1);
 await client.stop('contract complete');
 assert.deepEqual(actions,['StartVoiceChat','StopVoiceChat']);
 console.log(JSON.stringify({ok:true,actions,result,cloud:false,hardware:false}));
 }finally{if(client)await client.stop();await service.close();}
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
"""


class RTCContractTests(unittest.IsolatedAsyncioTestCase):
    async def test01_actual_node_session_and_client_round_trip_through_python_bridge(self):
        if not shutil.which('node'):
            self.skipTest('Node is not installed')
        runtime = Runtime()
        events = []
        bridge = RTCBridge(runtime, Camera(), events.append, max_session_seconds=30)
        await bridge.start()
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                'node', '-e', NODE, bridge.url, cwd=ROOT / 'modules' / 'rtc',
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(process.communicate(), 15)
            self.assertEqual(process.returncode, 0, err.decode('utf-8', errors='replace'))
            result = json.loads(out.decode('utf-8'))
            self.assertTrue(result['ok'])
            self.assertFalse(result['cloud'])
            self.assertFalse(result['hardware'])
            self.assertEqual([call for call in runtime.calls if call[0] == 'run'],
                             [('run', 'print(drone.get_status())')])
            self.assertTrue(any(event.get('event') == 'tool_result' for event in events))
            bound = [event for event in events if event.get('event') == 'rtc_session_bound']
            self.assertEqual(len(bound), 1)
            for name in ('sessionId', 'roomId', 'taskId', 'botId', 'userId'):
                self.assertIsInstance(bound[0][name], str)
                self.assertTrue(bound[0][name])
            self.assertTrue(bound[0]['timestamp'].endswith('Z'))
            self.assertNotIn(bridge.token, json.dumps(bound))
            self.assertNotIn(bridge.url, json.dumps(bound))
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            await bridge.close()


if __name__ == '__main__':
    unittest.main()
