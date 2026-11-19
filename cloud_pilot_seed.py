"""Validate environment before provisioning demonstration accounts.

Not intended to be a real cargo dispatch system. Only sample non-hazardous data.
"""
import os


def refuse(reason):
    raise SystemExit('Cloud pilot setup: ' + reason)

if os.environ.get('LOGISTIK_DEPLOYMENT', 'pilot').lower() != 'pilot':
    refuse('LOGISTIK_DEPLOYMENT must be pilot')
if os.environ.get('LOGISTIK_DEMO', '0') != '0':
    refuse('LOGISTIK_DEMO must be 0')
if os.environ.get('LOGISTIK_ALLOW_SIGNUP', '0') not in ('0','1'):
    refuse('LOGISTIK_ALLOW_SIGNUP must be 0 or 1')
if os.environ.get('LOGISTIK_SECURE_COOKIE', '0') != '1':
    refuse('HTTPS cookie required')
if not os.environ.get('LOGISTIK_DATABASE_URL','').startswith('postgresql://'):
    refuse('this build requires managed PostgreSQL; do not deploy with SQLite')
# Independent credentials: sharing one role's password must never reveal other roles.
PASSWORD_ENV = {
    'pilot.shipper@example.test': 'LOGISTIK_TEST_SHIPPER_PASSWORD',
    'pilot.carrier1@example.test': 'LOGISTIK_TEST_CARRIER1_PASSWORD',
    'pilot.driver1@example.test': 'LOGISTIK_TEST_DRIVER1_PASSWORD',
    'pilot.carrier2@example.test': 'LOGISTIK_TEST_CARRIER2_PASSWORD',
    'pilot.driver2@example.test': 'LOGISTIK_TEST_DRIVER2_PASSWORD',
}
passwords={email:os.environ.get(env,'') for email,env in PASSWORD_ENV.items()}
if any(len(p)<18 or len(p)>128 for p in passwords.values()):
    refuse('set five independent 18–128 character passwords in Render environment variables')
if len(set(passwords.values()))!=len(passwords):
    refuse('all five passwords must be different')
if any(p.lower() in ('demo-2026-start!', 'password1234567890') for p in passwords.values()):
    refuse('common demonstration passwords are not allowed')

from pilot_data import create_pilot_data
create_pilot_data(passwords)
