#!/usr/bin/env python3
"""FORTE: real-time force + slip visualization (no gripper, vis-only demo).

Launches four cooperating processes from :mod:`forte.runtime.force_and_slip`:

* sensor data updater (FORTE sensor -> filtered ring buffer)
* SVR force estimator
* PSD-based slip predictor
* PyQt visualizer

Usage::

    python examples/force_slip_vis_realtime.py [--model models/SVR_ckpt.pkl]
"""

import argparse
import time
from multiprocessing import Process
from pathlib import Path

from omegaconf import OmegaConf

from forte import REPO_ROOT
from forte.runtime.force_and_slip import (
    BUFFER_SIZE,
    NUM_CHANNELS,
    force_estimator,
    qt_visualizer,
    sensor_data_updater,
    slip_predictor,
)
from forte.runtime.sys_utils import ForceRingBuffer, SharedRingBuffer


DEFAULT_MODEL = REPO_ROOT / "models" / "SVR_ckpt.pkl"
DEFAULT_CONFIG = REPO_ROOT / "configs" / "sensor" / "FORTE_sensor.yaml"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        type=Path,
        default=DEFAULT_MODEL,
        help="Path to the SVR force-estimation checkpoint (.pkl).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Path to the FORTE sensor YAML config.",
    )
    args = parser.parse_args()

    cfg = OmegaConf.load(args.config)

    shared_sensor_buffer = SharedRingBuffer(BUFFER_SIZE, NUM_CHANNELS, "d")
    force_buffer = ForceRingBuffer(2500, "d")
    slip_buffer = SharedRingBuffer(12500, 3, "d")

    processes = [
        Process(target=sensor_data_updater, args=(cfg.FORTE, shared_sensor_buffer)),
        Process(target=force_estimator, args=(str(args.model), shared_sensor_buffer, force_buffer)),
        Process(target=slip_predictor, args=(shared_sensor_buffer, slip_buffer)),
        Process(target=qt_visualizer, args=(shared_sensor_buffer, force_buffer, slip_buffer)),
    ]
    for p in processes:
        p.start()

    try:
        while True:
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("Interrupted by user.")
    finally:
        for p in processes:
            p.terminate()
        for p in processes:
            p.join(timeout=1)
        shared_sensor_buffer.close()
        force_buffer.close()
        slip_buffer.close()
        print("Demo completed. Resources cleaned up.")


if __name__ == "__main__":
    main()
