# MeshCore offline flasher

A Python command-line app for flashing **locally stored** MeshCore firmware
without internet access. It supports ESP32 merged images through `esptool` and
boards with a UF2 mass-storage bootloader (such as supported nRF52 and RP2040
boards). Other flashing methods, including nRF52 serial DFU and STM32, are not
supported.

## Prepare before going offline

1. Install Python 3.10 or newer and save this repository on the computer that
   will flash the devices.
2. Download and keep the correct [MeshCore firmware](https://github.com/meshcore-dev/MeshCore/releases)
   for each exact board and role (companion, repeater, etc.). For ESP32 use a
   **merged** `.bin` image, not the application-only `.bin`; for UF2 use the
   board's `.uf2`. This app does not download firmware, identify hardware, or
   check that an image matches the board.
3. For ESP32, install the optional dependency while online:

   ```sh
   python3 -m pip install -r requirements-esp32.txt
   ```

   Use the same Python interpreter to run the app later. If installation must
   happen offline, build or download the dependency and all its wheels in advance with
   `python3 -m pip wheel -r requirements-esp32.txt -w wheels`, then install
   with `python3 -m pip install --no-index --find-links wheels -r requirements-esp32.txt`.
   Prepare the wheels for the target computer's Python and operating system.
   UF2 flashing does not require `esptool`.

Back up device settings and keys first; flashing can overwrite them. Only use
trusted firmware intended for your exact hardware. Keep a working USB data
cable and any required serial drivers available offline.

## Flash offline

Run commands from this directory. On Windows, `python` may be used in place of
`python3`.

**ESP32:** Connect the device, close other serial applications, and identify
its serial port. Put the board into its download mode if needed. Flash the
merged image at `0x0`:

```sh
python3 meshcore_flasher.py esp32 /path/to/firmware-merged.bin --port /dev/ttyUSB0
```

Use a port such as `COM3` on Windows. The default baud rate is 460800; use
`--baud 115200` if needed. Serial permissions may need to be configured on
Linux. `esptool` reports progress and verifies its write; failures result in a
nonzero exit status.

**UF2:** Enter the board's UF2 bootloader mode (often by double-tapping reset)
and locate the mounted bootloader drive. Supply its root directory:

```sh
python3 meshcore_flasher.py uf2 /path/to/firmware.uf2 --volume /media/user/BOOTLOADER
```

The volume must contain `INFO_UF2.TXT`. The app copies the firmware to it;
the bootloader normally flashes and reboots the device automatically. A
successful copy does not confirm that the device accepted the firmware.
Follow the board's instructions to verify it after rebooting.

Use `python3 meshcore_flasher.py --help` or append `--help` to either command
for options.

## Tests

```sh
python3 -m unittest discover -s tests -v
```
