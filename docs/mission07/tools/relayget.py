import json,time,sys,pathlib
R=pathlib.Path('/home/nill/perso/work/AutoWork/.autowork-runs/m07')
def get(i,path,purpose='tool health smoke'):
    (R/'inbox'/f'{i}.json').write_text(json.dumps({"id":i,"agent":"ORCHESTRATOR","method":"GET","path":path,"purpose":purpose}))
    for _ in range(120):
        time.sleep(1); f=R/'outbox'/f'{i}.json'
        if f.exists(): o=json.load(open(f)); return o['http'],o['body']
def post(i,tool,body):
    (R/'inbox'/f'{i}.json').write_text(json.dumps({"id":i,"agent":"ORCHESTRATOR","method":"POST","path":f"/operator/tools/{tool}","body":body}))
    for _ in range(3600):
        time.sleep(2); f=R/'outbox'/f'{i}.json'
        if f.exists(): o=json.load(open(f)); return o['http'],o['body']
