import sys, json
from pathlib import Path
from datetime import datetime, timezone
sys.path.insert(0,'/Users/a/code/databricks-labs-workshop-terminal')
import asyncio
from evals.generated_apps.adapters.browser import AuthenticatedBrowser
root=Path('/private/tmp/wt-eval-r02-1009-e569')
sid='b809e2a2-2e07-4361-b8f1-87cca8dd89c9'
url='https://wt-eval-r02-1009-e569-wt-7474655950495189.aws.databricksapps.com'
async def main():
    async with AuthenticatedBrowser(root/'attendee-private-state.json') as b:
        r=await b.page.goto(url+'/api/config',wait_until='domcontentloaded'); assert r.status==200
        assert (await r.json())['user']['email']=='labuser+1@awsbricks.com'
        r=await b.context.request.get(url+'/api/sessions'); assert r.status==200
        sessions=(await r.json())['sessions']; assert len(sessions)==1 and sessions[0]['id']==sid and sessions[0]['agent_id']=='claude'
        out=root/'attempt-3-controlled-stop.json'; assert not out.exists()
        report={'schema_version':1,'operation':'stop_exact_stalled_observer_test','scope':'simulated_control_tower',
            'control_tower_mutations':0,'session_id':sid,'observed_at':datetime.now(timezone.utc).isoformat(),
            'reason':'Visible native question is absent from bounded native message projection; no simulator answer or technical rescue was sent',
            'question_diagnostic':'terminal-progress-5.txt','native_metadata':'journey-3-native-progress-2.json','status':'stopping'}
        out.write_text(json.dumps(report,indent=2)+'\n'); out.chmod(0o600)
        r=await b.context.request.delete(url+'/api/sessions/'+sid); assert r.status==200
        report.update(status='exact_session_stopped',http_status=r.status)
        out.write_text(json.dumps(report,indent=2)+'\n'); print(json.dumps(report))
asyncio.run(main())
