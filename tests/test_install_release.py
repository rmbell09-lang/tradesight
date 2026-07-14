import importlib.util
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def load_installer():
    spec = importlib.util.spec_from_file_location('install_tradesight', ROOT / 'install_tradesight.py')
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def test_installer_dry_run_uses_supported_runtime_and_never_clones():
    result = subprocess.run(
        [sys.executable, 'install_tradesight.py', '--dry-run', '--python', sys.executable],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    plan = json.loads(result.stdout)
    assert plan['mode'] == 'PAPER_ONLY'
    assert plan['port'] == 5001
    assert plan['clone_source'] is False
    assert plan['live_trading_enabled'] is False
    assert plan['register_install'] is True


def test_installer_rejects_unsupported_python(monkeypatch):
    installer = load_installer()
    monkeypatch.setattr(installer, 'interpreter_candidates', lambda explicit=None: iter(['/unsupported/python']))
    monkeypatch.setattr(installer, 'version_of', lambda executable: (3, 9, 6))
    try:
        installer.choose_interpreter()
    except RuntimeError as exc:
        assert '3.11' in str(exc)
    else:
        raise AssertionError('unsupported Python was accepted')


def test_cli_does_not_clone_or_install_dependencies():
    text = (ROOT / 'tradesight' / 'cli.py').read_text()
    assert 'git clone' not in text
    assert 'pip", "install' not in text
    assert 'DEFAULT_PORT = 5001' in text
    assert text.index('roots.extend((Path.cwd()') < text.index('if INSTALL_POINTER.is_file()')


def test_release_policy_excludes_private_runtime_state():
    policy = json.loads((ROOT / 'ops' / 'release_policy.json').read_text())
    assert policy['paper_only'] is True
    assert '.env' in policy['excluded_fragments']
    assert 'state' in policy['excluded_roots']
    assert 'data' in policy['excluded_roots']
    assert policy['dashboard_port'] == 5001


def test_release_keeps_source_data_package_but_excludes_runtime_data():
    builder_path = ROOT / 'scripts' / 'build_release.py'
    spec = importlib.util.spec_from_file_location('build_release', builder_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    policy = json.loads((ROOT / 'ops' / 'release_policy.json').read_text())
    assert module.included(Path('src/data/alpaca_client.py'), policy)
    assert not module.included(Path('data/positions.db'), policy)
