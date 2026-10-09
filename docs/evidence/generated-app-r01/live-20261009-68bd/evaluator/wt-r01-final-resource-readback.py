import sys,json,hashlib
from pathlib import Path
from datetime import datetime,timezone
sys.path.insert(0,'/Users/a/code/databricks-labs-workshop-terminal')
from scripts.deploy_generated_app_test import workspace_client
from scripts.run_generated_app_journey import enum
from evals.generated_apps.report import write_evidence
from databricks.sdk.errors import NotFound
c=workspace_client('labs',http_timeout_seconds=15);deploypath=Path('/private/tmp/wt-r01-ct-compatible-observer6-68bd.json');d=json.loads(deploypath.read_text());owned={x['kind']:x for x in d['created_resources']};gen=json.load(open('/private/tmp/wt-r01-generated-app13-cleanup-plan-68bd.json'))
result={'schema_version':1,'operation':'r01_final_resource_readback','scope':'simulated_control_tower','control_tower_mutations':0,'workspace_mutations':0,'accepted':False,'deployment_receipt_sha256':hashlib.sha256(deploypath.read_bytes()).hexdigest(),'observed_at':datetime.now(timezone.utc).isoformat(),'observations':[]}
lookups=[('wt_app',owned['app']['id'],lambda:c.apps.get(owned['app']['name'])),('generated_app',gen['generated_app']['id'],lambda:c.apps.get(gen['generated_app']['name'])),('catalog',owned['catalog']['name'],lambda:c.catalogs.get(owned['catalog']['name'])),('temporary_group',owned['group']['id'],lambda:c.groups.get(owned['group']['id'])),('wt_source',owned['workspace_source']['id'],lambda:c.workspace.get_status(owned['workspace_source']['path'])),('lakebase_project',gen['lakebase']['uid'],lambda:c.postgres.get_project(name=gen['lakebase']['name']))]
for kind,identity,lookup in lookups:
 try:
  observed=lookup();print(json.dumps({'unexpected_kind':kind,'expected_id':identity,'observed_id':getattr(observed,'id',getattr(observed,'uid',None)),'observed_name':getattr(observed,'name',None)}),flush=True);raise AssertionError('Expected exact resource absence')
 except NotFound:result['observations'].append({'kind':kind,'identity':identity,'absence_verified':True})
projects=c.workspace.get_status(d['work_sync']['path']);assert str(projects.object_id)==d['work_sync']['id'];result['attendee_projects']={'id':str(projects.object_id),'directory_preserved':True,'entries_preserved':[{'path':o.path,'object_type':enum(o.object_type)} for o in c.workspace.list(d['work_sync']['path'])]}
result['app_service_principals']=[]
for name in [owned['app']['service_principal_client_id'],gen['generated_app']['id']]:
 matches=list(c.service_principals.list(filter=f'applicationId eq "{name}"'));result['app_service_principals'].append({'application_id':name,'workspace_principal_absence_verified':len(matches)==0})
ct=c.apps.get('control-tower');after={'id':ct.id,'active_deployment_id':ct.active_deployment.deployment_id,'state':enum(ct.app_status.state),'compute':enum(ct.compute_status.state)};assert after==gen['control_tower_before'];result['control_tower']=after;result['control_tower_deployment_unchanged']=True;result['status']='cleanup_independently_verified'
write_evidence('/private/tmp/wt-r01-final-resource-readback-68bd.json',result);print(json.dumps(result))
