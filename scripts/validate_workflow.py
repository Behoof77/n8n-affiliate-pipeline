import json
import re
import sys

PATH = "workflows/meesho-reels-pipeline.json"
wf = json.load(open(PATH, encoding="utf-8"))
names = {n["name"] for n in wf["nodes"]}
issues = []

if len(names) != len(wf["nodes"]):
    issues.append("duplicate node names")

for src, c in wf["connections"].items():
    if src not in names:
        issues.append("dangling source: " + src)
    for outs in c.get("main", []):
        for t in outs:
            if t["node"] not in names:
                issues.append("dangling target: " + t["node"])

for n in wf["nodes"]:
    if n["type"] == "n8n-nodes-base.if":
        outs = wf["connections"].get(n["name"], {}).get("main", [])
        if len(outs) != 2 or not outs[0] or not outs[1]:
            issues.append("if-wiring %s (%d outs)" % (n["name"], len(outs)))
    if n["type"] == "n8n-nodes-base.wait" and not n.get("webhookId"):
        issues.append("no webhookId: " + n["name"])
    if "REPLACE_AFTER_IMPORT" in json.dumps(n.get("credentials", {})):
        pass
    else:
        if n["type"] in ("n8n-nodes-base.gmail", "n8n-nodes-base.httpRequest"):
            if n["name"].startswith(("Email", "Write Reel", "Generate", "Render")):
                issues.append("missing credentials: " + n["name"])

refs = set()
for n in wf["nodes"]:
    refs.update(re.findall(r"\$\('([^']+)'\)", json.dumps(n)))
for r in sorted(refs - names):
    issues.append("expression refs missing node: " + r)

for top in ("name", "nodes", "connections", "settings", "pinData", "active"):
    if top not in wf:
        issues.append("missing top-level key: " + top)

print("nodes:", len(wf["nodes"]))
print("refs:", sorted(refs))
print("issues:", issues if issues else "NONE")
sys.exit(1 if issues else 0)
