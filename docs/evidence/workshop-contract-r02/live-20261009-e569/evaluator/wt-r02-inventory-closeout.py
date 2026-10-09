import sys,json,hashlib
from pathlib import Path
from datetime import datetime,timezone
sys.path.insert(0,'/Users/a/code/databricks-labs-workshop-terminal')
import httpx
from scripts.run_generated_app_journey import workspace_client,validate_receipt,timestamp,enum
from databricks.sdk.errors import NotFound
root=Path('/private/tmp/wt-eval-r02-1009-e569')
phase=sys.argv[1]; assert phase in {'before','after'}
receipt=json.loads((root/'observer-native-notification-update.json').read_text()); binding=validate_receipt(receipt)
c=workspace_client('labs'); owner=binding['app']['service_principal_client_id']
apps=[{'id':a.id,'name':a.name,'creator':a.creator,'create_time':a.create_time} for a in c.apps.list()
      if a.creator==owner and timestamp(a.create_time)>=binding['created_at']]
projects=[{'name':p.name,'uid':p.uid,'owner':p.status.owner,'create_time':p.as_dict().get('create_time')}
          for p in c.postgres.list_projects() if p.status and p.status.owner==owner]
assert apps==[] and projects==[]
def absent(call):
    try: call()
    except NotFound: return True
    return False
assert absent(lambda:c.apps.get('bakery-orders'))
assert absent(lambda:c.postgres.get_project(name='projects/bakery-orders'))
ct=c.apps.get('control-tower'); assert ct.id=='4f1951af-9397-4462-b283-9a66c76b234f'
assert ct.active_deployment.deployment_id=='01f1bbc1de2911cb93356caa765f77a3'
assert enum(ct.app_status.state)=='RUNNING' and enum(ct.compute_status.state)=='ACTIVE'
sync=receipt['work_sync']; actual=c.workspace.get_status(sync['path']); assert str(actual.object_id)==sync['id']=='1462869392157865'
entries=[o.path for o in c.workspace.list(sync['path'])]; assert entries==[]
repo=Path('/Users/a/code/databricks-labs-workshop-terminal')
ctroot=Path('/Users/a/.codex/worktrees/ct-labs-latest-deploy/databricks-labs-control-tower')
original=json.loads((repo/'docs/evidence/generated-app-r01/live-20261009-68bd/ct-source-closeout.json').read_text())
hashes=[dict(p,unchanged=hashlib.sha256((ctroot/p['path']).read_bytes()).hexdigest()==p['sha256']) for p in original['files']]
assert all(p['unchanged'] for p in hashes)
report={'schema_version':1,'operation':'independent_isolated_run_'+phase+'_cleanup_readback','scope':'simulated_control_tower',
 'control_tower_mutations':0,'observed_at':datetime.now(timezone.utc).isoformat(),'generated_apps':apps,'owned_lakebase_projects':projects,
 'prior_generated_app_absence_verified':True,'prior_aborted_project_absence_verified':True,
 'attendee_projects':{'path':sync['path'],'id':sync['id'],'preserved':True,'entries':entries},
 'control_tower':{'id':ct.id,'deployment_id':ct.active_deployment.deployment_id,'state':enum(ct.app_status.state),'compute':enum(ct.compute_status.state)},
 'reviewed_ct_files':hashes,'reviewed_ct_files_unchanged':True}
if phase=='before':
    with httpx.Client(timeout=20) as http:
        response=http.get(binding['app']['url']+'/api/admin/presence',headers=c.config.authenticate());response.raise_for_status()
        payload=response.json(); active=[s for u in payload['users'] if u['email']==binding['attendee']['email'] for s in u['sessions'] if not s.get('exited')]
        assert not active;report['active_owned_sessions']=active
else:
    indexed={r['kind']:r for r in receipt['created_resources']}
    checks={'app':absent(lambda:c.apps.get(indexed['app']['name'])),
            'catalog':absent(lambda:c.catalogs.get(indexed['catalog']['name'])),
            'group':absent(lambda:c.groups.get(indexed['group']['id'])),
            'workspace_source':absent(lambda:c.workspace.get_status(indexed['workspace_source']['path']))}
    assert all(checks.values());report['wt_resources_absence_verified']=checks
out=root/(phase+'-cleanup-independent-readback.json');assert not out.exists();out.write_text(json.dumps(report,indent=2)+'\n');out.chmod(0o600)
print(json.dumps(report))
