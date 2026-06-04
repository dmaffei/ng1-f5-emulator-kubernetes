import uuid, time, logging
from abc import ABC, abstractmethod
logger = logging.getLogger(__name__)

def virtual_server(name, destination, pool=None, description="", protocol="tcp", partition="Common", enabled=True):
    ip, port = (destination.split(":", 1) + ["80"])[:2]
    return {
        "kind": "tm:ltm:virtual:virtualstats", "name": name, "partition": partition,
        "fullPath": f"/{partition}/{name}", "generation": 1,
        "selfLink": f"https://localhost/mgmt/tm/ltm/virtual/~{partition}~{name}?ver=14.1.0",
        "description": description, "destination": f"/{partition}/{ip}:{port}",
        "mask": "255.255.255.255", "enabled": enabled, "disabled": not enabled,
        "ipProtocol": protocol, "pool": f"/{partition}/{pool}" if pool else "",
        "rules": [],
        "profiles": [
            {"name": "http", "partition": "Common", "fullPath": "/Common/http", "generation": 1,
             "selfLink": "https://localhost/mgmt/tm/ltm/profile/http/~Common~http?ver=14.1.0", "context": "all"},
            {"name": "tcp",  "partition": "Common", "fullPath": "/Common/tcp",  "generation": 1,
             "selfLink": "https://localhost/mgmt/tm/ltm/profile/tcp/~Common~tcp?ver=14.1.0",  "context": "all"},
        ],
        "sourceAddressTranslation": {"type": "automap"}, "translateAddress": "enabled",
        "translatePort": "enabled", "vlansEnabled": False, "vlansDisabled": True,
        "addressStatus": "yes", "autoLasthop": "default", "cmpEnabled": "yes",
        "connectionLimit": 0, "gtmScore": 0, "mirror": "disabled",
        "mobileAppTunnel": "disabled", "nat64": "disabled", "rateLimit": "disabled",
        "rateLimitDstMask": 0, "rateLimitMode": "object", "rateLimitSrcMask": 0,
        "source": "0.0.0.0/0", "sourcePort": "preserve",
    }

def pool(name, description="", lb_method="round-robin", monitor="http", partition="Common"):
    return {
        "kind": "tm:ltm:pool:poolcollectionstats", "name": name, "partition": partition,
        "fullPath": f"/{partition}/{name}", "description": description, "generation": 1,
        "selfLink": f"https://localhost/mgmt/tm/ltm/pool/~{partition}~{name}?ver=14.1.0",
        "loadBalancingMode": lb_method, "monitor": f"/Common/{monitor}",
        "allowNat": "yes", "allowSnat": "yes", "ignorePersistedWeight": "disabled",
        "ipTosToClient": "pass-through", "ipTosToServer": "pass-through",
        "linkQosToClient": "pass-through", "linkQosToServer": "pass-through",
        "minActiveMembers": 0, "minUpMembers": 0, "minUpMembersAction": "failover",
        "minUpMembersChecking": "disabled", "queueDepthLimit": 0,
        "queueOnConnectionLimit": "disabled", "queueTimeLimit": 0,
        "reselectTries": 0, "serviceDownAction": "none", "slowRampTime": 10,
        "membersReference": {"link": f"https://localhost/mgmt/tm/ltm/pool/~{partition}~{name}/members?ver=14.1.0"},
    }

def pool_member(address, name="", state="up", partition="Common"):
    ip, port = (address.split(":", 1) + ["80"])[:2]
    member_name = name or f"{ip}:{port}"
    return {
        "kind": "tm:ltm:pool:members:membersstats", "name": member_name, "partition": partition,
        "fullPath": f"/{partition}/{member_name}", "address": ip, "ephemeral": "false",
        "fqdn": {"autopopulate": "disabled"}, "inheritProfile": "enabled", "logging": "disabled",
        "monitor": "default", "priorityGroup": 0, "rateLimit": "disabled", "ratio": 1,
        "generation": 1,
        "selfLink": f"https://localhost/mgmt/tm/ltm/pool/~{partition}~pool/members/~{partition}~{member_name}?ver=14.1.0",
        "session": "user-enabled" if state == "up" else "user-disabled", "state": state,
    }

class TokenStore:
    def __init__(self, ttl=1200):
        self._tokens = {}; self._ttl = ttl
    def create(self, username):
        token = str(uuid.uuid4()).replace("-", "")
        self._tokens[token] = {"token": token, "username": username, "created": time.time()}
        return token
    def validate(self, token):
        entry = self._tokens.get(token)
        if not entry: return None
        if time.time() - entry["created"] > self._ttl:
            del self._tokens[token]; return None
        return entry
    def revoke(self, token): self._tokens.pop(token, None)

class TmosBase(ABC):
    adapter_name = "tmos-shim"; f5_version = "14.1.0"
    def __init__(self, username="admin", password="admin"):
        self._username = username; self._password = password; self.tokens = TokenStore()
    def validate_credentials(self, u, p): return u == self._username and p == self._password
    @abstractmethod
    def get_virtuals(self): ...
    @abstractmethod
    def get_pools(self): ...
    @abstractmethod
    def get_pool_members(self, pool_name): ...
    def tmos_login_response(self, username, password):
        if not self.validate_credentials(username, password):
            return 401, {"code": 401, "message": "Authentication failed.", "errorStack": []}
        token = self.tokens.create(username)
        return 200, {"kind": "tm:auth:login:loginresponse",
            "selfLink": "https://localhost/mgmt/shared/authn/login?ver=14.1.0",
            "token": {"kind": "shared:authz:tokens:authtokenitemstate",
                "selfLink": f"https://localhost/mgmt/shared/authz/tokens/{token}?ver=14.1.0",
                "token": token, "name": token, "userName": username, "authProviderName": "tmos",
                "user": {"link": f"https://localhost/mgmt/shared/authz/users/{username}"},
                "timeout": 1200, "startTime": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "expirationTime": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "timedOut": False},
            "loginReference": {"link": f"https://localhost/mgmt/shared/authz/tokens/{token}"}}
    def tmos_version_response(self):
        return {"kind": "tm:sys:version:versionstats",
            "selfLink": "https://localhost/mgmt/tm/sys/version?ver=14.1.0",
            "entries": {"https://localhost/mgmt/tm/sys/version/0": {"nestedStats": {"entries": {
                "Build": {"description": "0.0.9"}, "Date": {"description": "Thu Sep 15 00:00:00 PDT 2022"},
                "Edition": {"description": "Final"}, "Product": {"description": "BIG-IP"},
                "Title": {"description": "Main Package"}, "Version": {"description": self.f5_version}}}}}}
