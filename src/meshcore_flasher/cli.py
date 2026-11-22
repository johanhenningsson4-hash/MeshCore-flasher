from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from meshcore_flasher.flash import (
    BAUD_RATES,
    DEFAULT_BAUD_RATE,
    FlashError,
    FirmwareImage,
    discover_ports,
    flash_firmware,
    parse_binary_spec,
)


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meshcore-flash",
        description="Flash MeshCore firmware to an ESP32-based device.",
    )
    parser.add_argument(
        "firmware",
        nargs="?",
        type=Path,
        help="merged firmware binary to write at address 0x0",
    )
    parser.add_argument(
        "-p",
        "--port",
        help="serial port, for example COM5 or /dev/ttyUSB0",
    )
    parser.add_argument(
        "--baud",
        type=int,
        choices=BAUD_RATES,
        default=DEFAULT_BAUD_RATE,
        help=f"connection baud rate (default: {DEFAULT_BAUD_RATE})",
    )
    parser.add_argument(
        "--binary",
        action="append",
        metavar="ADDRESS=FILE",
        help="firmware binary with a decimal or 0x address; may be repeated",
    )
    parser.add_argument(
        "--erase",
        action="store_true",
        help="erase the entire flash before writing firmware",
    )
    parser.add_argument(
        "--list-ports",
        action="store_true",
        help="list connected serial ports and exit",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="open the graphical interface",
    )
    return parser


def list_available_ports() -> None:
    ports = discover_ports()
    if not ports:
        print("No serial ports found.")
        return
    for port in ports:
        print(f"{port.device}\t{port.description}\t{port.hardware_id}")


def images_from_args(args: argparse.Namespace) -> list[FirmwareImage]:
    if args.firmware and args.binary:
        raise FlashError("Use either a merged firmware file or --binary, not both.")
    if args.binary:
        return [parse_binary_spec(spec) for spec in args.binary]
    if args.firmware:
        return [FirmwareImage(0, args.firmware)]
    raise FlashError("Provide a firmware file or at least one --binary option.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = create_parser()
    args = parser.parse_args(argv)

    if args.gui:
        from meshcore_flasher.gui import main as gui_main

        return gui_main()
    if args.list_ports:
        list_available_ports()
        return 0

    try:
        if not args.port:
            raise FlashError("A serial port is required. Use --list-ports to find it.")
        images = images_from_args(args)
        flash_firmware(
            args.port,
            images,
            baud_rate=args.baud,
            erase=args.erase,
            output=print,
        )
    except FlashError as exc:
        parser.error(str(exc))
    except KeyboardInterrupt:
        print("\nFlashing cancelled.", file=sys.stderr)
        return 130

    print("Flashing completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
