# FORTE: Tactile Force Estimation & Slip Detection

FORTE is a minimal pip-installable Python package for real-time **force estimation** and **slip detection** with a custom 6-channel analog tactile gripper.

This repository ships two demos:

| Script | What it does |
|---|---|
| `examples/force_slip_vis_realtime.py` | Sensor → SVR force estimate → PSD slip detector → live PyQt visualization. **No actuator required.** |
| `examples/gripper_showcase_vis_realtime.py` | Same pipeline **plus** keyboard-driven FORTE gripper control and (optional) Franka arm motion for the slip-grasp demo. |

---

## Repository layout

```
forte/                    # main package
├── sensing/              # FORTE_sensor + SensorDataBuffer (HDF5 logging)
└── runtime/              # shared ring buffers + force_and_slip pipeline
forte_gripper/            # Dynamixel-based FORTE_gripper control package
configs/
├── sensor/FORTE_sensor.yaml
└── actuator/FORTE_gripper.yaml
models/SVR_ckpt.pkl       # SVR force regressor checkpoint
examples/                 # the two demos above
```

---

## Install

Python 3.9+. From the repo root:

```bash
pip install -e .
```

This installs two importable top-level packages — `forte` and `forte_gripper` — plus all runtime dependencies (`numpy`, `scipy`, `joblib`, `pyserial`, `PyQt5`, `pyqtgraph`, `omegaconf`, `h5py`, `pynput`, `matplotlib`).

### Optional: Franka arm support

The `Alt+Right` keybinding in the showcase demo drives a Franka arm via Deoxys. It is **not required** for the rest of the demo and is imported lazily — the script will print a warning and continue without it.

Install Deoxys separately from [UT-Austin-RPL/deoxys_control](https://github.com/UT-Austin-RPL/deoxys_control).

---

## Hardware

| Device | Default port |
|---|---|
| FORTE 6-channel sensor (USB serial) | `/dev/ttyACM0` @ 2 000 000 baud |
| FORTE gripper (Dynamixel U2D2)      | `/dev/ttyUSB0` @ 4 000 000 baud |

Edit the YAMLs under `configs/` if your ports differ.

### Linux serial permissions

Add your user to `dialout`, then log out and back in:

```bash
sudo usermod -aG dialout $USER
```

### Reduce USB latency (recommended)

```bash
echo 1 | sudo tee /sys/module/usbcore/parameters/usbfs_memory_mb
```

### Set CPUs to performance mode

```bash
for cpu in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
  echo performance | sudo tee "$cpu"
done
```

---

## Running the demos

```bash
# Vis-only: sensor + force + slip + plots
python examples/force_slip_vis_realtime.py

# Vis + gripper (keyboard-driven), optional Franka arm
python examples/gripper_showcase_vis_realtime.py
```

Both scripts accept `--model PATH` to point at a different force model checkpoint (default: `models/SVR_ckpt.pkl`).

### Keyboard bindings (showcase demo)

| Key | Action |
|---|---|
| `Ctrl + Right` | Close gripper until force threshold reached |
| `Alt + Right`  | Start arm motion + slip-reactive grasp (requires Deoxys) |
| `Space`        | Open gripper |
| `CapsLock`     | Close gripper |
| `+` / `-`      | Incremental grip position adjust |
| `1` / `2` / `3`| Mode = FORTE / On-Off / WO_Slip |
| `Esc`          | Quit |

---

## Troubleshooting

### Serial port stuck or returning no data

Re-upload any sketch from the Arduino IDE to free the port, or replug the USB cable.

### Permission denied on `/dev/ttyACM0`

```bash
ls -l /dev/ttyACM0      # expect group `dialout` with rw
sudo usermod -aG dialout $USER && newgrp dialout
```
