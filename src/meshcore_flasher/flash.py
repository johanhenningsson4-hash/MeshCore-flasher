from __future__ import annotations

import os
import re
import hashlib
import json
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Generator, Iterable, Sequence

from serial.tools import list_ports


DEFAULT_BAUD_RATE = 460_800
BAUD_RATES = (115_200, 230_400, 460_800, 921_600, 1_500_000)
ADDRESS_PATTERN = re.compile(r"^0[xX][0-9a-fA-F]+$|^[0-9]+$")
SUPPORTED_CHIPS = (
    "esp32",
    "esp32c2",
    "esp32c3",
    "esp32c5",
    "esp32c6",
    "esp32h2",
    "esp32p4",
    "esp32s2",
    "esp32s3",
)
CHIP_DESCRIPTION_PATTERN = re.compile(r"Chip is ([^\s(]+)", re.IGNORECASE)
FIRMWARE_MANIFEST_NAMES = ("firmware.json", "manifest.json")

OutputCallback = Callable[[str], None]


def _discard_output(_: str) -> None:
    return None


class FlashError(Exception):
    """Raised when a flash configuration is invalid or flashing fails."""


@dataclass(frozen=True)
class FirmwareImage:
    address: int
    path: Path
    sha256: str | None = None
    size: int | None = None

    def __post_init__(self) -> None:
        if self.address < 0:
            raise FlashError("Firmware addresses cannot be negative.")
        if not self.path.is_file():
            raise FlashError(f"Firmware file not found: {self.path}")
        if self.sha256 is not None:
            if (
                not isinstance(self.sha256, str)
                or not re.fullmatch(r"[0-9a-fA-F]{64}", self.sha256)
            ):
                raise FlashError("Firmware SHA-256 must be exactly 64 hexadecimal characters.")
            object.__setattr__(self, "sha256", self.sha256.casefold())
        if self.size is not None:
            if isinstance(self.size, bool) or not isinstance(self.size, int):
                raise FlashError("Firmware size must be an integer number of bytes.")
            if self.size < 0:
                raise FlashError("Firmware size cannot be negative.")

    def verify_integrity(self) -> str:
        """Verify and return this image's SHA-256 digest."""
        actual_size = 0
        digest = hashlib.sha256()
        try:
            with self.path.open("rb") as firmware:
                while chunk := firmware.read(1024 * 1024):
                    actual_size += len(chunk)
                    digest.update(chunk)
        except OSError as exc:
            raise FlashError(f"Could not read firmware file '{self.path}': {exc}") from exc

        actual_sha256 = digest.hexdigest()
        if self.size is not None and actual_size != self.size:
            raise FlashError(
                f"Firmware size check failed for '{self.path}': expected "
                f"{self.size} bytes, found {actual_size}."
            )
        if self.sha256 is not None and actual_sha256 != self.sha256:
            raise FlashError(
                f"Firmware SHA-256 check failed for '{self.path}': expected "
                f"{self.sha256}, found {actual_sha256}."
            )
        return actual_sha256


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


@dataclass(frozen=True)
class DetectedChip:
    family: str
    description: str


@dataclass(frozen=True)
class FirmwarePackage:
    name: str
    chip: str | None
    images: tuple[FirmwareImage, ...]


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


