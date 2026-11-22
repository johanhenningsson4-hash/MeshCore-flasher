from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from serial.tools import list_ports


DEFAULT_BAUD_RATE = 460_800
BAUD_RATES = (115_200, 230_400, 460_800, 921_600, 1_500_000)
ADDRESS_PATTERN = re.compile(r"^0[xX][0-9a-fA-F]+$|^[0-9]+$")

OutputCallback = Callable[[str], None]


class FlashError(Exception):
    """Raised when a flash configuration is invalid or flashing fails."""


@dataclass(frozen=True)
class FirmwareImage:
    address: int
    path: Path

    def __post_init__(self) -> None:
        if self.address < 0:
            raise FlashError("Firmware addresses cannot be negative.")
        if not self.path.is_file():
            raise FlashError(f"Firmware file not found: {self.path}")


@dataclass(frozen=True)
class SerialPort:
    device: str
    description: str
    hardware_id: str

    @property
    def display_name(self) -> str:
        details = self.description
        if details and details != self.device:
            return f"{self.device} - {details}"
        return self.device


def discover_ports() -> list[SerialPort]:
    """Return connected serial ports in a stable display order."""
    ports = [
        SerialPort(port.device, port.description or "", port.hwid or "")
        for port in list_ports.comports()
    ]
    return sorted(ports, key=lambda item: item.device.casefold())


def parse_address(value: str) -> int:
    """Parse a decimal or hexadecimal firmware offset."""
    value = value.strip()
    if not ADDRESS_PATTERN.fullmatch(value):
        raise FlashError(
            f"Invalid firmware address '{value}'. Use decimal or a 0x hex value."
        )
    return int(value, 0)


def parse_binary_spec(spec: str) -> FirmwareImage:
    """Parse an OFFSET=FILE specification."""
    if "=" not in spec:
        raise FlashError(
            f"Invalid binary specification '{spec}'. Expected OFFSET=FILE."
        )
    address_text, filename = spec.split("=", 1)
    if not filename.strip():
        raise FlashError("A firmware path is required after '='.")
    return FirmwareImage(parse_address(address_text), Path(filename).expanduser())


def normalize_images(
    images: Iterable[FirmwareImage | Path | str],
) -> list[FirmwareImage]:
    normalized: list[FirmwareImage] = []
    for image in images:
        if isinstance(image, FirmwareImage):
            normalized.append(image)
        else:
            normalized.append(FirmwareImage(0, Path(image).expanduser()))
    if not normalized:
        raise FlashError("Select at least one firmware binary.")

    addresses = [image.address for image in normalized]
    if len(addresses) != len(set(addresses)):
        raise FlashError("Each firmware binary must have a unique flash address.")
    return sorted(normalized, key=lambda item: item.address)


def build_esptool_command(
    port: str,
    images: Sequence[FirmwareImage],
    *,
    baud_rate: int = DEFAULT_BAUD_RATE,
    erase: bool = False,
) -> list[str]:
    """Build an esptool invocation without involving a shell."""
    if not port.strip():
        raise FlashError("Select a serial port.")
    if baud_rate <= 0:
        raise FlashError("Baud rate must be positive.")
    images = normalize_images(images)

    common = [
        sys.executable,
        "-m",
        "esptool",
        "--chip",
        "auto",
        "--port",
        port,
        "--baud",
        str(baud_rate),
    ]
    if erase:
        common.extend(["erase_flash"])
    else:
        common.extend(
            [
                "--before",
                "default_reset",
                "--after",
                "hard_reset",
                "write_flash",
                "--flash_mode",
                "keep",
                "--flash_size",
                "detect",
            ]
        )
        for image in images:
            common.extend([f"0x{image.address:x}", str(image.path.resolve())])
    return common


def flash_firmware(
    port: str,
    images: Sequence[FirmwareImage],
    *,
    baud_rate: int = DEFAULT_BAUD_RATE,
    erase: bool = False,
    output: OutputCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    """Flash firmware and stream esptool output to a callback."""
    images = normalize_images(images)
    if output is None:
        output = lambda line: None

    operations: list[tuple[str, list[str]]] = []
    if erase:
        operations.append(
            (
                "Erasing flash",
                build_esptool_command(port, images, baud_rate=baud_rate, erase=True),
            )
        )
    operations.append(
        (
            "Writing firmware",
            build_esptool_command(port, images, baud_rate=baud_rate, erase=False),
        )
    )

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    for label, command in operations:
        output(f"{label}...")
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                bufsize=1,
            )
        except OSError as exc:
            raise FlashError(f"Could not start esptool: {exc}") from exc

        assert process.stdout is not None
        try:
            for line in process.stdout:
                if cancel_event and cancel_event.is_set():
                    process.terminate()
                    process.wait()
                    raise FlashError("Flashing cancelled.")
                output(line.rstrip())
            return_code = process.wait()
        finally:
            process.stdout.close()

        if return_code != 0:
            raise FlashError(
                f"{label} failed: esptool exited with status {return_code}."
            )
