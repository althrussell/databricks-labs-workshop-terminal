"""Behavioral delivery regressions for R05's single upstream UX authority."""
from pathlib import Path
import json
import shutil
import subprocess
import tarfile
import zipfile

import pytest

from server.bootstrap import impeccable
from server.bootstrap.artifacts import directory_checksum, load_manifest
from server.bootstrap.skill_projection import RETIRED_UX_SKILLS, project_skills

ROOT = Path(__file__).resolve().parents[1]


def test_projection_retires_routes_without_changing_api_content(tmp_path):
    for name in RETIRED_UX_SKILLS:
        (tmp_path / name).mkdir()
        (tmp_path / name / 'SKILL.md').write_text('obsolete')
    apps = tmp_path / 'databricks-apps/SKILL.md'
    apps.parent.mkdir()
    apps.write_text('**For data UI design (required)**: use `databricks-app-design`\n\n## API\nkeep the actual signature unchanged\n')
    before = directory_checksum(tmp_path)
    project_skills(tmp_path)
    after = directory_checksum(tmp_path)
    assert before != after
    assert not any((tmp_path / name).exists() for name in RETIRED_UX_SKILLS)
    assert 'impeccable' in apps.read_text()
    assert '## API\nkeep the actual signature unchanged\n' in apps.read_text()
    project_skills(tmp_path)
    assert directory_checksum(tmp_path) == after


def test_packaged_fallback_is_the_reviewed_compiled_skill():
    skill = ROOT / 'assets/skills/impeccable'
    entry = load_manifest('')['artifacts']['impeccable_skill_bundle']
    assert directory_checksum(skill) == entry['content_sha256']
    assert (skill / 'scripts/VERSION').read_text().strip() == impeccable.ENGINE_VERSION
    assert not (skill / 'scripts/bin').exists(), 'native binary must stay out of Git/project copies'
    for name in RETIRED_UX_SKILLS:
        assert not (ROOT / 'assets/skills' / name).exists()
    for root in (ROOT / 'assets/instructions', ROOT / 'assets/skills'):
        for path in root.rglob('*.md'):
            if path.name == 'SKILLS_SOURCE.md':
                continue  # provenance records the retired names
            assert not any(name in path.read_text() for name in RETIRED_UX_SKILLS), path


@pytest.fixture
def installer_inputs(tmp_path):
    skill_root = tmp_path / 'bundle'
    for provider in ('.agents', '.claude'):
        skill = skill_root / provider / 'skills/impeccable'
        (skill / 'scripts').mkdir(parents=True)
        (skill / 'SKILL.md').write_text('upstream skill')
        (skill / 'scripts/VERSION').write_text(impeccable.ENGINE_VERSION + '\n')
        (skill / 'scripts/impeccable').write_text('#!/bin/sh\n')
    digest = directory_checksum(skill_root / '.agents/skills/impeccable')
    bundle = Path(shutil.make_archive(str(tmp_path / 'bundle-archive'), 'zip', skill_root))
    package = tmp_path / 'npm/package/cli/bin'
    package.mkdir(parents=True)
    (package / 'cli.js').write_text('official npm shim')
    npm = tmp_path / 'launcher.tgz'
    with tarfile.open(npm, 'w:gz') as archive:
        archive.add(tmp_path / 'npm/package', arcname='package')
    engine = tmp_path / 'native'
    engine.write_text('#!/bin/sh\necho impeccable-engine ' + impeccable.ENGINE_VERSION + '\n')
    entries = {
        'impeccable_npm_launcher': (str(npm), {'version': impeccable.LAUNCHER_VERSION}),
        'impeccable_engine_linux_x64': (str(engine), {'version': impeccable.ENGINE_VERSION}),
        'impeccable_skill_bundle': (str(bundle), {'version': impeccable.SKILL_VERSION, 'content_sha256': digest}),
    }
    return entries


def test_supported_installer_uses_pinned_bundle_engine_and_no_hooks(tmp_path, monkeypatch, installer_inputs):
    prefix = tmp_path / 'shared'
    (prefix / 'bin').mkdir(parents=True)
    target = tmp_path / 'skills'
    target.mkdir()
    calls = []

    def installer(argv, *, cwd, env, **kwargs):
        calls.append(argv)
        assert argv[-5:] == ['install', '--providers=claude,codex', '--scope=project', '--no-hooks', '--yes']
        bundle = Path(env['IMPECCABLE_BUNDLE_PATH'])
        assert Path(env['IMPECCABLE_BIN']).is_file()
        for provider in ('.claude', '.agents'):
            source = bundle / provider / 'skills/impeccable'
            assert (source / 'scripts/bin/linux-x64/impeccable').is_file()
            shutil.copytree(source, Path(cwd) / provider / 'skills/impeccable')
        return subprocess.CompletedProcess(argv, 0, '', '')

    monkeypatch.setattr(impeccable.subprocess, 'run', installer)
    impeccable.install_skill(str(target), str(prefix), installer_inputs.__getitem__, {})
    assert len(calls) == 1
    assert directory_checksum(target / 'impeccable') == installer_inputs['impeccable_skill_bundle'][1]['content_sha256']
    assert not (target / 'impeccable/scripts/bin').exists()
    assert (prefix / 'bin/impeccable').stat().st_mode & 0o111
    assert 'IMPECCABLE_BIN=' in (prefix / 'bin/impeccable').read_text()


def test_bundle_path_traversal_is_rejected_before_install(tmp_path, installer_inputs):
    archive = Path(installer_inputs['impeccable_skill_bundle'][0])
    with zipfile.ZipFile(archive, 'a') as bundle:
        bundle.writestr('../escaped', 'must not be written')
    prefix = tmp_path / 'shared'
    prefix.mkdir()
    with pytest.raises(RuntimeError, match='escaping path'):
        impeccable.install_skill(str(tmp_path / 'skills'), str(prefix), installer_inputs.__getitem__, {})
    assert not (tmp_path / 'escaped').exists()


def test_warm_fork_refresh_retires_obsolete_ux_even_if_current_forks_match(tmp_path, monkeypatch):
    from server.bootstrap import install

    assets, target = tmp_path / 'assets', tmp_path / 'shared/skills'
    for root in (assets, target):
        (root / 'impeccable').mkdir(parents=True)
        (root / 'impeccable/SKILL.md').write_text('current pinned UX')
    for name in RETIRED_UX_SKILLS:
        (target / name).mkdir()
        (target / name / 'SKILL.md').write_text('old UX')
    (target / 'my-skill').mkdir()
    (target / 'my-skill/SKILL.md').write_text('custom')
    monkeypatch.setattr(install, '_ASSETS_SKILLS', str(assets))
    assert not install._fork_skills_current(str(target))
    install._refresh_fork_skills(str(target.parent), str(target))
    assert install._fork_skills_current(str(target))
    assert not any((target / name).exists() for name in RETIRED_UX_SKILLS)
    assert (target / 'my-skill/SKILL.md').read_text() == 'custom'
