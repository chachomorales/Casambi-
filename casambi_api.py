"""
Casambi Cloud API client.
Docs: https://developer.casambi.com/
"""

import json

import requests

BASE_URL = "https://door.casambi.com"
WS_URL = "wss://door.casambi.com/v1/bridge/"


class CasambiAPIError(Exception):
    pass


class CasambiClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session_id = None
        # Per-network session IDs from /v1/networks/session
        self._network_sessions: dict[str, str] = {}

    @property
    def _headers(self) -> dict:
        h = {
            "X-Casambi-Key": self.api_key,
            "Content-Type": "application/json",
        }
        if self.session_id:
            h["X-Casambi-Session"] = self.session_id
        return h

    def _headers_for(self, network_id: str) -> dict:
        """Return headers using the network-specific session if available."""
        h = {
            "X-Casambi-Key": self.api_key,
            "Content-Type": "application/json",
        }
        session = self._network_sessions.get(str(network_id)) or self.session_id
        if session:
            h["X-Casambi-Session"] = session
        return h

    def _get(self, path: str, network_id: str = None) -> dict:
        url = f"{BASE_URL}{path}"
        headers = self._headers_for(network_id) if network_id else self._headers
        resp = requests.get(url, headers=headers, timeout=30)
        if not resp.ok:
            raise CasambiAPIError(f"GET {path} → {resp.status_code}: {resp.text}")
        return resp.json()

    def _post(self, path: str, body: dict) -> dict:
        url = f"{BASE_URL}{path}"
        resp = requests.post(url, headers=self._headers, json=body, timeout=30)
        if not resp.ok:
            raise CasambiAPIError(f"POST {path} → {resp.status_code}: {resp.text}")
        return resp.json()

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------

    def create_user_session(self, email: str, password: str) -> dict:
        """Authenticate with user credentials. Returns raw session payload."""
        data = self._post("/v1/users/session/", {"email": email, "password": password})
        self.session_id = data["sessionId"]
        return data

    def create_networks_session(self, email: str, password: str) -> dict:
        """
        Authenticate via /v1/networks/session — returns all networks the user
        administers, each with its own sessionId. Stores them for later use.
        """
        data = self._post("/v1/networks/session", {"email": email, "password": password})
        for net_id, net in data.items():
            if isinstance(net, dict) and "sessionId" in net:
                self._network_sessions[net_id] = net["sessionId"]
        return data

    def list_networks(self, user_session_data: dict, networks_session_data: dict) -> list[dict]:
        """
        Merge networks from both session endpoints, deduplicated by ID.
        Enriches each entry with 'site_name' where available.
        """
        networks: dict[str, dict] = {}

        # From /v1/users/session — top-level networks
        for net_id, net in user_session_data.get("networks", {}).items():
            networks[net_id] = dict(net)

        # From /v1/users/session — networks nested inside sites
        for site in user_session_data.get("sites", {}).values():
            site_name = site.get("name", "")
            for net_id, net in site.get("networks", {}).items():
                if net_id not in networks:
                    networks[net_id] = dict(net)
                networks[net_id]["site_name"] = site_name

        # From /v1/networks/session — add any missing networks
        for net_id, net in networks_session_data.items():
            if not isinstance(net, dict):
                continue
            if net_id not in networks:
                networks[net_id] = {k: v for k, v in net.items() if k != "sessionId"}

        return sorted(networks.values(), key=lambda n: n.get("name", ""))

    # ------------------------------------------------------------------
    # Network data
    # ------------------------------------------------------------------

    def get_network(self, network_id) -> dict:
        """Full network config: units, groups, scenes — normalized to lists."""
        data = self._get(f"/v1/networks/{network_id}", network_id=str(network_id))
        return self._normalize_network(data)

    def get_network_state(self, network_id) -> dict:
        """Live state of all units and groups — normalized to lists."""
        data = self._get(f"/v1/networks/{network_id}/state", network_id=str(network_id))
        return self._normalize_network(data)

    def get_image(self, network_id, image_id: str) -> bytes:
        """
        Download a network image (gallery photo or unit icon) as PNG bytes.
        Image IDs come from network['photos'][n]['image'] and unit['image'].
        """
        url = f"{BASE_URL}/v1/networks/{network_id}/images/{image_id}"
        resp = requests.get(url, headers=self._headers_for(str(network_id)), timeout=60)
        if not resp.ok:
            raise CasambiAPIError(
                f"GET /v1/networks/{network_id}/images/{image_id} → "
                f"{resp.status_code}: {resp.text}"
            )
        return resp.content

    def get_fixture(self, fixture_id: int) -> dict:
        """Return fixture details (vendor, model, name) for the given fixture ID."""
        return self._get(f"/v1/fixtures/{fixture_id}")

    def get_fixtures_for_units(self, units: list[dict], progress=None) -> dict[int, dict]:
        """
        Fetch fixture details for all unique fixtureIds found in units.
        Returns {fixtureId: fixture_dict}. Silently skips IDs that fail.

        progress: callable(done, total) — se llama tras cada modelo descargado.
        Es la parte lenta (una petición por modelo distinto), así que la app la
        usa para mover la barra de carga.
        """
        ids = {u["fixtureId"] for u in units if u.get("fixtureId")}
        fixtures: dict[int, dict] = {}
        for done, fid in enumerate(sorted(ids), start=1):
            try:
                fixtures[fid] = self.get_fixture(fid)
            except CasambiAPIError:
                pass
            if progress:
                progress(done, len(ids))
        return fixtures

    # ------------------------------------------------------------------
    # Control (WebSocket bridge — requiere un gateway online en la red)
    # ------------------------------------------------------------------

    def activate_scene(self, network_id, scene_id, level: float = 1.0,
                       timeout: int = 10) -> None:
        """
        Activate a scene via the WebSocket bridge.
        The network needs an online gateway (phone or hardware) to apply it.
        """
        from websocket import create_connection

        session = self._network_sessions.get(str(network_id)) or self.session_id
        if not session:
            raise CasambiAPIError("No hay sesión activa para esta red")

        ws = create_connection(WS_URL, subprotocols=[self.api_key], timeout=timeout)
        try:
            ws.send(json.dumps({
                "method": "open",
                "id": str(network_id),
                "session": session,
                "ref": "1",
                "wire": 1,
                "type": 1,
            }))

            # Esperar la confirmación de apertura del wire
            opened = False
            for _ in range(20):
                raw = ws.recv()
                if isinstance(raw, bytes):
                    try:
                        raw = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                # El bridge confirma con wireStatus o con peerChanged online
                if msg.get("wireStatus") == "openWireSucceed" or (
                    msg.get("method") == "peerChanged" and msg.get("online")
                ):
                    opened = True
                    break
                if msg.get("wireStatus") == "openWireFailed" or "error" in msg:
                    raise CasambiAPIError(f"El bridge rechazó la conexión: {msg}")
            if not opened:
                raise CasambiAPIError(
                    "No se pudo abrir el canal con la red "
                    "(¿hay un gateway online?)"
                )

            ws.send(json.dumps({
                "wire": 1,
                "method": "controlScene",
                "id": int(scene_id),
                "level": level,
            }))
        finally:
            ws.close()

    @staticmethod
    def _normalize_network(data: dict) -> dict:
        """Convert dict-keyed collections (units, groups, scenes) to lists."""
        result = dict(data)
        for key in ("units", "groups", "scenes"):
            val = result.get(key, {})
            if isinstance(val, dict):
                result[key] = list(val.values())
        return result
