import sys,json,hashlib,time
from pathlib import Path
from datetime import datetime,timezone
sys.path.insert(0,'/Users/a/code/databricks-labs-workshop-terminal')
from scripts.deploy_generated_app_test import workspace_client
from scripts.run_generated_app_journey import enum
from evals.generated_apps.report import write_evidence
from databricks.sdk.errors import NotFound
c=workspace_client('labs',http_timeout_seconds=15);plan=json.load(open('/private/tmp/wt-r01-generated-app13-cleanup-plan-68bd.json'));purge=Path('/private/tmp/wt-r01-generated-app13-cleanup-final-68bd.json');assert json.loads(purge.read_text())['status']=='cleaning'
record={'schema_version':1,'operation':'verify_generated_cleanup_after_purge','scope':'simulated_control_tower','control_tower_mutations':0,'workspace_mutations':0,'accepted':False,'purge_receipt_sha256':hashlib.sha256(purge.read_bytes()).hexdigest(),'collector_failure':'The purge request succeeded, but serializing the SDK operation.name method stopped its receipt writer. This independent followup verifies actual absence.','observations':[],'started_at':datetime.now(timezone.utc).isoformat()}
def absence(kind,identity,lookup):
 end=time.monotonic()+60
 while True:
  try:lookup()
  except NotFound:
   record['observations'].append({'kind':kind,'identity':identity,'absence_verified':True});return
  if time.monotonic()>=end:raise RuntimeError('Cleanup absence not yet proved')
  time.sleep(2)
absence('generated_app',plan['generated_app']['id'],lambda:c.apps.get(plan['generated_app']['name']))
for entry in plan['workspace_directories']:absence('workspace_directory',entry['id'],lambda entry=entry:c.workspace.get_status(entry['path']))
absence('lakebase_project',plan['lakebase']['uid'],lambda:c.postgres.get_project(name=plan['lakebase']['name']))
ct=c.apps.get('control-tower');after={'id':ct.id,'active_deployment_id':ct.active_deployment.deployment_id,'state':enum(ct.app_status.state),'compute':enum(ct.compute_status.state)};assert after==plan['control_tower_before'];record['control_tower_after']=after
record.update(status='cleanup_verified',control_tower_deployment_unchanged=True,completed_at=datetime.now(timezone.utc).isoformat())
write_evidence('/private/tmp/wt-r01-generated-app13-cleanup-readback-68bd.json',record);print(json.dumps(record))
