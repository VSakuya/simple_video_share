# Local Serene Pub deployment

Target: Ubuntu server `vsakuya@192.168.57.238`, port **8234**. The cloud server
does not run Docker or a second model for this deployment.

Stage `compose.yaml` and `install.sh` in
`/home/vsakuya/services/serene-pub/`, then execute:

```sh
sudo bash /home/vsakuya/services/serene-pub/install.sh
```

The installer uses Docker's signed Ubuntu repository, preserves the existing
forwarding policy, pulls official release `0.5.3-beta` and pins its registry
digest in `.env`. It publishes only `192.168.57.238:8234`, keeps data in the
`serene-pub_serene-pub-data` named volume, limits memory/CPU/log growth, and starts
the container automatically after reboot. It does not add the user to Docker's
root-equivalent group or change existing Jellyfin, WireGuard, swap or app accounts.

Local entry: `http://192.168.57.238:8234/`.

Website service fields (owner-managed):

| Field | Value |
| --- | --- |
| Name | Serene Pub |
| Identifier | serene |
| Address | http://192.168.57.238:8234/ |

The existing website gateway connects across WireGuard. Automatic TLS makes
`https://serene.services.vsakuya.xyz` ready after the service is added. The public
origin is already declared in Compose for HTTPS cookies, CSRF and Socket.IO.
The gateway must preserve browser Origin/Referer headers.

The owner initializes the Serene Pub administrator/multi-user settings and the
OpenAI-compatible connection at `http://192.168.57.233:8999/v1`. The installer
checks `/v1/models` from inside the container but does not generate text or store
credentials. Account and connection settings are intentionally not prefilled.

Operations, from the project directory:

```sh
sudo docker compose ps
sudo docker compose logs --tail=100
sudo docker compose restart
```

Back up the named volume before upgrades. Update to a reviewed release and pin
its new digest deliberately; there is no automatic image update. `docker compose
down` retains the data volume; do not use `down -v` unless deleting all app data
is intentional.
