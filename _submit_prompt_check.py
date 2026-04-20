import json, time, urllib.request, urllib.error
from pathlib import Path

wf_path = Path(r"D:\1Repo\Github\ComfyUI\Workflow\Z-image超真实动漫转真人.json")
base = "http://127.0.0.1:8000"

wf = json.loads(wf_path.read_text(encoding="utf-8"))
links = {int(l[0]): l for l in wf.get("links", [])}
control_tokens = {"randomize", "fixed", "increment", "decrement"}

def compatible(v, t):
    if t == "INT":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "FLOAT":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t == "BOOLEAN":
        return isinstance(v, bool)
    if t in ("STRING", "COMBO"):
        return isinstance(v, str)
    return True

prompt = {}
for n in wf.get("nodes", []):
    nid = str(n["id"])
    inputs = {}
    vals = list(n.get("widgets_values", []))
    vi = 0
    for inp in n.get("inputs", []):
        name = inp.get("name")
        if not name:
            continue
        link_id = inp.get("link")
        if link_id is not None:
            lk = links.get(int(link_id))
            if lk:
                inputs[name] = [str(lk[1]), int(lk[2])]
            continue
        if "widget" in inp:
            t = inp.get("type", "")
            chosen = None
            while vi < len(vals):
                v = vals[vi]
                if isinstance(v, str) and v in control_tokens and t in ("INT", "FLOAT", "BOOLEAN"):
                    vi += 1
                    continue
                if compatible(v, t):
                    chosen = v
                    vi += 1
                    break
                vi += 1
            if chosen is not None:
                inputs[name] = chosen
    prompt[nid] = {"class_type": n.get("type"), "inputs": inputs, "_meta": {"title": n.get("title", n.get("type", ""))}}

payload = {"client_id": "copilot-runner", "prompt": prompt}
req = urllib.request.Request(url=f"{base}/prompt", data=json.dumps(payload).encode("utf-8"), headers={"Content-Type":"application/json"}, method="POST")
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode("utf-8", errors="ignore")
        print('SUBMIT_STATUS', r.status)
        print(body)
except urllib.error.HTTPError as e:
    body = e.read().decode("utf-8", errors="ignore")
    print('SUBMIT_HTTP_ERROR', e.code)
    print(body)
