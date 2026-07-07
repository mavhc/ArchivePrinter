from __future__ import annotations

import logging
import re
import socket
from dataclasses import dataclass

from .config import AppConfig


LOGGER = logging.getLogger("archive_printer.mdns")
IPP_SERVICE_TYPE = "_ipp._tcp.local."


@dataclass(frozen=True)
class MdnsAdvertisement:
    service_type: str
    service_name: str
    server_name: str
    address: str
    port: int
    properties: dict[str, str]


class MdnsPublisher:
    def __init__(self, config: AppConfig):
        self.config = config
        self._zeroconf = None
        self._service_info = None

    def start(self) -> None:
        if not self.config.enable_mdns:
            LOGGER.info("mDNS advertisement disabled")
            return

        try:
            from zeroconf import ServiceInfo, Zeroconf
        except ImportError:
            LOGGER.warning("zeroconf package is not installed; mDNS advertisement disabled")
            return

        advertisement = build_advertisement(self.config)
        try:
            self._zeroconf = Zeroconf()
            self._service_info = ServiceInfo(
                advertisement.service_type,
                advertisement.service_name,
                addresses=[socket.inet_aton(advertisement.address)],
                port=advertisement.port,
                properties=advertisement.properties,
                server=advertisement.server_name,
            )
            self._zeroconf.register_service(self._service_info)
        except Exception:
            LOGGER.exception("failed to register mDNS advertisement")
            self.stop()
            return

        LOGGER.info(
            "advertising %s at ipp://%s:%s/ipp/print",
            advertisement.service_name,
            advertisement.server_name.rstrip("."),
            advertisement.port,
        )

    def stop(self) -> None:
        if not self._zeroconf:
            return
        try:
            if self._service_info:
                self._zeroconf.unregister_service(self._service_info)
        finally:
            self._zeroconf.close()
            self._zeroconf = None
            self._service_info = None


def build_advertisement(config: AppConfig) -> MdnsAdvertisement:
    service_type = "_ipps._tcp.local." if config.enable_tls else "_ipp._tcp.local."
    server_name = _server_name(config)
    admin_scheme = "https" if config.enable_tls else "http"
    return MdnsAdvertisement(
        service_type=service_type,
        service_name=f"{_dns_label(config.printer_name)}.{service_type}",
        server_name=server_name,
        address=_advertised_address(config),
        port=config.port,
        properties={
            "txtvers": "1",
            "qtotal": "1",
            "rp": "ipp/print",
            "ty": "Archive Printer PDF Sink",
            "note": config.printer_name,
            "adminurl": f"{admin_scheme}://{server_name.rstrip('.')}:{config.port}/",
            "pdl": "application/pdf",
            "product": "(Archive Printer)",
            "Color": "T",
            "Duplex": "F",
            "UUID": _uuid_from_name(config.printer_name),
        },
    )


def _server_name(config: AppConfig) -> str:
    configured = config.mdns_host or "archive-printer"
    label = _dns_label(configured.removesuffix(".local").removesuffix("."))
    return f"{label}.local."


def _advertised_address(config: AppConfig) -> str:
    if config.mdns_address:
        return config.mdns_address
    if config.bind_host not in {"", "0.0.0.0", "::"}:
        return config.bind_host
    return _primary_ipv4()


def _primary_ipv4() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def _dns_label(value: str) -> str:
    label = re.sub(r"[^A-Za-z0-9-]+", "-", value.strip()).strip("-")
    return label[:63] or "Archive-Printer"


def _uuid_from_name(value: str) -> str:
    import uuid

    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"archive-printer:{value}"))
