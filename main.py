"""
main.py — tmos-shim

One container = one backend. The backend is defined in backends.yml.
nG1 calls standard F5 iControl REST endpoints directly:

  GET /mgmt/tm/ltm/virtual
  GET /mgmt/tm/ltm/pool
  GET /mgmt/tm/ltm/pool/{pool}/members/members
  GET /mgmt/tm/sys/version

Configure nG1 BIG-IP Discovery:
  Host: <shim-host>
  Port: 8444 (A10)
"""

import logging
import os
import ssl

import uvicorn
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
log = logging.getLogger("tmos-shim")

from fastapi import Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from typing import Optional
import secrets, base64

app = FastAPI(title="tmos-shim-kubernetes", version="1.0.0")
security = HTTPBasic(auto_error=False)

SHIM_USER = os.environ.get("TMOS_USERNAME", "admin")
SHIM_PASS = os.environ.get("TMOS_PASSWORD", "netscout1")

def require_auth(request: Request, credentials: Optional[HTTPBasicCredentials] = Depends(security)):
    # 1. Accept F5 token via X-F5-Auth-Token header (nG1 flow)
    token = request.headers.get("X-F5-Auth-Token", "")
    if token:
        if _validate_token(token):
            return True
        log.warning("Invalid or expired token")
        raise HTTPException(401, "Invalid token", headers={"WWW-Authenticate": "Basic realm=\"tmos-shim\""})
    # 2. Fall back to Basic Auth
    if credentials is None:
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Basic realm=\"tmos-shim\""})
    ok_user = secrets.compare_digest(credentials.username.encode(), SHIM_USER.encode())
    ok_pass = secrets.compare_digest(credentials.password.encode(), SHIM_PASS.encode())
    if not (ok_user and ok_pass):
        log.warning("Auth failed — user=%s", credentials.username)
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Basic realm=\"tmos-shim\""})
    return True

# Single active adapter loaded at startup
ADAPTER = None
ADAPTER_NAME = ""

# Token store — maps token → username
import uuid, time
_TOKENS: dict = {}
TOKEN_TTL = 3600  # 1 hour

def _issue_token(username: str) -> str:
    token = str(uuid.uuid4()).replace("-", "")
    _TOKENS[token] = {"username": username, "expires": time.time() + TOKEN_TTL}
    return token

def _validate_token(token: str) -> bool:
    entry = _TOKENS.get(token)
    if not entry:
        return False
    if time.time() > entry["expires"]:
        del _TOKENS[token]
        return False
    return True


def load_adapter():
    global ADAPTER, ADAPTER_NAME
    path = os.environ.get("BACKENDS_FILE", "backends.yml")
    if not os.path.exists(path):
        log.error("backends.yml not found — no adapter loaded")
        return
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    if not cfg:
        log.error("backends.yml is empty")
        return

    # Load first (and typically only) entry
    name, conf = next(iter(cfg.items()))
    btype = conf.get("type", name)

    try:
        if btype == "kubernetes":
            from adapters.kubernetes_adapter import KubernetesAdapter
            ADAPTER = KubernetesAdapter(
                api_url          = conf["api_url"],
                token            = conf.get("token", ""),
                ca_cert          = conf.get("ca_cert", ""),
                verify_ssl       = conf.get("verify_ssl", False),
                ng1_url          = conf.get("ng1_url", ""),
                ng1_lb_name      = conf.get("ng1_lb_name", name),
                ng1_user         = conf.get("ng1_user", "admin"),
                ng1_pass         = conf.get("ng1_pass", "netscout1"),
                include_clusterip= conf.get("include_clusterip", False),
                namespaces       = conf.get("namespaces", ""),
            )
        else:
            log.error("Unknown adapter type: %s", btype)
            return

        ADAPTER_NAME = name
        log.info("Adapter loaded: %s (%s @ %s)", name, btype, conf.get("host"))

    except Exception as e:
        log.error("Failed to load adapter %s: %s", name, e)


def get_adapter():
    if ADAPTER is None:
        raise HTTPException(503, "No adapter configured — check backends.yml")
    return ADAPTER


