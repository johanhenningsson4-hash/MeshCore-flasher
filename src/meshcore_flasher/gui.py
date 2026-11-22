from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from meshcore_flasher.flash import (
    BAUD_RATES,
    DEFAULT_BAUD_RATE,
    FlashError,
    FirmwareImage,
    SUPPORTED_CHIPS,
    discover_ports,
    flash_firmware,
    open_firmware_package,
)


class FlasherApp(ttk.Frame):
    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=16)
        self.master = master
        self.output_queue: queue.Queue[str | None] = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.port_var = tk.StringVar()
        self.firmware_var = tk.StringVar()
        self.address_var = tk.StringVar(value="0x0")
        self.baud_var = tk.StringVar(value=str(DEFAULT_BAUD_RATE))
        self.chip_var = tk.StringVar(value="Auto-detect")
        self.erase_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Connect a device and select firmware.")
        self._build_ui()
        self.refresh_ports()
        self.master.protocol("WM_DELETE_WINDOW", self.close)
        self.after(100, self._drain_output)
        self.grid(row=0, column=0, sticky="nsew")

    def _build_ui(self) -> None:
        self.master.title("MeshCore Flasher")
        self.master.minsize(680, 500)
        self.master.columnconfigure(0, weight=1)
        self.master.rowconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(6, weight=1)

        ttk.Label(self, text="Serial port").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=4
        )
        self.port_combo = ttk.Combobox(
            self, textvariable=self.port_var, state="readonly"
        )
        self.port_combo.grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Button(self, text="Refresh", command=self.refresh_ports).grid(
            row=0, column=2, padx=(8, 0), pady=4
        )

        ttk.Label(self, text="Firmware/package").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=4
        )
        firmware_entry = ttk.Entry(self, textvariable=self.firmware_var)
        firmware_entry.grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Button(self, text="Browse…", command=self.choose_firmware).grid(
            row=1, column=2, padx=(8, 0), pady=4
        )

        ttk.Label(self, text="Flash address").grid(
            row=2, column=0, sticky="w", padx=(0, 8), pady=4
        )
        address_entry = ttk.Entry(self, textvariable=self.address_var, width=14)
        address_entry.grid(row=2, column=1, sticky="w", pady=4)
        ttk.Label(
            self,
            text="Use 0x0 for a merged image.",
            foreground="#555555",
        ).grid(row=2, column=1, sticky="e", pady=4)

        ttk.Label(self, text="Baud rate").grid(
            row=3, column=0, sticky="w", padx=(0, 8), pady=4
        )
        baud_combo = ttk.Combobox(
            self,
            textvariable=self.baud_var,
            values=[str(rate) for rate in BAUD_RATES],
            state="readonly",
            width=14,
        )
        baud_combo.grid(row=3, column=1, sticky="w", pady=4)
        ttk.Label(self, text="Expected chip").grid(
            row=4, column=0, sticky="w", padx=(0, 8), pady=4
        )
        chip_combo = ttk.Combobox(
            self,
            textvariable=self.chip_var,
            values=("Auto-detect", *SUPPORTED_CHIPS),
            state="readonly",
            width=14,
        )
        chip_combo.grid(row=4, column=1, sticky="w", pady=4)

        ttk.Checkbutton(
            self,
            text="Erase all flash before writing",
            variable=self.erase_var,
        ).grid(row=4, column=2, sticky="e", pady=4)

        button_frame = ttk.Frame(self)
        button_frame.grid(row=5, column=0, columnspan=3, sticky="ew", pady=12)
        self.flash_button = ttk.Button(
            button_frame, text="Flash firmware", command=self.start_flash
        )
        self.flash_button.pack(side="left")
        self.cancel_button = ttk.Button(
            button_frame, text="Cancel", command=self.cancel_flash, state="disabled"
        )
        self.cancel_button.pack(side="left", padx=8)
        ttk.Label(button_frame, textvariable=self.status_var).pack(
            side="right"
        )

        log_frame = ttk.LabelFrame(self, text="Device output", padding=4)
        log_frame.grid(row=6, column=0, columnspan=3, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log = tk.Text(log_frame, wrap="none", height=14, state="disabled")
        self.log.grid(row=0, column=0, sticky="nsew")
        scroll_y = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        scroll_y.grid(row=0, column=1, sticky="ns")
        scroll_x = ttk.Scrollbar(log_frame, orient="horizontal", command=self.log.xview)
        scroll_x.grid(row=1, column=0, sticky="ew")
        self.log.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)

    def refresh_ports(self) -> None:
        ports = discover_ports()
        values = [port.display_name for port in ports]
        self.port_combo["values"] = values
        self._port_devices = {port.display_name: port.device for port in ports}
        if values and self.port_var.get() not in values:
            self.port_var.set(values[0])
        elif not values:
            self.port_var.set("")
        self._append_log(
            f"Found {len(ports)} serial port(s)."
            if ports
            else "No serial ports found. Connect the device and refresh."
        )

    def choose_firmware(self) -> None:
        filename = filedialog.askopenfilename(
            title="Select MeshCore firmware or package",
            filetypes=(
                ("Firmware packages", "*.zip"),
                ("Firmware manifests", "*.json"),
                ("Firmware binaries", "*.bin"),
                ("All files", "*.*"),
            ),
        )
        if filename:
            self.firmware_var.set(filename)

    def _selected_port(self) -> str:
        return self._port_devices.get(self.port_var.get(), self.port_var.get())

    def start_flash(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            from meshcore_flasher.flash import parse_address

            source = Path(self.firmware_var.get()).expanduser()
            port = self._selected_port()
            if not port:
                raise FlashError("Select a serial port.")
            baud = int(self.baud_var.get())
            expected_chip = (
                None if self.chip_var.get() == "Auto-detect" else self.chip_var.get()
            )
            if source.suffix.casefold() in (".zip", ".json"):
                with open_firmware_package(source) as package:
                    if (
                        expected_chip
                        and package.chip
                        and expected_chip != package.chip
                    ):
                        raise FlashError(
                            f"Selected chip {expected_chip} conflicts with package "
                            f"chip {package.chip}."
                        )
                    firmware_input: tuple[FirmwareImage, ...] | Path = source
                    expected_chip = expected_chip or package.chip
                    package_name = package.name
            else:
                firmware_input = (
                    FirmwareImage(parse_address(self.address_var.get()), source),
                )
                package_name = source.name
        except (FlashError, ValueError) as exc:
            messagebox.showerror("Cannot flash", str(exc), parent=self)
            return

        if self.erase_var.get() and not messagebox.askyesno(
            "Erase device",
            "This will erase the entire chip before flashing. Continue?",
            parent=self,
        ):
            return

        self.cancel_event.clear()
        self._set_busy(True)
        self._clear_log()
        self._append_log(f"Flashing {package_name} to {port}.")
        self.worker = threading.Thread(
            target=self._flash_worker,
            args=(
                port,
                firmware_input,
                baud,
                self.erase_var.get(),
                expected_chip,
            ),
            daemon=True,
        )
        self.worker.start()

    def _flash_worker(
        self,
        port: str,
        firmware_input: tuple[FirmwareImage, ...] | Path,
        baud: int,
        erase: bool,
        expected_chip: str | None,
    ) -> None:
        try:
            if isinstance(firmware_input, Path):
                with open_firmware_package(firmware_input) as package:
                    flash_firmware(
                        port,
                        package.images,
                        baud_rate=baud,
                        erase=erase,
                        output=self.output_queue.put,
                        cancel_event=self.cancel_event,
                        expected_chip=expected_chip,
                    )
            else:
                flash_firmware(
                    port,
                    firmware_input,
                    baud_rate=baud,
                    erase=erase,
                    output=self.output_queue.put,
                    cancel_event=self.cancel_event,
                    expected_chip=expected_chip,
                )
        except FlashError as exc:
            self.output_queue.put(f"ERROR: {exc}")
            self.output_queue.put("__FLASH_FAILED__")
        else:
            self.output_queue.put("Flashing completed successfully.")
            self.output_queue.put("__FLASH_COMPLETE__")
        finally:
            self.output_queue.put(None)

    def cancel_flash(self) -> None:
        if self.worker and self.worker.is_alive():
            self.cancel_event.set()
            self.status_var.set("Cancelling…")

    def close(self) -> None:
        if self.worker and self.worker.is_alive():
            self.cancel_event.set()
            self.status_var.set("Cancelling before exit…")
            self.worker.join(timeout=5)
            if self.worker.is_alive():
                return
        self.master.destroy()

    def _set_busy(self, busy: bool) -> None:
        self.flash_button.configure(state="disabled" if busy else "normal")
        self.cancel_button.configure(state="normal" if busy else "disabled")
        self.status_var.set("Flashing…" if busy else "Ready.")

    def _drain_output(self) -> None:
        result: str | None = None
        while True:
            try:
                line = self.output_queue.get_nowait()
            except queue.Empty:
                break
            if line is None:
                self._set_busy(False)
            elif line == "__FLASH_COMPLETE__":
                result = "complete"
            elif line == "__FLASH_FAILED__":
                result = "failed"
            else:
                self._append_log(line)
        if result == "complete":
            messagebox.showinfo("Success", "Flashing completed successfully.", parent=self)
        elif result == "failed":
            messagebox.showerror(
                "Flashing failed",
                "The operation did not complete. Check the device output for details.",
                parent=self,
            )
        self.after(100, self._drain_output)

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")


def main() -> int:
    root = tk.Tk()
    FlasherApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
