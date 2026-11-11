"""
V-GUARD — o enlace, a saúde e a identidade do dispositivo, para o publicador
MQTT e o envio dos eventos.

Até o ticket 3, este módulo também mandava a saúde e a telemetria pelo HTTP
antigo (o Sync). Desde o ticket 4 (Issue #18), elas vão pelo MQTT
(mqtt_publisher.py), e daqui saem só:

    active_link()          "wifi", "cellular" ou None: por onde o tráfego sai
    health(manager, link)  a saúde do contrato, para o tópico saude
    device_token()         o token do backend, para o PUT da foto
"""

import os
import socket
import subprocess
import time
import urllib.parse


# ============================================================
# CONFIGURAÇÃO
# ============================================================

BACKEND_URL = os.environ.get("VGUARD_BACKEND", "https://vguard.example/api")
DEVICE_ID = os.environ.get("VGUARD_DEVICE", "vguard-01")

# O token vem de um arquivo (VGUARD_TOKEN_FILE), para não aparecer no
# ambiente; na falta dele, do VGUARD_TOKEN, como no ticket 3.
DEVICE_TOKEN = os.environ.get("VGUARD_TOKEN", "")
TOKEN_FILE = os.environ.get("VGUARD_TOKEN_FILE", "")

# Nomes das conexões Wi-Fi do NetworkManager autorizadas a levar dados,
# separados por vírgula (VGUARD_WIFI). Na bancada, a rede da casa; sem
# nenhuma, o Wi-Fi não leva os eventos (só o 4G).
AUTHORIZED_WIFI = [name for name in os.environ.get("VGUARD_WIFI", "").split(",") if name]

# Interfaces do módulo 4G quando ele aparece como placa de rede USB.
# (Se aparecer como modem "gsm" no NetworkManager, já é reconhecido.)
CELLULAR_DEVICES = ["usb0", "wwan0"]


def device_token():
    """O token do backend: do VGUARD_TOKEN_FILE, ou do VGUARD_TOKEN. Vazio se nenhum."""
    if TOKEN_FILE:
        try:
            with open(os.path.expanduser(TOKEN_FILE)) as f:
                return f.read().strip()
        except OSError:
            return ""
    return DEVICE_TOKEN


# ============================================================
# ENLACE
# ============================================================

def active_link():
    """
    Retorna "wifi", "cellular" ou None.

    Pergunta ao kernel por qual interface o tráfego até o backend sai de
    fato. A precedência do Wi-Fi sobre o 4G (RF30) é a métrica de rota do
    sistema: a conexão Wi-Fi precisa ter métrica menor que a do 4G.
    """
    try:
        host = urllib.parse.urlsplit(BACKEND_URL).hostname
        route = _run("ip", "-o", "route", "get", socket.gethostbyname(host))
        device = route.split()[route.split().index("dev") + 1]

        if device in CELLULAR_DEVICES:
            return "cellular"

        # Backend na mesma máquina (desenvolvimento no notebook): sem custo
        # de franquia, vale como Wi-Fi. Na Pi o backend é remoto e nunca cai aqui.
        if device == "lo":
            return "wifi"

        kind, connection = _run(
            "nmcli", "-g", "GENERAL.TYPE,GENERAL.CONNECTION",
            "device", "show", device
        ).splitlines()[:2]

    except (OSError, ValueError, subprocess.SubprocessError):
        return None

    if kind == "gsm":
        return "cellular"
    if kind == "wifi" and connection in AUTHORIZED_WIFI:
        return "wifi"
    return None


def _run(*command):
    return subprocess.run(
        command, capture_output=True, text=True, timeout=5, check=True
    ).stdout


def _read_number(path):
    try:
        with open(path) as f:
            return int(f.read())
    except (OSError, ValueError):
        return None


# ============================================================
# SAÚDE
# ============================================================

def health(manager, link):
    """
    A saúde do dispositivo (contrato, tópico saude). Os campos throttled,
    vision_fps, esp32 e time_source são do ticket 5.
    """
    temperature = _read_number("/sys/class/thermal/thermal_zone0/temp")

    # Bytes desde o boot: o contador zera a cada reinicialização.
    cellular_bytes = sum(
        _read_number(
            "/sys/class/net/%s/statistics/%s" % (device, counter)
        ) or 0
        for device in CELLULAR_DEVICES
        for counter in ("rx_bytes", "tx_bytes")
    )

    return {
        "ts": round(time.time(), 3),
        "link": link,
        "pending": {"events": manager.pending_counts()["events"]},
        "storage": manager.storage_status(),
        "temperature": temperature and temperature / 1000,
        "cellular_bytes": cellular_bytes,
    }
