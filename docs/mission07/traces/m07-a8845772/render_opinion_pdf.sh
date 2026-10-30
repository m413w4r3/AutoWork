#!/bin/sh
# A lancer PAR L'UTILISATEUR (rendu de lecture d'un BROUILLON, aucun effet sur l'etat, aucune publication).
# Rend le PDF de la version courante de l'avis et l'ecrit dans .autowork-runs/m07/editorial/.
# Le jeton humain est lu dans l'environnement du conteneur backend et n'est jamais affiche.
mkdir -p "$(dirname "$0")/editorial"
docker exec -i cti-bulletin-backend-1 python - <<'PY' > "$(dirname "$0")/editorial/.render.out"
import os,json,urllib.request,urllib.error,sys,base64
INV="a8845772-5025-4702-8850-2b6be776855b"
tok=os.environ["OPERATOR_API_TOKEN"]
def call(path):
    r=urllib.request.Request("http://127.0.0.1:8000"+path,headers={"Authorization":"Bearer "+tok})
    try:
        x=urllib.request.urlopen(r,timeout=600);return x.status,x.read(),x.headers
    except urllib.error.HTTPError as e: return e.code,e.read(),e.headers
st,b,_=call(f"/api/investigations/{INV}/opinion")
if st!=200: print(json.dumps({"error":"opinion","status":st,"body":b.decode()[:300]}));sys.exit(0)
d=json.loads(b)["draft"]
oid,ver,sha,base=d["id"],d["version"],d["content_sha256"],d["base_publication_sha256"]
st,pdf,h=call(f"/api/opinions/{oid}/preview.pdf?version={ver}&opinion_sha256={sha}&base_publication_sha256={base}")
print(json.dumps({"status":st,"opinion_id":oid,"version":ver,"opinion_sha256":sha,"pdf_sha256":h.get("X-PDF-SHA256"),
 "error":(pdf.decode()[:400] if st!=200 else None),"pdf_b64":(base64.b64encode(pdf).decode() if st==200 else None)}))
PY
python3 - "$(dirname "$0")/editorial" <<'PY'
import json,sys,base64,pathlib
d=pathlib.Path(sys.argv[1]);o=json.loads((d/'.render.out').read_text())
if o.get("status")!=200: print("ECHEC",{k:v for k,v in o.items() if k!='pdf_b64'});sys.exit(1)
f=d/f"opinion-v{o['version']}-draft.pdf";f.write_bytes(base64.b64decode(o['pdf_b64']))
print("OK",f,"opinion sha",o['opinion_sha256'][:16],"pdf sha",o['pdf_sha256'])
(d/'.render.out').unlink()
PY
