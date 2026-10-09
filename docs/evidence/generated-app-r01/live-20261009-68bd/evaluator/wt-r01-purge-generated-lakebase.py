import sys,json,hashlib,time
from pathlib import Path
from datetime import datetime,timezone
sys.path.insert(0,'/Users/a/code/databricks-labs-workshop-terminal')
from scripts.deploy_generated_app_test import workspace_client
from scripts.run_generated_app_journey import enum,timestamp
from evals.generated_apps.report import write_evidence
from databricks.sdk.errors import NotFound
plan=json.load(open('/private/tmp/wt-r01-generated-app13-cleanup-plan-68bd.json'));oldpath=Path('/private/tmp/wt-r01-generated-app13-cleanup-followup-68bd.json');old=json.loads(oldpath.read_text());assert old['status']=='cleanup_failed' and old['generated_app_absence_verified'] and old['bundle_and_both_snapshot_absence_verified']
c=workspace_client('labs',http_timeout_seconds=30);project=c.postgres.get_project(name=plan['lakebase']['name']);raw=project.as_dict()
assert project.uid==plan['lakebase']['uid'] and project.status.owner==plan['lakebase']['owner'] and raw['create_time']==plan['lakebase']['create_time'] and raw.get('delete_time') and timestamp(raw['delete_time'])>=timestamp(old['started_at'])-1
out=Path('/private/tmp/wt-r01-generated-app13-cleanup-final-68bd.json');assert not out.exists();record={'schema_version':1,'operation':'purge_exact_generated_lakebase_tombstone','scope':'simulated_control_tower','control_tower_mutations':0,'accepted':False,'previous_cleanup_sha256':hashlib.sha256(oldpath.read_bytes()).hexdigest(),'lakebase':{**plan['lakebase'],'delete_time':raw['delete_time']},'reason':'Soft-deleted test project remains retrievable; purge only this proven new synthetic-data project.','generated_app_absence_verified':True,'bundle_and_both_snapshot_absence_verified':True,'status':'cleaning','started_at':datetime.now(timezone.utc).isoformat()};write_evidence(out,record)
try:
 operation=c.postgres.delete_project(project.name,purge=True);record['purge_operation_name']=operation.name;write_evidence(out,record)
 deadline=time.monotonic()+90
 while True:
  try:c.postgres.get_project(name=project.name)
  except NotFound:break
  if time.monotonic()>deadline:raise RuntimeError('Lakebase purge absence unverified')
  time.sleep(2)
 ct=c.apps.get('control-tower');after={'id':ct.id,'active_deployment_id':ct.active_deployment.deployment_id,'state':enum(ct.app_status.state),'compute':enum(ct.compute_status.state)};assert after==plan['control_tower_before'];record['control_tower_after']=after
 record.update(status='cleanup_verified',lakebase_project_absence_verified=True,control_tower_deployment_unchanged=True,completed_at=datetime.now(timezone.utc).isoformat());write_evidence(out,record);print(json.dumps(record))
except Exception as e:
 record.update(status='cleanup_failed',error_type=type(e).__name__);write_evidence(out,record);raise
