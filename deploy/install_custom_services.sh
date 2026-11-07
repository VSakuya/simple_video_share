#!/bin/bash
# Install the isolated HTTPS service gateway. Run via sudo on the production VPS.
set -euo pipefail
if [ "$(id -u)" != 0 ]; then
  echo "Run this installer with sudo." >&2
  exit 1
fi
stage=/home/vsakuya/.local/share/custom-services-setup
project=/home/vsakuya/simple_video_share
install -d -m 755 /usr/local/libexec /var/www/custom-services-acme
install -o root -g root -m 755 "$stage/custom_services_tls.py" /usr/local/libexec/custom-services-tls.py

cat > /etc/apache2/sites-available/custom-services-acme.conf <<'APACHE'
<VirtualHost *:80>
    ServerName services.vsakuya.xyz
    ServerAlias *.services.vsakuya.xyz
    DocumentRoot /var/www/custom-services-acme
    <Directory /var/www/custom-services-acme>
        Require all granted
    </Directory>
    RewriteEngine On
    RewriteCond %{REQUEST_URI} !^/\.well-known/acme-challenge/
    RewriteRule ^ https://%{HTTP_HOST}%{REQUEST_URI} [R=301,L]
</VirtualHost>
APACHE
a2ensite custom-services-acme.conf
apache2ctl configtest
systemctl reload apache2
resolvectl flush-caches || true
python3 /usr/local/libexec/custom-services-tls.py --initial

cat > /etc/apache2/sites-available/custom-services-ssl.conf <<'APACHE'
<VirtualHost *:443>
    ServerName services.vsakuya.xyz
    ServerAlias *.services.vsakuya.xyz
    Protocols h2 http/1.1
    SSLEngine on
    SSLCertificateFile /etc/letsencrypt/live/custom-services/fullchain.pem
    SSLCertificateKeyFile /etc/letsencrypt/live/custom-services/privkey.pem
    ProxyPreserveHost On
    ProxyRequests Off
    ProxyTimeout 1800
    Header always unset X-Frame-Options
    Header always set Strict-Transport-Security "max-age=31536000"
    RewriteEngine On
    RewriteCond %{HTTP:Upgrade} websocket [NC]
    RewriteCond %{HTTP:Connection} upgrade [NC]
    RewriteRule ^/(.*) ws://127.0.0.1:8090/$1 [P,L]
    ProxyPass / http://127.0.0.1:8090/ timeout=1800 flushpackets=on
    ProxyPassReverse / http://127.0.0.1:8090/
    ErrorLog ${APACHE_LOG_DIR}/custom-services.error.log
    CustomLog ${APACHE_LOG_DIR}/custom-services.access.log combined
</VirtualHost>
APACHE
a2ensite custom-services-ssl.conf
apache2ctl configtest

runuser -u vsakuya -- python3 - <<'PY'
import json
import os
import shutil
from pathlib import Path
cfg = Path('/home/vsakuya/simple_video_share/config.json')
backup = Path('/home/vsakuya/.local/share/custom-services-setup/config.before-services.json')
if not backup.exists():
    shutil.copy2(cfg, backup)
    backup.chmod(0o600)
data = json.loads(cfg.read_text())
data.update(services_origin_template='https://{slug}.services.vsakuya.xyz',
            services_parent_origin='https://video.vsakuya.xyz',
            services_gateway_host='127.0.0.1', services_gateway_port=8090)
temp = cfg.with_name('config.json.services-tmp')
temp.write_text(json.dumps(data, indent=2) + '\n')
temp.chmod(0o600)
os.replace(temp, cfg)
PY

install -d -o vsakuya -g vsakuya -m 700 /home/vsakuya/.config/systemd/user
cat > /home/vsakuya/.config/systemd/user/custom-services-gateway.service <<'UNIT'
[Unit]
Description=Authenticated custom services gateway
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/vsakuya/simple_video_share
ExecStart=/home/vsakuya/simple_video_share/.venv/bin/python /home/vsakuya/simple_video_share/service_gateway.py
Restart=on-failure
RestartSec=3
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
UNIT
chown vsakuya:vsakuya /home/vsakuya/.config/systemd/user/custom-services-gateway.service
chmod 600 /home/vsakuya/.config/systemd/user/custom-services-gateway.service
loginctl enable-linger vsakuya
user_uid=$(id -u vsakuya)
user_systemctl() {
  runuser -u vsakuya -- env XDG_RUNTIME_DIR="/run/user/$user_uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$user_uid/bus" systemctl --user "$@"
}
user_systemctl daemon-reload
user_systemctl enable custom-services-gateway.service
user_systemctl restart custom-services-gateway.service

cat > /etc/systemd/system/custom-services-tls.service <<'UNIT'
[Unit]
Description=Reconcile certificates for configured custom services
After=network-online.target apache2.service

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/libexec/custom-services-tls.py
UMask=0077
UNIT
cat > /etc/systemd/system/custom-services-tls.timer <<'UNIT'
[Unit]
Description=Check new custom service domains and certificate renewal

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now custom-services-tls.timer
systemctl restart my_video.service
systemctl reload apache2
for attempt in $(seq 1 30); do
  if curl --noproxy '*' --silent --fail --output /dev/null http://127.0.0.1:8080/auth/login; then break; fi
  sleep 1
done
user_systemctl is-active custom-services-gateway.service
curl --noproxy '*' --silent --output /dev/null --write-out 'Gateway anonymous HTTP %{http_code}\n' -H 'Host: traven.services.vsakuya.xyz' http://127.0.0.1:8090/
curl --noproxy '*' --silent --fail --output /dev/null --write-out 'Website HTTP %{http_code}\n' https://video.vsakuya.xyz/auth/login
echo 'Production gateway installed. Browser verification is the next step.'
