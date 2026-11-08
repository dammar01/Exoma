"""Reviewer-context regressions, reached by the existing Codex install check and --only."""
import contextlib
import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from exoma.installer import base, codex_policy, rollback, uninstall
from tests.checks.support import assert_true, no_console


def check_codex_auto_review():
    body = (base.DIST_CONFIG / codex_policy.TEMPLATE_KEY).read_text(encoding="utf-8")
    for banner, expected in (
        ('codex-cli 0.163.0', True), ('0.163.0', True),
        ('codex-cli 0.163.0-beta', False), ('codex-cli 0.163.0+local', False),
        ('codex-cli 0.163.0.1', False), ('codex-cli 0.163.1', False),
    ):
        with patch('exoma.core.provider.versions.read_version', return_value=banner):
            assert_true(codex_policy.cli_supported() is expected,
                        'compatibility requires the complete tested release: ' + banner)
    import tomllib
    samples = [
        '', 'model = "mine"', 'model = "mine"\n',
        '[auto_review]\npolicy = "keep replacement"\n',
        '[auto_review]\nextra_policy = \'user rule\' # user comment\n',
        '[auto_review]\nextra_policy = "user \\"quote\\" and \\\\path"\n[other]\nx = 1\n',
        "[auto_review]\nextra_policy = '''user\nmultiline'''\n",
        '[auto_review]\nextra_policy = """user\nmultiline"""\n',
        "notes = '''\n[auto_review]\nextra_policy = fake\n'''\n",
        'auto_review.extra_policy = "dotted user policy"\n',
    ]
    for original in samples:
        merged = codex_policy.merge(original, body, True)
        assert_true(not merged["error"], f"supported TOML merges: {merged['error']}: {original!r}")
        text = merged["text"]
        assert_true(codex_policy.merge(text, body, True)["text"] == text,
                    "repeated installation is byte-idempotent")
        parsed = tomllib.loads(text)
        assert_true(parsed["auto_review"]["extra_policy"].count(codex_policy.START) == 1,
                    "exactly one owned block, even with an existing extra policy")
        removed = codex_policy.merge(text, '', False)
        assert_true(not removed["error"] and removed["text"] == original,
                    f"uninstall restores untouched user bytes: {removed}: {original!r}")
        updated = codex_policy.merge(text, body + '\nAdditional context.', True)
        assert_true(not updated["error"] and updated["status"] == 'outdated'
                    and codex_policy.merge(updated["text"], '', False)["text"] == original,
                    "template updates preserve the original user value")
        edited = text.replace('Routine inspection', 'Changed inspection')
        assert_true(codex_policy.merge(edited, body, True)["status"] == 'conflict'
                    and codex_policy.merge(edited, '', False)["text"] == edited,
                    "edited owned text is preserved on refresh and removal")
        meta_line = next(line for line in text.splitlines() if line.startswith(codex_policy.META))
        metadata = json.loads(meta_line[len(codex_policy.META):])
        metadata['new_key'] = not metadata['new_key']
        changed_meta = text.replace(meta_line, codex_policy.META + json.dumps(metadata))
        assert_true(codex_policy.merge(changed_meta, '', False)['status'] == 'conflict',
                    'edited ownership metadata cannot remove unrelated user text')

    original = '[auto_review]\npolicy = "my replacement"\nextra_policy = "user"\n'
    installed = codex_policy.merge(original, body, True)["text"]
    value = tomllib.loads(installed)["auto_review"]["extra_policy"]
    edited = installed.replace(json.dumps(value, ensure_ascii=False),
                               json.dumps('prefix\n' + value + '\nsuffix', ensure_ascii=False))
    removed = codex_policy.merge(edited, '', False)
    parsed = tomllib.loads(removed["text"])
    assert_true(parsed['auto_review']['extra_policy'] == 'prefix\nuser\nsuffix'
                and parsed['auto_review']['policy'] == 'my replacement',
                "user prefix/suffix edits and replacement policy survive uninstall")
    for malformed in ('[auto_review\n', '[auto_review]\nextra_policy = 2\n',
                      installed.replace(codex_policy.END, ''), installed + installed,
                      'auto_review = { extra_policy = "inline" }\n'):
        result = codex_policy.merge(malformed, body, True)
        assert_true(result['error'] and result['text'] == malformed,
                    "invalid, ambiguous or unsupported layouts stay untouched")
    with patch.object(codex_policy, '_parse', side_effect=ValueError('unparseable')):
        failed = codex_policy.merge('model = "user"\n', body, True)
        assert_true(failed['status'] == 'conflict' and failed['text'] == 'model = "user"\n',
                    "a parse failure is a conflict that writes nothing, never a successful install")
    _lifecycle(body)
    _missing_template()
    _choice()
    _doctor_agrees(body)
    _orchestration()


