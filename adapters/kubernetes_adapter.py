"""
kubernetes_adapter.py — Kubernetes → F5 BIG-IP adapter for tmos-shim
=====================================================================
Maps Kubernetes LoadBalancer Services, ClusterIP Services, and Ingress
rules to F5 iControl REST objects (virtual servers + pools + members).

K8s API calls (read-only):
  GET /api/v1/services             — all services (LoadBalancer + ClusterIP)
  GET /api/v1/endpoints            — all endpoint slices (pool members)
  GET /networking.k8s.io/v1/ingresses — ingress rules

Background Watch thread:
  Watches /api/v1/services and /api/v1/endpoints for ADDED/MODIFIED/DELETED
  events and refreshes the in-memory cache instantly. Calls nG1 relearn API
  on change so nG1 reflects updates within seconds instead of waiting for
  the poll cycle.
"""

import logging
import ssl
import threading
import time
import urllib.request
import urllib.error
import json
import os
from typing import Optional

from core.tmos_base import TmosBase, virtual_server, pool, pool_member

log = logging.getLogger("kubernetes_adapter")

# ── F5 lb-method mapping ──────────────────────────────────────────────────────
_LB_MAP = {
    "RoundRobin":          "round-robin",
    "LeastConnection":     "least-connections-node",
    "Random":              "random",
    "SourceIPAffinity":    "least-connections-node",
}

# ── Protocol mapping ──────────────────────────────────────────────────────────
_PROTO_MAP = {
    "TCP":  "tcp",
    "UDP":  "udp",
    "SCTP": "tcp",
}


