import sys,json,asyncio,os,hashlib
from pathlib import Path
from datetime import datetime,timezone
from urllib.parse import urlsplit
sys.path.insert(0,'/Users/a/code/databricks-labs-workshop-terminal')
from evals.generated_apps.adapters.browser import AuthenticatedBrowser,GeneratedAppBrowserVerifier
from scripts.verify_generated_app_result import captures,load_inputs,verify_generated_app
from scripts.run_generated_app_journey import workspace_client
from evals.generated_apps.report import write_evidence
async def main():
 final_path=Path('/private/tmp/wt-r01-generated-app13-final-binding-68bd.json');f=json.loads(final_path.read_text());url=f['final_candidate']['url']
 inputs=load_inputs('/private/tmp/wt-r01-ct-compatible-observer6-68bd.json','/private/tmp/wt-r01-bakery-journey13-68bd.json','/private/tmp/wt-r01-bakery-seed7-68bd.json');assert inputs['candidate']==f['original_candidate'];inputs['candidate']=f['final_candidate'];c=workspace_client('labs')
 report={'schema_version':1,'operation':'generated_app_attendee_preview','scope':'simulated_control_tower','control_tower_requests':0,'accepted':False,'input_sha256':inputs['input_sha256'],'deployment_followup_sha256':hashlib.sha256(final_path.read_bytes()).hexdigest(),'deployment':verify_generated_app(inputs,c),'auth_state_in_evidence':False,'observed_at':datetime.now(timezone.utc).isoformat()}
 root=Path('/private/tmp/wt-r01-generated-app13-preview3-68bd');root.mkdir(mode=0o700)
 async with AuthenticatedBrowser('/private/tmp/wt-r01-browser-state-labuser-68bd.json',headless=True) as b:
  await b.page.goto(url,wait_until='domcontentloaded',timeout=30000)
  await b.page.wait_for_timeout(2000)
  if urlsplit(b.page.url).netloc==urlsplit(inputs['binding']['workspace_host']).netloc:
   await b.page.get_by_role('heading',name='Permission Requested',exact=True).wait_for(timeout=10000)
   text=await b.page.locator('body').aria_snapshot()
   assert '- heading "bakery-orders"' in text and '- strong: labuser+1@awsbricks.com' in text
   headings=await b.page.get_by_role('heading',level=3).all_text_contents()
   assert set(headings)=={'Verify your identity, view your profile and email address','Allow longer-running access','Confirm your Databricks identity and access'}
   report['normal_test_user_consent']={'assigned_identity_verified':True,'requested_scopes':'platform_default_identity_login','additional_user_api_scopes':False}
   await b.page.get_by_role('button',name='Authorize',exact=True).click(timeout=10000)
  await b.page.wait_for_url(url+'/**',timeout=30000)
  await b.page.get_by_role('heading',name="Today's orders",exact=True).wait_for(timeout=20000)
  await b.page.wait_for_timeout(2000)
  assert urlsplit(b.page.url).netloc==urlsplit(url).netloc and await b.page.locator('input[type=password]').count()==0
  v=GeneratedAppBrowserVerifier(b.page,artifact_dir=root,timeout_seconds=15)
  report['first_preview']={'independently_observed':True,'viewports':await captures(v,'first-preview',url),'task_results':[]}
  report['page_errors']=v.page_errors
  state=Path('/private/tmp/wt-r01-generated-browser-state-labuser-68bd.json');fd=os.open(state,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
  with os.fdopen(fd,'w') as handle:json.dump(await b.context.storage_state(),handle)
 report['final_deployment_verification']=verify_generated_app(inputs,c);report['binding_current_at_end_verified']=True;report['status']='ui_observed';report['verdict']='unverified'
 write_evidence('/private/tmp/wt-r01-generated-app13-preview3-68bd.json',report)
 print(json.dumps({'status':report['status'],'page_errors':report['page_errors'],'viewports':report['first_preview']['viewports']}))
asyncio.run(main())
