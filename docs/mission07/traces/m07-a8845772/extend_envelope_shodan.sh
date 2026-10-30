#!/bin/sh
# A lancer PAR L'UTILISATEUR (decision G2 humaine): etend l'enveloppe de l'enquete a8845772
# (ajoute Shodan, 1000 credits; tool_seconds relevé pour couvrir les anciennes reservations fuitees).
docker exec -i cti-bulletin-backend-1 python - <<'PY'
import os,json,urllib.request,urllib.error
INV="a8845772-5025-4702-8850-2b6be776855b"
base="http://127.0.0.1:8000/api/investigations/"+INV
tok=os.environ["OPERATOR_API_TOKEN"]
def call(method,path,body=None,idem=None):
    h={"Authorization":"Bearer "+tok,"Content-Type":"application/json"}
    if idem: h["Idempotency-Key"]=idem
    r=urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,method=method,headers=h)
    try: x=urllib.request.urlopen(r,timeout=60); return x.status,x.read().decode()
    except urllib.error.HTTPError as e: return e.code,e.read().decode()
st,b=call("GET","/envelope"); v=json.loads(b)["version"]; print("envelope version",v)
body={"expected_version":v,
 "allowed_providers":["bridge","vt","shodan"],
 "budget_limits":{"shodan_credits":1000,"tool_seconds":345600},
 "reason":"Decision utilisateur 2026-10-28: Shodan passif 1000 credits; tool_seconds releve (reservations Bridge historiques fuitees); suite de la politique OSINT_OPEN v2.",
 "correlation_id":"m07-env-shodan"}
st,b=call("POST","/envelope/extend",body,"m07-env-shodan-extend")
print(st); print(b[:1500])
PY