# ── F5 iControl REST endpoints ────────────────────────────────────────────────

@app.post("/mgmt/shared/authn/login")
async def f5_login(request: Request):
    """F5-compatible login endpoint — nG1 POSTs credentials here to get a token."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    username = body.get("username", "")
    password = body.get("password", "")
    ok_user = secrets.compare_digest(username.encode(), SHIM_USER.encode())
    ok_pass = secrets.compare_digest(password.encode(), SHIM_PASS.encode())
    if not (ok_user and ok_pass):
        log.warning("Login failed — user=%s", username)
        raise HTTPException(401, "Authentication failed")
    token = _issue_token(username)
    log.info("Login OK — user=%s token=%s...", username, token[:8])
    return {
        "username": username,
        "loginReference": {
            "link": "https://localhost/mgmt/shared/authn/login"
        },
        "loginProviderName": "tmos",
        "token": {
            "token":            token,
            "name":             token,
            "userName":         username,
            "authProviderName": "tmos",
            "user": {
                "link": f"https://localhost/mgmt/shared/authz/users/{username}"
            },
            "timeout":          TOKEN_TTL,
            "startTime":        time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "selfLink":         f"https://localhost/mgmt/shared/authz/tokens/{token}",
            "expirationMicros": int((time.time() + TOKEN_TTL) * 1e6),
            "kind":             "shared:authz:tokens:authtokenitemstate",
        },
        "generation": 1,
        "lastUpdateMicros": int(time.time() * 1e6),
        "kind": "shared:authn:login:loginstatecollectionstate",
        "selfLink": "https://localhost/mgmt/shared/authn/login",
    }


@app.get("/health")
def health_unauthed():
    return {"status": "ok", "adapter": ADAPTER_NAME or "none"}


@app.get("/")
def index(auth: HTTPBasicCredentials = Depends(require_auth)):
    return {
        "service":      "tmos-shim",
        "version":      "2.1.0",
        "adapter":      ADAPTER_NAME or "none",
        "endpoints": [
            "GET /mgmt/tm/ltm/virtual",
            "GET /mgmt/tm/ltm/pool",
            "GET /mgmt/tm/ltm/pool/{pool}/members/members",
            "GET /mgmt/tm/sys/version",
        ],
    }


@app.get("/mgmt/tm/sys/version")
def sys_version(auth: HTTPBasicCredentials = Depends(require_auth)):
    return {
        "kind":     "tm:sys:version:versionstats",
        "selfLink": "https://localhost/mgmt/tm/sys/version?ver=12.1.6",
        "entries":  {
            "https://localhost/mgmt/tm/sys/version/0": {
                "nestedStats": {
                    "entries": {
                        "Build":   {"description": "0.0.1"},
                        "Date":    {"description": "Mon Jun  2 00:00:00 PDT 2026"},
                        "Edition": {"description": "Final"},
                        "Product": {"description": f"BIG-IP ({ADAPTER_NAME})"},
                        "Title":   {"description": "Main Package"},
                        "Version": {"description": "12.1.6"},
                    }
                }
            }
        },
    }


@app.get("/mgmt/tm/ltm/virtual/")
@app.get("/mgmt/tm/ltm/virtual")
def get_virtuals(auth: HTTPBasicCredentials = Depends(require_auth)):
    try:
        result = get_adapter().get_virtuals()
        result["totalItems"] = len(result.get("items", []))
        # Ensure longName is populated from name (maps to nG1 Long Name column)
        for v in result.get("items", []):
            if not v.get("longName"):
                v["longName"] = v.get("name", "")
        import json as _json
        log.info("VIRTUAL RESPONSE SAMPLE: %s", _json.dumps(result.get("items", [{}])[0], indent=2)[:500] if result.get("items") else "empty")
        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as e:
        log.error("get_virtuals error: %s", e)
        raise HTTPException(500, str(e))


@app.get("/mgmt/tm/ltm/pool/")
@app.get("/mgmt/tm/ltm/pool")
def get_pools(request: Request, auth: HTTPBasicCredentials = Depends(require_auth)):
    try:
        expand = request.query_params.get("expandSubcollections", "false").lower() == "true"
        adapter = get_adapter()
        result = adapter.get_pools()
        # Add totalItems to collection
        result["totalItems"] = len(result.get("items", []))
        if expand:
            # Build pool→virtual map for linking
            virtuals = adapter.get_virtuals()
            pool_to_vs = {}
            for vs in virtuals.get("items", []):
                pname = vs.get("pool", "").lstrip("/").split("/")[-1]
                if pname:
                    pool_to_vs[pname] = vs
            for pool in result.get("items", []):
                try:
                    members_result = adapter.get_pool_members(pool["name"])
                    members = members_result.get("items", [])
                except Exception:
                    members = []
                # NOTE: Real F5 does NOT include vsName/virtualServers/destination
                # on pool objects. nG1 derives the virtual->pool link via the
                # virtual.pool field. Including these caused nG1 to render
                # "Pool Short Name" as "<virtual>-<pool>" concatenation.
                # Embed members both in members and membersReference
                pool["members"]      = members
                pool["memberCount"]  = len(members)
                pool["membersReference"] = {
                    "link":            pool.get("membersReference", {}).get("link", ""),
                    "isSubcollection": True,
                    "items":           members,
                }
        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as e:
        log.error("get_pools error: %s", e)
        raise HTTPException(500, str(e))


@app.get("/mgmt/tm/ltm/pool/{pool_name}/members/members")
def get_pool_members(pool_name: str, auth: HTTPBasicCredentials = Depends(require_auth)):
    try:
        return JSONResponse(content=get_adapter().get_pool_members(pool_name))
    except HTTPException:
        raise
    except Exception as e:
        log.error("get_pool_members(%s) error: %s", pool_name, e)
        raise HTTPException(500, str(e))


@app.get("/mgmt/tm/cm/device")
def cm_device(auth=Depends(require_auth)):
    """F5 cluster member info — nG1 calls this to identify the BIG-IP device."""
    return {
        "kind":     "tm:cm:device:devicecollectionstate",
        "selfLink": "https://localhost/mgmt/tm/cm/device?ver=12.1.6",
        "items": [
            {
                "kind":              "tm:cm:device:devicestate",
                "name":              ADAPTER_NAME,
                "fullPath":          f"/Common/{ADAPTER_NAME}",
                "selfLink":          f"https://localhost/mgmt/tm/cm/device/~Common~{ADAPTER_NAME}?ver=12.1.6",
                "activeModules":     [],
                "baseMac":           "00:00:00:00:00:00",
                "build":             "0.0.1",
                "chassisId":         "00000000-0000-0000-0000-000000000000",
                "chassisType":       "individual",
                "configsyncIp":      "none",
                "edition":           "Final",
                "failoverState":     "active",
                "haCapacity":        0,
                "hostname":          f"{ADAPTER_NAME}.local",
                "managementIp":      os.environ.get("TMOS_HOST_IP", "192.168.30.35"),
                "marketingName":     f"BIG-IP Virtual Edition ({ADAPTER_NAME})",
                "mirrorIp":          "any6",
                "mirrorSecondaryIp": "any6",
                "multicastIp":       "any",
                "multicastMac":      "none",
                "multicastPort":     0,
                "optionalModules":   [],
                "platform":          "Z100",
                "product":           "BIG-IP",
                "selfDevice":        "true",
                "timeZone":          "UTC",
                "unicastAddress": [
                    {
                        "effectiveIp":   os.environ.get("TMOS_HOST_IP", "192.168.30.35"),
                        "effectivePort": 1026,
                        "ip":            os.environ.get("TMOS_HOST_IP", "192.168.30.35"),
                        "port":          1026,
                    }
                ],
                "version":           "12.1.6",
            }
        ],
    }


# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    load_adapter()
    log.info("tmos-shim started — adapter: %s", ADAPTER_NAME or "NONE")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8443))
    cert = os.environ.get("TLS_CERT", "certs/cert.pem")
    key  = os.environ.get("TLS_KEY",  "certs/key.pem")

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        ssl_keyfile=key  if os.path.exists(key)  else None,
        ssl_certfile=cert if os.path.exists(cert) else None,
        log_level="info",
    )
