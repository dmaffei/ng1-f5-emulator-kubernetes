# INSTALL.md — ng1-f5-emulator-kubernetes

Complete deployment guide for integrating Kubernetes LoadBalancer Services
and Ingress rules with nGeniusONE BIG-IP® Discovery.

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Prerequisites](#prerequisites)
4. [Step 1 — Host preparation](#step-1--host-preparation)
5. [Step 2 — Kubernetes service account](#step-2--kubernetes-service-account)
6. [Step 3 — Clone and run install.sh](#step-3--clone-and-run-installsh)
7. [Step 4 — nG1 BIG-IP Discovery](#step-4--ng1-bigip-discovery)
8. [Step 5 — Verify](#step-5--verify)
9. [Configuration reference](#configuration-reference)
10. [Upgrading](#upgrading)
11. [Troubleshooting](#troubleshooting)

---

## Overview

This emulator runs a container (`tmos-shim-kubernetes`) that sits between
nGeniusONE and a Kubernetes cluster. nG1 thinks it is talking to a real F5
BIG-IP device. The container translates real-time Kubernetes state into F5
iControl REST responses.

**What nG1 discovers:**

| Kubernetes object | nG1 sees |
|---|---|
| LoadBalancer Service port | Virtual Server (VIP) |
| Ingress rule | Virtual Server (via Ingress controller IP) |
| Endpoint pod IPs | Pool members |
| Pod readiness (Ready/NotReady) | Member state (up/down) |

**No changes are made to the Kubernetes cluster** beyond creating a
read-only service account.

---

## Architecture

```
nGeniusONE BIG-IP® Discovery
        │  HTTPS (F5 iControl REST)
        ▼
┌─────────────────────────────────┐
│  tmos-shim-kubernetes           │
│  (Docker container)             │
│                                 │
│  ┌───────────────────────────┐  │
│  │  kubernetes_adapter.py    │  │
│  │  ┌─────────────────────┐  │  │
│  │  │ Watch thread        │  │  │
│  │  │ api/v1/services     │  │  │
│  │  │ api/v1/endpoints    │  │  │
│  │  └─────────────────────┘  │  │
│  └───────────────────────────┘  │
└─────────────────────────────────┘
        │  HTTPS (Kubernetes API)
        ▼
Kubernetes API Server
  ├── Services (LoadBalancer)  → VIPs
  ├── Endpoints (pod IPs)      → Pool members
  └── Ingresses                → Additional VIPs
```

The adapter maintains an in-memory cache refreshed by two background watch
threads (services + endpoints). When any service or endpoint changes, the
cache is refreshed and nG1 is triggered to re-poll immediately via the
`rediscoverLBDevice` API — changes appear in nG1 within seconds.

---

## Prerequisites

### On the Docker host (where the container runs)

| Requirement | Version | Notes |
|---|---|---|
| Docker Engine | 20.10+ | `docker --version` |
| Docker Compose | v2 (plugin) | `docker compose version` |
| openssl | any | For self-signed TLS cert generation |
| Network access | — | Must reach K8s API server on port 6443 or 8443 |
| Network access | — | Must reach nG1 on port 443 |

The container binds to a host IP:port that nG1 must be able to reach over
HTTPS. Ensure no firewall blocks the chosen port (default `8447`) between
nG1 and this host.

### On the Kubernetes cluster

- Kubernetes **1.20+**
- `kubectl` access with cluster-admin rights (for service account creation only)
- MetalLB or another LoadBalancer controller that assigns `EXTERNAL-IP` to
  LoadBalancer services (required for VIP discovery)
- The Docker host must be able to reach the Kubernetes API server endpoint
  (check with `curl -k https://<api-server>/healthz`)

### In nGeniusONE

- nGeniusONE **6.3+**
- An account with **Administrator** role (for BIG-IP Discovery registration)
- BIG-IP® Discovery module licensed and enabled

---

## Step 1 — Host preparation

```bash
# Verify Docker
docker --version
docker compose version

# Verify connectivity to the K8s API server
curl -k https://<your-k8s-api>:<port>/healthz
# Expected: "ok"

# Verify connectivity to nG1
curl -sk -o /dev/null -w "%{http_code}" https://<ng1-ip>/ng1api/ncm/bigip/loadbalancers
# Expected: 401 (auth required — connectivity confirmed)
```

Clone the repository:

```bash
git clone https://github.com/dmaffei/ng1-f5-emulator-kubernetes.git
cd ng1-f5-emulator-kubernetes
```

---

## Step 2 — Kubernetes service account

This is the **only change required on the Kubernetes cluster**.
It creates a read-only service account and exports its token and CA cert.

### 2a. Apply RBAC

```bash
kubectl apply -f - <<EOF
apiVersion: v1
kind: ServiceAccount
metadata:
  name: netscout-ng1
  namespace: kube-system
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: netscout-ng1-reader
rules:
- apiGroups: [""]
  resources: ["services", "endpoints"]
  verbs: ["get", "list", "watch"]
- apiGroups: ["networking.k8s.io"]
  resources: ["ingresses"]
  verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: netscout-ng1-reader
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: netscout-ng1-reader
subjects:
- kind: ServiceAccount
  name: netscout-ng1
  namespace: kube-system
---
apiVersion: v1
kind: Secret
metadata:
  name: netscout-ng1-token
  namespace: kube-system
  annotations:
    kubernetes.io/service-account.name: netscout-ng1
type: kubernetes.io/service-account-token
EOF
```

### 2b. Export token and CA cert

Run these commands on a machine with `kubectl` access to the cluster:

```bash
# Wait a few seconds for the token to be populated
sleep 5

# Export token
kubectl get secret netscout-ng1-token -n kube-system \
  -o jsonpath='{.data.token}' | base64 -d > kubeconfig/token

# Export CA certificate
kubectl get secret netscout-ng1-token -n kube-system \
  -o jsonpath='{.data.ca\.crt}' | base64 -d > kubeconfig/ca.crt

# Verify
echo "Token length: $(wc -c < kubeconfig/token)"
echo "CA cert:"
openssl x509 -in kubeconfig/ca.crt -noout -subject -dates
```

### 2c. Find the API server endpoint

```bash
kubectl cluster-info | head -2
# Example output:
# Kubernetes control plane is running at https://10.0.0.110:8443
```

Note the URL — you will need it in install.sh.

### 2d. Verify token works

```bash
API_URL=$(kubectl cluster-info | grep "control plane" | awk '{print $NF}')
TOKEN=$(cat kubeconfig/token)

curl -sk --cacert kubeconfig/ca.crt \
  -H "Authorization: Bearer $TOKEN" \
  "$API_URL/api/v1/services?limit=5" | python3 -c "
import sys,json; d=json.load(sys.stdin)
print(f'API access OK — found {len(d[\"items\"])} services')
"
```

Expected output: `API access OK — found N services`

---

## Step 3 — Clone and run install.sh

```bash
chmod +x install.sh
bash install.sh
```

The script will prompt for:

| Prompt | Description | Example |
|---|---|---|
| Kubernetes API URL | From `kubectl cluster-info` | `https://10.0.0.110:8443` |
| Token file path | Path to exported token | `kubeconfig/token` |
| CA cert path | Path to exported CA cert | `kubeconfig/ca.crt` |
| Host IP | IP of the Docker host | `192.168.30.35` |
| Port | Port to expose the shim | `8447` |
| nG1 URL | nGeniusONE HTTPS URL | `https://192.168.30.20:443` |
| nG1 username | nG1 admin account | `admin` |
| nG1 password | nG1 admin password | `netscout1` |
| BIG-IP device name | Name in nG1 | `kubernetes` |
| Namespaces | Comma-separated, blank = all | `` (blank) |
| Include ClusterIP | Monitor internal services | `n` |
| tmos-shim username | nG1 uses this to connect | `admin` |
| tmos-shim password | nG1 uses this to connect | `netscout1` |

The script will:
1. Validate dependencies
2. Generate a self-signed TLS certificate
3. Write `backends/kubernetes.yml` and `.env`
4. Register the device in nG1 BIG-IP Discovery
5. Build and start the Docker container
6. Trigger an initial nG1 relearn

---

## Step 4 — nG1 BIG-IP Discovery

1. Log in to nGeniusONE
2. Navigate to **Application Configuration → BIG-IP® Discovery**
3. Filter by Load Balancer = `kubernetes`
4. Review the discovered entries (status = **new**)
5. Select all and click **Apply**

The entries will move to **Application Configuration → Applications** where
they can be associated with services and monitored interfaces.

### nG1 BIG-IP Discovery settings

If not auto-registered by install.sh, add manually:

| Field | Value |
|---|---|
| Name | `kubernetes` (or any label) |
| Host | Docker host IP (e.g. `192.168.30.35`) |
| Port | `8447` (or your chosen port) |
| Username | `admin` (tmos-shim credential) |
| Password | `netscout1` (tmos-shim credential) |
| Login Provider | `tmos` |

---

## Step 5 — Verify

```bash
# Container status
docker ps | grep tmos-shim-kubernetes

# Health check
curl -sk https://<host-ip>:<port>/health
# Expected: {"status":"ok","adapter":"kubernetes"}

# Virtual servers (unauthenticated count)
curl -sk -u admin:netscout1 https://<host-ip>:<port>/mgmt/tm/ltm/virtual | \
  python3 -c "import sys,json; d=json.load(sys.stdin); print('VIPs:', len(d.get('items',[])))"

# Watch logs
docker logs -f tmos-shim-kubernetes
```

---

## Configuration reference

### backends/kubernetes.yml

| Key | Description | Default |
|---|---|---|
| `api_url` | Kubernetes API server URL | required |
| `token` | Service account JWT token | required |
| `ca_cert` | Path to cluster CA cert inside container | `/app/kubeconfig/ca.crt` |
| `verify_ssl` | Verify K8s API TLS certificate | `true` |
| `ng1_url` | nGeniusONE HTTPS URL | optional |
| `ng1_lb_name` | BIG-IP device name in nG1 | `kubernetes` |
| `ng1_user` | nG1 username for relearn trigger | `admin` |
| `ng1_pass` | nG1 password for relearn trigger | required if ng1_url set |
| `include_clusterip` | Expose ClusterIP services as VIPs | `false` |
| `namespaces` | Comma-separated namespace filter | `` (all) |

### .env

| Key | Description |
|---|---|
| `TMOS_USERNAME` | Username nG1 uses to authenticate to the shim |
| `TMOS_PASSWORD` | Password nG1 uses to authenticate to the shim |
| `TMOS_HOST_IP` | Host IP returned in device info to nG1 |
| `PORT` | Internal container port (always 8443) |

### Naming convention

| Object | Format | Example |
|---|---|---|
| Virtual Server | `k-{svc-name}-{port}` | `k-traefik-80` |
| Server Pool | `sp-{svc-name}-{port}` | `sp-traefik-80` |

All names are prefixed with `k-` (virtual servers) and `sp-` (pools) to
identify Kubernetes-sourced entries in nG1.

---

## Upgrading

```bash
cd ng1-f5-emulator-kubernetes
git pull origin main
docker compose build --no-cache
docker compose up -d
```

The `backends/kubernetes.yml`, `.env`, and `kubeconfig/` are not tracked
in git — they are preserved across upgrades.

---

## Troubleshooting

### Container fails to start

```bash
docker logs tmos-shim-kubernetes
```

Common causes:
- `backends.yml not found` — run install.sh or create backends/kubernetes.yml manually
- `SSL: CERTIFICATE_VERIFY_FAILED` — set `verify_ssl: false` in backends/kubernetes.yml
- `Connection refused` — wrong API server URL or port, or firewall blocking

### 0 VIPs returned

```bash
curl -sk -u admin:netscout1 https://<host>:<port>/mgmt/tm/ltm/virtual
```

- Check K8s API connectivity: `curl -sk https://<api-url>/healthz`
- Verify token has not expired: re-export from the secret
- Verify services have `EXTERNAL-IP` assigned (MetalLB required)
- Check logs for watch thread errors

### nG1 shows no entries after relearn

- Confirm the device is registered: nG1 → BIG-IP Discovery → check device list
- Confirm the shim is reachable from nG1: `curl -sk https://<shim-host>:<port>/health`
- Check nG1 firewall allows HTTPS to the shim host/port

### Token expired

Service account tokens created via `kubernetes.io/service-account-token`
secrets are long-lived (no expiry by default). If your cluster enforces
token rotation, re-export and update `backends/kubernetes.yml`:

```bash
kubectl get secret netscout-ng1-token -n kube-system \
  -o jsonpath='{.data.token}' | base64 -d > kubeconfig/token
# Update the token field in backends/kubernetes.yml
docker restart tmos-shim-kubernetes
```

### Watch thread reconnects frequently

Normal — the watch stream has a server-side timeout (default 5 minutes).
The adapter reconnects automatically. Check logs for persistent errors.
