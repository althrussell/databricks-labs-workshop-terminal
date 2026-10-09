import sys,json,hashlib
from pathlib import Path
sys.path.insert(0,'/Users/a/code/databricks-labs-workshop-terminal')
from scripts.run_generated_app_journey import workspace_client,enum
c=workspace_client('labs');f=json.load(open('/private/tmp/wt-r01-generated-app13-final-binding-68bd.json'))
root=Path('/private/tmp/wt-r01-generated-app13-source-68bd');root.mkdir(mode=0o700)
manifest={}
for label in ['original','final']:
 base=f['snapshots'][label]['path']; pending=[base]; files=[]
 while pending:
  path=pending.pop(); objs=list(c.workspace.list(path));assert len(files)+len(objs)<500
  for o in objs:
   if enum(o.object_type)=='DIRECTORY':pending.append(o.path)
   else:
    rel=o.path[len(base)+1:]; assert not rel.startswith('/') and '..' not in Path(rel).parts
    with c.workspace.download(o.path) as stream:data=stream.read(4*1024*1024+1)
    assert len(data)<=4*1024*1024
    local=root/label/rel;local.parent.mkdir(mode=0o700,parents=True,exist_ok=True);local.write_bytes(data);local.chmod(0o600)
    files.append({'path':rel,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
 manifest[label]={'snapshot':base,'files':sorted(files,key=lambda x:x['path'])}
manifest['snapshots_equal']=manifest['original']['files']==manifest['final']['files']
manifest['scope']='simulated_control_tower';manifest['control_tower_requests']=0;manifest['workspace_mutations']=0
(root/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
print(json.dumps({'snapshots_equal':manifest['snapshots_equal'],'files':[x['path'] for x in manifest['final']['files']]}))
try:
 p=c.postgres.get_project(name='projects/bakery-orders-db')
 # Project metadata only; never include credential or endpoint details.
 raw=p.as_dict(); selected={k:raw.get(k) for k in ['name','uid','create_time','update_time','status','spec']}
 out=Path('/private/tmp/wt-r01-generated-app13-lakebase-metadata-68bd.json');out.write_text(json.dumps(selected,indent=2)+'\n');out.chmod(0o600)
 print(json.dumps(selected))
except Exception as e: print(json.dumps({'lakebase_error_type':type(e).__name__}))
