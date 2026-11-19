"""Private workspace preview with isolated synthetic accounts. Never a deployment entrypoint."""
from pathlib import Path
import argparse
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', '3000')))
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--strictPort', action='store_true')
    args = parser.parse_args()
    (ROOT / 'artifacts').mkdir(exist_ok=True)
    os.environ.update(LOGISTIK_DATABASE_URL='sqlite:///' + str(ROOT / 'artifacts/preview.sqlite3'),
                      LOGISTIK_DEMO='0', LOGISTIK_DEPLOYMENT='pilot',
                      LOGISTIK_ALLOW_SIGNUP='0', LOGISTIK_SECURE_COOKIE='0')
    from pilot_data import create_pilot_data
    from verify_multiuser_http import PASSWORDS
    create_pilot_data(PASSWORDS)
    import uvicorn
    uvicorn.run('app:app', host=args.host, port=args.port, proxy_headers=True, forwarded_allow_ips='*')


if __name__ == '__main__': main()