def load_firmware_package(
    source: Path | str,
) -> FirmwarePackage:
    """Load a validated firmware manifest from a directory or JSON file."""
    path = Path(source).expanduser()
    if path.is_dir():
        manifest_path = next(
            (path / name for name in FIRMWARE_MANIFEST_NAMES if (path / name).is_file()),
            None,
        )
        if manifest_path is None:
            raise FlashError(
                "Firmware package directory must contain firmware.json or manifest.json."
            )
    elif path.is_file() and path.suffix.casefold() == ".json":
        manifest_path = path
    else:
        raise FlashError(f"Firmware manifest not found: {path}")

    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FlashError(f"Could not read firmware manifest: {exc}") from exc
    if not isinstance(document, dict):
        raise FlashError("Firmware manifest must be a JSON object.")

    package_name = document.get("name", manifest_path.parent.name)
    chip = document.get("chip")
    raw_images = document.get("images")
    if not isinstance(package_name, str) or not package_name.strip():
        raise FlashError("Firmware manifest 'name' must be a non-empty string.")
    if chip is not None:
        if not isinstance(chip, str):
            raise FlashError("Firmware manifest 'chip' must be a string.")
        chip = re.sub(r"[^a-z0-9]", "", chip.casefold())
        if chip not in SUPPORTED_CHIPS:
            raise FlashError(f"Firmware manifest specifies unsupported chip '{chip}'.")
    if not isinstance(raw_images, list) or not raw_images:
        raise FlashError("Firmware manifest 'images' must be a non-empty array.")

    base_dir = manifest_path.parent.resolve()
    images: list[FirmwareImage] = []
    for index, raw_image in enumerate(raw_images):
        if not isinstance(raw_image, dict):
            raise FlashError(f"Firmware image {index + 1} must be an object.")
        filename = raw_image.get("file")
        address = raw_image.get("address")
        expected_sha256 = raw_image.get("sha256")
        expected_size = raw_image.get("size")
        if not isinstance(filename, str) or not filename.strip():
            raise FlashError(f"Firmware image {index + 1} requires a 'file'.")
        if not isinstance(address, (str, int)) or isinstance(address, bool):
            raise FlashError(
                f"Firmware image {index + 1} requires a decimal or 0x 'address'."
            )
        image_path = (base_dir / filename).resolve()
        try:
            image_path.relative_to(base_dir)
        except ValueError as exc:
            raise FlashError(
                f"Firmware image path escapes the package directory: {filename}"
            ) from exc
        image = FirmwareImage(
            parse_address(str(address)),
            image_path,
            expected_sha256,
            expected_size,
        )
        image.verify_integrity()
        images.append(image)

    return FirmwarePackage(package_name.strip(), chip, tuple(normalize_images(images)))


def _safe_extract_zip(archive: zipfile.ZipFile, destination: Path) -> None:
    destination = destination.resolve()
    for member in archive.infolist():
        target = (destination / member.filename).resolve()
        try:
            target.relative_to(destination)
        except ValueError as exc:
            raise FlashError(
                f"Firmware archive contains an unsafe path: {member.filename}"
            ) from exc
        if member.is_dir():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member) as source, target.open("wb") as output:
            while chunk := source.read(1024 * 1024):
                output.write(chunk)


@contextmanager
def open_firmware_package(source: Path | str) -> Generator[FirmwarePackage]:
    """Open a directory, JSON manifest, or safely extracted ZIP package."""
    path = Path(source).expanduser()
    if path.suffix.casefold() != ".zip":
        yield load_firmware_package(path)
        return
    if not path.is_file():
        raise FlashError(f"Firmware package not found: {path}")
    try:
        with zipfile.ZipFile(path) as archive:
            with tempfile.TemporaryDirectory(prefix="meshcore-firmware-") as temp:
                destination = Path(temp)
                _safe_extract_zip(archive, destination)
                manifest = next(
                    (
                        destination / name
                        for name in FIRMWARE_MANIFEST_NAMES
                        if (destination / name).is_file()
                    ),
                    None,
                )
                if manifest is None:
                    candidates = [
                        candidate
                        for name in FIRMWARE_MANIFEST_NAMES
                        for candidate in destination.rglob(name)
                    ]
                    if len(candidates) != 1:
                        raise FlashError(
                            "ZIP must contain exactly one firmware.json or manifest.json."
                        )
                    manifest = candidates[0]
                yield load_firmware_package(manifest)
    except (OSError, zipfile.BadZipFile) as exc:
        raise FlashError(f"Could not read firmware package: {exc}") from exc


