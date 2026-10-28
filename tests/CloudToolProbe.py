"""Opt-in cloud protocol comparison. Never executes generated code or aircraft tools."""
import argparse
import ast
import asyncio
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
import uuid

from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'modules'/'ai'))
from AlibabaRealtime import AlibabaConfig, normalize_tools, session_update, strict_json, validate_arguments
from main import OMNI_INSTRUCTIONS, OMNI_TOOLS

FOLDER = ROOT/'logs'/'CloudProbe'
SHORT = ('根据用户要求调用工具生成无人机程序。run_program必须提供code字符串，内容是完整Python。'
         '通过drone.observe("桌子")观察目标；观察不清或有歧义就报告，不能猜测距离或已到达。'
         '需要移动时使用drone.move_forward(20)等20厘米短步，每步重新观察，设置总步数和时间上限。'
         '可用drone.get_status()查询，print记录结果。用户要求降落用interrupt(land=true)。'
         '没有真实执行结果不能声称成功。')

def stamp():
    return datetime.now(timezone.utc).isoformat()

def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')

def estimate(usage):
    incoming=usage.get('input_tokens_details',{})
    outgoing=usage.get('output_tokens_details',{})
    audio_in=incoming.get('audio_tokens',0)
    audio_out=outgoing.get('audio_tokens',0)
    return ((usage.get('input_tokens',0)-audio_in)*1.5+audio_in*6+
            outgoing.get('text_tokens',max(0,usage.get('output_tokens',0)-audio_out))*4.5+audio_out*12)/1e6

def variant(name):
    instructions=OMNI_INSTRUCTIONS
    definitions=copy.deepcopy(OMNI_TOOLS)
    user='请移动到前方的桌子正上空。'
    if name=='SimpleSchema':
        for tool in definitions:
            tool['parameters'].pop('additionalProperties',None)
            for prop in tool['parameters']['properties'].values(): prop.pop('maxLength',None)
    elif name in ('ShortPrompt','PlainCode','PlainCodeTextOnly'):
        instructions=SHORT
        if name.startswith('PlainCode'):
            definitions=[]
            instructions+='本轮不提供工具；直接以纯文本输出你原本要放入code字段的完整Python，不执行它。'
    elif name=='StringControl':
        instructions='这是字符串传参测试。按用户要求调用echo，不要省略必填参数。'
        definitions=[{'type':'function','name':'echo','description':'记录字符串，不执行任何动作',
                      'parameters':{'type':'object','properties':{'value':{'type':'string'}},'required':['value']}}]
        user='请调用echo，把字符串“桌子正上方 Probe731”放入value参数。'
    elif name=='CurrentNoSpeech':
        instructions+='需要工具时直接产生工具调用，不先口头描述意图；收到工具结果后再向用户说话。'
    elif name=='SpatialReasoningTextOnly':
        definitions=[]
        user=('这是离线推理题，不执行动作：已知前视相机看到桌子，visible=true，ambiguous=false，'
              'center_x=0.5，width_fraction=0.6，飞机高度70厘米；没有桌高、深度、相机俯仰角或水平位置。'
              '能否据此确认飞机就在桌子正上方？能否算出保证到达正上方的前进距离和上升距离？请明确说明依据和缺失信息。')
    elif name not in ('Current','CurrentTextOnly'): raise ValueError('unknown_case')
    return instructions,normalize_tools(definitions),user

def inspect_call(call, tools):
    result={'name':call.get('name'),'call_id':call.get('call_id'),'raw':call.get('arguments')}
    try:
        args=strict_json(result['raw'])
        schema=next(t['function']['parameters'] for t in tools if t['function']['name']==result['name'])
        validate_arguments(args,schema)
        result['schema_valid']=True
        if result['name']=='run_program':
            ast.parse(args['code'])
            result['python_valid']=True
    except Exception as exc:
        result['error']=type(exc).__name__+': '+str(exc)[:1000]
    return result