class KubernetesAdapter(TmosBase):
    """
    Translates Kubernetes service topology into F5 BIG-IP virtual server objects.

    Mapping:
      LoadBalancer Service port  → Virtual Server (VIP = external IP)
      Ingress rule               → Virtual Server (VIP = ingress load balancer IP)
      Endpoints (pod IPs+ports)  → Pool members
      Service (ClusterIP)        → Pool (for Ingress backends)
    """

    adapter_name = "kubernetes"

    def __init__(
        self,
        api_url: str,
        token: str = "",
        ca_cert: str = "",
        verify_ssl: bool = True,
        ng1_url: str = "",
        ng1_lb_name: str = "kubernetes",
        ng1_user: str = "admin",
        ng1_pass: str = "netscout1",
        include_clusterip: bool = False,
        namespaces: str = "",          # comma-separated, empty = all
    ):
        super().__init__()
        self._api_url        = api_url.rstrip("/")
        self._token          = token
        self._ca_cert        = ca_cert
        self._verify_ssl     = verify_ssl
        self._ng1_url        = ng1_url.rstrip("/")
        self._ng1_lb_name    = ng1_lb_name
        self._ng1_user       = ng1_user
        self._ng1_pass       = ng1_pass
        self._include_cip    = include_clusterip
        self._namespaces     = [n.strip() for n in namespaces.split(",") if n.strip()]

        # In-memory cache — refreshed by background watch thread
        self._cache_lock     = threading.Lock()
        self._virtuals_cache: Optional[dict] = None
        self._pools_cache:    Optional[dict] = None
        self._members_cache:  dict = {}   # pool_name → items list
        self._cache_time:     float = 0.0

        # Start background watch thread
        self._watch_thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._watch_thread.start()
        log.info("[k8s] Adapter init — API: %s  watch thread started", self._api_url)

    # ── SSL context ───────────────────────────────────────────────────────────

    def _ctx(self):
        ctx = ssl.create_default_context()
        if not self._verify_ssl:
            ctx.check_hostname = False
            ctx.verify_mode    = ssl.CERT_NONE
        elif self._ca_cert and os.path.exists(self._ca_cert):
            ctx.load_verify_locations(self._ca_cert)
        return ctx

    # ── K8s API GET ───────────────────────────────────────────────────────────

    def _kget(self, path: str, params: dict = None) -> dict:
        url = f"{self._api_url}/{path.lstrip('/')}"
        if params:
            url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        req = urllib.request.Request(url)
        if self._token:
            req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, context=self._ctx(), timeout=15) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            log.warning("[k8s] GET %s HTTP %d: %s", path, e.code, e.read().decode()[:200])
            return {}
        except Exception as e:
            log.warning("[k8s] GET %s error: %s", path, e)
            return {}

    # ── Data collection ───────────────────────────────────────────────────────

    def _fetch_all(self):
        """Fetch services, endpoints, ingresses — build virtuals/pools/members."""
        log.info("[k8s] Fetching cluster state...")

        # 1. Services
        svc_data = self._kget("api/v1/services")
        services = svc_data.get("items", [])
        if self._namespaces:
            services = [s for s in services if s["metadata"]["namespace"] in self._namespaces]

        # 2. Endpoints
        ep_data = self._kget("api/v1/endpoints")
        endpoints = {
            f"{ep['metadata']['namespace']}/{ep['metadata']['name']}": ep
            for ep in ep_data.get("items", [])
        }

        # 3. Ingresses
        ing_data = self._kget("apis/networking.k8s.io/v1/ingresses")
        ingresses = ing_data.get("items", [])
        if self._namespaces:
            ingresses = [i for i in ingresses if i["metadata"]["namespace"] in self._namespaces]

        virtuals = []
        pools    = []
        members  = {}  # pool_name → member list

        # ── LoadBalancer services → virtual servers ───────────────────────────
        for svc in services:
            svc_type = svc["spec"].get("type", "")
            ns       = svc["metadata"]["namespace"]
            name     = svc["metadata"]["name"]
            ext_ips  = [
                ing.get("ip", "")
                for ing in svc["status"].get("loadBalancer", {}).get("ingress", [])
            ]

            if svc_type == "LoadBalancer" and ext_ips:
                ext_ip = ext_ips[0]
                for port_def in svc["spec"].get("ports", []):
                    port     = port_def.get("port", 80)
                    proto    = _PROTO_MAP.get(port_def.get("protocol", "TCP"), "tcp")
                    tgt_port = port_def.get("targetPort", port)
                    vs_name  = f"k-{name}-{port}"
                    pool_name = f"p-{name}-{port}"
                    enabled  = svc["spec"].get("externalTrafficPolicy") != "None"

                    virtuals.append(virtual_server(
                        name=vs_name,
                        destination=f"{ext_ip}:{port}",
                        pool=pool_name,
                        description=f"K8s LoadBalancer {ns}/{name}",
                        protocol=proto,
                        enabled=enabled,
                    ))

                    # Pool
                    p = pool(name=pool_name, description=f"{ns}/{name} port {port}", lb_method="round-robin")
                    pools.append(p)

                    # Members from endpoints
                    ep_key = f"{ns}/{name}"
                    ep_members = self._build_members(endpoints.get(ep_key), tgt_port, pool_name)
                    members[pool_name] = ep_members

            elif svc_type == "ClusterIP" and self._include_cip:
                cluster_ip = svc["spec"].get("clusterIP", "")
                if not cluster_ip or cluster_ip == "None":
                    continue
                for port_def in svc["spec"].get("ports", []):
                    port      = port_def.get("port", 80)
                    proto     = _PROTO_MAP.get(port_def.get("protocol", "TCP"), "tcp")
                    tgt_port  = port_def.get("targetPort", port)
                    vs_name   = f"{ns}-{name}_{port}"
                    pool_name = f"p-{name}-{port}"

                    virtuals.append(virtual_server(
                        name=vs_name,
                        destination=f"{cluster_ip}:{port}",
                        pool=pool_name,
                        description=f"K8s ClusterIP {ns}/{name}",
                        protocol=proto,
                    ))
                    p = pool(name=pool_name, description=f"{ns}/{name} port {port}")
                    pools.append(p)
                    ep_key   = f"{ns}/{name}"
                    ep_members = self._build_members(endpoints.get(ep_key), tgt_port, pool_name)
                    members[pool_name] = ep_members

        # ── Ingress rules → virtual servers ─────────────────────────────────────
        # One VS per ingress — nG1 keys on IP:port so multiple paths per
        # ingress would conflict. One VS per ingress using first backend pool.
        for ing in ingresses:
            ns      = ing["metadata"]["namespace"]
            name    = ing["metadata"]["name"]
            ing_ips = [
                lb.get("ip", "")
                for lb in ing["status"].get("loadBalancer", {}).get("ingress", [])
            ]
            if not ing_ips:
                continue
            ing_ip = ing_ips[0]

            hosts = []
            first_svc_name = ""
            first_svc_port = 80
            for rule in ing["spec"].get("rules", []):
                host = rule.get("host", "")
                if host:
                    hosts.append(host)
                for path_def in rule.get("http", {}).get("paths", []):
                    svc_ref  = path_def.get("backend", {}).get("service", {})
                    svc_name = svc_ref.get("name", "")
                    svc_port = svc_ref.get("port", {}).get("number", 80)
                    if not svc_name:
                        continue
                    if not first_svc_name:
                        first_svc_name = svc_name
                        first_svc_port = svc_port
                    pool_name = f"p-{svc_name}-{svc_port}"
                    if not any(p["name"] == pool_name for p in pools):
                        pools.append(pool(name=pool_name,
                                          description=f"K8s Ingress {ns}/{name} -> {svc_name}:{svc_port}",
                                          lb_method="round-robin"))
                        ep_key = f"{ns}/{svc_name}"
                        members[pool_name] = self._build_members(endpoints.get(ep_key), svc_port, pool_name)

            if not first_svc_name:
                continue

            vs_name   = f"k-ing-{name}-{first_svc_port}"
            pool_name = f"p-{first_svc_name}-{first_svc_port}"
            desc      = f"K8s Ingress {ns}/{name} hosts={','.join(hosts)}"

            if not any(v["name"] == vs_name for v in virtuals):
                virtuals.append(virtual_server(
                    name=vs_name,
                    destination=f"{ing_ip}:80",
                    pool=pool_name,
                    description=desc,
                ))

        # ── Deduplicate by destination — nG1 keys on IP:port ────────────────────
        seen_dest = set()
        deduped   = []
        for vs in virtuals:
            dest = vs.get("destination", "")
            if dest not in seen_dest:
                seen_dest.add(dest)
                deduped.append(vs)
            else:
                log.debug("[k8s] Skipping duplicate destination %s (%s)", dest, vs["name"])
        virtuals = deduped

        log.info("[k8s] Fetched: %d virtuals, %d pools, %d total members",
                 len(virtuals), len(pools), sum(len(m) for m in members.values()))

        return (
            {"kind": "tm:ltm:virtual:virtualcollectionstats", "items": virtuals},
            {"kind": "tm:ltm:pool:poolcollectionstats",       "items": pools},
            members,
        )

    def _build_members(self, ep: Optional[dict], target_port, pool_name: str) -> list:
        """Build pool member list from K8s endpoint object."""
        if not ep:
            return []
        result = []
        for subset in ep.get("subsets", []):
            # Find matching port
            ep_port = target_port
            for p in subset.get("ports", []):
                if p.get("port") == target_port or p.get("name") == str(target_port):
                    ep_port = p["port"]
                    break
            # Ready addresses → UP
            for addr in subset.get("addresses", []):
                ip = addr.get("ip", "")
                if ip:
                    result.append(pool_member(f"{ip}:{ep_port}", state="up"))
            # NotReady addresses → DOWN
            for addr in subset.get("notReadyAddresses", []):
                ip = addr.get("ip", "")
                if ip:
                    result.append(pool_member(f"{ip}:{ep_port}", state="down"))
        return result

    # ── Cache refresh ─────────────────────────────────────────────────────────

    def _refresh_cache(self):
        v, p, m = self._fetch_all()
        with self._cache_lock:
            self._virtuals_cache = v
            self._pools_cache    = p
            self._members_cache  = m
            self._cache_time     = time.time()

    # ── TmosBase interface ────────────────────────────────────────────────────

    def get_virtuals(self) -> dict:
        with self._cache_lock:
            if self._virtuals_cache is not None:
                return self._virtuals_cache
        self._refresh_cache()
        return self._virtuals_cache

    def get_pools(self) -> dict:
        with self._cache_lock:
            if self._pools_cache is not None:
                return self._pools_cache
        self._refresh_cache()
        return self._pools_cache

    def get_pool_members(self, pool_name: str) -> dict:
        with self._cache_lock:
            items = self._members_cache.get(pool_name, [])
        return {"kind": "tm:ltm:pool:members:memberscollectionstats", "items": items}

    # ── Background Watch thread ───────────────────────────────────────────────

    def _watch_loop(self):
        """Watch K8s services and endpoints — refresh cache on any change."""
        # Initial load
        try:
            self._refresh_cache()
        except Exception as e:
            log.error("[k8s] Initial cache load failed: %s", e)

        while True:
            try:
                self._watch_resource("api/v1/services")
            except Exception as e:
                log.warning("[k8s] Watch error: %s — retrying in 15s", e)
                time.sleep(15)

    def _watch_resource(self, path: str):
        """Open a watch stream and refresh cache on any event."""
        # Get current resourceVersion first
        data = self._kget(path)
        rv   = data.get("metadata", {}).get("resourceVersion", "0")

        url = f"{self._api_url}/{path.lstrip('/')}?watch=true&resourceVersion={rv}&timeoutSeconds=300"
        req = urllib.request.Request(url)
        if self._token:
            req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Accept", "application/json")

        log.info("[k8s] Opening watch stream: %s (rv=%s)", path, rv)
        with urllib.request.urlopen(req, context=self._ctx(), timeout=310) as r:
            for raw_line in r:
                line = raw_line.decode("utf-8").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype   = event.get("type", "")
                obj     = event.get("object", {})
                ns      = obj.get("metadata", {}).get("namespace", "")
                name    = obj.get("metadata", {}).get("name", "")
                log.info("[k8s] Watch event: %s %s/%s — refreshing cache", etype, ns, name)

                self._refresh_cache()
                self._trigger_ng1_relearn()

    def _trigger_ng1_relearn(self):
        """Tell nG1 to re-poll this BIG-IP device immediately."""
        if not self._ng1_url:
            return
        try:
            import base64
            creds = base64.b64encode(f"{self._ng1_user}:{self._ng1_pass}".encode()).decode()
            url   = f"{self._ng1_url}/ng1api/ncm/bigip/rediscoverLBDevice"
            body  = json.dumps({"lbDeviceName": self._ng1_lb_name}).encode()
            req   = urllib.request.Request(url, data=body, method="POST")
            req.add_header("Authorization", f"Basic {creds}")
            req.add_header("Content-Type", "application/json")
            ng1_ctx = ssl.create_default_context()
            ng1_ctx.check_hostname = False
            ng1_ctx.verify_mode    = ssl.CERT_NONE
            with urllib.request.urlopen(req, context=ng1_ctx, timeout=5):
                log.info("[k8s] nG1 relearn triggered for %s", self._ng1_lb_name)
        except Exception as e:
            log.debug("[k8s] nG1 relearn failed (non-fatal): %s", e)
