#!/bin/bash
# Run with sudo on the local Ubuntu server, 192.168.57.238.
set -euo pipefail
if [ "$(id -u)" != 0 ]; then
  echo "Run this installer with sudo." >&2
  exit 1
fi
project=/home/vsakuya/services/serene-pub
cd "$project"
. /etc/os-release
if [ "$ID" != ubuntu ]; then
  echo "This installer requires Ubuntu." >&2
  exit 1
fi
if ! ip -4 address show | grep -Fq '192.168.57.238/'; then
  echo "This installer is restricted to the local server 192.168.57.238." >&2
  exit 1
fi

if ! command -v docker >/dev/null; then
  # Configure this before the new daemon first starts; keep existing routing
  # behavior instead of letting Docker change the default forwarding policy.
  install -d -m 755 /etc/docker /etc/apt/keyrings
  python3 - <<'PY'
import json
from pathlib import Path
path = Path('/etc/docker/daemon.json')
data = json.loads(path.read_text()) if path.exists() else {}
data['ip-forward-no-drop'] = True
path.write_text(json.dumps(data, indent=2) + '\n')
PY
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates curl
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  architecture=$(dpkg --print-architecture)
  cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: $architecture
Signed-By: /etc/apt/keyrings/docker.asc
EOF
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi
systemctl enable --now docker
docker compose config --quiet
docker compose pull
# Pin the downloaded official image by immutable registry digest.
image=$(docker compose config --images | head -n 1)
digest=$(docker image inspect "$image" --format '{{index .RepoDigests 0}}')
printf 'SERENE_IMAGE=%s\n' "$digest" > .env
chown vsakuya:vsakuya .env
chmod 600 .env
docker compose up -d
for attempt in $(seq 1 90); do
  if curl --noproxy '*' --silent --fail --output /dev/null http://192.168.57.238:8234/; then
    break
  fi
  sleep 2
done
docker compose ps
curl --noproxy '*' --connect-timeout 5 --max-time 15 --silent --fail --output /dev/null --write-out 'Serene Pub HTTP %{http_code}\n' http://192.168.57.238:8234/
docker compose exec -T serene-pub node -e "fetch('http://192.168.57.233:8999/v1/models',{signal:AbortSignal.timeout(10000)}).then(r=>{console.log('Home LLM models HTTP',r.status);process.exit(r.ok?0:1)}).catch(e=>{console.error(e.message);process.exit(1)})"
echo 'Serene Pub is installed. Initialize application accounts and connections in its UI.'
