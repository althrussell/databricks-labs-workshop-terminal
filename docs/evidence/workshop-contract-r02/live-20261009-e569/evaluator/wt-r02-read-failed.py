import sys,json,hashlib
from pathlib import Path
from datetime import datetime,timezone
sys.path.insert(0,'/Users/a/code/databricks-labs-workshop-terminal')
from scripts.run_generated_app_journey import workspace_client,enum,timestamp,canonical_path,validate_receipt
from evals.generated_apps.report import write_evidence
root=Path('/private/tmp/wt-eval-r02-1009-e569')
j=json.loads((root/'journey-2.json').read_text());candidate=j['app_discovery']['candidates'][0]
b=validate_receipt(json.loads((root/'observer-diagnostic-update.json').read_text()));c=workspace_client('labs')
a=c.apps.get(candidate['app_name']);d=c.apps.get_deployment(a.name,candidate['deployment_id'])
assert a.id==candidate['app_id'] and a.creator==candidate['creator']==b['app']['service_principal_client_id']
assert a.active_deployment.deployment_id==d.deployment_id==candidate['deployment_id'] and enum(d.status.state)=='FAILED'
assert timestamp(j['journey']['messages'][0]['timestamp'])<=timestamp(a.create_time)<b['expires_at']
assert canonical_path(d.source_code_path)==canonical_path(candidate['source_code_path'])
snapshot=getattr(d.deployment_artifacts,'source_code_path',None) or d.source_code_path
target=root/'attempt-2-generated-source';assert not target.exists();target.mkdir(mode=0o700)
pending=[snapshot];files=[]
while pending:
 for o in c.workspace.list(pending.pop()):
  if enum(o.object_type)=='DIRECTORY':pending.append(o.path);continue
  rel=o.path[len(snapshot)+1:];assert rel and not rel.startswith('/') and '..' not in Path(rel).parts and len(files)<500
  with c.workspace.download(o.path) as stream:data=stream.read(4*1024*1024+1)
  assert len(data)<=4*1024*1024
  local=target/rel;local.parent.mkdir(mode=0o700,parents=True,exist_ok=True);local.write_bytes(data);local.chmod(0o600)
  files.append({'path':rel,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
backend=[]
for r in a.resources or []:
 postgres=getattr(r,'postgres',None)
 if postgres:
  p=c.postgres.get_project(name='/'.join(postgres.branch.split('/')[:2]));raw=p.as_dict()
  backend.append({'name':p.name,'uid':p.uid,'owner':p.status.owner,'create_time':raw.get('create_time')})
paths=[]
for path in [d.source_code_path.rsplit('/default/files',1)[0],snapshot]:
 if path not in [x['path'] for x in paths]:
  o=c.workspace.get_status(path);paths.append({'path':path,'id':str(o.object_id)})
report={'schema_version':1,'operation':'failed_generated_app_source_readback','scope':'simulated_control_tower','control_tower_mutations':0,'workspace_mutations':0,'observed_at':datetime.now(timezone.utc).isoformat(),'app':{'id':a.id,'name':a.name,'creator':a.creator,'create_time':a.create_time,'service_principal_id':a.service_principal_id,'service_principal_client_id':a.service_principal_client_id,'deployment_id':d.deployment_id,'deployment_state':enum(d.status.state),'source_code_path':d.source_code_path,'snapshot':snapshot},'resources':[r.as_dict() for r in a.resources or []],'lakebase':backend,'workspace_directories':paths,'files':files,'app_usable':False,'agent_recovery_opportunity':'collector closed native session immediately after system task notification'}
current=c.apps.get(a.name);assert current.id==a.id and current.active_deployment.deployment_id==d.deployment_id
write_evidence(root/'attempt-2-source-readback.json',report);print(json.dumps({'app':report['app'],'lakebase':backend,'file_count':len(files),'resources':report['resources']}))
