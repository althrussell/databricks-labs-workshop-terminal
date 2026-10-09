import sys,json
from pathlib import Path
from datetime import datetime,timezone
sys.path.insert(0,'/Users/a/code/databricks-labs-workshop-terminal')
from scripts.run_generated_app_journey import workspace_client
from databricks.sdk.errors import NotFound
root=Path('/private/tmp/wt-eval-r02-1009-e569');c=workspace_client('labs')
principals=[{'id':75917485218280,'client_id':'bd6e0ccd-4b90-49a6-bca2-b92689893360'},
            {'id':73887571980643,'client_id':'2780d902-1b6b-4fee-b668-628ed1881f6f'}]
acl=c.permissions.get('sql/warehouses','72db7ec713837d92').as_dict()
for p in principals:
    rows=[r for r in acl.get('access_control_list',[]) if r.get('service_principal_name')==p['client_id']]
    p['warehouse_acl_entries']=rows
    try:
        sp=c.service_principals.get(str(p['id']));assert sp.application_id==p['client_id'];p['scim_present']=True
    except NotFound:p['scim_present']=False
assert all(not p['warehouse_acl_entries'] for p in principals)
report={'schema_version':1,'operation':'independent_test_principal_shared_access_closeout','workspace_mutations':0,
 'control_tower_mutations':0,'observed_at':datetime.now(timezone.utc).isoformat(),'warehouse_id':'72db7ec713837d92',
 'principals':principals,'test_warehouse_acl_absence_verified':True}
out=root/'shared-access-closeout.json';assert not out.exists();out.write_text(json.dumps(report,indent=2)+'\n');out.chmod(0o600)
print(json.dumps(report))
