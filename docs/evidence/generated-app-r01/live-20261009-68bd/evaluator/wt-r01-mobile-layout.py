import sys,json,asyncio
from pathlib import Path
sys.path.insert(0,'/Users/a/code/databricks-labs-workshop-terminal')
from evals.generated_apps.adapters.browser import AuthenticatedBrowser
async def main():
 url='https://bakery-orders-7474655950495189.aws.databricksapps.com';report={'operation':'read_only_mobile_layout','scope':'simulated_control_tower','control_tower_requests':0,'app_mutations':0,'accepted':False,'viewport':{'width':390,'height':844}}
 async with AuthenticatedBrowser('/private/tmp/wt-r01-generated-browser-state-labuser-68bd.json',headless=True) as b:
  await b.page.set_viewport_size(report['viewport']);await b.page.goto(url,wait_until='domcontentloaded');button=b.page.get_by_role('button',name='Packed',exact=True);await button.wait_for(timeout=15000)
  report['packed_button_box']=await button.bounding_box()
  report['table_layout']=await b.page.get_by_role('table').evaluate('''table => {const result=[];for(let e=table;e;e=e.parentElement){if(e.scrollWidth>e.clientWidth){result.push({tag:e.tagName,clientWidth:e.clientWidth,scrollWidth:e.scrollWidth,overflowX:getComputedStyle(e).overflowX});}}return result;}''')
  report['page_horizontal_overflow']=await b.page.evaluate('document.documentElement.scrollWidth > innerWidth')
  report['action_initially_within_viewport']=report['packed_button_box']['x']+report['packed_button_box']['width']<=390
  # Exercise normal horizontal UI scrolling without activating the order action.
  await b.page.get_by_role('table').hover();await b.page.mouse.wheel(650,0);await b.page.wait_for_timeout(500)
  report['packed_button_box_after_horizontal_scroll']=await button.bounding_box()
 p=Path('/private/tmp/wt-r01-generated-app13-mobile-layout-68bd.json');assert not p.exists();p.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))
asyncio.run(main())
