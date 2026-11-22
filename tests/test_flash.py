from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from meshcore_flasher.flash import (
    DEFAULT_BAUD_RATE,
    FlashError,
    FirmwareImage,
    build_esptool_command,
    parse_address,
    parse_binary_spec,
)


class FlashCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.firmware = Path(self.temp_dir.name) / "firmware.bin"
        self.firmware.write_bytes(b"firmware")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_builds_merged_image_command(self) -> None:
        command = build_esptool_command("COM5", [FirmwareImage(0, self.firmware)])

        self.assertEqual(command[:4], [sys.executable, "-m", "esptool", "--chip"])
        self.assertIn("COM5", command)
        self.assertIn(str(DEFAULT_BAUD_RATE), command)
        self.assertIn("write_flash", command)
        self.assertEqual(command[-2:], ["0x0", str(self.firmware.resolve())])

    def test_sorts_partition_images_by_address(self) -> None:
        high = FirmwareImage(0x10000, self.firmware)
        bootloader = FirmwareImage(0x1000, self.firmware)

        command = build_esptool_command("COM5", [high, bootloader])

        final_arguments = command[command.index("detect") + 1 :]
        self.assertEqual(final_arguments[0], "0x1000")
        self.assertEqual(final_arguments[2], "0x10000")

    def test_builds_separate_erase_command(self) -> None:
        command = build_esptool_command(
            "COM5", [FirmwareImage(0, self.firmware)], erase=True
        )

        self.assertEqual(command[-1], "erase_flash")
        self.assertNotIn("write_flash", command)

    def test_rejects_duplicate_addresses(self) -> None:
        images = [
            FirmwareImage(0, self.firmware),
            FirmwareImage(0, self.firmware),
        ]

        with self.assertRaises(FlashError):
            build_esptool_command("COM5", images)


class ParsingTests(unittest.TestCase):
    def test_parses_decimal_and_hex_addresses(self) -> None:
        self.assertEqual(parse_address("65536"), 0x10000)
        self.assertEqual(parse_address("0X10000"), 0x10000)

    def test_rejects_invalid_address(self) -> None:
        with self.assertRaisesRegex(FlashError, "Invalid firmware address"):
            parse_address("first-partition")

    def test_parses_binary_specification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            firmware = Path(directory) / "boot.bin"
            firmware.write_bytes(b"boot")

            image = parse_binary_spec(f"0x1000={firmware}")

            self.assertEqual(image.address, 0x1000)
            self.assertEqual(image.path, firmware)


if __name__ == "__main__":
    unittest.main()
