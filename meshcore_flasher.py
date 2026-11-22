#!/usr/bin/env python3
"""Flash locally stored MeshCore firmware without a network connection."""

import argparse
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys


def firmware_file(value, suffix):
    path = Path(value).expanduser()
    if path.suffix.lower() != suffix or not path.is_file():
        raise ValueError(f"Firmware must be an existing {suffix} file: {path}")
    if path.stat().st_size == 0:
        raise ValueError(f"Firmware file is empty: {path}")
    return path.resolve()


def flash_esp32(firmware, port, baud):
    firmware = firmware_file(firmware, ".bin")
    if importlib.util.find_spec("esptool") is None:
        raise ValueError(
            "esptool is not installed for this Python interpreter; "
            "install it before going offline (see README)."
        )
    command = [
        sys.executable, "-m", "esptool", "--port", port, "--baud", str(baud),
        "write-flash", "0x0", str(firmware),
    ]
    result = subprocess.run(command, check=False)
    if result.returncode:
        raise ValueError(f"esptool failed with exit code {result.returncode}")


def flash_uf2(firmware, volume):
    firmware = firmware_file(firmware, ".uf2")
    volume = Path(volume).expanduser()
    if not volume.is_dir() or not (volume / "INFO_UF2.TXT").is_file():
        raise ValueError("Select the root of a mounted UF2 bootloader volume containing INFO_UF2.TXT")
    destination = volume / "meshcore.uf2"
    with firmware.open("rb") as source, destination.open("xb") as target:
        shutil.copyfileobj(source, target)
        target.flush()
        os.fsync(target.fileno())


def positive_int(value):
    try:
        number = int(value)
        if number > 0:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("must be a positive integer")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Flash a previously downloaded MeshCore firmware image offline."
    )
    subparsers = parser.add_subparsers(dest="platform", required=True)
    esp = subparsers.add_parser("esp32", help="flash a merged ESP32 .bin with esptool")
    esp.add_argument("firmware", help="local merged firmware .bin")
    esp.add_argument("--port", required=True, help="serial port, e.g. /dev/ttyUSB0 or COM3")
    esp.add_argument("--baud", type=positive_int, default=460800)
    uf2 = subparsers.add_parser("uf2", help="copy a .uf2 to a bootloader volume")
    uf2.add_argument("firmware", help="local firmware .uf2")
    uf2.add_argument("--volume", required=True, help="mounted UF2 bootloader volume root")
    args = parser.parse_args(argv)
    try:
        if args.platform == "esp32":
            flash_esp32(args.firmware, args.port, args.baud)
            print("esptool completed successfully.")
        else:
            flash_uf2(args.firmware, args.volume)
            print("UF2 copy completed; the device may reboot automatically.")
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
