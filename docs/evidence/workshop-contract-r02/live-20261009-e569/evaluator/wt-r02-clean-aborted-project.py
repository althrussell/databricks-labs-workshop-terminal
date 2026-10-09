import sys,json,time,hashlib
from pathlib import Path
from datetime import datetime,timezone
sys.path.insert(0,'/Users/a/code/databricks-labs-workshop-terminal')
from scripts.run_generated_app_journey import workspace_client,timestamp,validate_receipt
from databricks.sdk.errors import NotFound
from evals.generated_apps.report import write_evidence
root=Path('/private/tmp/wt-eval-r02-1009-e569')
ip=root/'generated-resource-inventory-after-stop.json'; inventory=json.loads(ip.read_text())
receipt=json.loads((root/'deployment.json').read_text());journey=json.loads((root/'journey-1.json').read_text())
owner=validate_receipt(receipt)['app']['service_principal_client_id']
assert inventory['owner']==owner and inventory['apps']==[] and len(inventory['lakebase_projects'])==1
expected=inventory['lakebase_projects'][0]
assert timestamp(journey['journey']['messages'][0]['timestamp'])<timestamp(expected['create_time'])<timestamp(inventory['observed_at'])
c=workspace_client('labs');p=c.postgres.get_project(name=expected['name']);raw=p.as_dict()
assert p.uid==expected['uid'] and p.status.owner==expected['owner']==owner and raw['create_time']==expected['create_time']
out=root/'aborted-project-cleanup.json';assert not out.exists()
report={'schema_version':1,'operation':'cleanup_exact_aborted_build_lakebase','scope':'simulated_control_tower','control_tower_mutations':0,'inventory_sha256':hashlib.sha256(ip.read_bytes()).hexdigest(),'lakebase':expected,'status':'cleaning','operations':[],'started_at':datetime.now(timezone.utc).isoformat()};write_evidence(out,report)
c.postgres.delete_project(p.name);report['operations'].append({'operation':'soft_delete_requested','uid':p.uid});write_evidence(out,report)
try:
    tombstone=c.postgres.get_project(name=p.name);traw=tombstone.as_dict()
    assert tombstone.uid==expected['uid'] and tombstone.status.owner==owner and traw.get('delete_time')
    c.postgres.delete_project(p.name,purge=True);report['operations'].append({'operation':'purge_exact_tombstone_requested','uid':p.uid});write_evidence(out,report)
except NotFound:pass
deadline=time.monotonic()+90
while True:
    try:c.postgres.get_project(name=p.name)
    except NotFound:break
    if time.monotonic()>=deadline:raise RuntimeError('exact Lakebase project absence unverified')
    time.sleep(2)
report.update(status='cleanup_verified',absence_verified=True,completed_at=datetime.now(timezone.utc).isoformat());write_evidence(out,report);print(json.dumps(report))
