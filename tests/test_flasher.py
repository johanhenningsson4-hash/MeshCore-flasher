import contextlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import meshcore_flasher as flasher


class FlasherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "firmware-merged.bin"
        self.bin.write_bytes(b"firmware")
        self.uf2 = self.root / "firmware.uf2"
        self.uf2.write_bytes(b"uf2 firmware")
        self.volume = self.root / "bootloader"
        self.volume.mkdir()
        (self.volume / "INFO_UF2.TXT").write_text("UF2 bootloader")

    @mock.patch("meshcore_flasher.subprocess.run")
    @mock.patch("meshcore_flasher.importlib.util.find_spec", return_value=True)
    def test_esp32_uses_local_image_and_propagates_failures(self, _spec, run):
        run.return_value = subprocess.CompletedProcess([], 0)
        self.assertEqual(flasher.main(["esp32", str(self.bin), "--port", "COM3"]), 0)
        self.assertEqual(
            run.call_args.args[0],
            [flasher.sys.executable, "-m", "esptool", "--port", "COM3",
             "--baud", "460800", "write-flash", "0x0", str(self.bin)],
        )
        run.return_value = subprocess.CompletedProcess([], 2)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(flasher.main(["esp32", str(self.bin), "--port", "COM3"]), 1)

    @mock.patch("meshcore_flasher.importlib.util.find_spec", return_value=None)
    def test_missing_esptool_is_reported(self, _spec):
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(flasher.main(["esp32", str(self.bin), "--port", "COM3"]), 1)
        self.assertIn("esptool is not installed", error.getvalue())

    def test_uf2_copy_and_refuse_overwrite(self):
        self.assertEqual(flasher.main(["uf2", str(self.uf2), "--volume", str(self.volume)]), 0)
        destination = self.volume / "meshcore.uf2"
        self.assertEqual(destination.read_bytes(), self.uf2.read_bytes())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(flasher.main(["uf2", str(self.uf2), "--volume", str(self.volume)]), 1)
        self.assertEqual(destination.read_bytes(), self.uf2.read_bytes())

    def test_invalid_firmware_and_volume(self):
        for firmware, volume in [
            (self.bin, self.volume),
            (self.root / "missing.uf2", self.volume),
            (self.uf2, self.root),
        ]:
            with self.subTest(firmware=firmware, volume=volume):
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(flasher.main(["uf2", str(firmware), "--volume", str(volume)]), 1)
        self.uf2.write_bytes(b"")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(flasher.main(["uf2", str(self.uf2), "--volume", str(self.volume)]), 1)

    def test_rejects_nonpositive_baud(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            flasher.main(["esp32", str(self.bin), "--port", "COM3", "--baud", "0"])


if __name__ == "__main__":
    unittest.main()