def _doctor_agrees(body):
    """Doctor classifies the owned block with the installer's classifier, never by existence."""
    from exoma.core.audit import bundle_integrity

    key = codex_policy.TEMPLATE_KEY
    with tempfile.TemporaryDirectory(prefix='codex-policy-doctor-') as tmp:
        home = Path(tmp)
        dist = home / 'dist'
        (dist / 'hosts/codex').mkdir(parents=True)
        (dist / key).write_text(body, encoding='utf-8')
        config = home / '.codex/config.toml'
        config.parent.mkdir()
        installed = codex_policy.merge('model = "user"\n', body, True)['text']
        cases = [
            # (config text or None for absent, enabled, expected reason or None for clean)
            (None, False, None),
            ('model = "user"\n', False, None),
            (installed, True, None),
            ('model = "user"\n', True, 'missing'),
            (installed.replace('Routine inspection', 'Changed inspection'), True, 'conflict'),
            (codex_policy.merge('model = "user"\n', body + '\nOld.', True)['text'], True, 'outdated'),
            (installed, False, 'removed'),
            ('[auto_review\n', True, 'conflict'),
        ]
        for text, enabled, expected in cases:
            if text is None:
                config.unlink(missing_ok=True)
            else:
                config.write_text(text, encoding='utf-8')
            before = config.read_bytes() if config.exists() else None
            result = {'checked': 0, 'mismatched': [], 'missing': [], 'drift': []}
            with patch.object(bundle_integrity, '_install_mode',
                              return_value={'codex_auto_review_context': enabled}), \
                    patch.object(codex_policy, 'cli_supported', return_value=True), \
                    patch.object(codex_policy, 'location_supported', return_value=True):
                bundle_integrity._audit_codex_auto_review(result, key, config, dist, home)
                agreed = codex_policy.installation_status(text or '', dist / key, enabled, home)
            assert_true((config.read_bytes() if config.exists() else None) == before,
                        'doctor never writes the config it audits')
            if expected is None:
                assert_true(not result['drift'] and not result['missing'] and not result['mismatched'],
                            f'clean state stays clean ({enabled}, {text!r}): {result}')
                assert_true(agreed in ('installed', 'disabled'), f'install --check agrees: {agreed}')
                continue
            reasons = ' '.join(row['reason'] for row in result['drift'])
            assert_true(expected.replace('-', '_') in reasons.replace('-', '_') and agreed == expected,
                        f'doctor and install --check agree on {expected}: {result} / {agreed}')
            listed = result['missing'] if expected == 'missing' else result['mismatched']
            assert_true(listed == [key], f'{expected} lands in the matching list: {result}')
        config.write_text(installed or 'model = "user"\n', encoding='utf-8')
        result = {'checked': 0, 'mismatched': [], 'missing': [], 'drift': []}
        with patch.object(bundle_integrity, '_install_mode', return_value={'codex_auto_review_context': True}), \
                patch.object(codex_policy, 'cli_supported', return_value=False):
            bundle_integrity._audit_codex_auto_review(result, key, config, dist, home)
        assert_true(any('support-unverified' in row['reason'] for row in result['drift']),
                    f'an unverified CLI is reported, not passed as clean: {result}')
        # Doctor decodes the config like the installer does: an ANSI-written comment gets the
        # same verdict from both entry points instead of `unreadable` in one of them.
        from exoma.installer.base import _read_text_lenient
        ansi = (installed or 'model = "user"\n').encode('utf-8') + '# café — note\n'.encode('cp1252')
        for enabled, raw in ((True, ansi), (False, b'model = "user"\n# caf\xe9\n'), (True, b'\xff\xfe broken')):
            config.write_bytes(raw)
            result = {'checked': 0, 'mismatched': [], 'missing': [], 'drift': []}
            with patch.object(bundle_integrity, '_install_mode',
                              return_value={'codex_auto_review_context': enabled}), \
                    patch.object(codex_policy, 'cli_supported', return_value=True), \
                    patch.object(codex_policy, 'location_supported', return_value=True):
                bundle_integrity._audit_codex_auto_review(result, key, config, dist, home)
                agreed = codex_policy.installation_status(_read_text_lenient(config), dist / key, enabled, home)
            reasons = [row['reason'] for row in result['drift']]
            assert_true('unreadable' not in reasons and config.read_bytes() == raw,
                        f'a decodable config is classified, not called unreadable: {result}')
            if agreed in ('installed', 'disabled'):
                assert_true(not result['drift'] and not result['missing'],
                            f'doctor is clean exactly when install --check is ({agreed}): {result}')
            else:
                assert_true(any(agreed.replace('-', '_') in r.replace('-', '_') for r in reasons),
                            f'doctor reports what install --check reports ({agreed}): {result}')


