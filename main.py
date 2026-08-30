#!/usr/bin/env python3
"""
Casambi Network Report Generator
Uso: python main.py

Requiere un archivo .env con:
    CASAMBI_API_KEY=tu_api_key
    CASAMBI_EMAIL=tu_email
    CASAMBI_PASSWORD=tu_password
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from casambi_api import CasambiAPIError, CasambiClient
from report import generate_report

load_dotenv()


def get_credentials() -> tuple[str, str, str]:
    api_key  = os.getenv("CASAMBI_API_KEY", "").strip()
    email    = os.getenv("CASAMBI_EMAIL", "").strip()
    password = os.getenv("CASAMBI_PASSWORD", "").strip()

    if not api_key:
        api_key = input("API Key de Casambi: ").strip()
    if not email:
        email = input("Email de tu cuenta Casambi: ").strip()
    if not password:
        import getpass
        password = getpass.getpass("Contraseña: ")

    return api_key, email, password


def select_network(networks: list[dict]) -> dict:
    if not networks:
        print("No se encontraron redes en tu cuenta.")
        sys.exit(1)

    print("\nRedes disponibles:")
    for i, net in enumerate(networks, start=1):
        site = f"  [{net['site_name']}]" if net.get("site_name") else ""
        print(f"  [{i}] {net.get('name', 'Sin nombre')}{site}  (ID: {net.get('id')})")

    while True:
        raw = input("\nSelecciona el número de red: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(networks):
            return networks[int(raw) - 1]
        print(f"  Por favor ingresa un número entre 1 y {len(networks)}.")


def main() -> None:
    print("=" * 55)
    print("   GENERADOR DE INFORMES CASAMBI")
    print("=" * 55)

    api_key, email, password = get_credentials()

    client = CasambiClient(api_key)

    print("\nConectando con Casambi Cloud...")
    try:
        user_session    = client.create_user_session(email, password)
        networks_session = client.create_networks_session(email, password)
    except CasambiAPIError as e:
        print(f"\nError de autenticación: {e}")
        sys.exit(1)

    networks = client.list_networks(user_session, networks_session)
    selected = select_network(networks)

    network_id = selected["id"]
    print(f"\nDescargando datos de: {selected.get('name', network_id)} ...")

    try:
        network = client.get_network(network_id)
        network["site_name"] = selected.get("site_name", "")
        state   = client.get_network_state(network_id)
    except CasambiAPIError as e:
        print(f"\nError al obtener datos de la red: {e}")
        sys.exit(1)

    units  = network.get("units", [])
    groups = network.get("groups", [])
    scenes = network.get("scenes", [])

    print(f"  Dispositivos: {len(units)}")
    print(f"  Grupos:       {len(groups)}")
    print(f"  Escenas:      {len(scenes)}")

    print("\nObteniendo información de modelos...")
    try:
        fixtures = client.get_fixtures_for_units(units)
        print(f"  Modelos encontrados: {len(fixtures)}")
    except Exception as e:
        print(f"  Advertencia: no se pudo obtener info de modelos ({e})")
        fixtures = {}

    output_dir = Path(__file__).parent / "reportes"
    output_dir.mkdir(exist_ok=True)

    print("\nGenerando informe Excel...")
    filepath = generate_report(network, state, fixtures=fixtures, output_dir=str(output_dir))

    print(f"\nInforme generado exitosamente:")
    print(f"  {filepath.resolve()}")


if __name__ == "__main__":
    main()
