import sys,json,hashlib
from pathlib import Path
from datetime import datetime,timezone
sys.path.insert(0,'/Users/a/code/databricks-labs-workshop-terminal')
from scripts.run_generated_app_journey import workspace_client,timestamp,enum
from scripts.verify_generated_app_result import load_inputs,verify_generated_app
from evals.generated_apps.report import write_evidence
c=workspace_client('labs');f=json.load(open('/private/tmp/wt-r01-generated-app13-final-binding-68bd.json'));inputs=load_inputs('/private/tmp/wt-r01-ct-compatible-observer6-68bd.json','/private/tmp/wt-r01-bakery-journey13-68bd.json','/private/tmp/wt-r01-bakery-seed7-68bd.json');assert f['original_candidate']==inputs['candidate'];inputs['candidate']=f['final_candidate'];verified=verify_generated_app(inputs,c)
app=c.apps.get(verified['app_name']);resource=[r for r in app.resources if r.name=='postgres'];assert len(resource)==1
postgres=resource[0].postgres;assert postgres.branch=='projects/bakery-orders-db/branches/production' and postgres.database=='projects/bakery-orders-db/branches/production/databases/databricks-postgres'
project=c.postgres.get_project(name='projects/bakery-orders-db');old=json.load(open('/private/tmp/wt-r01-generated-app13-lakebase-metadata-68bd.json'))
assert project.uid==old['uid'] and project.as_dict()['create_time']==old['create_time'] and project.status.owner==app.creator==inputs['binding']['app']['id']
j=inputs['journey'];start=timestamp(j['journey']['messages'][0]['timestamp']);assert start<timestamp(project.as_dict()['create_time'])<timestamp(app.create_time)<inputs['binding']['expires_at']
paths=[f"/Workspace/Users/{app.creator}/.bundle/{app.name}"]+[entry['path'] for entry in f['snapshots'].values()]
owned=[]
for p in paths:
 o=c.workspace.get_status(p);assert enum(o.object_type)=='DIRECTORY';owned.append({'path':p,'id':str(o.object_id)})
# Read-only proof that the CT app is still on its working deployment.
ct=c.apps.get('control-tower');ct_meta={'id':ct.id,'active_deployment_id':ct.active_deployment.deployment_id,'state':enum(ct.app_status.state),'compute':enum(ct.compute_status.state)}
plan={'schema_version':1,'operation':'generated_app_cleanup_plan','scope':'simulated_control_tower','control_tower_mutations':0,'accepted':False,'observed_at':datetime.now(timezone.utc).isoformat(),'generated_app':{'id':app.id,'name':app.name,'creator':app.creator,'create_time':app.create_time,'active_deployment_id':app.active_deployment.deployment_id},'lakebase':{'name':project.name,'uid':project.uid,'owner':project.status.owner,'create_time':project.as_dict()['create_time'],'run_creation_proved':True},'workspace_directories':owned,'control_tower_before':ct_meta,'bound_plan_expires_at':inputs['binding']['expires_at'],'attendee_projects_directory_preserved':True}
write_evidence('/private/tmp/wt-r01-generated-app13-cleanup-plan-68bd.json',plan);print(json.dumps(plan))
