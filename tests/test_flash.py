from __future__ import annotations

import sys
import tempfile
import unittest
import json
import zipfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from meshcore_flasher.flash import (
    DEFAULT_BAUD_RATE,
    FlashError,
    FirmwareImage,
    build_esptool_command,
    chip_family_from_description,
    detect_chip,
    flash_firmware,
    open_firmware_package,
    load_firmware_package,
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

    def test_uses_detected_chip_for_flash_command(self) -> None:
        command = build_esptool_command(
            "COM5", [FirmwareImage(0, self.firmware)], chip="esp32s3"
        )

        self.assertEqual(command[command.index("--chip") + 1], "esp32s3")

    def test_builds_verification_command_with_same_images(self) -> None:
        boot = FirmwareImage(0x1000, self.firmware)
        app = FirmwareImage(0x10000, self.firmware)

        command = build_esptool_command(
            "COM5", [app, boot], verify=True, chip="esp32s3"
        )

        self.assertIn("verify_flash", command)
        self.assertNotIn("write_flash", command)
        self.assertEqual(
            command[command.index("verify_flash") + 1 :],
            [
                "0x1000",
                str(self.firmware.resolve()),
                "0x10000",
                str(self.firmware.resolve()),
            ],
        )

    def test_rejects_combined_erase_and_verify(self) -> None:
        with self.assertRaises(FlashError):
            build_esptool_command(
                "COM5",
                [FirmwareImage(0, self.firmware)],
                erase=True,
                verify=True,
            )

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

    def test_resolves_chip_family_from_device_description(self) -> None:
        self.assertEqual(
            chip_family_from_description("ESP32-S3FN8"), "esp32s3"
        )
        self.assertEqual(chip_family_from_description("ESP32-D0WDQ6"), "esp32")

    def test_parses_binary_specification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            firmware = Path(directory) / "boot.bin"
            firmware.write_bytes(b"boot")

            image = parse_binary_spec(f"0x1000={firmware}")

            self.assertEqual(image.address, 0x1000)
            self.assertEqual(image.path, firmware)


class DeviceDetectionTests(unittest.TestCase):
    @patch("meshcore_flasher.flash.subprocess.run")
    def test_detects_chip_from_esptool_output(self, run_mock) -> None:
        run_mock.return_value.returncode = 0
        run_mock.return_value.stdout = (
            "Detecting chip type... ESP32-S3\n"
            "Chip is ESP32-S3 (revision v0.2)\n"
        )

        detected = detect_chip("COM5")

        self.assertEqual(detected.family, "esp32s3")
        self.assertEqual(detected.description, "ESP32-S3")

    @patch("meshcore_flasher.flash.subprocess.run")
    def test_reports_detection_failure_before_flash(self, run_mock) -> None:
        run_mock.return_value.returncode = 2
        run_mock.return_value.stdout = "Failed to connect\n"

        with self.assertRaisesRegex(FlashError, "Device detection failed"):
            detect_chip("COM5")

    @patch("meshcore_flasher.flash.detect_chip")
    @patch("meshcore_flasher.flash.subprocess.Popen")
    def test_rejects_mismatched_chip_before_write(self, popen_mock, detect_mock) -> None:
        from meshcore_flasher.flash import DetectedChip

        with tempfile.TemporaryDirectory() as directory:
            firmware = Path(directory) / "firmware.bin"
            firmware.write_bytes(b"firmware")
            detect_mock.return_value = DetectedChip("esp32", "ESP32-D0WDQ6")

            with self.assertRaisesRegex(FlashError, "Chip verification failed"):
                flash_firmware(
                    "COM5",
                    [FirmwareImage(0, firmware)],
                    expected_chip="ESP32-S3",
                )

        popen_mock.assert_not_called()

    @patch("meshcore_flasher.flash.detect_chip")
    @patch("meshcore_flasher.flash.subprocess.Popen")
    def test_verifies_after_writing(self, popen_mock, detect_mock) -> None:
        from meshcore_flasher.flash import DetectedChip

        with tempfile.TemporaryDirectory() as directory:
            firmware = Path(directory) / "firmware.bin"
            firmware.write_bytes(b"firmware")
            detect_mock.return_value = DetectedChip("esp32s3", "ESP32-S3")
            write_process = unittest.mock.Mock()
            write_process.stdout = StringIO("write ok\n")
            write_process.wait.return_value = 0
            verify_process = unittest.mock.Mock()
            verify_process.stdout = StringIO("verification ok\n")
            verify_process.wait.return_value = 0
            popen_mock.side_effect = [write_process, verify_process]

            flash_firmware("COM5", [FirmwareImage(0, firmware)])

        self.assertEqual(popen_mock.call_count, 2)
        write_command = popen_mock.call_args_list[0].args[0]
        verify_command = popen_mock.call_args_list[1].args[0]
        self.assertIn("write_flash", write_command)
        self.assertIn("verify_flash", verify_command)

    @patch("meshcore_flasher.flash.detect_chip")
    @patch("meshcore_flasher.flash.subprocess.Popen")
    def test_reports_verification_failure(self, popen_mock, detect_mock) -> None:
        from meshcore_flasher.flash import DetectedChip

        with tempfile.TemporaryDirectory() as directory:
            firmware = Path(directory) / "firmware.bin"
            firmware.write_bytes(b"firmware")
            detect_mock.return_value = DetectedChip("esp32s3", "ESP32-S3")
            write_process = unittest.mock.Mock()
            write_process.stdout = StringIO("write ok\n")
            write_process.wait.return_value = 0
            verify_process = unittest.mock.Mock()
            verify_process.stdout = StringIO("mismatch\n")
            verify_process.wait.return_value = 1
            popen_mock.side_effect = [write_process, verify_process]

            with self.assertRaisesRegex(FlashError, "Verifying firmware failed"):
                flash_firmware("COM5", [FirmwareImage(0, firmware)])


class FirmwarePackageTests(unittest.TestCase):
    def test_loads_manifest_with_chip_and_images(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "boot.bin").write_bytes(b"boot")
            (root / "app.bin").write_bytes(b"app")
            (root / "firmware.json").write_text(
                json.dumps(
                    {
                        "name": "MeshCore release",
                        "chip": "ESP32-S3",
                        "images": [
                            {"address": "0x1000", "file": "boot.bin"},
                            {"address": "0x10000", "file": "app.bin"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            package = load_firmware_package(root)

            self.assertEqual(package.name, "MeshCore release")
            self.assertEqual(package.chip, "esp32s3")
            self.assertEqual(
                [image.address for image in package.images],
                [0x1000, 0x10000],
            )

    def test_loads_zip_package_while_context_is_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_dir = root / "release"
            package_dir.mkdir()
            (package_dir / "app.bin").write_bytes(b"app")
            (package_dir / "firmware.json").write_text(
                json.dumps(
                    {
                        "name": "Archive",
                        "images": [{"address": "0x0", "file": "app.bin"}],
                    }
                ),
                encoding="utf-8",
            )
            archive_path = root / "release.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.write(package_dir / "firmware.json", "firmware.json")
                archive.write(package_dir / "app.bin", "app.bin")

            with open_firmware_package(archive_path) as package:
                self.assertTrue(package.images[0].path.is_file())
                extracted_path = package.images[0].path

            self.assertFalse(extracted_path.exists())

    def test_rejects_manifest_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "firmware.json").write_text(
                json.dumps(
                    {
                        "images": [{"address": "0x0", "file": "../outside.bin"}]
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(FlashError, "escapes the package"):
                load_firmware_package(root)

    def test_rejects_unsafe_zip_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "unsafe.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../firmware.json", "{}")

            with self.assertRaisesRegex(FlashError, "unsafe path"):
                with open_firmware_package(archive_path):
                    pass


if __name__ == "__main__":
    unittest.main()