def chip_family_from_description(description: str) -> str:
    """Resolve an esptool chip description to a supported chip family."""
    normalized = re.sub(r"[^a-z0-9]", "", description.casefold())
    matches = [
        family
        for family in SUPPORTED_CHIPS
        if normalized.startswith(family) or normalized.startswith(f"{family}x")
    ]
    if not matches:
        raise FlashError(
            f"Connected chip '{description}' is not a supported MeshCore target."
        )
    return max(matches, key=len)


_PROCESS_POLL_INTERVAL_SECONDS = 0.05
_PROCESS_TERMINATE_TIMEOUT_SECONDS = 3
_END_OF_OUTPUT = object()


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=_PROCESS_TERMINATE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=_PROCESS_TERMINATE_TIMEOUT_SECONDS)


def _run_esptool_process(
    command: list[str],
    output: OutputCallback,
    cancel_event: threading.Event | None = None,
) -> tuple[int, list[str]]:
    """Run esptool while polling cancellation independently of output activity."""
    if cancel_event and cancel_event.is_set():
        raise FlashError("Flashing cancelled.")
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            bufsize=1,
        )
    except OSError as exc:
        raise FlashError(f"Could not start esptool: {exc}") from exc

    assert process.stdout is not None
    output_queue: queue.Queue[str | Exception | object] = queue.Queue()

    def read_output() -> None:
        try:
            for line in process.stdout:
                output_queue.put(line.rstrip())
        except Exception as exc:
            output_queue.put(exc)
        finally:
            output_queue.put(_END_OF_OUTPUT)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    lines: list[str] = []
    reader_finished = False
    completed = False
    try:
        while True:
            if cancel_event and cancel_event.is_set():
                raise FlashError("Flashing cancelled.")
            try:
                item = output_queue.get(timeout=_PROCESS_POLL_INTERVAL_SECONDS)
            except queue.Empty:
                item = None
            if item is _END_OF_OUTPUT:
                reader_finished = True
            elif isinstance(item, Exception):
                raise FlashError(f"Could not read esptool output: {item}") from item
            elif isinstance(item, str):
                lines.append(item)
                output(item)

            return_code = process.poll()
            if reader_finished and return_code is not None:
                completed = True
                return return_code, lines
    finally:
        if not completed and process.poll() is None:
            _terminate_process(process)
        reader.join(timeout=_PROCESS_TERMINATE_TIMEOUT_SECONDS)
        process.stdout.close()


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


@contextmanager
def _stage_firmware_images(
    images: Sequence[FirmwareImage],
    output: OutputCallback,
) -> Generator[tuple[FirmwareImage, ...]]:
    """Copy and validate firmware once so flashing cannot observe file changes."""
    with tempfile.TemporaryDirectory(prefix="meshcore-flash-") as temp:
        destination = Path(temp)
        staged: list[FirmwareImage] = []
        for index, image in enumerate(images):
            staged_path = destination / f"{index}-{image.path.name}"
            try:
                shutil.copyfile(image.path, staged_path)
            except OSError as exc:
                raise FlashError(
                    f"Could not stage firmware file '{image.path}': {exc}"
                ) from exc
            staged_image = FirmwareImage(
                image.address,
                staged_path,
                image.sha256,
                image.size,
            )
            digest = staged_image.verify_integrity()
            staged.append(staged_image)
            output(
                f"Verified {image.path.name} at 0x{image.address:x} "
                f"({staged_path.stat().st_size} bytes, SHA-256 {digest})."
            )
        yield tuple(staged)


def build_esptool_command(
    port: str,
    images: Sequence[FirmwareImage],
    *,
    baud_rate: int = DEFAULT_BAUD_RATE,
    erase: bool = False,
    verify: bool = False,
    chip: str = "auto",
) -> list[str]:
    """Build an esptool invocation without involving a shell."""
    if not port.strip():
        raise FlashError("Select a serial port.")
    if baud_rate <= 0:
        raise FlashError("Baud rate must be positive.")
    if chip != "auto" and chip not in SUPPORTED_CHIPS:
        raise FlashError(f"Unsupported chip family '{chip}'.")
    if erase and verify:
        raise FlashError("Erase and verify cannot be requested in one operation.")
    images = normalize_images(images)

    common = [
        sys.executable,
        "-m",
        "esptool",
        "--chip",
        chip,
        "--port",
        port,
        "--baud",
        str(baud_rate),
    ]
    if erase:
        common.extend(["erase_flash"])
    elif verify:
        common.extend(["verify_flash"])
        for image in images:
            common.extend([f"0x{image.address:x}", str(image.path.resolve())])
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


