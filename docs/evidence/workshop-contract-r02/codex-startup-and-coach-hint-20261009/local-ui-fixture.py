"""Local UI fixture: controlled startup failure, no native CLI/model qualification."""
import os
from pathlib import Path
import sys

ROOT = Path('/Users/a/code/databricks-labs-workshop-terminal')
sys.path.insert(0,str(ROOT))
os.environ.update(LOCAL_DEV='1',DATA_ROOT='/private/tmp/wt-codex-startup-diagnostic/local-data',DEV_FAKE_EMAIL='local-ui-test@example.com',DATABRICKS_HOST='https://example.invalid',ONBOARDING_WIZARD_ENABLED='false',WORKSHOP_AGENTS='claude,codex',ADMIN_GROUP='platform_admins')
from server import main
main.install.ready=lambda: {'claude':True,'codex':True,'omnigent':True}
main.agents.launch_block=lambda *_args: ''
main.ensure_user_credentials=lambda _user:None
main.user_content.provision=lambda _user:None
main.identity.observe=lambda _user:None
def fixture_command(agent):
    if agent['id'] == 'codex':
        return ['/bin/sh','-c','sleep 3; printf "\\r\\nControlled startup failure: diagnostic detail stays visible.\\r\\n"; exit 17']
    return ['/bin/sh','-c','printf "Fixture prompt> "; read -r line; printf "\\r\\nSubmitted fixture input: %s\\r\\n" "$line"']
main.agents.launch_command=fixture_command
import uvicorn
uvicorn.run(main.app, host='127.0.0.1',port=8768)
