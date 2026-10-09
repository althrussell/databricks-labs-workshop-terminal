import sys,json,asyncio,hashlib
from pathlib import Path
from datetime import datetime,timezone
from urllib.parse import urlsplit
sys.path.insert(0,'/Users/a/code/databricks-labs-workshop-terminal')
from evals.generated_apps.adapters.browser import AuthenticatedBrowser,GeneratedAppBrowserVerifier
from scripts.verify_generated_app_result import captures,load_inputs,verify_generated_app
from scripts.run_generated_app_journey import workspace_client
from evals.generated_apps.report import write_evidence
OUTPUT=Path('/private/tmp/wt-r01-generated-app13-ui-task-68bd.json');ARTIFACTS=Path('/private/tmp/wt-r01-generated-app13-ui-task-68bd')
async def main():
 assert not OUTPUT.exists() and not ARTIFACTS.exists();ARTIFACTS.mkdir(mode=0o700)
 fpath=Path('/private/tmp/wt-r01-generated-app13-final-binding-68bd.json'); f=json.loads(fpath.read_text());url=f['final_candidate']['url']
 inputs=load_inputs('/private/tmp/wt-r01-ct-compatible-observer6-68bd.json','/private/tmp/wt-r01-bakery-journey13-68bd.json','/private/tmp/wt-r01-bakery-seed7-68bd.json');assert inputs['candidate']==f['original_candidate'];inputs['candidate']=f['final_candidate'];c=workspace_client('labs')
 report={'schema_version':1,'operation':'generated_app_ui_task_followup','scope':'simulated_control_tower','control_tower_requests':0,'accepted':False,'input_sha256':inputs['input_sha256'],'deployment_followup_sha256':hashlib.sha256(fpath.read_bytes()).hexdigest(),'deployment':verify_generated_app(inputs,c),'observed_at':datetime.now(timezone.utc).isoformat(),'task':{'source':'private_evaluator_task_after_generation','builder_rescue':False,'customer':'R01 Bakery Customer','item':'2 dozen rolls','due_date':'2026-10-08','notes':'Synthetic R01 UI test','action':'Add the late order, then mark it packed once'},'app_response_observations':[],'independent_backend_oracle':'unsupported_lakebase','backend_persistence_verified':False,'process_restart_verified':False}
 pending=set()
 async with AuthenticatedBrowser('/private/tmp/wt-r01-generated-browser-state-labuser-68bd.json',headless=True,clear_app_storage_origins=[url]) as b:
  async def observe(response):
   parsed=urlsplit(response.url)
   if parsed.netloc!=urlsplit(url).netloc or not parsed.path.startswith('/api/lakebase/orders'):return
   req=response.request;observation={'method':req.method,'path':parsed.path,'http_status':response.status,'source':'actual_app_browser_response_not_independent_storage'}
   if response.ok:
    try:
     data=await response.json()
     if isinstance(data,list):observation['row_count']=len(data);observation['target_rows']=[r for r in data if r.get('customer_name')==report['task']['customer']]
     elif isinstance(data,dict):observation['target_row']=data if data.get('customer_name')==report['task']['customer'] else None
    except Exception as e:observation['error_type']=type(e).__name__
   report['app_response_observations'].append(observation)
  def enqueue(response):
   t=asyncio.create_task(observe(response));pending.add(t);t.add_done_callback(pending.discard)
  b.page.on('response',enqueue)
  v=GeneratedAppBrowserVerifier(b.page,artifact_dir=ARTIFACTS,timeout_seconds=15)
  try:
   await v.open(url);await b.page.get_by_role('heading',name="Today's orders",exact=True).wait_for(timeout=15000)
   await b.page.get_by_role('button',name='New order',exact=True).click()
   dialog=b.page.get_by_role('dialog',name='Add an order',exact=True);await dialog.wait_for()
   await dialog.get_by_label('Customer',exact=True).fill(report['task']['customer'])
   await dialog.get_by_label('Order',exact=True).fill(report['task']['item'])
   await dialog.get_by_label('Due date',exact=True).fill(report['task']['due_date'])
   await dialog.get_by_label('Notes (optional)',exact=True).fill(report['task']['notes'])
   report['new_order_dialog']=await captures(v,'add-order',url)
   await b.page.set_viewport_size({'width':1440,'height':900})
   await dialog.get_by_role('button',name='Add order',exact=True).click()
   await dialog.wait_for(state='hidden',timeout=15000)
   row=b.page.get_by_role('row').filter(has=b.page.get_by_role('cell',name=report['task']['customer'],exact=True))
   assert await row.count()==1
   await row.get_by_role('button',name='Mark packed',exact=True).wait_for(timeout=15000)
   report['before_packed_aria']=await row.aria_snapshot()
   report['before_packed']=await captures(v,'before-packed',url)
   report['created_order_via_ui']=True
   await b.page.set_viewport_size({'width':1440,'height':900})
   async with b.page.expect_response(lambda r: urlsplit(r.url).path.startswith('/api/lakebase/orders/') and r.request.method=='PATCH',timeout=15000) as patch:
    await row.get_by_role('button',name='Mark packed',exact=True).click()
   response=await patch.value;report['packed_http_status']=response.status;assert response.status==200
   await row.get_by_role('cell',name='Packed',exact=True).wait_for(timeout=15000)
   report['after_packed_aria']=await row.aria_snapshot();report['packed_ui_verified']=True;report['packed_click_attempts']=1
   await b.page.reload(wait_until='domcontentloaded')
   await row.get_by_role('cell',name='Packed',exact=True).wait_for(timeout=15000)
   report['reload_ui_verified']=True
   second=await b.new_context()
   try:
    page=await second.new_page();fresh=GeneratedAppBrowserVerifier(page,artifact_dir=ARTIFACTS/'fresh-context',timeout_seconds=15)
    await fresh.open(url);await page.get_by_role('heading',name="Today's orders",exact=True).wait_for(timeout=15000)
    freshrow=page.get_by_role('row').filter(has=page.get_by_role('cell',name=report['task']['customer'],exact=True))
    await freshrow.get_by_role('cell',name='Packed',exact=True).wait_for(timeout=15000)
    report['fresh_context_ui_verified']=True;report['fresh_context_aria']=await freshrow.aria_snapshot()
    report['final']=await captures(fresh,'final',url);report['fresh_page_errors']=fresh.page_errors
   finally:await second.close()
   report['status']='ui_tasks_finished'
  except Exception as e:
   report.update(status='ui_task_failed',error_type=type(e).__name__)
   report['failure_capture']=await captures(v,'failure',url)
  finally:
   if pending:await asyncio.gather(*pending,return_exceptions=True)
   report['page_errors']=v.page_errors
 report['final_deployment_verification']=verify_generated_app(inputs,c);report['binding_current_at_end_verified']=True
 write_evidence(OUTPUT,report)
 print(json.dumps({k:v for k,v in report.items() if k not in ['deployment','final_deployment_verification','input_sha256','new_order_dialog','before_packed','final','failure_capture']}))
asyncio.run(main())
