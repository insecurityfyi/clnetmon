# Collaborative NMS

Aggregates network and user data from multiple sources into PostgreSQL, serves it over a
REST API, and feeds Grafana dashboards, including a network map rebuilt from collected
neighbor and routing data on every poll.

Integrations so far: **UniFi** (UniFi OS console, read-only).

## How it fits together

```
UniFi console --HTTPS--> collector (every 60 s) --> PostgreSQL --> Grafana dashboards + map
                                                        \--> REST API  /api/v1/...
```

One systemd service (`nms`) runs the collectors and the API. Code reaches the server
through GitHub: `nms-update.timer` fast-forwards `/opt/nms` to `origin/main` every two
minutes and restarts the service when something changed. Anything pushed to `main` is
therefore deployed automatically, so keep write access to the repo tight.

## What the UniFi integration collects

| Data | Controller endpoint | Tables |
|---|---|---|
| Devices: model, firmware, state, CPU/memory, temperature | `stat/device` | `devices`, `device_metrics` |
| Radios: channel, width, power, channel utilization, clients | `stat/device` | `radio_metrics` |
| Switch ports: link, speed, PoE, bytes, errors | `stat/device` | `port_metrics` |
| Connected clients: IP, AP/SSID or switch port, signal, noise, rates, bytes, experience | `stat/sta` | `clients`, `client_metrics` |
| Every known client: alias, fixed IP, first/last seen | `rest/user` | `clients` |
| SSIDs (passphrases are stripped, never stored) | `rest/wlanconf` | `wlans` |
| Networks / VLANs / subnets | `rest/networkconf` | `networks` |
| Gateway routing table | `stat/routing` | `routes` |
| Uplinks, LLDP neighbors, client attachment | derived from the above | `topology_links` |

The detailed data comes from the controller's legacy API using the same API key as the
official Integration API. If a console rejects the key there, set
`NMS_UNIFI_USERNAME`/`NMS_UNIFI_PASSWORD` (a local read-only admin) and the client falls
back to a session login. If the legacy API is unavailable altogether, the collector
degrades to the official Integration API (inventory and attachment only) and records a
warning. The collector only issues GET requests.

SNMP is not used: for UniFi gear the API is a superset of what SNMPv2 exposes. It is the
planned route for non-UniFi devices.

## Network map

`topology_nodes` and `topology_edges` are SQL views over the latest collected data:

* UniFi devices, joined by controller-reported uplinks (wired or wireless mesh)
* neighbors heard over LLDP, including third-party gear the controller does not manage
* routed prefixes from the gateway routing table, labelled with the matching network/VLAN
* optionally, every connected client on its AP or switch port

Links not seen for 15 minutes drop off the map. Read it at `GET /api/v1/topology`
(`?include_clients=true`, `?include_routes=false`) or in Grafana's *NMS overview*
dashboard (node-graph panel, with toggles for clients and routes).

## Install on CentOS Stream 9

The repo is private, so the server needs a deploy key before it can clone. Create it
first, add the printed public key to the repo (Settings > Deploy keys, write access off),
then clone and run the installer:

```
sudo dnf -y install git
sudo mkdir -p /etc/nms
sudo ssh-keygen -t ed25519 -N "" -f /etc/nms/deploy_key
sudo cat /etc/nms/deploy_key.pub        # add this to the repo's deploy keys

sudo GIT_SSH_COMMAND="ssh -i /etc/nms/deploy_key -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" \
    git clone git@github.com:insecurityfyi/clnetmon.git /tmp/clnetmon
sudo bash /tmp/clnetmon/deploy/install.sh git@github.com:insecurityfyi/clnetmon.git
```

The installer reuses that key, so just press Enter when it asks about the deploy key.
Alternatively, copy `deploy/install.sh` to the server by any other means and run it; it
creates the key itself.

The script prints a deploy key to add to the repo (read-only), then installs Python 3.12,
PostgreSQL 16 and Grafana and enables the services. Afterwards:

1. Create an API key on the console: Network > Settings > Control Plane > Integrations.
2. Put the console URL and key in `/etc/nms/nms.env`.
3. `sudo -u nms bash -c 'set -a; . /etc/nms/nms.env; /opt/nms/.venv/bin/nms check-unifi'`
   reports which endpoints the console answers.
4. `sudo systemctl restart nms`, then open Grafana on port 3000.

## Operate

```
nmsupdate                                  # pull the latest code and redeploy now
nmsupdate --force                          # reinstall and restart even if unchanged
journalctl -u nms -f                       # collector + API log
journalctl -u nms-update                   # deploy log
curl -s localhost:8080/api/v1/collectors   # last run of each collector, with warnings
curl -s localhost:8080/docs                # API reference
```

History is kept for `NMS_RETENTION_DAYS` (default 90).

## Develop

```
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
NMS_TEST_DATABASE_URL=postgresql://... .venv/bin/pytest     # scratch DB; its public schema is wiped
```

Tests run the collector against a fake console (`tests/sample.py`) and a real PostgreSQL.

## Adding an integration

Create `src/nms/integrations/<name>/` with a collector exposing `name`, `interval` and
`run_once(pool)`, write into the shared tables with its own `src` value, and register it
in `scheduler.build_collectors`. Schema changes go in a new numbered file under
`src/nms/migrations/`; they are applied at service start.
