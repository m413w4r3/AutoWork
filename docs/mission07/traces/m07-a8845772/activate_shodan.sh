#!/bin/sh
# A lancer PAR L'UTILISATEUR (decision humaine): etend OSINT_OPEN dc034634 v1 -> v2 avec Shodan 1000 credits.
# Le jeton humain est lu dans l'environnement du conteneur backend et n'est jamais affiche.
docker exec -i cti-bulletin-backend-1 python - <<'PY'
import os,json,urllib.request,urllib.error
h={"Authorization":"Bearer "+os.environ["OPERATOR_API_TOKEN"],"Content-Type":"application/json","Idempotency-Key":"m07-shodan-extend-v2"}
body={
 "policy_id":"dc034634-0e36-57cd-ba4e-a61bb2bb736a","expected_version":1,
 "capabilities":{
  "allowed_providers":["bridge","vt","shodan"],
  "budget_limits":{"tool_seconds":86400,"vt_units":10000,"downloads":1000,"bridge_turns":200,
                   "context_bytes":67108864,"context_tokens":16000000,"shodan_credits":1000},
  "acquire_hits_allowed":True,
  "disclosure":{"tlp":"clear","do_not_submit":False,"external_llm_allowed":True},
  "max_effect_claims_per_investigation":500},
 "reason":"Decision utilisateur 2026-10-28: activer Shodan (passif) avec 1000 credits pour la mission 07; budgets inchanges par ailleurs.",
 "correlation_id":"m07-shodan-extend"}
r=urllib.request.Request("http://127.0.0.1:8000/api/osint-open-policy/extend",data=json.dumps(body).encode(),method="POST",headers=h)
try: x=urllib.request.urlopen(r,timeout=60); print(x.status); print(x.read().decode()[:1500])
except urllib.error.HTTPError as e: print(e.code); print(e.read().decode()[:1500])
PY
