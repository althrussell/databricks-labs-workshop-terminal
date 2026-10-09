"""Opt-in rendered adapter contract; every backend is a synthetic local fixture.

Run externally with WORKSHOP_E2E_BROWSER_TESTS=1. This does not qualify a cloud
harness, Unity Gateway, generated application, or app-process restart.
"""

import asyncio
import json
import os
import pytest
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from evals.generated_apps.adapters.browser import (
    BackendPersistenceEvidence, BrowserAction, BrowserLaunchConfig,
    GeneratedAppBrowserVerifier, RoleLocator, WorkshopBrowserDriver,
)

WT = '''<!doctype html><html><head><style>
.modal-backdrop{position:fixed;inset:0;background:#ccc9;display:grid;place-items:center;z-index:10}
.hero{padding-top:300px}
</style></head><body><div id="app"></div><script>
let wizard=true,skipped=false,industry=false,goal='',socket;
async function request(path, body) {const r=await fetch(path,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});return r.json();}
function wireTerminal(){ const text=document.querySelector('.xterm-helper-textarea');socket=new WebSocket('wss://wt.test/ws/sessions/fresh-session');socket.onopen=()=>socket.send(JSON.stringify({t:'resize',cols:120,rows:40})); text.oninput=e=>socket.send(JSON.stringify({t:'input',data:e.data||text.value}));text.onkeydown=e=>{if(e.key==='Enter')socket.send(JSON.stringify({t:'input',data:'\\r'}));}; }
async function launch(){await request('/api/sessions',{agent_id:'claude'});document.querySelector('#app').innerHTML='<div class="terminal-container"><textarea class="xterm-helper-textarea"></textarea></div>';wireTerminal();if(wizard&&!skipped){await request('/api/sessions/fresh-session/type',{text:goal});}}
async function next(){goal=document.querySelector('textarea').value;await request('/api/wizard',{what_building:goal,industry:'retail',persona:'business'});document.querySelector('[role=dialog]').innerHTML='<h2>Pick your agent and go</h2><div class="hero-cards"><button onclick="launch()">Claude Code ready</button></div>';}
async function skip(){await request('/api/wizard',{skipped:true});skipped=true;document.querySelector('[role=dialog]').remove();}
async function start(){const [c,a,s]=await Promise.all([request('/api/config'),request('/api/agents'),request('/api/sessions')]);wizard=c.onboarding_wizard.enabled;
document.querySelector('#app').innerHTML='<div class="hero"><div class="hero-cards"><button onclick="launch()">Claude Code ready</button></div></div>';
const state=await request('/api/wizard');skipped=!state.should_show;
if(state.enabled&&state.should_show){document.querySelector('#app').insertAdjacentHTML('beforeend','<div role="dialog" class="modal-backdrop"><div><button>Retail</button><textarea></textarea><button>A little context (optional)</button><button style="display:none" id="plain">Plain language</button><button disabled id="next">Next</button><button onclick="skip()">Skip</button></div></div>');const d=document.querySelector('[role=dialog]');d.querySelector('button').onclick=()=>{industry=true;document.querySelector('#next').disabled=false;};d.querySelectorAll('button')[1].onclick=()=>document.querySelector('#plain').style.display='block';document.querySelector('#next').onclick=next;}}
start();
</script></body></html>'''

APP = '''<!doctype html><html><body><h1>Orders</h1><p>Sample orders</p><table><tr><th>Order</th><th>Status</th><th>Action</th></tr><tr><td>BK-1001</td><td id="status"></td><td><button id="pack">Pack</button></td></tr></table><script>
async function state(){const r=await fetch('/state');const s=await r.json();document.querySelector('#status').textContent=s.packed?'Packed':'Unpacked';}
document.querySelector('#pack').onclick=async()=>{await fetch('/pack',{method:'POST'});await state();};state();
</script></body></html>'''