async def trial(args, config, name, index, budget):
    instructions,definitions,user=variant(name)
    sample={'case':name,'repeat':index,'started':stamp(),'model':config.model,
            'input_mode':'text_no_camera_no_microphone','hardware_connected':False,
            'generated_code_executed':False,'responses':[],'events':[], 'user':user}
    hidden=(config.api_key,config.workspace_id)
    def redact(value):
        if isinstance(value,dict): return {k:redact(v) for k,v in value.items()}
        if isinstance(value,list): return [redact(v) for v in value]
        if isinstance(value,str):
            for secret in hidden: value=value.replace(secret,'[redacted]')
        return value
    started=time.monotonic()
    async def send(event):
        await ws.send(json.dumps(dict(event,event_id='Probe'+uuid.uuid4().hex),ensure_ascii=False))
        sample['events'].append({'direction':'client','type':event['type'],'elapsed':time.monotonic()-started})
    async def receive():
        event=strict_json(await asyncio.wait_for(ws.recv(),25))
        kind=event.get('type')
        sample['events'].append({'direction':'server','type':kind,'elapsed':time.monotonic()-started})
        if kind=='error':
            sample['provider_error']=redact(event.get('error'))
            raise RuntimeError('provider_error')
        return event
    async def generate():
        if budget['submitted']>=budget['max_generations']: raise RuntimeError('probe_generation_limit')
        budget['submitted']+=1
        save(FOLDER/'Budget.json',budget)
        await send({'type':'response.create'})
        result={'argument_events':[],'deltas':{},'output_items':[],'text':''}
        while True:
            event=await receive()
            kind=event.get('type')
            if kind=='response.function_call_arguments.delta':
                cid=event.get('call_id',event.get('item_id','unknown'))
                result['deltas'][cid]=result['deltas'].get(cid,'')+event.get('delta','')
            elif kind=='response.function_call_arguments.done':
                result['argument_events'].append(inspect_call(event,definitions))
            elif kind=='response.output_item.done':
                item=event.get('item',{})
                if item.get('type')=='function_call': result['output_items'].append(inspect_call(item,definitions))
            elif kind in ('response.text.delta','response.audio_transcript.delta'):
                result['text']+=event.get('delta','')
            elif kind=='response.done':
                response=event.get('response',{})
                result.update({key:response.get(key) for key in ('id','status','status_details','usage')})
                result['final_calls']=[inspect_call(item,definitions) for item in response.get('output',[])
                                       if item.get('type')=='function_call']
                usage=response.get('usage') or {}
                budget['estimated_rmb']+=estimate(usage)
                save(FOLDER/'Budget.json',budget)
                sample['responses'].append(redact(result))
                return result
    try:
        async with asyncio.timeout(100):
            async with connect(config.validate(),additional_headers={'Authorization':'Bearer '+config.api_key},
                               proxy=None,max_size=2000000,open_timeout=10,close_timeout=2) as ws:
                if (await receive()).get('type')!='session.created': raise RuntimeError('missing_session_created')
                update=session_update(config,instructions,definitions,mode='server_vad',silence_ms=1200)
                update['session']['turn_detection'].update(create_response=False,interrupt_response=False)
                update['session']['max_tokens']=args.tokens
                if name.endswith('TextOnly'): update['session']['modalities']=['text']
                sample['requested']=redact(update['session'])
                await send(update)
                while True:
                    event=await receive()
                    if event.get('type')=='session.updated':
                        sample['effective']=redact({key:event.get('session',{}).get(key) for key in
                            ('model','tools','instructions','max_tokens','modalities','turn_detection',
                             'temperature','top_p','top_k','repetition_penalty','presence_penalty')})
                        sample['schema_echo_equal']=sample['effective']['tools']==sample['requested']['tools']
                        break
                await send({'type':'conversation.item.create','item':{'type':'message','role':'user',
                            'content':[{'type':'input_text','text':user}]}})
                while True:
                    event=await receive()
                    item=event.get('item',{})
                    if event.get('type')=='conversation.item.created' and item.get('role')=='user':
                        sample['user_echo']=redact(item)
                        break
                result=await generate()
                invalid=[c for c in result['argument_events'] if c.get('error')]
                if invalid and args.feedback:
                    for call in result['argument_events']:
                        error=call.get('error','Another tool in this response has invalid arguments.')
                        feedback={'ok':False,'status':'failed','reason':'invalid_tool_arguments','debug':error}
                        await send({'type':'conversation.item.create','item':{'type':'function_call_output',
                                    'call_id':call['call_id'],'output':json.dumps(feedback,ensure_ascii=False)}})
                    sample['feedback']='same_local_error_format_no_execution'
                    await generate()
    except Exception as exc:
        sample['failure']=redact(type(exc).__name__+': '+str(exc)[:1000])
    sample['finished']=stamp()
    namepart=name+str(index)+datetime.now().strftime('%Y%m%dT%H%M%S')
    path=FOLDER/(namepart+'.json')
    save(path,redact(sample))
    print(json.dumps({'case':name,'repeat':index,'file':str(path),'echo_equal':sample.get('schema_echo_equal'),
          'failure':sample.get('failure'),'responses':[{'status':r['status'],'calls':len(r['argument_events']),
          'valid':sum(c.get('schema_valid',False) and not c.get('error') for c in r['argument_events']),
          'errors':[c.get('error') for c in r['argument_events'] if c.get('error')],
          'text':r['text'][:160],'usage':r['usage']} for r in sample['responses']]},ensure_ascii=False),flush=True)

async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases',nargs='+',default=['Current','SimpleSchema','ShortPrompt','PlainCode','StringControl'])
    parser.add_argument('--repeat',type=int,default=1,choices=range(1,4))
    parser.add_argument('--tokens',type=int,default=2048,choices=[2048,4096])
    parser.add_argument('--feedback',action='store_true')
    parser.add_argument('--credentials',type=Path,default=Path.home()/'Desktop'/'tmp'/'DomesticAiTrials'/'AlibabaCredentials.json')
    args=parser.parse_args()
    config=AlibabaConfig.from_credentials(args.credentials,paid_budget_rmb=3)
    FOLDER.mkdir(parents=True,exist_ok=True)
    budget_path=FOLDER/'Budget.json'
    budget=json.loads(budget_path.read_text(encoding='utf-8')) if budget_path.exists() else {
        'max_generations':20,'submitted':0,'estimated_rmb':0,'actual_bill_verified':False,
        'prices_source':'https://help.aliyun.com/zh/model-studio/qwen3-8-omni-flash-realtime',
        'hardware_connected':False}
    save(budget_path,budget)
    for index in range(1,args.repeat+1):
        for name in args.cases: await trial(args,config,name,index,budget)

if __name__=='__main__': asyncio.run(main())
