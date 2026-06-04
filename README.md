# ng1-f5-emulator-kubernetes

Translates Kubernetes LoadBalancer Services and Ingress rules into F5 BIG-IP iControl REST objects, enabling nGeniusONE BIG-IP Discovery to monitor Kubernetes workloads without any changes to the cluster.

## Architecture

```
nGeniusONE BIG-IP Discovery
        │
        ▼
tmos-shim-kubernetes  (192.168.30.35:8447)
        │
        ├── GET /api/v1/services         → Virtual Servers
        ├── GET /api/v1/endpoints        → Pool Members
        └── GET /networking.k8s.io/v1/ingresses → Ingress VIPs
              │
              ▼
        Kubernetes API (10.0.0.110:8443)
```

## Mapping

| Kubernetes | F5 BIG-IP |
|---|---|
| LoadBalancer Service port | Virtual Server |
| Ingress rule | Virtual Server |
| Endpoints (pod IPs) | Pool members |
| Pod readiness | Member state (up/down) |

## Deploy

```bash
# 1. Create service account on the cluster
kubectl apply -f rbac/netscout-ng1-rbac.yml

# 2. Copy token and CA cert to kubeconfig/
kubectl get secret netscout-ng1-token -n kube-system -o jsonpath='{.data.token}' | base64 -d > kubeconfig/token
kubectl get secret netscout-ng1-token -n kube-system -o jsonpath='{.data.ca\.crt}' | base64 -d > kubeconfig/ca.crt

# 3. Edit backends/kubernetes.yml — set api_url, ng1_url, token

# 4. Deploy
docker compose up -d
```

## nG1 BIG-IP Discovery

Register in nGeniusONE:
- **Host:** 192.168.30.35
- **Port:** 8447
- **Username:** admin
- **Password:** netscout1

## Watch Thread

A background thread watches Kubernetes Services and Endpoints in real time.
On any ADDED/MODIFIED/DELETED event the cache is refreshed and nG1 is
triggered to re-poll immediately via the relearn API — changes appear in
nG1 within seconds.
