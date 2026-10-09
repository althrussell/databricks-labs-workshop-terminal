import sys,json,asyncio,hashlib
from pathlib import Path
from datetime import datetime,timezone
sys.path.insert(0,'/Users/a/code/databricks-labs-workshop-terminal')
from evals.generated_apps.adapters.browser import AuthenticatedBrowser,GeneratedAppBrowserVerifier
from scripts.verify_generated_app_result import captures,load_inputs,verify_generated_app
from scripts.run_generated_app_journey import workspace_client
from evals.generated_apps.report import write_evidence
async def main():
 oldpath=Path('/private/tmp/wt-r01-generated-app13-ui-task-68bd.json');old=json.loads(oldpath.read_text()); assert old['packed_http_status']==200
 f=json.load(open('/private/tmp/wt-r01-generated-app13-final-binding-68bd.json'));url=f['final_candidate']['url'];inputs=load_inputs('/private/tmp/wt-r01-ct-compatible-observer6-68bd.json','/private/tmp/wt-r01-bakery-journey13-68bd.json','/private/tmp/wt-r01-bakery-seed7-68bd.json');assert inputs['candidate']==f['original_candidate'];inputs['candidate']=f['final_candidate'];c=workspace_client('labs')
 root=Path('/private/tmp/wt-r01-generated-app13-packed-followup-68bd');assert not root.exists();root.mkdir(mode=0o700)
 report={'schema_version':1,'operation':'read_only_packed_ui_followup','scope':'simulated_control_tower','control_tower_requests':0,'accepted':False,'app_mutations':0,'original_task_sha256':hashlib.sha256(oldpath.read_bytes()).hexdigest(),'evaluator_correction':'Packed exact cell has two matches: status cell and action cell. Select only the cell with no button.','observed_at':datetime.now(timezone.utc).isoformat(),'deployment':verify_generated_app(inputs,c),'backend_persistence_verified':False,'process_restart_verified':False,'app_response_observations':[]}
 pending=set()
 async with AuthenticatedBrowser('/private/tmp/wt-r01-generated-browser-state-labuser-68bd.json',headless=True,clear_app_storage_origins=[url]) as b:
  async def observe(res):
   if res.request.method=='GET' and res.url==url+'/api/lakebase/orders' and res.status==200:
    data=await res.json();report['app_response_observations'].append({'source':'app_response_not_independent_backend','method':'GET','row_count':len(data),'target_rows':[r for r in data if r.get('customer_name')=='R01 Bakery Customer']})
  def enqueue(res):
   t=asyncio.create_task(observe(res));pending.add(t);t.add_done_callback(pending.discard)
  b.page.on('response',enqueue)
  def row(page):return page.get_by_role('row').filter(has=page.get_by_role('cell',name='R01 Bakery Customer',exact=True))
  def status(page):return row(page).get_by_role('cell',name='Packed',exact=True).filter(has_not=page.get_by_role('button'))
  v=GeneratedAppBrowserVerifier(b.page,artifact_dir=root,timeout_seconds=15)
  await v.open(url);await status(b.page).wait_for(timeout=15000);assert await status(b.page).count()==1
  report['packed_ui_verified']=True;report['packed_aria']=await row(b.page).aria_snapshot()
  await b.page.reload(wait_until='domcontentloaded');await status(b.page).wait_for(timeout=15000);report['reload_ui_verified']=True
  second=await b.new_context()
  try:
   page=await second.new_page();page.on('response',enqueue);fresh=GeneratedAppBrowserVerifier(page,artifact_dir=root/'fresh-context',timeout_seconds=15)
   await fresh.open(url);await status(page).wait_for(timeout=15000);report['fresh_context_ui_verified']=True;report['fresh_context_aria']=await row(page).aria_snapshot()
   report['final']=await captures(fresh,'final',url);report['fresh_page_errors']=fresh.page_errors
  finally:await second.close()
  if pending:await asyncio.gather(*pending,return_exceptions=True)
  report['page_errors']=v.page_errors
 report['final_deployment_verification']=verify_generated_app(inputs,c);report['binding_current_at_end_verified']=True;report['status']='ui_followup_finished'
 write_evidence('/private/tmp/wt-r01-generated-app13-packed-followup-68bd.json',report)
 print(json.dumps({k:v for k,v in report.items() if k not in ['deployment','final_deployment_verification','final']}))
asyncio.run(main())
