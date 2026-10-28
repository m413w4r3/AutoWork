import sys,json,time,os;sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)));from relayget import *
def ver():
    h,b=get(f'orch-v{int(time.time()*1000)}','/operator-status','version');d=json.loads(b);return d['working_state_version']
def run(tag,tool,args,purpose):
    i=f'orch-sm-{tag}-{int(time.time())}'
    h,b=post(i,tool,{"schema_version":"1","arguments":args,"purpose":"Orchestrator capability smoke (no analytical decision): "+purpose,"expected_version":ver()})
    try:
        d=json.loads(b);inv=d.get('invocation') or {}
        print(f'{tag}: http={h} status={inv.get("status")} err={inv.get("error_code")} keys={list(d.keys())[:6]}')
        for k in('result','tool_result','output','advisor_output'):
            if k in d: print('   ',k,json.dumps(d[k],ensure_ascii=False)[:500])
        return d
    except Exception as e: print(tag,h,b[:400]);return None