def detect_chip(
    port: str,
    *,
    baud_rate: int = DEFAULT_BAUD_RATE,
    output: OutputCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> DetectedChip:
    """Connect to the device and identify its ESP chip."""
    if not port.strip():
        raise FlashError("Select a serial port.")
    if baud_rate <= 0:
        raise FlashError("Baud rate must be positive.")
    command = [
        sys.executable,
        "-m",
        "esptool",
        "--chip",
        "auto",
        "--port",
        port,
        "--baud",
        str(baud_rate),
        "--before",
        "default_reset",
        "--after",
        "no_reset",
        "chip_id",
    ]
    output = output or _discard_output
    output("Detecting device...")
    return_code, lines = _run_esptool_process(command, output, cancel_event)
    if return_code != 0:
        raise FlashError(
            "Device detection failed: check the cable, selected port, and "
            "whether the device is in download mode."
        )

    combined = "\n".join(lines)
    match = CHIP_DESCRIPTION_PATTERN.search(combined)
    if not match:
        raise FlashError("Device connected, but its chip model could not be identified.")
    description = match.group(1)
    detected = DetectedChip(chip_family_from_description(description), description)
    output(f"Detected {detected.description} ({detected.family}).")
    return detected


def flash_firmware(
    port: str,
    images: Sequence[FirmwareImage],
    *,
    baud_rate: int = DEFAULT_BAUD_RATE,
    erase: bool = False,
    output: OutputCallback | None = None,
    cancel_event: threading.Event | None = None,
    expected_chip: str | None = None,
) -> None:
    """Flash firmware and stream esptool output to a callback."""
    images = normalize_images(images)
    if output is None:
        output = _discard_output
    expected_chip = (
        re.sub(r"[^a-z0-9]", "", expected_chip.casefold()) if expected_chip else None
    )
    if expected_chip and expected_chip not in SUPPORTED_CHIPS:
        raise FlashError(f"Unsupported expected chip family '{expected_chip}'.")

    with _stage_firmware_images(images, output) as staged_images:
        detected_chip = detect_chip(
            port,
            baud_rate=baud_rate,
            output=output,
            cancel_event=cancel_event,
        )
        if expected_chip and detected_chip.family != expected_chip:
            raise FlashError(
                f"Chip verification failed: firmware expects {expected_chip}, "
                f"but the connected device is {detected_chip.description} "
                f"({detected_chip.family})."
            )
        output(f"Verified chip family: {detected_chip.family}.")

        operations: list[tuple[str, list[str]]] = []
        if erase:
            operations.append(
                (
                    "Erasing flash",
                    build_esptool_command(
                        port,
                        staged_images,
                        baud_rate=baud_rate,
                        erase=True,
                        chip=detected_chip.family,
                    ),
                )
            )
        operations.append(
            (
                "Writing firmware",
                build_esptool_command(
                    port,
                    staged_images,
                    baud_rate=baud_rate,
                    erase=False,
                    chip=detected_chip.family,
                ),
            )
        )
        operations.append(
            (
                "Verifying firmware",
                build_esptool_command(
                    port,
                    staged_images,
                    baud_rate=baud_rate,
                    verify=True,
                    chip=detected_chip.family,
                ),
            )
        )

        for label, command in operations:
            output(f"{label}...")
            return_code, _ = _run_esptool_process(command, output, cancel_event)
            if return_code != 0:
                raise FlashError(
                    f"{label} failed: esptool exited with status {return_code}."
                )

    output("Firmware verification completed successfully.")