async def main():
    web = pytest.importorskip("aiohttp.web")
    async_playwright = pytest.importorskip("playwright.async_api").async_playwright
    logs=[];wt_state={'enabled':True,'seen':False};storage={'packed':False};entry_events=[]
    goal='Help with bakery orders.'
    async def handler(request):
        path=request.path
        if path.startswith('/ws/sessions/'):
            ws=web.WebSocketResponse();await ws.prepare(request)
            async for message in ws:
                await ws.send_json({'t':'output','data':'Synthetic ready screen'})
            return ws
        if path=='/':return web.Response(text=WT.replace('wss://wt.test','ws://'+request.host),content_type='text/html')
        if path=='/app':return web.Response(text=APP,content_type='text/html')
        if path=='/state':return web.json_response(storage)
        if path=='/pack':storage['packed']=True;return web.json_response(storage)
        if path=='/api/config':body={'user':{'email':'attendee@example.com'},'onboarding_wizard':{'enabled':wt_state['enabled']}}
        elif path=='/api/agents':body={'agents':[{'id':'claude','label':'Claude Code','ready':True}]}
        elif path=='/api/sessions':
            if request.method=='POST':entry_events.append('launch')
            body={'sessions':[]} if request.method=='GET' else {'session':{'id':'fresh-session','agent_id':'claude'}}
        elif path=='/api/wizard':
            if request.method=='GET':
                # The actual WT renders Hero before this separate request
                # completes. A home-card visibility check races onboarding.
                await asyncio.sleep(.5)
                entry_events.append('wizard_state')
                body={'enabled':wt_state['enabled'],'should_show':wt_state['enabled'] and not wt_state['seen']}
            else:
                payload=await request.json()
                entry_events.append('skip' if payload.get('skipped') else 'save')
                body={'brief':{'what_building':goal,'industry':'retail','persona':'business'},'starter_prompt':goal}
        else:body={'status':'ok'}
        return web.json_response(body)
    app=web.Application();app.router.add_route('*','/{tail:.*}',handler)
    server=web.AppRunner(app);await server.setup();site=web.TCPSite(server,'127.0.0.1',0);await site.start()
    wt_url='http://127.0.0.1:'+str(site._server.sockets[0].getsockname()[1])
    try:
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True)
            try:
                for arm in ['gate','compatible','disabled','skip','returning']:
                    context=await browser.new_context();wt_state['enabled']=arm!='disabled';wt_state['seen']=arm=='returning';entry_events.clear()
                    page=await context.new_page();driver=WorkshopBrowserDriver(page)
                    result=await driver.enter(BrowserLaunchConfig(wt_url=wt_url,agent_id='claude',opening_message=goal,
                        expected_attendee_email='attendee@example.com',entry_path='wizard_disabled' if arm=='disabled' else 'skip_wizard' if arm in {'skip','returning'} else 'wizard',
                        allow_industry_step=arm=='compatible',industry='Retail' if arm=='compatible' else None,deadline_seconds=10))
                    assert result.error_code==('industry_required' if arm=='gate' else ''),(result.to_dict(),driver.harness.evidence())
                    if arm!='gate':
                        await asyncio.sleep(.05)
                        assert result.opening_submitted and driver.harness.inputs
                    if arm=='skip':assert entry_events==['wizard_state','skip','launch'],entry_events
                    if arm=='returning':assert entry_events==['wizard_state','launch'],entry_events
                    logs.append({'arm':arm,'error_code':result.error_code,'opening_submitted':result.opening_submitted,
                        'frames_observed':len(driver.harness.frames),'consultation_verified':False})
                    await context.close()
                async def app_context():return await browser.new_context()
                context=await app_context();page=await context.new_page();app_url=wt_url+'/app'
                verifier=GeneratedAppBrowserVerifier(page,artifact_dir=Path(tempfile.mkdtemp(prefix='r01-browser-smoke-')))
                await verifier.open(app_url)
                results=await verifier.run_actions([BrowserAction('pack an order','click',RoleLocator('button','Pack')),
                    BrowserAction('observe update','expect_text',RoleLocator('row','BK-1001',exact=False),'Packed')])
                assert all(r['passed'] for r in results),results
                async def backend():return BackendPersistenceEvidence('synthetic independent fixture dictionary','BK-1001','packed',True,storage['packed'],True)
                persist=await verifier.verify_persistence(reload_actions=[BrowserAction('packed after reload','expect_text',RoleLocator('row','BK-1001',exact=False),'Packed')],
                    second_context_factory=app_context,app_url=app_url,second_context_actions=[BrowserAction('packed in fresh context','expect_text',RoleLocator('row','BK-1001',exact=False),'Packed')],backend_observer=backend)
                assert persist['persistence_verified'],persist
                capture=await verifier.capture_viewport('mobile',390,844)
                assert not capture['layout_observation']['horizontalOverflow']
                logs.append({'app_tasks':results,'synthetic_persistence':persist,'screenshot_and_aria':'captured'})
                await context.close()
            finally:await browser.close()
    finally:await server.cleanup()
    print(json.dumps({'source':'synthetic rendered UI plus localhost HTTP/WebSocket; no workspace access','results':logs}))

@pytest.mark.skipif(os.environ.get("WORKSHOP_E2E_BROWSER_TESTS") != "1", reason="opt-in external Chromium/localhost contract test")
def test_real_rendered_ui_and_websocket_contract():
    asyncio.run(main())
