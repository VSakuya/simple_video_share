#!/usr/bin/env python3
"""Issue/renew TLS for configured service hosts; installed root-owned by setup."""
import fcntl
import re
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

DB = Path('/home/vsakuya/simple_video_share/storage/data/app.db')
CERT = Path('/etc/letsencrypt/live/custom-services/cert.pem')
SUFFIX = '.services.vsakuya.xyz'


def main():
    with open('/run/lock/custom-services-tls.lock', 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        with sqlite3.connect(f'file:{DB}?mode=ro', uri=True) as conn:
            slugs = [row[0] for row in conn.execute('SELECT slug FROM custom_services WHERE enabled=1')]
        requested = {slug + SUFFIX for slug in slugs if re.fullmatch(r'[a-z](?:[a-z0-9-]{0,38}[a-z0-9])?', slug)}
        existing = set()
        if CERT.exists():
            output = subprocess.check_output(['openssl', 'x509', '-in', str(CERT), '-noout', '-ext', 'subjectAltName'], text=True)
            existing = set(re.findall(r'DNS:([a-z0-9.-]+)', output))
        # Retain names already on the certificate to avoid unnecessary issuance
        # when services are temporarily disabled or removed.
        new = requested - existing
        if '--initial' not in sys.argv:
            reachable = set()
            for domain in new:
                try:
                    if '206.237.20.173' in socket.gethostbyname_ex(domain)[2]:
                        reachable.add(domain)
                except OSError:
                    continue
            new = reachable
        domains = sorted(existing | new)
        if not domains:
            raise SystemExit('No valid service domains are ready for TLS.')
        if new or not CERT.exists():
            if len(domains) > 100:
                raise SystemExit('Service certificate exceeds the 100-name limit.')
            subprocess.run([
                '/usr/local/bin/certbot', 'certonly', '--webroot',
                '--webroot-path', '/var/www/custom-services-acme',
                '--cert-name', 'custom-services', '--non-interactive', '--expand',
                '--domains', ','.join(domains), '--deploy-hook', 'systemctl reload apache2',
            ], check=True)
        elif subprocess.run(['openssl', 'x509', '-checkend', '2592000', '-noout', '-in', str(CERT)], stdout=subprocess.DEVNULL).returncode:
            subprocess.run(['/usr/local/bin/certbot', 'renew', '--cert-name', 'custom-services', '--quiet', '--deploy-hook', 'systemctl reload apache2'], check=True)


if __name__ == '__main__':
    main()