def _missing_template():
    from exoma.installer import check
    with tempfile.TemporaryDirectory(prefix='codex-policy-missing-') as tmp:
        home = Path(tmp)
        output = io.StringIO()
        with patch.object(base, 'HOME', home), patch.object(check, 'HOME', home), \
                patch.object(check, 'DIST_CONFIG', home / 'missing-bundle'), \
                patch.object(check, '_installed_hosts', return_value=('codex',)), \
                patch.object(check, '_stored_codex_auto_review_context', return_value=True), \
                patch.object(check, '_targets', return_value=[]), \
                patch.object(codex_policy, 'cli_supported', return_value=True), \
                patch.object(codex_policy, 'location_supported', return_value=True), \
                contextlib.redirect_stdout(output):
            code = check._run_check({'files': [], 'targets': {}})
        assert_true(code == 1 and 'template-unreadable' in output.getvalue(),
                    'a missing template is controlled installation drift, not an exception')


def _lifecycle(body):
    with tempfile.TemporaryDirectory(prefix='codex-policy-lifecycle-') as tmp:
        home = Path(tmp)
        mode = home / '.claude/.workflow-install-mode.json'
        with patch.object(base, 'HOME', home), patch.object(uninstall, 'HOME', home), \
                patch.object(rollback, 'HOME', home), patch.object(rollback, '_MODE_FILE', mode), \
                patch.object(codex_policy, 'cli_supported', return_value=True), \
                patch.dict(os.environ, {'CODEX_HOME': str(home / '.codex')}):
            dest = base.codex_config_dest()
            dest.parent.mkdir(parents=True)
            original = 'model = "user"\n[auto_review]\npolicy = "user replacement"\nextra_policy = \'user rule\'\n'
            dest.write_bytes(original.encode('utf-8'))
            if os.name != 'nt':
                dest.chmod(0o600)
            backup = home / '.claude/backups/install_policy'
            base._reset_receipt()
            try:
                base._begin_receipt(backup / 'install_receipt.json', {'hosts': ['codex']})
                base._install_codex_grant(base.Plan(), True, backup, True)
                assert_true(len(base._RECEIPT) == 1 and base.CODEX_GRANT_MARKERS[0] in dest.read_text(encoding='utf-8')
                            and codex_policy.START in dest.read_text(encoding='utf-8'),
                            "grant and context share a single config receipt and original backup")
                merged = dest.read_bytes()
                if os.name != 'nt':
                    assert_true(stat.S_IMODE(dest.stat().st_mode) == 0o600,
                                'atomic installation preserves private config permissions')
                base._install_codex_grant(base.Plan(), True, backup, True)
                assert_true(dest.read_bytes() == merged and len(base._RECEIPT) == 1,
                            "reinstall does not overwrite original backup or append a second receipt")
                base._flush_receipt(complete=True)
                with contextlib.redirect_stdout(io.StringIO()):
                    code = rollback._run_rollback('install_policy', True)
                assert_true(code == 0 and dest.read_bytes() == original.encode('utf-8'),
                            "combined transaction rollback restores original user bytes")

                base._reset_receipt()
                if os.name != 'nt':
                    dest.chmod(0o600)
                base._install_codex_grant(base.Plan(), True, home / 'second-backup', True)
                dest.write_bytes(dest.read_bytes() + b'\n[another_project]\nkeep = true\n')
                uninstall._uninstall_codex_grant(base.Plan(), True, home / 'uninstall-backup')
                if os.name != 'nt':
                    assert_true(stat.S_IMODE(dest.stat().st_mode) == 0o600,
                                'atomic uninstall preserves private config permissions')
                text = dest.read_text(encoding='utf-8')
                assert_true(text.startswith(original) and '[another_project]' in text
                            and codex_policy.START not in text and base.CODEX_GRANT_MARKERS[0] not in text,
                            "combined uninstall keeps later user sections and original policy")
                before = dest.read_bytes()
                with patch.object(codex_policy, 'cli_supported', return_value=False):
                    plan = base.Plan()
                    base._install_codex_grant(plan, True, home / 'unsupported', True)
                    assert_true(dest.read_bytes() == before and plan.codex_policy_problem,
                                "unverified CLI support cannot partially mutate config")
                with patch.dict(os.environ, {'CODEX_HOME': str(home / 'different')}):
                    plan = base.Plan()
                    base._install_codex_grant(plan, True, home / 'wrong-home', True)
                    assert_true(dest.read_bytes() == before and plan.codex_policy_problem,
                                "ambiguous CODEX_HOME does not write a second or incorrect config")
                rollback._store_install_mode(False, ('codex',), False, [], False, base.Plan(), True,
                                             home / 'mode-backup', codex_auto_review_context=True)
                assert_true(rollback._stored_codex_auto_review_context(), 'opt-in persists for no-input update')
                rollback._store_install_mode(False, ('codex',), False, [], False, base.Plan(), True,
                                             home / 'mode-backup2', codex_auto_review_context=False)
                assert_true(not rollback._stored_codex_auto_review_context(), 'explicit opt-out persists')
            finally:
                base._reset_receipt()


