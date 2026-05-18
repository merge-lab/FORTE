"""FORTE sensorized gripper: a serial sensor that streams 6-channel readings.

The FORTE gripper publishes lines of the form
``T1:<val>,T2:<val>,...,T6:<val>`` over a USB serial port. This module reads
those lines in a background thread, parses them into a numpy array, and pushes
them through a :class:`SensorDataBuffer` for both in-memory access and HDF5
logging.
"""

import re
import threading
import time
from multiprocessing import Event
from queue import Empty, Queue

import numpy as np
import serial
from omegaconf import DictConfig

from forte.sensing.sensor_buffer import SensorDataBuffer
from forte.sensing.utils import get_high_precision_timestamp, setup_logger


_LINE_RE = re.compile(r"(\w+:[^,]+)")


class FORTE_sensor:
    """Read 6-channel tactile data from the FORTE sensorized gripper.

    Parameters
    ----------
    config : DictConfig
        Sensor config block. Must expose ``config.sensor.{frequency, name,
        log_file, selected_keys, port, baudrate, h5_file_path,
        enable_preprocessing}`` and ``config.sensor.use_simulation`` (kept for
        backward compatibility, but only the hardware path is used here).
    """

    WARM_UP_SECONDS = 1.0

    def __init__(self, config: DictConfig):
        self.config = config
        self.name = config.sensor.name
        self.frequency = config.sensor.frequency
        self.port = config.sensor.port
        self.baudrate = config.sensor.baudrate
        self.selected_keys = list(config.sensor.selected_keys)
        self.enable_preprocessing = config.sensor.get("enable_preprocessing", False)

        self.logger = setup_logger(config.sensor.log_file, self.name)
        self.logger.info(
            f"FORTE sensor '{self.name}' initialized at {self.frequency} Hz"
        )

        self.data_buffer = SensorDataBuffer(self.frequency, config.sensor.h5_file_path)
        self.raw_data_queue: Queue = Queue()
        self.stop_event = Event()

        self.serial_connection: serial.Serial | None = None
        self.read_thread: threading.Thread | None = None
        self.process_thread: threading.Thread | None = None

        # Last successfully parsed sample, used as a fallback when a line
        # is corrupted mid-transmission.
        self._prev_data: dict = {}

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self):
        """Start the read (and optionally processing) background threads."""
        self.read_thread = threading.Thread(target=self._read_loop, daemon=True)
        self.read_thread.start()
        if self.enable_preprocessing:
            self.process_thread = threading.Thread(
                target=self._process_loop, daemon=True
            )
            self.process_thread.start()

    def stop(self):
        """Stop background threads, flush the HDF5 file, close the port."""
        self.logger.info("Stopping FORTE sensor.")
        self.stop_event.set()
        self.data_buffer.shutdown()
        if self.read_thread and self.read_thread.is_alive():
            self.read_thread.join()
        if self.process_thread and self.process_thread.is_alive():
            self.process_thread.join()
        self._disconnect()

    def read_last(self, n: int = 1):
        """Return the most recent ``n`` parsed samples (chronological)."""
        return self.data_buffer.read_last(n)

    # ------------------------------------------------------------------ #
    # Serial I/O
    # ------------------------------------------------------------------ #
    def _connect(self):
        try:
            self.serial_connection = serial.Serial(
                self.port, self.baudrate, timeout=0.001, rtscts=True
            )
            self.logger.info(f"Connected to {self.port} @ {self.baudrate} baud.")
        except Exception as exc:
            self.logger.error(f"Failed to open serial port {self.port}: {exc}")
            raise

    def _disconnect(self):
        if self.serial_connection and self.serial_connection.is_open:
            self.serial_connection.close()
            self.logger.info(f"Disconnected from {self.port}.")

    def _warm_up(self):
        """Drain the serial buffer for ``WARM_UP_SECONDS`` to let the MCU sync."""
        self.logger.info(f"Warming up sensor for {self.WARM_UP_SECONDS}s...")
        start = time.perf_counter()
        while time.perf_counter() - start < self.WARM_UP_SECONDS:
            self._read_one_line()

    def _read_one_line(self) -> dict:
        try:
            raw = self.serial_connection.readline().decode("utf-8").strip()
            while not raw:
                raw = self.serial_connection.readline().decode("utf-8").strip()
            return {"raw_data": raw}
        except Exception as exc:
            self.logger.error(f"Serial read error: {exc}")
            return {}

    # ------------------------------------------------------------------ #
    # Threads
    # ------------------------------------------------------------------ #
    def _read_loop(self):
        """Read raw lines as fast as possible, timestamp + parse + push."""
        self._connect()
        self._warm_up()
        while not self.stop_event.is_set():
            try:
                raw = self._read_one_line()
                if not raw:
                    continue
                raw["timestamp"] = get_high_precision_timestamp()
                parsed = self._parse(raw)
                if parsed.get("sensor_data") is None:
                    continue
                # Push to the in-memory buffer immediately so callers waiting
                # on read_last() see fresh samples.
                self.data_buffer.write(parsed["sensor_data"])
                # Optional: also expose the raw queue for downstream
                # processing consumers.
                if self.enable_preprocessing:
                    self.raw_data_queue.put(parsed)
            except Exception as exc:
                self.logger.warning(f"Read loop error: {exc}")

    def _process_loop(self):
        """Optional consumer of ``raw_data_queue`` for downstream processing."""
        while not self.stop_event.is_set():
            try:
                self.raw_data_queue.get_nowait()
            except Empty:
                time.sleep(0.0001)
            except Exception as exc:
                self.logger.warning(f"Process loop error: {exc}")

    # ------------------------------------------------------------------ #
    # Parsing
    # ------------------------------------------------------------------ #
    def _parse(self, data: dict) -> dict:
        """Convert one ``Tx:<val>,Tx:<val>,...`` line into a numpy array."""
        if "raw_data" not in data:
            return {}

        try:
            matches = _LINE_RE.findall(data["raw_data"])
            values = np.array(
                [float(pair.split(":")[1].strip()) for pair in matches]
            )
            parsed = {
                "timestamp": data["timestamp"],
                "sensor_data": values,
            }
            self._prev_data = parsed
            return parsed
        except Exception as exc:
            # Corrupted line: re-use the previous sample, just stamped now.
            self.logger.error(f"Parse error: {exc}")
            fallback = dict(self._prev_data)
            fallback["timestamp"] = data["timestamp"]
            return fallback
