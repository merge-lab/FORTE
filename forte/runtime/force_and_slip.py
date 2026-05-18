"""Real-time force estimation and slip detection pipeline.

This module packs every worker process used by the FORTE demos:

* :func:`sensor_data_updater` -- streams filtered samples from the FORTE
  sensor into a shared ring buffer.
* :func:`force_estimator`     -- runs an SVR force model on the latest
  20000-frame window and publishes the predicted force.
* :func:`slip_predictor`      -- computes per-channel Welch PSDs and a
  moving-variance based slip indicator.
* :func:`qt_visualizer`       -- a PyQt window plotting all three streams
  plus a difference feature.

The four functions are designed to be launched as independent
``multiprocessing.Process`` targets sharing
:class:`~forte.runtime.sys_utils.SharedRingBuffer` and
:class:`~forte.runtime.sys_utils.ForceRingBuffer` instances.
"""

import csv
import os
import signal
import sys
import time
from collections import deque
from datetime import datetime

import joblib
import numpy as np

from forte.runtime.sys_utils import sensor2force_feature


# ---------------------------------------------------------------------- #
# Shared constants
# ---------------------------------------------------------------------- #
BUFFER_SIZE = 50_000   # samples in the sensor ring buffer (~25 s @ 2 kHz)
NUM_CHANNELS = 6       # FORTE sensor channels (T1..T6)
SENSOR_HZ = 2000


# ---------------------------------------------------------------------- #
# Slip-detection math (Welch PSD + moving variance)
# ---------------------------------------------------------------------- #
def welch(x, fs=1.0, nperseg=256, noverlap=128, detrend="constant"):
    """Welch power spectral density estimate."""
    x = np.asarray(x)
    n = len(x)
    step = nperseg - noverlap
    n_segments = 1 if n < nperseg else 1 + (n - nperseg) // step

    window = np.hanning(nperseg)
    U = np.sum(window ** 2)

    psd_list = []
    for i in range(n_segments):
        start = i * step
        seg = x[start : start + nperseg]
        if len(seg) < nperseg:
            seg = np.pad(seg, (0, nperseg - len(seg)), mode="constant")
        if detrend == "constant":
            seg = seg - np.mean(seg)
        seg = seg * window
        Y = np.fft.rfft(seg, n=nperseg)
        psd = (1 / (fs * U)) * np.abs(Y) ** 2
        if nperseg % 2 == 0:
            psd[1:-1] *= 2
        else:
            psd[1:] *= 2
        psd_list.append(psd)

    Pxx = np.mean(psd_list, axis=0)
    freqs = np.fft.rfftfreq(nperseg, d=1.0 / fs)
    return freqs, Pxx


def compute_window_psd(window, fs, nperseg, noverlap, low, high):
    """Max PSD (dB) within ``[low, high]`` Hz for a 1D ``window``."""
    f, Pxx = welch(window, fs=fs, nperseg=nperseg, noverlap=noverlap)
    Pxx_dB = 10 * np.log10(Pxx + 1e-12)
    mask = (f >= low) & (f <= high)
    return np.max(Pxx_dB[mask]) if np.any(mask) else -1e9


def compute_moving_variance(
    psd_history, var_window, monotonic=True, threshold_condition=False, threshold_db=-70.0
):
    """Moving variance over a 1D PSD history."""
    n = len(psd_history)
    if n < var_window:
        return np.array([])

    out = []
    for i in range(n - var_window + 1):
        win = psd_history[i : i + var_window]
        if monotonic and not np.all(np.diff(win) > 0.1):
            out.append(0)
            continue
        var = np.var(win)
        if threshold_condition and np.max(win) <= threshold_db:
            var = 0
        out.append(var)
    return np.array(out)


def update_psd_history(window, fs, nperseg, noverlap, low, high, psd_history):
    """Append one PSD-max value per channel from ``window`` (samples x channels)."""
    for ch in range(window.shape[1]):
        psd_history[ch].append(
            compute_window_psd(window[:, ch], fs, nperseg, noverlap, low, high)
        )
    return psd_history


