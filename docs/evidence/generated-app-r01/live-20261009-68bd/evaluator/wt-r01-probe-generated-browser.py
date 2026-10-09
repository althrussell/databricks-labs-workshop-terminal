import sys,json,asyncio
from pathlib import Path
from urllib.parse import urlsplit
sys.path.insert(0,'/Users/a/code/databricks-labs-workshop-terminal')
from evals.generated_apps.adapters.browser import AuthenticatedBrowser,GeneratedAppBrowserVerifier
from scripts.verify_generated_app_result import captures
from scripts.run_generated_app_journey import workspace_client
async def main():
 f=json.load(open('/private/tmp/wt-r01-generated-app13-final-binding-68bd.json')); url=f['final_candidate']['url']; root=Path('/private/tmp/wt-r01-generated-app13-browser-probe-68bd'); root.mkdir(mode=0o700)
 report={'scope':'simulated_control_tower','control_tower_requests':0,'accepted':False,'source':'genuine_attendee_browser','url':url}
 async with AuthenticatedBrowser('/private/tmp/wt-r01-browser-state-labuser-68bd.json',headless=True) as b:
  response=await b.page.goto(url,wait_until='domcontentloaded',timeout=30000)
  report['initial_http_status']=response.status if response else None
  await b.page.wait_for_timeout(5000)
  parsed=urlsplit(b.page.url);report['final_origin']=parsed.scheme+'://'+parsed.netloc;report['password_field_count']=await b.page.locator('input[type=password]').count()
  if report['password_field_count']==0:
   v=GeneratedAppBrowserVerifier(b.page,artifact_dir=root,timeout_seconds=15)
   await b.page.screenshot(path=str(root/'observed.png'),full_page=True)
   text=await b.page.locator('body').aria_snapshot();(root/'observed.aria.txt').write_text(text);report['aria']=text
   if parsed.netloc==urlsplit(url).netloc:
    report['viewports']=await captures(v,'first-preview',url)
    report['page_errors']=v.page_errors
 p=Path('/private/tmp/wt-r01-generated-app13-browser-probe-68bd.json');assert not p.exists();p.write_text(json.dumps(report,indent=2)+'\n');p.chmod(0o600)
 print(json.dumps(report))
asyncio.run(main())
