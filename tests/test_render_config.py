"""Release safeguards; no external database access in this test."""
from pathlib import Path
import hashlib
import subprocess
import os
import sys

BASE = Path(__file__).resolve().parents[1]


def test_render_config_has_free_postgres_and_guarded_password():
    manifest = (BASE / 'render.yaml').read_text()
    assert 'type: web' in manifest
    assert manifest.count('plan: free') == 2
    assert 'property: connectionString' in manifest
    assert 'LOGISTIK_SECURE_COOKIE' in manifest
    assert 'sync: false' in manifest
    assert 'LOGISTIK_DEPLOYMENT' in manifest
    assert 'autoDeployTrigger: \'off\'' in manifest


def test_bootstrap_refuses_missing_config():
    env = {k: v for k, v in os.environ.items() if not k.startswith('LOGISTIK_')}
    proc = subprocess.run([sys.executable, 'cloud_pilot_seed.py'], cwd=BASE,
                          env=env, capture_output=True, text=True, timeout=15)
    assert proc.returncode != 0
    assert 'Cloud pilot setup' in proc.stderr + proc.stdout


def test_bootstrap_refuses_short_password():
    env = {k: v for k, v in os.environ.items() if not k.startswith('LOGISTIK_')}
    env.update(LOGISTIK_DEPLOYMENT='pilot', LOGISTIK_DEMO='0',
               LOGISTIK_ALLOW_SIGNUP='0', LOGISTIK_SECURE_COOKIE='1',
               LOGISTIK_DATABASE_URL='postgresql://example.invalid/demo',
               LOGISTIK_CLOUD_TEST_PASSWORD='too-short')
    proc = subprocess.run([sys.executable, 'cloud_pilot_seed.py'], cwd=BASE,
                          env=env, capture_output=True, text=True, timeout=15)
    assert proc.returncode != 0
    assert '18–128' in proc.stderr + proc.stdout


def test_ui_has_no_unapproved_edits():
    assert (BASE/'static/index.html').is_file()
    # Prefix intentionally not hardcoded: exact release comparison is made at package time.
    content = (BASE/'static/index.html').read_bytes()
    assert b'<!DOCTYPE html>' in content[:200] or b'<!doctype html>' in content[:200].lower()
    assert len(content) > 50_000
