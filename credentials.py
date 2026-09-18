"""
Cuentas de Casambi, guardadas en el Llavero de macOS o en un fichero cifrado.

La app admite varias cuentas (varias API keys / usuarios); las redes de todas
se muestran juntas en la barra lateral, y cada red recuerda a qué cuenta
pertenece para usar su sesión.

En el Llavero, bajo el servicio com.impelsa.casambi:
  · "accounts"          → índice JSON [{"id", "label", "email"}, …]
  · "<id>.api_key"      → API key de esa cuenta
  · "<id>.email"        → email de esa cuenta
  · "<id>.password"     → contraseña de esa cuenta

En macOS se leen y escriben con /usr/bin/security, así que no hace falta
ninguna dependencia extra ni que el .app esté firmado con un certificado de
desarrollador. En el servidor ese binario no existe, y la misma estructura vive
en un único fichero cifrado con Fernet (ver _BackendFichero): las contraseñas
de Casambi dan control sobre los edificios de los clientes y no pueden quedar
legibles en el disco del VPS ni en las copias de seguridad.

El backend lo elige config.backend_credenciales(); de la capa de índice hacia
arriba, el resto del módulo no sabe cuál está usando.

Los valores se guardan en base64: `security find-generic-password -w` imprime
el dato en hexadecimal cuando contiene bytes no ASCII (una contraseña con
acentos, por ejemplo), y ese hex es indistinguible de un valor ASCII que por
casualidad solo tenga caracteres hex. Guardando siempre base64 la lectura es
inequívoca.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import threading
import uuid
from pathlib import Path

import config

SECURITY = "/usr/bin/security"
SERVICE = "com.impelsa.casambi"
INDEX_ACCOUNT = "accounts"
FIELDS = ("api_key", "email", "password")

# Entradas de la versión de una sola cuenta, que se migran al índice.
LEGACY_FIELDS = FIELDS

_LABEL = "CASAMBI — credenciales de Casambi Cloud"


class CredentialsError(RuntimeError):
    pass


# ── Llavero ───────────────────────────────────────────────────────────────────

def _encode(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _decode(raw: str) -> str:
    try:
        return base64.b64decode(raw, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        # Entrada creada a mano o corrupta: se ignora en vez de romper la app.
        return ""


def _read_raw(account: str) -> str:
    try:
        proc = subprocess.run(
            [SECURITY, "find-generic-password", "-s", SERVICE, "-a", account, "-w"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:  # 44 = no existe la entrada
        return ""
    return proc.stdout.strip()


def _write_raw(account: str, payload: str) -> None:
    try:
        proc = subprocess.run(
            [SECURITY, "add-generic-password", "-U", "-s", SERVICE,
             "-a", account, "-l", _LABEL, "-w"],
            # `-w` sin valor pide el dato por stdin (dos veces, para confirmar);
            # así la credencial no aparece en la línea de comandos ni en `ps`.
            input=f"{payload}\n{payload}\n",
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise CredentialsError(f"No se pudo acceder al Llavero: {e}") from e
    if proc.returncode != 0:
        raise CredentialsError(
            f"El Llavero rechazó guardar «{account}»: {proc.stderr.strip()}"
        )


def _delete_raw(account: str) -> None:
    subprocess.run(
        [SECURITY, "delete-generic-password", "-s", SERVICE, "-a", account],
        capture_output=True, text=True, timeout=15,
    )


# ── Backend: Llavero de macOS ─────────────────────────────────────────────────

# El prompt de `security -w` lee como mucho 128 caracteres por stdin y trunca
# el resto en silencio, así que el valor se reparte en varias entradas
# (`clave#0`, `clave#1`, …). Pasarlo como argumento no tendría ese límite,
# pero dejaría la credencial visible en `ps`.
_CHUNK = 120


class _BackendLlavero:
    """El Llavero de macOS, vía /usr/bin/security. El troceado es cosa suya."""

    def leer(self, account: str) -> str:
        first = _read_raw(f"{account}#0")
        if not first:
            # Entradas escritas por la versión anterior, sin trocear.
            return _decode(_read_raw(account))

        parts = [first]
        index = 1
        while True:
            chunk = _read_raw(f"{account}#{index}")
            if not chunk:
                break
            parts.append(chunk)
            index += 1
        return _decode("".join(parts))

    def escribir(self, account: str, value: str) -> None:
        payload = _encode(value)
        chunks = [payload[i:i + _CHUNK] for i in range(0, len(payload), _CHUNK)]
        for index, chunk in enumerate(chunks):
            _write_raw(f"{account}#{index}", chunk)

        # Restos de un valor anterior más largo, y del esquema sin trocear.
        index = len(chunks)
        while _read_raw(f"{account}#{index}"):
            _delete_raw(f"{account}#{index}")
            index += 1
        _delete_raw(account)

        if self.leer(account) != value:
            raise CredentialsError(
                f"El Llavero no guardó «{account}» correctamente (valor alterado)."
            )

    def borrar(self, account: str) -> None:
        _delete_raw(account)
        index = 0
        while _read_raw(f"{account}#{index}"):
            _delete_raw(f"{account}#{index}")
            index += 1


# ── Backend: fichero cifrado (servidor) ───────────────────────────────────────

_FICHERO = "credentials.enc"
_FORMATO = 1

# La derivación de clave es cara a propósito (protege de una CASAMBI_SECRET_KEY
# pobre), así que el resultado se cachea: si no, cada petición pagaría el coste
# varias veces, porque list_accounts() se llama en casi todas las rutas.
_claves_derivadas: dict[tuple[bytes, bytes], bytes] = {}
_lock_fichero = threading.RLock()


def _home() -> Path:
    """Igual que en app.py: CASAMBI_HOME, o el directorio del código."""
    bruto = os.environ.get("CASAMBI_HOME")
    return Path(bruto) if bruto else Path(__file__).parent


class _BackendFichero:
    """Un único fichero cifrado con Fernet, con la misma forma clave → valor.

    Se cifra el diccionario entero y no valor a valor: así ni los nombres de
    las entradas (que revelan cuántas cuentas hay) quedan a la vista.
    """

    @property
    def ruta(self) -> Path:
        return _home() / "data" / _FICHERO

    def _clave(self, salt: bytes) -> bytes:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

        # Se traduce la excepción: para quien llama a este módulo, una clave
        # que falta es un problema de credenciales, y así lo recoge el
        # errorhandler de app.py sin tener que conocer config.
        try:
            secreto = config.clave_secreta_persistente()
        except config.ConfigError as e:
            raise CredentialsError(str(e)) from e
        cacheada = _claves_derivadas.get((secreto, salt))
        if cacheada is not None:
            return cacheada
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                         iterations=600_000)
        clave = base64.urlsafe_b64encode(kdf.derive(secreto))
        _claves_derivadas[(secreto, salt)] = clave
        return clave

    def _cargar(self) -> dict:
        ruta = self.ruta
        if not ruta.is_file():
            return {}
        from cryptography.fernet import Fernet, InvalidToken

        try:
            sobre = json.loads(ruta.read_text(encoding="utf-8"))
            salt = base64.b64decode(sobre["salt"])
            crudo = Fernet(self._clave(salt)).decrypt(sobre["datos"].encode("ascii"))
            datos = json.loads(crudo.decode("utf-8"))
        except (OSError, ValueError, KeyError, InvalidToken) as e:
            # No devolver {} en silencio: eso presentaría la app como «sin
            # configurar» y el siguiente guardado pisaría las credenciales
            # buenas. Casi siempre es una CASAMBI_SECRET_KEY equivocada.
            # InvalidToken no trae texto, así que sin el nombre de la clase
            # el mensaje quedaría con dos puntos y nada detrás.
            detalle = str(e) or type(e).__name__
            raise CredentialsError(
                f"No se pudo descifrar {ruta.name} ({detalle}). "
                "¿Es la CASAMBI_SECRET_KEY con la que se escribió?"
            ) from e
        return datos if isinstance(datos, dict) else {}

    def _guardar(self, datos: dict) -> None:
        from cryptography.fernet import Fernet

        ruta = self.ruta
        ruta.parent.mkdir(parents=True, exist_ok=True)
        salt = os.urandom(16)
        sobre = {
            "v": _FORMATO,
            "salt": base64.b64encode(salt).decode("ascii"),
            "datos": Fernet(self._clave(salt)).encrypt(
                json.dumps(datos, ensure_ascii=False).encode("utf-8")
            ).decode("ascii"),
        }
        tmp = ruta.with_name(f".{ruta.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(sobre), encoding="utf-8")
            tmp.chmod(0o600)
            os.replace(tmp, ruta)  # atómico: nunca queda un fichero a medias
        except OSError as e:
            tmp.unlink(missing_ok=True)
            raise CredentialsError(f"No se pudo escribir {ruta}: {e}") from e

    def leer(self, account: str) -> str:
        with _lock_fichero:
            return self._cargar().get(account, "")

    def escribir(self, account: str, value: str) -> None:
        with _lock_fichero:
            datos = self._cargar()
            datos[account] = value
            self._guardar(datos)

    def borrar(self, account: str) -> None:
        with _lock_fichero:
            datos = self._cargar()
            if datos.pop(account, None) is not None:
                self._guardar(datos)


# ── Despacho ──────────────────────────────────────────────────────────────────

_backends: dict[str, object] = {}


def _backend():
    nombre = config.backend_credenciales()
    if nombre not in _backends:
        _backends[nombre] = (
            _BackendLlavero() if nombre == "keychain" else _BackendFichero()
        )
    return _backends[nombre]


def _read(account: str) -> str:
    return _backend().leer(account)


def _write(account: str, value: str) -> None:
    _backend().escribir(account, value)


def _delete(account: str) -> None:
    _backend().borrar(account)


# ── Índice de cuentas ─────────────────────────────────────────────────────────

def _read_index() -> list[dict]:
    raw = _read(INDEX_ACCOUNT)
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [i for i in items if isinstance(i, dict) and i.get("id")]


def _write_index(items: list[dict]) -> None:
    _write(INDEX_ACCOUNT, json.dumps(items, ensure_ascii=False))


def _key(account_id: str, field: str) -> str:
    return f"{account_id}.{field}"


# ── API pública ───────────────────────────────────────────────────────────────

def list_accounts(home: Path | None = None) -> list[dict]:
    """Cuentas configuradas: [{'id', 'label', 'email'}]. Migra lo heredado."""
    _migrate_legacy(home)
    return _read_index()


def get_account(account_id: str) -> dict | None:
    """Cuenta completa con sus secretos, o None si no existe."""
    entry = next((a for a in _read_index() if a["id"] == account_id), None)
    if entry is None:
        return None
    return {
        "id": account_id,
        "label": entry.get("label", ""),
        "api_key": _read(_key(account_id, "api_key")),
        "email": _read(_key(account_id, "email")),
        "password": _read(_key(account_id, "password")),
    }


def load_all(home: Path | None = None) -> list[dict]:
    """Todas las cuentas con sus secretos, listas para autenticar."""
    return [
        acc for acc in (get_account(a["id"]) for a in list_accounts(home))
        if acc and acc["api_key"] and acc["email"] and acc["password"]
    ]


def add_account(label: str, api_key: str, email: str, password: str) -> str:
    """Añade una cuenta y devuelve su id."""
    values = {"api_key": api_key.strip(), "email": email.strip(), "password": password}
    missing = [k for k, v in values.items() if not v]
    if missing:
        raise CredentialsError(f"Faltan datos: {', '.join(missing)}")

    account_id = uuid.uuid4().hex[:8]
    for field, value in values.items():
        _write(_key(account_id, field), value)

    index = _read_index()
    index.append({
        "id": account_id,
        "label": label.strip() or values["email"],
        "email": values["email"],
    })
    _write_index(index)
    return account_id


def update_account(account_id: str, label: str, api_key: str, email: str,
                   password: str = "") -> None:
    """Actualiza una cuenta. Contraseña vacía = conservar la guardada."""
    index = _read_index()
    entry = next((a for a in index if a["id"] == account_id), None)
    if entry is None:
        raise CredentialsError("La cuenta ya no existe")

    api_key, email = api_key.strip(), email.strip()
    if not api_key or not email:
        raise CredentialsError("La API key y el email son obligatorios")
    if not password:
        password = _read(_key(account_id, "password"))
        if not password:
            raise CredentialsError("Falta la contraseña")

    _write(_key(account_id, "api_key"), api_key)
    _write(_key(account_id, "email"), email)
    _write(_key(account_id, "password"), password)

    entry["label"] = label.strip() or email
    entry["email"] = email
    _write_index(index)


def delete_account(account_id: str) -> None:
    index = [a for a in _read_index() if a["id"] != account_id]
    _write_index(index)
    for field in FIELDS:
        _delete(_key(account_id, field))


def is_configured(home: Path | None = None) -> bool:
    return bool(load_all(home))


def clear_all() -> None:
    """Borra todas las cuentas del Llavero (incluidas las entradas heredadas)."""
    for account in _read_index():
        for field in FIELDS:
            _delete(_key(account["id"], field))
    _delete(INDEX_ACCOUNT)
    for field in LEGACY_FIELDS:
        _delete(field)


# ── Migración desde versiones anteriores ──────────────────────────────────────

def legacy_env_path(home: Path | None = None) -> Path | None:
    """Ruta del .env antiguo, si todavía existe."""
    if home is None:
        return None
    path = home / ".env"
    return path if path.is_file() else None


def _read_legacy_env(home: Path | None) -> dict | None:
    path = legacy_env_path(home)
    if path is None:
        return None
    try:
        from dotenv import dotenv_values
        values = dotenv_values(path)
    except (OSError, ImportError):
        return None
    return {
        "api_key": (values.get("CASAMBI_API_KEY") or "").strip(),
        "email": (values.get("CASAMBI_EMAIL") or "").strip(),
        "password": (values.get("CASAMBI_PASSWORD") or "").strip(),
    }


def _migrate_legacy(home: Path | None = None) -> None:
    """
    Convierte en cuenta lo que dejaron versiones anteriores: primero las
    entradas sueltas del Llavero (una sola cuenta), y si no, un .env heredado.
    Se ejecuta una sola vez: al terminar, el índice ya no está vacío.
    """
    if _read_index():
        return

    # Un fallo aquí no debe tumbar la petición: la migración es oportunista y
    # se dispara desde list_accounts(), que se llama en casi todas las rutas.
    # Sin esta guarda, un .env heredado en un servidor que aún no puede
    # escribir credenciales devolvía un 500 en la portada.
    try:
        single = {field: _read(field) for field in LEGACY_FIELDS}
        if all(single.values()):
            add_account(single["email"], single["api_key"], single["email"],
                        single["password"])
            for field in LEGACY_FIELDS:
                _delete(field)
            return

        env = _read_legacy_env(home)
        if env and all(env.values()):
            add_account(env["email"], env["api_key"], env["email"], env["password"])
    except CredentialsError:
        return
