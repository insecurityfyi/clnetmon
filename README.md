# Collaborative NMS

Aggregates network and user data from multiple sources into PostgreSQL, serves it over a
REST API, and feeds Grafana dashboards, including a network map rebuilt from collected
neighbor and routing data on every poll.

Integrations so far: **UniFi** (UniFi OS console) and **SNMP** (v2c, any vendor, with
discovery). Both are read-only.

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

## What the SNMP integration collects

Standard MIBs only, so switches, firewalls, routers, printers and UniFi devices with SNMP
enabled all produce the same shape of data.

| Data | MIB | Tables |
|---|---|---|
| Name, description, uptime, location | MIB-II system | `devices` (`src = 'snmp'`) |
| Interfaces: status, speed, description, bytes, errors, discards | IF-MIB | `port_metrics` |
| LLDP neighbors | LLDP-MIB | `topology_links` |
| Routing table (IPv4) | IP-FORWARD-MIB, MIB-II | `routes` |
| ARP table (IP to MAC) | MIB-II / IP-MIB | `arp_entries` |
| Switch MAC table (MAC to port, per VLAN) | Q-BRIDGE / BRIDGE-MIB | `fdb_entries` |
| CPU and memory | HOST-RESOURCES-MIB | `device_metrics` |
| Printer status, page count, toner and other supply levels | Printer-MIB | `printer_metrics`, `printer_supplies` |

A device is typed automatically: `printer` if it answers the Printer-MIB, `firewall` or
`router` if it forwards IP (firewall when its description names a known firewall product),
`switch` if it has a MAC table, otherwise `host`. Vendor-specific data (firewall session
counts, VPN tunnels, PoE budgets) is not collected yet.

A device seen by both UniFi and SNMP is recognised by its MAC and shown once on the map.

### Configure

In `/etc/nms/nms.env`:

```
NMS_SNMP_COMMUNITIES=myreadonlystring           # several allowed; tried in order
NMS_SNMP_TARGETS=192.168.7.1, 192.168.7.60      # always polled
NMS_SNMP_DISCOVERY=192.168.7.0/24, auto         # swept for anything that answers
```

**Discovery** sends one SNMP request per community string to every address in the listed
networks (hourly, and at start). Whatever answers becomes a target and is polled from
then on. `auto` adds every subnet learned from the UniFi controller. Networks larger
than a /22 are refused. Enable SNMP v2c with a read-only community on each device first,
and allow UDP 161 from the NMS server.

```
nms check-snmp
```

shows what answers, without storing anything. Then `sudo systemctl restart nms`.

`GET /api/v1/snmp/targets` lists every target with its last result, `/api/v1/printers`
the printers and supplies, and `/api/v1/locate?q=<ip, mac or name>` says where a host is
by combining UniFi client data with ARP and switch MAC tables.

## Network map

`topology_nodes` and `topology_edges` are SQL views over the latest collected data:

* UniFi devices, joined by controller-reported uplinks (wired or wireless mesh)
* SNMP-polled switches, firewalls, routers and printers
* neighbors heard over LLDP (from UniFi and SNMP), including gear nothing manages
* routed prefixes from each router's routing table, labelled with the matching network/VLAN
* optionally, every connected client on its AP or switch port

Links not seen for 15 minutes drop off the map. Read it at `GET /api/v1/topology`
(`?include_clients=true`, `?include_routes=false`) or in Grafana's *NMS overview*
dashboard (node-graph panel, with toggles for clients and routes).

## Dashboards

Three dashboards are provisioned into Grafana's *NMS* folder, linked to each other from
the top bar:

* **NMS overview**: network map, clients, wireless, routing, printers, SNMP targets.
* **Devices and interfaces**: firewall and gateway interface traffic, every device with
  CPU, memory and throughput, the busiest interfaces and those with errors. Click a device
  name to open its detail page.
* **Device detail**: one device's CPU and memory history, per-interface traffic in and
  out, errors and discards, the full interface table with neighbors, LLDP neighbors,
  routing table, attached hosts, Wi-Fi channel utilization and printer supplies.

Interface rates (`rx_bps`, `tx_bps`, `errors_ps` in `port_metrics`) are computed when
each sample is stored, from the previous sample of the same interface.

The JSON files are generated: edit `tools/build_dashboards.py` and run it.

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
3. `nms check-unifi` reports which endpoints the console answers.
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
