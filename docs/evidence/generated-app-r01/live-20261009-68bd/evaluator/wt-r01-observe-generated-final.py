import sys,json,asyncio,hashlib
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,'/Users/a/code/databricks-labs-workshop-terminal')
from scripts import verify_generated_app_result as r
inputs=r.load_inputs('/private/tmp/wt-r01-ct-compatible-observer6-68bd.json','/private/tmp/wt-r01-bakery-journey13-68bd.json','/private/tmp/wt-r01-bakery-seed7-68bd.json')
p=Path('/private/tmp/wt-r01-generated-app13-final-binding-68bd.json'); final=json.loads(p.read_text())
assert final['original_candidate']==inputs['candidate'] and final['input_sha256']==inputs['input_sha256']
assert {k:v for k,v in final['final_candidate'].items() if k!='deployment_id'}=={k:v for k,v in inputs['candidate'].items() if k!='deployment_id'}
inputs['candidate']=final['final_candidate']
args=SimpleNamespace(artifacts='/private/tmp/wt-r01-generated-app13-preview2-68bd',browser_state='/private/tmp/wt-r01-browser-state-labuser-68bd.json',show_browser=False,action_timeout_seconds=15)
report={'schema_version':1,'operation':'observe_generated_app_final_followup','scope':'simulated_control_tower','control_tower_requests':0,'accepted':False,'input_sha256':inputs['input_sha256'],'deployment_followup_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'target':inputs['candidate']}
try: asyncio.run(r.observe_only(args,inputs,report))
except Exception as e:
 report.update(status='blocked',error_type=type(e).__name__,reason=str(e) if isinstance(e,r.ResultError) else None)
path=Path('/private/tmp/wt-r01-generated-app13-preview2-68bd.json');assert not path.exists();r.write_evidence(path,report)
print(json.dumps({k:v for k,v in report.items() if k not in ['terminal_verification','input_sha256']}))