def compute_slip_indicator(
    psd_history, var_window, monotonic, threshold_condition, threshold_db, slip_var_threshold=2
):
    """Reduce the 6-channel PSD history to ``(slip, avgL, avgR)``.

    Channels 0..2 form the *left* group and channels 3..5 the *right* group.
    If either group's average moving variance is below 0.6 we treat the whole
    indicator as 0 (single-finger contact suppression).
    """
    moving_var = []
    for ch in range(len(psd_history)):
        hist = np.array(psd_history[ch])
        if len(hist) == var_window:
            mv = compute_moving_variance(hist, var_window, monotonic, threshold_condition, threshold_db)
            moving_var.append(mv[0] if mv.size > 0 else 0)
        else:
            moving_var.append(0)

    avgL = float(np.nanmean(moving_var[0:3])) if moving_var[0:3] else 0.0
    avgR = float(np.nanmean(moving_var[3:6])) if moving_var[3:6] else 0.0
    if avgL < 0.6 or avgR < 0.6:
        avgL = 0.0
        avgR = 0.0
    slip = 1 if (avgL > slip_var_threshold or avgR > slip_var_threshold) else 0
    return slip, avgL, avgR


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #
def _install_sigterm(running_box):
    """Wire SIGTERM / SIGINT so the worker exits its loop cleanly."""

    def handler(signum, frame):  # noqa: ARG001
        running_box[0] = False

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def _csv_dump(filename, header, rows):
    with open(filename, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


def load_model(model_path):
    """Load the joblib-pickled SVR force model."""
    try:
        return joblib.load(model_path)
    except Exception as exc:
        print(f"[force_estimator] cwd={os.getcwd()} load error: {exc}")
        return None


# ---------------------------------------------------------------------- #
# Worker processes
# ---------------------------------------------------------------------- #
def sensor_data_updater(sensor_config, shared_sensor_buffer):
    """Read from the FORTE sensor, median-filter, push into the ring buffer.

    Performs:
      1. Sensor start + 4 s warm-up.
      2. Baseline subtraction over the first 4000 samples.
      3. Length-11 rolling median per channel.
      4. Publishes each filtered sample to ``shared_sensor_buffer``.
      5. Logs every (timestamp, sample) to a timestamped CSV on shutdown.
    """
    running = [True]
    _install_sigterm(running)

    sensor_log = []

    from forte.sensing import FORTE_sensor  # local import: child-process safety

    sensor = FORTE_sensor(sensor_config)

    try:
        sensor.start()
        time.sleep(4)

        baseline = np.mean(sensor.read_last(4000), axis=0)
        print(f"[sensor_updater] baseline = {baseline}")

        window_size = 11
        recent = np.zeros((window_size, NUM_CHANNELS))
        sample_count = 0
        r_idx = 0

        num_samples = sensor.data_buffer.num_samples
        prev_num_samples = num_samples
        last_throughput_log = time.time()

        while running[0]:
            if sensor.data_buffer.num_samples > num_samples:
                num_samples = sensor.data_buffer.num_samples
                sample = np.array(sensor.read_last(1)).flatten()
                recent[r_idx, :] = sample - baseline
                r_idx = (r_idx + 1) % window_size
                sample_count += 1

                if sample_count < window_size:
                    filtered = np.median(recent[:sample_count, :], axis=0)
                else:
                    filtered = np.median(recent, axis=0)

                sensor_log.append([time.time()] + filtered.tolist())
                shared_sensor_buffer.update(filtered)

            if time.time() - last_throughput_log > 1:
                print(
                    f"[sensor_updater] throughput = "
                    f"{num_samples - prev_num_samples} Hz"
                )
                prev_num_samples = num_samples
                last_throughput_log = time.time()

            time.sleep(0.0001)
    finally:
        filename = "sensor_log_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv"
        _csv_dump(
            filename,
            ["timestamp"] + [f"ch{i+1}" for i in range(NUM_CHANNELS)],
            sensor_log,
        )
        print(f"[sensor_updater] data logged to {filename}")


def force_estimator(model_path, shared_sensor_buffer, force_buffer):
    """Run the SVR force model at ~100 Hz on the latest 20000-frame window."""
    running = [True]
    _install_sigterm(running)

    force_log = []

    try:
        time.sleep(4)

        model = load_model(model_path)
        if model is None:
            print("[force_estimator] model loading failed; exiting.")
            return

        while running[0]:
            start = time.time()
            sensor_window = shared_sensor_buffer.get_latest(20000)
            features = sensor2force_feature(sensor_window)
            predicted = float(model.predict(features.reshape(1, -1))[0])

            force_log.append([time.time(), predicted])
            force_buffer.update(predicted)

            time.sleep(max(0, 0.01 - (time.time() - start)))
    finally:
        filename = "force_log_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv"
        _csv_dump(filename, ["timestamp", "predicted_force"], force_log)
        print(f"[force_estimator] data logged to {filename}")


def slip_predictor(shared_sensor_buffer, slip_buffer):
    """Slip detector based on Welch PSD + moving variance per channel."""
    running = [True]
    _install_sigterm(running)

    slip_log = []

    fs = SENSOR_HZ
    nperseg = 400
    noverlap = int(0.99 * nperseg)
    var_window = 15
    low, high = 10, 50
    monotonic = True
    threshold_condition = False
    threshold_db = -72.0
    slip_var_threshold = 2

    psd_history = [deque(maxlen=var_window) for _ in range(NUM_CHANNELS)]

    time.sleep(3)
    try:
        while running[0]:
            start = time.time()
            window = shared_sensor_buffer.get_latest(nperseg)
            if window.shape[0] < nperseg:
                time.sleep(0.01)
                continue

            psd_history = update_psd_history(
                window, fs, nperseg, noverlap, low, high, psd_history
            )

            if len(psd_history[0]) == var_window:
                slip, avgL, avgR = compute_slip_indicator(
                    psd_history,
                    var_window,
                    monotonic,
                    threshold_condition,
                    threshold_db,
                    slip_var_threshold,
                )
                slip_log.append([time.time(), slip, avgL, avgR])
                slip_buffer.update(np.array([slip, avgL, avgR]))

            time.sleep(max(0, 0.002 - (time.time() - start)))
    finally:
        filename = "slip_log_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv"
        _csv_dump(filename, ["timestamp", "slip_indicator", "avgL", "avgR"], slip_log)
        print(f"[slip_predictor] data logged to {filename}")


def qt_visualizer(shared_sensor_buffer, force_buffer, slip_buffer):
    """Launch a PyQt window plotting sensor data, force, slip, and diff features."""
    from PyQt5.QtCore import QTimer
    from PyQt5.QtWidgets import QApplication
    import pyqtgraph as pg

    class MainWindow(pg.GraphicsLayoutWidget):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("FORTE: Sensor, Force, and Slip")
            colors = ["r", "g", "b", "y", "m", "c"]

            self.sensor_plot = self.addPlot(title="Sensor Data (Filtered)")
            self.sensor_plot.addLegend()
            self.sensor_curves = [
                self.sensor_plot.plot(pen=pg.mkPen(colors[ch], width=1), name=f"Ch {ch+1}")
                for ch in range(NUM_CHANNELS)
            ]

            self.nextRow()
            self.force_plot = self.addPlot(title="Force Estimation")
            self.force_plot.addLegend()
            self.force_curve = self.force_plot.plot(
                pen=pg.mkPen("w", width=2), name="Force"
            )

            self.nextRow()
            self.slip_avg_plot = self.addPlot(title="Slip Detection: avgL and avgR")
            self.slip_avg_plot.addLegend()
            self.slip_avg_L = self.slip_avg_plot.plot(
                pen=pg.mkPen("c", width=2), name="avgL"
            )
            self.slip_avg_R = self.slip_avg_plot.plot(
                pen=pg.mkPen("m", width=2), name="avgR"
            )

            self.nextRow()
            self.slip_indicator_plot = self.addPlot(title="Slip Indicator")
            self.slip_indicator_curve = self.slip_indicator_plot.plot(
                pen=pg.mkPen("g", width=2), name="Slip"
            )

            self.nextRow()
            self.sensor_diff_plot = self.addPlot(
                title="Sensor Difference Feature (last - sample_200_ago)"
            )
            self.sensor_diff_plot.addLegend()
            self.sensor_diff_curves = [
                self.sensor_diff_plot.plot(pen=pg.mkPen(colors[ch], width=2), name=f"Diff Ch {ch+1}")
                for ch in range(NUM_CHANNELS)
            ]

            duration = BUFFER_SIZE / float(SENSOR_HZ)
            self.t_sensor = np.linspace(0, duration, BUFFER_SIZE)
            self.t_force = np.linspace(0, duration, force_buffer.buffer_size)

            slip_rate = 500.0
            slip_duration = slip_buffer.buffer_size / slip_rate
            self.t_slip = np.linspace(0, slip_duration, slip_buffer.buffer_size)

            self.diff_buffer_size = 500
            self.sensor_diff_buffer = np.zeros((self.diff_buffer_size, NUM_CHANNELS))
            self.t_diff = np.linspace(0, 25, self.diff_buffer_size)
            self.diff_index = 0

            self.timer = QTimer()
            self.timer.timeout.connect(self.update_plot)
            self.timer.start(50)

        def update_plot(self):
            sensor_data = shared_sensor_buffer.get_data()
            for ch in range(NUM_CHANNELS):
                self.sensor_curves[ch].setData(self.t_sensor, sensor_data[:, ch])

            force_data = force_buffer.get_data()
            self.force_curve.setData(self.t_force, force_data)
            if len(force_data) > 0:
                fmin, fmax = float(np.min(force_data)), float(np.max(force_data))
                margin = 0.1 * (fmax - fmin) if fmax != fmin else 1.0
                self.force_plot.setYRange(fmin - margin, fmax + margin)

            slip_data = slip_buffer.get_data()
            if slip_data.shape[0] > 0:
                self.slip_avg_L.setData(self.t_slip, slip_data[:, 1])
                self.slip_avg_R.setData(self.t_slip, slip_data[:, 2])
                self.slip_indicator_curve.setData(self.t_slip, slip_data[:, 0])

            if sensor_data.shape[0] >= 200:
                new_diff = sensor_data[-1, :] - sensor_data[-200, :]
            else:
                new_diff = np.zeros(NUM_CHANNELS)

            self.sensor_diff_buffer[self.diff_index, :] = new_diff
            self.diff_index = (self.diff_index + 1) % self.diff_buffer_size
            diff_ordered = np.roll(self.sensor_diff_buffer, -self.diff_index, axis=0)
            for ch in range(NUM_CHANNELS):
                self.sensor_diff_curves[ch].setData(self.t_diff, diff_ordered[:, ch])

    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())
