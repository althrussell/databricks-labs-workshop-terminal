import sys,json
from pathlib import Path
from datetime import datetime,timezone
sys.path.insert(0,'/Users/a/code/databricks-labs-workshop-terminal')
from scripts import verify_generated_app_result as r
from scripts.run_generated_app_journey import workspace_client,enum,timestamp,canonical_path
inputs=r.load_inputs('/private/tmp/wt-r01-ct-compatible-observer6-68bd.json','/private/tmp/wt-r01-bakery-journey13-68bd.json','/private/tmp/wt-r01-bakery-seed7-68bd.json')
c=workspace_client('labs'); candidate=inputs['candidate']; app=c.apps.get(candidate['app_name'])
assert app.id==candidate['app_id'] and app.creator==candidate['creator']
original=c.apps.get_deployment(app.name,candidate['deployment_id'])
final=c.apps.get_deployment(app.name,app.active_deployment.deployment_id)
assert original.creator==final.creator==candidate['creator']
assert canonical_path(original.source_code_path)==canonical_path(final.source_code_path)==canonical_path(candidate['source_code_path'])
assert timestamp(original.create_time)<=timestamp(final.create_time)<inputs['binding']['expires_at']
followup={**candidate,'deployment_id':final.deployment_id}
inputs['candidate']=followup
verified=r.verify_generated_app(inputs,c)
receipt={'schema_version':1,'operation':'generated_app_final_deployment_followup','scope':'simulated_control_tower','control_tower_requests':0,'workspace_mutations':0,'accepted':False,'observed_at':datetime.now(timezone.utc).isoformat(),'input_sha256':inputs['input_sha256'],'original_candidate':candidate,'final_candidate':followup,'verification':verified,'deployment_creator':final.creator,'original_deployment_create_time':original.create_time,'final_deployment_create_time':final.create_time,'original_journey_preserved':True,'snapshots':{}}
for label,d in [('original',original),('final',final)]:
 p=f'/Workspace/Users/{app.id}/src/{d.deployment_id}'
 try:
  objs=list(c.workspace.list(p)); receipt['snapshots'][label]={'path':p,'entries':[{'path':o.path,'object_type':enum(o.object_type),'object_id':o.object_id} for o in objs]}
 except Exception as e:receipt['snapshots'][label]={'path':p,'error_type':type(e).__name__}
path=Path('/private/tmp/wt-r01-generated-app13-final-binding-68bd.json'); assert not path.exists();path.write_text(json.dumps(receipt,indent=2)+'\n');path.chmod(0o600)
print(json.dumps(receipt))
