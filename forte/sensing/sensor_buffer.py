"""Ring buffer that stores recent sensor samples in memory and streams older
samples to an HDF5 file in a background thread.
"""

import collections
import os
import threading
import time
from itertools import islice

import h5py
import numpy as np


class SensorDataBuffer:
    """In-memory ring buffer plus background HDF5 writer.

    Parameters
    ----------
    sensor_frequency : int
        Expected sample rate in Hz. The in-memory deque holds ~10 s of data.
    h5_file_path : str
        Where to flush samples once a 10 s chunk has accumulated.
    """

    BUFFER_DURATION_S = 10  # seconds kept in memory and per-flush chunk size

    def __init__(self, sensor_frequency: int, h5_file_path: str):
        self.sensor_frequency = sensor_frequency
        self.h5_file_path = h5_file_path

        self.active_max_size = sensor_frequency * self.BUFFER_DURATION_S
        self.storage_threshold = sensor_frequency * self.BUFFER_DURATION_S
        self.num_samples = 0

        # Recent samples (sensor_data only) for fast read_last().
        self.active_deque = collections.deque(maxlen=self.active_max_size)

        # Double-buffered storage queues: writes go into _active, the saver
        # thread drains _inactive after a swap.
        self.storage_queue_active = collections.deque()
        self.storage_queue_inactive = collections.deque()
        self.active_storage_lock = threading.Lock()
        self.storage_lock = threading.Lock()

        self.save_event = threading.Event()
        self.shutdown_flag = threading.Event()
        self.file_lock = threading.Lock()

        self.saving_thread = threading.Thread(
            target=self._save_to_disk_thread, daemon=True
        )
        self.saving_thread.start()

    def write(self, sensor_data):
        """Append a new sample (numpy array) with a perf_counter timestamp."""
        self.active_deque.append(sensor_data)
        self.num_samples += 1

        entry = (time.perf_counter(), sensor_data)
        with self.active_storage_lock:
            self.storage_queue_active.append(entry)
            if len(self.storage_queue_active) >= self.storage_threshold:
                with self.storage_lock:
                    self.storage_queue_active, self.storage_queue_inactive = (
                        self.storage_queue_inactive,
                        self.storage_queue_active,
                    )
                    self.save_event.set()

    def read_last(self, k: int):
        """Return the most recent ``k`` samples in chronological order."""
        return list(islice(reversed(self.active_deque), k))[::-1]

    def _save_to_disk_thread(self):
        while not self.shutdown_flag.is_set() or self.storage_queue_inactive:
            self.save_event.wait(timeout=10)
            self.save_event.clear()

            with self.storage_lock:
                data_to_save = list(self.storage_queue_inactive)
                self.storage_queue_inactive.clear()

            if data_to_save:
                self._save_data(data_to_save)
            time.sleep(0.1)

    def _save_data(self, data):
        """Flush ``[(timestamp, sample), ...]`` to the HDF5 file."""
        if not data:
            return

        timestamps = np.array([t for t, _ in data], dtype="float64")

        def _squeeze_leading_1(arr):
            arr = np.asarray(arr)
            if arr.ndim > 0 and arr.shape[0] == 1:
                return arr.squeeze(0)
            return arr

        sample_shape = _squeeze_leading_1(data[0][1]).shape
        try:
            sensor_data = np.stack(
                [_squeeze_leading_1(s) for _, s in data], axis=0
            ).astype(np.float32)
        except Exception:
            sensor_data = np.array([np.asarray(s) for _, s in data], dtype=np.float32)

        with self.file_lock, h5py.File(self.h5_file_path, "a") as f:
            if "sensor_data" not in f:
                dset = f.create_dataset(
                    "sensor_data",
                    (0,) + sample_shape,
                    maxshape=(None,) + sample_shape,
                    dtype="float32",
                    compression="gzip",
                    compression_opts=4,
                )
                tset = f.create_dataset(
                    "timestamps",
                    (0,),
                    maxshape=(None,),
                    dtype="float64",
                    compression="gzip",
                    compression_opts=4,
                )
            else:
                dset = f["sensor_data"]
                tset = f["timestamps"]

            current_len = dset.shape[0]
            new_len = current_len + sensor_data.shape[0]
            dset.resize((new_len,) + sample_shape)
            tset.resize((new_len,))
            dset[current_len:] = sensor_data
            tset[current_len:] = timestamps

    def shutdown(self):
        """Stop the saver thread and flush any remaining samples to disk."""
        self.shutdown_flag.set()
        self.save_event.set()
        self.saving_thread.join()

        with self.active_storage_lock, self.storage_lock:
            remaining = list(self.storage_queue_inactive) + list(
                self.storage_queue_active
            )
            print(
                f"Saving {len(remaining)} remaining samples to "
                f"{os.path.abspath(self.h5_file_path)}"
            )
            if remaining:
                self._save_data(remaining)
            self.storage_queue_active.clear()
            self.storage_queue_inactive.clear()

        self.active_deque.clear()
