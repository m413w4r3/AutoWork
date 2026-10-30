import os,sys,json,urllib.request,urllib.error
tok=os.environ["OPERATOR_API_TOKEN"]
method,path=sys.argv[1],sys.argv[2]
body=sys.argv[3] if len(sys.argv)>3 else None
r=urllib.request.Request("http://127.0.0.1:8000"+path,data=body.encode() if body else None,method=method,headers={"Authorization":"Bearer "+tok,"Content-Type":"application/json"})
try:
    x=urllib.request.urlopen(r,timeout=300);print(x.status);print(x.read().decode())
except urllib.error.HTTPError as e:
    print(e.code);print(e.read().decode())
