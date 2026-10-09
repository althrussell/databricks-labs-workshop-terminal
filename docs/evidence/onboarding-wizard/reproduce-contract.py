#!/usr/bin/env python3
"""Offline demonstrations of current wizard defects, NOT acceptance tests.

Run from the repository root using its dependencies:
    .venv/bin/python -B docs/evidence/onboarding-wizard/reproduce-contract.py

To save fresh synthetic evidence:
    .venv/bin/python -B docs/evidence/onboarding-wizard/reproduce-contract.py \
        --output docs/evidence/onboarding-wizard/contract-reproduction.json

Model outputs, attendee identities, goals and collision IDs are synthetic. This
script intentionally asserts observed broken behavior, so an assertion failing
after remediation means the demonstration needs retiring or updating; it does
not mean the remediation is wrong. It does not exercise real models, browsers,
Databricks services or coding harnesses. External network connections are blocked,
discovery is private/in-memory, event emission is mocked, and attendee homes are
cleaned at exit. Production source files are not edited.
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import tempfile
import types
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))


def audited_head() -> str:
    return subprocess.check_output(
        ['git', '-C', str(REPO_ROOT), 'rev-parse', 'HEAD'], text=True
    ).strip()


def reproduce(homes: ExitStack, temporary_paths: list[str]) -> dict:
    # Imports and all exercised application functions run with network blocked.
    from server import wizard, wizard_llm, demo_data, discovery, content, user_content
    from server.main import _WizardBody

    # Ignore any operator CONTENT_PACK_PATH so the examples use the shipped pack.
    shipped = content.ContentPack.model_validate(
        json.loads((REPO_ROOT / 'content' / 'default_pack.json').read_text())
    )
    homes.enter_context(patch.object(content.content_service, '_pack', shipped))
    sequence = 0

    def new_user():
        nonlocal sequence
        sequence += 1
        home = homes.enter_context(tempfile.TemporaryDirectory(prefix='wizard-audit-'))
        temporary_paths.append(home)
        return types.SimpleNamespace(email=f'audit-{sequence}@example.com', home=home)

    out = {'purpose': 'Offline defect demonstration; assertions document current broken behavior and are not acceptance tests.'}
    raw = {'industry':'retail', 'ideas':[{'id':'fresh-bakery-app','label':'Bakery staff orders','outcome':'Manage bakery pickup orders','prompt':'Build a staff app: save pickup orders, search by customer, mark fulfilled; use Lakebase.','shape':'app','products':['Databricks Apps','Lakebase'],'demo_tables':[]}]}
    with patch.object(demo_data.config, 'workshop_demo_catalog', return_value=''), patch.object(wizard_llm.config,'llm_wizard_enabled', return_value=True), patch.object(discovery.config,'discovery_enabled',return_value=True), patch.object(discovery, '_emit'), patch.object(discovery,'discovery_store', discovery.DiscoveryStore()):
        with patch.object(wizard_llm,'_ask_model',return_value=(raw,'mock-model')):
            suggestion=wizard_llm.suggest('I run a bakery and lose track of orders')
        card=suggestion['ideas'][0]
        u=new_user()
        payload=_WizardBody(what_building=card['outcome'],idea_id=card['id'],industry='retail',industry_stated=True).model_dump(exclude_unset=True)
        brief=wizard.save(u,payload)
        prompt=wizard.starter_prompt(brief)
        assert card['prompt'] != prompt and 'Lakebase' not in prompt
        assert brief.stated_industry == '' and not brief.industry_stated
        assert wizard.to_discovery(brief)['interest_signals'] == ['wizard_mode:typed']
        assert 'databricks_products' not in wizard.to_discovery(brief)
        out['dynamic_card_roundtrip']={'returned_card':card,'submitted_payload':payload,'saved_brief':asdict(brief),'starter_prompt':prompt,'discovery':wizard.to_discovery(brief),'overlay':user_content._wizard_overlay(u)}

        static=content.content_service.ideas()[0]
        collision_raw={'industry':'retail','ideas':[dict(raw['ideas'][0],id=static.id)]}
        with patch.object(wizard_llm,'_ask_model',return_value=(collision_raw,'mock-model')):
            collision=wizard_llm.suggest('bakery orders')['ideas'][0]
        collision_brief=wizard.save(new_user(),{'what_building':collision['outcome'],'idea_id':collision['id'],'industry':'retail','industry_stated':True})
        launched=wizard.starter_prompt(collision_brief)
        assert collision['prompt'] != launched and launched == static.prompt
        assert collision_brief.stated_industry == wizard.industry_of(static)
        out['dynamic_static_id_collision']={'id':collision['id'],'shown_prompt':collision['prompt'],'submitted_industry':'retail','saved_industry':collision_brief.stated_industry,'launched_prompt':launched,'static_label':static.label}

        u=new_user()
        initial=wizard.save(u,{'what_building':static.outcome,'idea_id':static.id})
        changed=wizard.save(u,{'what_building':'Actually I need a staff booking calendar'})
        assert changed.idea_id == static.id and wizard.starter_prompt(changed) == static.prompt
        out['omitted_idea_id_on_goal_edit']={'new_goal':changed.what_building,'old_idea_id_retained':changed.idea_id,'launched_prompt':wizard.starter_prompt(changed),'caveat':'Supported partial API contract; frontend editing normally clears idea_id.'}
        cleared=wizard.save(u,{'what_building':'','idea_id':'','industry':'','industry_stated':False})
        rec=discovery.discovery_store.for_attendee(u.email)[0]
        assert not cleared.has_content and rec.goal == changed.what_building
        out['clear_all_leaves_discovery']={'saved_has_content':cleared.has_content,'saved_goal':cleared.what_building,'discovery_goal':rec.goal,'same_record_id':rec.record_id == cleared.record_id}

        u=new_user()
        before=wizard.save(u,{'what_building':'Bakery pickup orders','industry':'retail','intent':'business_problem','current_stack':['Spreadsheets']})
        discovery.record(u.email,{'record_id':before.record_id,'blockers':['orders duplicated between staff']})
        refined=discovery.discovery_store.for_attendee(u.email)[0]
        assert refined.goal == '' and refined.industry == '' and refined.current_stack == [] and refined.blockers
        out['partial_agent_refinement_replaces_wizard_fields']={'saved_brief':asdict(before),'record_after_partial_refinement':refined.payload()}
        # A changed wizard save emits a full brief and consequently drops agent facts.
        wizard.save(u, {'what_building':'Bakery pickup orders and fulfillment'})
        after_wizard_edit=discovery.discovery_store.for_attendee(u.email)[0]
        assert after_wizard_edit.blockers == []
        out['wizard_edit_replaces_agent_refinement']={'record':after_wizard_edit.payload()}

        before_prompt=wizard.starter_prompt(initial)
        replacement=static.model_copy(update={'prompt':'Build an unrelated new replacement task'})
        with patch.object(content.content_service,'ideas',return_value=[replacement]):
            after_prompt=wizard.starter_prompt(initial)
        assert before_prompt != after_prompt
        out['live_pack_changes_completed_prompt']={'before':before_prompt,'after':after_prompt}

        u=new_user()
        barrier=Barrier(2)
        original_read=wizard.read_brief
        def synchronized_read(user):
            read=original_read(user)
            barrier.wait(timeout=5)
            return read
        with patch.object(wizard,'read_brief',side_effect=synchronized_read):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(lambda _: wizard.save(u,{'what_building':'Same bakery pickup app'}),range(2)))
        ids=[r.record_id for r in results]
        assert len(set(ids)) == 2 and len(discovery.discovery_store.for_attendee(u.email)) == 2
        out['two_concurrent_first_saves_mint_two_records']={'record_ids':ids,'discovery_count':len(discovery.discovery_store.for_attendee(u.email)),'persisted_record_id':wizard.read_brief(u).record_id}

    with patch.object(demo_data.config,'workshop_demo_catalog',return_value=''), patch.object(discovery,'_emit'), patch.object(discovery,'discovery_store', discovery.DiscoveryStore()):
        u=new_user()
        with patch.object(discovery.config,'discovery_enabled',return_value=False):
            wizard.save(u,{'what_building':'Bakery app'})
        with patch.object(discovery.config,'discovery_enabled',return_value=True):
            wizard.save(u,{'what_building':'Bakery app'})
            claims_record='A discovery record already exists' in user_content._wizard_overlay(u)
            assert discovery.discovery_store.count_for(u.email) == 0 and claims_record
            out['capture_enable_unchanged_brief_claims_nonexistent_record']={'actual_record_count':0,'overlay_claims_existing_record':claims_record}
            wizard.save(u,{'persona':'business'})
            user_content.set_wizard_brief(u,wizard.read_brief(u))
            user_content.set_persona(u,'technical')
            assert user_content.read_persona(u)=='technical' and wizard.read_brief(u).persona=='business'
            out['persona_file_and_wizard_brief_diverge']={'current_persona_file':user_content.read_persona(u),'saved_brief_persona':wizard.read_brief(u).persona}

    out['metadata'] = {
        'audited_head': audited_head(),
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'evidence_type': 'synthetic_offline_current_defect_demonstration',
        'is_synthetic': True,
        'assertions_are_acceptance_tests': False,
        'assertion_status': 'all_current_defect_demonstrations_reproduced',
        'demonstration_count': len(out) - 1,
        'network_access': 'blocked_during_imports_and_reproduction',
        'external_services_exercised': [],
        'model': 'mock-model',
        'attendees': 'synthetic audit identities with temporary homes',
        'discovery': 'isolated in-memory stores; event emission mocked',
        'limitations': [
            'No evidence of event incidence or real model response quality.',
            'Concurrency overlap is deliberately synchronized to demonstrate the race.',
            'Record replacement is documented behavior; the demonstration shows its destructive effects across writers.',
            'Partial API goal edits normally clear idea_id in the current frontend.',
        ],
    }
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--output', type=Path, help='Optional path for the JSON evidence; stdout is always emitted.'
    )
    args = parser.parse_args()
    temporary_paths: list[str] = []
    message = 'Network access is forbidden in this offline demonstration'
    with ExitStack() as homes:
        homes.enter_context(patch.object(socket.socket, 'connect', side_effect=AssertionError(message)))
        homes.enter_context(patch.object(socket.socket, 'connect_ex', side_effect=AssertionError(message)))
        result = reproduce(homes, temporary_paths)
    assert all(not Path(path).exists() for path in temporary_paths)
    result['metadata']['temporary_homes_cleaned'] = True
    result['metadata']['temporary_home_count'] = len(temporary_paths)
    rendered = json.dumps(result, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered, end='')


if __name__ == '__main__':
    main()
