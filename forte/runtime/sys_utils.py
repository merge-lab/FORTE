"""Shared-memory ring buffers used to pass sensor / force / slip data
between FORTE worker processes, plus the feature extractor used by the
force estimator.
"""

from multiprocessing import Lock, shared_memory

import numpy as np


# ---------------------------------------------------------------------- #
# Low-level shared-memory wrappers
# ---------------------------------------------------------------------- #
class SharedBuffer:
    """A flat numpy view over a ``multiprocessing.shared_memory`` block."""

    def __init__(self, shape, dtype="d"):
        self.shape = shape
        self.dtype = np.dtype(dtype)
        self.size = int(np.prod(shape))
        self.shm = shared_memory.SharedMemory(
            create=True, size=self.size * self.dtype.itemsize
        )
        self.array = np.ndarray(shape, dtype=self.dtype, buffer=self.shm.buf)

    def get_obj(self):
        return memoryview(self.shm.buf).cast("d")

    def __getitem__(self, key):
        return self.array[key]

    def __setitem__(self, key, value):
        self.array[key] = value

    def __getstate__(self):
        state = self.__dict__.copy()
        state["shm"] = self.shm.name
        del state["array"]
        return state

    def __setstate__(self, state):
        self.shm = shared_memory.SharedMemory(name=state["shm"])
        self.shape = state["shape"]
        self.dtype = state["dtype"]
        self.array = np.ndarray(self.shape, dtype=self.dtype, buffer=self.shm.buf)

    def close(self):
        self.shm.close()
        self.shm.unlink()


class SharedIndex:
    """A single shared int32 used as a ring-buffer write index."""

    def __init__(self):
        self.dtype = np.int32
        self.shm = shared_memory.SharedMemory(
            create=True, size=np.dtype(self.dtype).itemsize
        )
        self.array = np.ndarray((1,), dtype=self.dtype, buffer=self.shm.buf)
        self.array[0] = 0

    def __getstate__(self):
        state = self.__dict__.copy()
        state["shm"] = self.shm.name
        del state["array"]
        return state

    def __setstate__(self, state):
        self.shm = shared_memory.SharedMemory(name=state["shm"])
        self.dtype = np.int32
        self.array = np.ndarray((1,), dtype=self.dtype, buffer=self.shm.buf)

    @property
    def value(self):
        return self.array[0]

    @value.setter
    def value(self, val):
        self.array[0] = val

    def close(self):
        self.shm.close()
        self.shm.unlink()


# ---------------------------------------------------------------------- #
# Ring buffers exposed to the rest of the pipeline
# ---------------------------------------------------------------------- #
class SharedRingBuffer:
    """A circular buffer of multi-channel samples shared between processes."""

    def __init__(self, buffer_size, num_channels, dtype="d"):
        self.buffer_size = buffer_size
        self.num_channels = num_channels
        self.buffer = SharedBuffer((buffer_size * num_channels,), dtype)
        self.index = SharedIndex()
        self.lock = Lock()

    def update(self, sample):
        with self.lock:
            idx = self.index.value
            for j in range(self.num_channels):
                self.buffer[idx * self.num_channels + j] = sample[j]
            self.index.value = (idx + 1) % self.buffer_size

    def get_data(self):
        with self.lock:
            idx = self.index.value
            data = np.frombuffer(self.buffer.get_obj())
        # SharedMemory may round up to a page; slice to the requested size.
        data = data[: self.buffer_size * self.num_channels].reshape(
            (self.buffer_size, self.num_channels)
        )
        return np.vstack((data[idx:], data[:idx]))

    def get_latest(self, k):
        """Return the most recent ``k`` frames as ``(k, num_channels)``."""
        return self.get_data()[-k:, :]

    def close(self):
        self.buffer.close()
        self.index.close()


class ForceRingBuffer:
    """A circular buffer of scalar force estimates."""

    def __init__(self, buffer_size, dtype="d"):
        self.buffer_size = buffer_size
        self.buffer = SharedBuffer((buffer_size,), dtype)
        self.index = SharedIndex()
        self.lock = Lock()

    def update(self, value):
        with self.lock:
            idx = self.index.value
            self.buffer.array[idx] = value
            self.index.value = (idx + 1) % self.buffer_size

    def get_data(self):
        with self.lock:
            idx = self.index.value
            data = np.frombuffer(self.buffer.get_obj())[: self.buffer_size]
        return np.concatenate((data[idx:], data[:idx]))

    def close(self):
        self.buffer.close()
        self.index.close()


# ---------------------------------------------------------------------- #
# Force-estimation feature extractor
# ---------------------------------------------------------------------- #
def sensor2force_feature(sensor_chunk: np.ndarray) -> np.ndarray:
    """Build the 24-dim feature vector consumed by the SVR force model.

    The vector concatenates, per channel:

    * the most recent sample,
    * the mean over the last 5000 frames,
    * the mean over the last 10000 frames,
    * the mean over the last 20000 frames.

    If ``sensor_chunk`` is shorter than a given window, the mean falls back
    to the whole chunk.
    """
    n = sensor_chunk.shape[0]
    last = sensor_chunk[-1, :]
    mean_5k = np.mean(sensor_chunk[-5000:, :], axis=0) if n >= 5000 else np.mean(sensor_chunk, axis=0)
    mean_10k = np.mean(sensor_chunk[-10000:, :], axis=0) if n >= 10000 else np.mean(sensor_chunk, axis=0)
    mean_20k = np.mean(sensor_chunk[-20000:, :], axis=0) if n >= 20000 else np.mean(sensor_chunk, axis=0)
    return np.concatenate((last, mean_5k, mean_10k, mean_20k))
