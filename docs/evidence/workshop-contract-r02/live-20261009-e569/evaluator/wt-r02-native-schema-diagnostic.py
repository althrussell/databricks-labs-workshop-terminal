import sys, asyncio, json, time
from pathlib import Path
sys.path.insert(0,'/Users/a/code/databricks-labs-workshop-terminal')
from scripts.run_generated_app_journey import StartupQualifiedDriver, validate_receipt
from evals.generated_apps.adapters.browser import AuthenticatedBrowser, BrowserLaunchConfig
root=Path('/private/tmp/wt-eval-r02-1009-e569')
binding=validate_receipt(json.loads((root/'observer-native-notification-update.json').read_text()))
cmd="! python3 -c 'import pathlib,json,collections; p=next((pathlib.Path.home()/\".claude/projects\").rglob(\"0f3a7e14-fbd9-4c68-a364-5550d6257cf0.jsonl\")); r=[json.loads(x) for x in p.read_text().splitlines()]; print(\"WT_RECORD_TYPES\",collections.Counter(x.get(\"type\") for x in r)); print(\"WT_RECORD_KEYS\",sorted(set().union(*(x.keys() for x in r))))'"
assert len(cmd)<500
async def main():
    async with AuthenticatedBrowser(root/'attendee-private-state.json') as b:
        driver=StartupQualifiedDriver(b.page,binding=binding,output=root/'native-schema-diagnostic.json')
        try:
            e=await driver.enter(BrowserLaunchConfig(wt_url=binding['app']['url'],agent_id='claude',opening_message=cmd,
                expected_attendee_email='labuser+1@awsbricks.com',entry_path='skip_wizard',deadline_seconds=120,run_deadline_seconds=180))
            assert not e.error_code, e.error_code
            until=time.monotonic()+30
            while time.monotonic()<until:
                screen=driver._startup_screen()
                if 'Counter({' in screen and 'WT_RECORD_KEYS [' in screen: break
                await asyncio.sleep(.1)
            else: raise RuntimeError('diagnostic_shell_output_unverified')
            await driver._capture_startup('read-only-native-schema',screen)
            report={'schema_version':1,'operation':'read_only_native_schema_diagnostic','scope':'simulated_control_tower',
                'control_tower_mutations':0,'diagnostic_only':True,'model_prompt_sent':False,
                'failed_test_session':'b809e2a2-2e07-4361-b8f1-87cca8dd89c9',
                'native_session':'0f3a7e14-fbd9-4c68-a364-5550d6257cf0','diagnostic_session':e.session_id,
                'screen':screen,'startup':driver.startup_evidence}
            out=root/'native-schema-diagnostic.json'; assert not out.exists()
            out.write_text(json.dumps(report,indent=2)+'\n'); out.chmod(0o600); print(screen)
        finally:
            if driver.evidence.session_id:
                await driver.close_launched_session('Claude Code',expected_session_id=driver.evidence.session_id,timeout_seconds=15)
asyncio.run(main())