def _choice():
    from exoma import install
    args = SimpleNamespace(only_command=False, with_e2e=True, agy_permissions=False,
                           codex_auto_review_context=None, update_providers=True, provider='codex')
    with patch.object(install, '_resolve_hosts', return_value=('codex',)), \
            patch.object(install, '_ask_yes_no', return_value=True) as ask, \
            patch.object(install, '_stored_codex_auto_review_context', return_value=False):
        install._ask_choices(args, 'codex', True, base.Plan(), True, True)
        assert_true(args.codex_auto_review_context is True and ask.call_count == 1,
                    'interactive Codex host asks the additive choice once')
    args.codex_auto_review_context = None
    with patch.object(install, '_resolve_hosts', return_value=('codex',)), \
            patch.object(install, '_ask_yes_no', side_effect=AssertionError('must not ask')):
        install._ask_choices(args, 'codex', True, base.Plan(), False, True)
        assert_true(args.codex_auto_review_context is None, 'no-input never manufactures consent')


def _orchestration():
    """Exercise actual installer flags, check, update, deselection and uninstall in a fake home."""
    with tempfile.TemporaryDirectory(prefix='codex-policy-install-') as tmp:
        home = Path(tmp)
        fakebin = home / 'bin'
        fakebin.mkdir()
        command = fakebin / ('codex.cmd' if os.name == 'nt' else 'codex')
        command.write_text('@echo off\necho codex-cli 0.163.0\n' if os.name == 'nt'
                           else '#!/bin/sh\nprintf "codex-cli 0.163.0\\n"\n', encoding='utf-8')
        if os.name != 'nt':
            command.chmod(0o755)
        codex = home / '.codex'
        codex.mkdir()
        config = codex / 'config.toml'
        original = 'model = "user"\n[auto_review]\npolicy = "keep my replacement"\n'
        config.write_bytes(original.encode('utf-8'))
        project = home / 'project'
        (project / '.workflow').mkdir(parents=True)
        (project / '.workflow/second_agent.json').write_text(
            json.dumps({'provider': 'codex'}), encoding='utf-8')
        env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), AGENT_HOME=str(home),
                   EXOMA_HOME=str(home / 'data'), CODEX_HOME=str(codex),
                   AGENT_ENV_FILE=str(home / 'no-env'),
                   PYTHONIOENCODING='utf-8',
                   PYTHONPATH=str(Path(__file__).resolve().parents[2] / 'src'),
                   PATH=str(fakebin) + os.pathsep + os.environ.get('PATH', ''))
        env.pop('WORKFLOW_AGENT_ROLE', None)

        def run(*args, expected=0):
            result = subprocess.run([sys.executable, '-m', 'exoma.install', *args],
                                    env=env, cwd=home, capture_output=True, text=True,
                                    encoding='utf-8', errors='replace', timeout=90, **no_console())
            assert_true(result.returncode == expected,
                        (f'installer {args}: rc={result.returncode}: {result.stdout[-1800:]} {result.stderr[-800:]}')
                        .encode('ascii', 'backslashreplace').decode('ascii'))
            return result

        def doctor():
            result = subprocess.run([
                sys.executable, '-c',
                'import json; from pathlib import Path; from exoma.installer.base import DIST_CONFIG; '
                'from exoma.core.audit.bundle_integrity import _bundle_integrity; '
                'print(json.dumps(_bundle_integrity(DIST_CONFIG, DIST_CONFIG.parent / "manifest.json", Path.cwd() / "project")))',
            ], env=env, cwd=home, capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=90, **no_console())
            assert_true(result.returncode == 0, 'doctor bundle inspection completes: ' + result.stderr)
            return json.loads(result.stdout)

        def check_bundle(enabled):
            checked = run('--check')
            status = 'installed' if enabled else 'disabled'
            assert_true('Auto-review context: ' + status in checked.stdout,
                        'install-check agrees with the saved reviewer-context choice')
            result = subprocess.run([
                sys.executable, '-c',
                'import json; from pathlib import Path; from exoma.installer.base import DIST_CONFIG; '
                'from exoma.core.audit.bundle_integrity import _bundle_integrity; '
                'print(json.dumps(_bundle_integrity(DIST_CONFIG, DIST_CONFIG.parent / "manifest.json", Path.cwd() / "project")))',
            ], env=env, cwd=home, capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=90, **no_console())
            assert_true(result.returncode == 0, 'doctor bundle inspection completes: ' + result.stderr)
            bundle = json.loads(result.stdout)
            assert_true(not bundle.get('error') and not bundle['missing'] and not bundle['drift'],
                        'doctor agrees with a clean Codex install: ' + str({
                            key: bundle.get(key) for key in ('error', 'missing', 'drift')}))

        run('codex', '--no-input', '--provider', 'codex')
        assert_true(codex_policy.START not in config.read_text(encoding='utf-8'),
                    'fresh no-input install does not opt into global policy')
        check_bundle(False)
        run('codex', '--no-input', '--provider', 'codex', '--codex-auto-review-context')
        enabled = config.read_bytes()
        assert_true(codex_policy.START in enabled.decode('utf-8'), 'explicit opt-in reaches config')
        check_bundle(True)
        run('codex', '--no-input', '--provider', 'codex')
        assert_true(config.read_bytes() == enabled, 'no-input update keeps enabled choice byte-idempotently')
        config.write_bytes(enabled.replace(b'Routine inspection', b'User-changed inspection'))
        conflict = run('--check', expected=1)
        assert_true('conflict' in conflict.stdout, 'check detects a hand-edited owned block')
        edited_bytes = config.read_bytes()
        assert_true(any(row['path'] == codex_policy.TEMPLATE_KEY and 'conflict' in row['reason']
                        for row in doctor()['drift']) and config.read_bytes() == edited_bytes,
                    'doctor reports the same hand-edited owned block, read-only')
        config.write_bytes(enabled)
        run('claude', '--no-input', '--provider', 'codex')
        assert_true(config.read_bytes() == original.encode('utf-8'),
                    'Codex host deselection removes its grant/context but keeps user policy')
        run('codex', '--no-input', '--provider', 'codex')
        assert_true(codex_policy.START in config.read_text(encoding='utf-8'),
                    'reselecting Codex keeps the explicitly saved preference')
        run('codex', '--no-input', '--provider', 'codex', '--no-codex-auto-review-context')
        disabled = config.read_bytes()
        run('codex', '--no-input', '--provider', 'codex')
        assert_true(config.read_bytes() == disabled and codex_policy.START not in disabled.decode('utf-8'),
                    'explicit opt-out survives subsequent update')
        check_bundle(False)
        run('--uninstall', '--apply')
        assert_true(config.read_bytes() == original.encode('utf-8'),
                    'full uninstall preserves user replacement policy and original bytes')
        broken = original + '\n' + base.CODEX_GRANT_MARKERS[0] + '\n'
        config.write_bytes(broken.encode('utf-8'))
        run('codex', '--no-input', '--provider', 'codex', '--codex-auto-review-context', expected=1)
        assert_true(config.read_bytes() == broken.encode('utf-8'),
                    'requested context with broken grant fails without changing user config')
