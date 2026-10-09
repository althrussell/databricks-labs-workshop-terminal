import sys, asyncio, json
from pathlib import Path
from datetime import datetime, timezone
sys.path.insert(0,'/Users/a/code/databricks-labs-workshop-terminal')
import pyte
from evals.generated_apps.adapters.browser import AuthenticatedBrowser
from evals.generated_apps.adapters.harness import redact_evidence
root=Path('/private/tmp/wt-eval-r02-1009-e569')
capture_index=int(sys.argv[1]) if len(sys.argv)>1 else 1
sid='91078d1e-3f46-4e6c-afd3-ad51a13f73bb'
if len(sys.argv)>2:
    from uuid import UUID
    sid=sys.argv[2]
    assert str(UUID(sid))==sid
url='https://wt-eval-r02-1009-e569-wt-7474655950495189.aws.databricksapps.com'
async def main():
    async with AuthenticatedBrowser(root/'attendee-private-state.json',headless=True) as b:
        response=await b.page.goto(url+'/api/config',wait_until='domcontentloaded',timeout=30000)
        assert response.status==200
        me=await response.json();assert me['user']['email']=='labuser+1@awsbricks.com'
        frames=await b.page.evaluate('''sid => new Promise((resolve,reject)=>{const ws=new WebSocket(location.origin.replace('https:','wss:')+'/ws/sessions/'+sid);let frames=[],timer=setTimeout(()=>{ws.close();resolve(frames)},3000);ws.onmessage=e=>{const f=JSON.parse(e.data);if(['replay','output','exit'].includes(f.t)){frames.push(f);if(JSON.stringify(frames).length>2200000){clearTimeout(timer);ws.close();reject(new Error('observation size limit'))}}};ws.onerror=()=>{clearTimeout(timer);ws.close();reject(new Error('observation websocket error'))};})''',sid)
        lines=(root/'journey-1.json.startup-browser/6-native-question-answer-review.txt').read_text().splitlines()
        screen=pyte.Screen(max(map(len,lines)),len(lines));stream=pyte.Stream(screen)
        for f in frames:
            if isinstance(f.get('data'),str):stream.feed(f['data'])
        text=redact_evidence('\n'.join(screen.display))
        out=root/f'terminal-progress-{capture_index}.txt';assert not out.exists();out.write_text(text);out.chmod(0o600)
        report={'observed_at':datetime.now(timezone.utc).isoformat(),'source':'read_only_attendee_websocket_replay_diagnostic','session_id':sid,'assigned_identity_verified':True,'input_frames_sent':0,'resize_frames_sent':0,'auth_state_exported':False,'not_assistant_turn_evidence':True,'capture':str(out),'frame_types':[f['t'] for f in frames]}
        p=root/f'terminal-progress-{capture_index}.json';assert not p.exists();p.write_text(json.dumps(report,indent=2));p.chmod(0o600)
        print(text)
asyncio.run(main())
