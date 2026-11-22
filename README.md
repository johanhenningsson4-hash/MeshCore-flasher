# MeshCore Flasher

A desktop and command-line application for flashing MeshCore firmware onto
ESP32-based devices.

## Features

- Discover connected serial ports
- Flash a merged firmware image at address `0x0`
- Flash multi-part firmware by assigning an address to each binary
- Optionally erase the chip before flashing
- Keep bootloader output visible in the application
- Select baud rate and serial port
- Run from a graphical interface or directly from a terminal

## Requirements

- Python 3.10 or newer
- A MeshCore-compatible, ESP32-based device
- A USB data cable
- The firmware `.bin` file(s) supplied by your firmware build

## Install and run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
meshcore-flasher
```

If Python is configured for your system, launching with
`python -m meshcore_flasher` is equivalent.

## Command line

Flash a merged firmware image:

```powershell
meshcore-flash --port COM5 firmware.bin
```

Erase before flashing:

```powershell
meshcore-flash --port COM5 --erase firmware.bin
```

Flash a partitioned image:

```powershell
meshcore-flash --port COM5 `
  --binary 0x1000=bootloader.bin `
  --binary 0x8000=partitions.bin `
  --binary 0x10000=app.bin
```

Use `meshcore-flash --list-ports` to find connected serial devices. The first
connected matching device is not selected automatically, so always verify the
port before flashing.

## Development

Run the test suite with:

```powershell
python -m unittest discover -s tests
```

The device must normally be in download mode while esptool connects. Many
development boards enter this mode automatically. If connection fails, hold
the BOOT button while resetting or reconnecting the board, then retry.

## Notes

- Flashing erases the target's current firmware. Use the erase option only
  when you intend to replace the complete device image.
- This flasher uses `esptool` as the underlying transport. It does not upload
  firmware or device information anywhere.
- Only ESP32-based MeshCore devices are currently supported. Other targets
  (for example, UF2-based boards) require their vendor-specific flashing tool.
