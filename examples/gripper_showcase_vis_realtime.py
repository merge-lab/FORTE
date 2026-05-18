#!/usr/bin/env python3
"""FORTE: real-time force + slip visualization with full gripper control.

Same vis/force/slip pipeline as ``force_slip_vis_realtime.py`` plus:

* A FORTE gripper control worker driven by keyboard input (pynput).
* An optional Cartesian-velocity controller for a Franka arm via Deoxys
  (https://github.com/UT-Austin-RPL/deoxys_control). The arm portion is
  guarded by a lazy import so the demo still runs without Deoxys installed.

Keyboard bindings::

    Ctrl+Right   close gripper until force threshold is reached
    Alt+Right    start arm + slip-reactive grasp (Deoxys required)
    Space        open gripper
    CapsLock     close gripper
    +/-          incremental grip pos adjust
    1 / 2 / 3    mode = FORTE / On-Off / WO_Slip
    Esc          quit

Usage::

    python examples/gripper_showcase_vis_realtime.py [--model models/SVR_ckpt.pkl]
"""

import argparse
import time
from multiprocessing import Process, Value
from pathlib import Path

import numpy as np
import pynput
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
from forte_gripper import FORTE_gripper


DEFAULT_MODEL = REPO_ROOT / "models" / "SVR_ckpt.pkl"
DEFAULT_SENSOR_CONFIG = REPO_ROOT / "configs" / "sensor" / "FORTE_sensor.yaml"
DEFAULT_GRIPPER_CONFIG = REPO_ROOT / "configs" / "actuator" / "FORTE_gripper.yaml"


# ---------------------------------------------------------------------- #
# Optional Franka / Deoxys controller (runs in its own process)
# ---------------------------------------------------------------------- #
def cartesian_velocity_control_worker(duration, arm_move_shared, interface_cfg="charmander.yml"):
    """Drive the Franka end-effector upward at a constant velocity.

    Imported lazily so the rest of the demo can run without Deoxys installed.
    """
    try:
        from deoxys import config_root
        from deoxys.franka_interface import FrankaInterface
        from deoxys.utils.config_utils import get_default_controller_config
    except ImportError as exc:
        print(
            "[arm_worker] Deoxys not installed; skipping arm motion. "
            "See https://github.com/UT-Austin-RPL/deoxys_control for setup."
        )
        print(f"[arm_worker] import error: {exc}")
        arm_move_shared.value = False
        return

    robot = FrankaInterface(f"{config_root}/{interface_cfg}", use_visualizer=False)
    time.sleep(1)

    controller_type = "CARTESIAN_VELOCITY"
    controller_cfg = get_default_controller_config(controller_type=controller_type)

    arm_move_shared.value = True
    for _ in range(duration * 20):
        action = [0.0, 0.0, 0.005, 0.0, 0.0, 0.0, -1]
        robot.control(
            controller_type=controller_type,
            action=action,
            controller_cfg=controller_cfg,
        )
    arm_move_shared.value = False


# ---------------------------------------------------------------------- #
# Gripper keyboard control loop
# ---------------------------------------------------------------------- #
def gripper_control_worker(
    sensor_buffer, force_buffer, slip_buffer, arm_move_shared, gripper_config_path
):
    """Keyboard-driven gripper controller. See module docstring for keys."""
    gripper = FORTE_gripper(str(gripper_config_path))
    gripper.enable()

    right_id = gripper.config["gripper"]["finger_right"]["id"]
    left_id = gripper.config["gripper"]["finger_left"]["id"]

    def get_start_pos():
        state = gripper.read_state()
        return float(np.max([abs(state[right_id]["position"]), abs(state[left_id]["position"])]))

    def reaching_object(mode):
        start_pos = get_start_pos()
        cmd_state = {"pos": [start_pos, start_pos], "current": [0.1, 0.1]}
        grasp_limit = 0.0
        num_steps = (start_pos - grasp_limit) / 0.0005
        close_steps = np.linspace(start_pos, grasp_limit, int(num_steps))

        force_baseline = force_buffer.get_data()[-200:].mean()
        print(f"[gripper] force baseline = {force_baseline:.3f}")

        L_touch = R_touch = False
        cmd_pos = start_pos
        for pos in close_steps:
            if mode != "On-Off":
                sensor_data = sensor_buffer.get_data()
                diff = sensor_data[-1, :] - sensor_data[-200, :]
                if max(diff[0:3]) > 0.01:
                    if not L_touch:
                        print("[gripper] left finger touched.")
                    L_touch = True
                if max(diff[3:6]) > 0.01:
                    if not R_touch:
                        print("[gripper] right finger touched.")
                    R_touch = True
                if L_touch and R_touch:
                    print("[gripper] both fingers reached object.")
                    break
                if L_touch or R_touch:
                    force_baseline = force_buffer.get_data()[-20:].mean()
                if force_buffer.get_data()[-1] > force_baseline + 0.15:
                    print("[gripper] reaching force threshold reached.")
                    break
            cmd_state["pos"] = [pos, pos]
            gripper.step(cmd_state)
            cmd_pos = pos
        return force_baseline, cmd_pos

    # Initial open.
    open_pos = 0.25
    state = gripper.read_state()
    start_pos = float(np.max([abs(state[right_id]["position"]), abs(state[left_id]["position"])]))
    cmd_state = {"pos": [open_pos, open_pos], "current": [0.05, 0.05]}
    for pos in np.linspace(start_pos, open_pos, 20):
        cmd_state["pos"] = [pos, pos]
        gripper.step(cmd_state)

    state_box = {"current_pos": open_pos, "mode": "FORTE", "cmd_state": cmd_state}

    def on_press(key):
        cmd_state = state_box["cmd_state"]
        if key == pynput.keyboard.Key.esc:
            print("[gripper] exiting.")
            gripper.shutdown()
            return False

        if key == pynput.keyboard.Key.ctrl_r:
            print("[gripper] closing until force threshold...")
            force_baseline, cmd_pos = reaching_object(state_box["mode"])
            time.sleep(2)
            target_force = force_baseline + 0.25
            if state_box["mode"] != "On-Off":
                count = 0
                while force_buffer.get_data()[-1] < target_force and count < 100:
                    cmd_pos -= 0.00005
                    cmd_state["pos"] = [cmd_pos, cmd_pos]
                    gripper.step(cmd_state)
                    count += 1
            print(
                f"[gripper] FE={force_buffer.get_data()[-1]:.3f} "
                f"baseline={force_baseline:.3f}"
            )
            return True

        if key == pynput.keyboard.Key.alt_r:
            print("[gripper] starting arm + slip detection...")
            duration = 5
            proc = Process(
                target=cartesian_velocity_control_worker,
                args=(duration, arm_move_shared),
            )
            proc.start()
            time.sleep(2)

            t_start = time.perf_counter()
            cmd_pos = get_start_pos()
            cmd_state["pos"] = [cmd_pos, cmd_pos]
            if state_box["mode"] == "FORTE":
                while time.perf_counter() - t_start < duration:
                    slip_latest = slip_buffer.get_data()[-1:]
                    if slip_latest[0][0] == 1 and arm_move_shared.value:
                        print("[gripper] slip detected.")
                        cmd_pos -= 0.006
                        cmd_state["pos"] = [cmd_pos, cmd_pos]
                        gripper.step(cmd_state)
                        time.sleep(0.2)
                    else:
                        time.sleep(0.002)
                print("[gripper] slip detection finished.")
            proc.join()
            return True

        if key == pynput.keyboard.Key.space:
            print("[gripper] opening.")
            open_pos = 0.30
            state_box["current_pos"] = open_pos
            state = gripper.read_state()
            start_pos = float(np.max([abs(state[right_id]["position"]), abs(state[left_id]["position"])]))
            cmd_state = {"pos": [open_pos, open_pos], "current": [0.05, 0.05]}
            for pos in np.linspace(start_pos, open_pos, 2):
                cmd_state["pos"] = [pos, pos]
                gripper.step(cmd_state)
            state_box["cmd_state"] = cmd_state
            return True

        if key == pynput.keyboard.Key.caps_lock:
            print("[gripper] closing.")
            close_pos = 0.45
            state_box["current_pos"] = close_pos
            state = gripper.read_state()
            start_pos = float(np.max([abs(state[right_id]["position"]), abs(state[left_id]["position"])]))
            cmd_state = {"pos": [close_pos, close_pos], "current": [0.05, 0.05]}
            for pos in np.linspace(start_pos, close_pos, 30):
                cmd_state["pos"] = [pos, pos]
                gripper.step(cmd_state)
            state_box["cmd_state"] = cmd_state
            return True

        if hasattr(key, "char") and key.char == "+":
            state_box["current_pos"] += 0.001
            cmd_state = {
                "pos": [state_box["current_pos"], state_box["current_pos"]],
                "current": [0.05, 0.05],
            }
            gripper.step(cmd_state)
            state_box["cmd_state"] = cmd_state
            print(f"[gripper] grip pos -> {state_box['current_pos']:.3f}")
            return True

        if hasattr(key, "char") and key.char == "-":
            state_box["current_pos"] -= 0.001
            cmd_state = {
                "pos": [state_box["current_pos"], state_box["current_pos"]],
                "current": [0.05, 0.05],
            }
            gripper.step(cmd_state)
            state_box["cmd_state"] = cmd_state
            print(f"[gripper] grip pos -> {state_box['current_pos']:.3f}")
            return True

        if hasattr(key, "char") and key.char in {"1", "2", "3"}:
            state_box["mode"] = {"1": "FORTE", "2": "On-Off", "3": "WO_Slip"}[key.char]
            print(f"[gripper] mode = {state_box['mode']}")
            return True

        return True

    try:
        with pynput.keyboard.Listener(on_press=on_press) as listener:
            listener.join()
    except KeyboardInterrupt:
        print("[gripper] interrupted by user.")
    finally:
        gripper.shutdown()
        print("[gripper] shutdown complete.")


# ---------------------------------------------------------------------- #
# Entrypoint
# ---------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--sensor-config", type=Path, default=DEFAULT_SENSOR_CONFIG)
    parser.add_argument("--gripper-config", type=Path, default=DEFAULT_GRIPPER_CONFIG)
    args = parser.parse_args()

    cfg = OmegaConf.load(args.sensor_config)

    shared_sensor_buffer = SharedRingBuffer(BUFFER_SIZE, NUM_CHANNELS, "d")
    force_buffer = ForceRingBuffer(2500, "d")
    slip_buffer = SharedRingBuffer(12500, 3, "d")
    arm_move_shared = Value("b", False)

    processes = {
        "sensor": Process(target=sensor_data_updater, args=(cfg.FORTE, shared_sensor_buffer)),
        "force": Process(
            target=force_estimator,
            args=(str(args.model), shared_sensor_buffer, force_buffer),
        ),
        "slip": Process(target=slip_predictor, args=(shared_sensor_buffer, slip_buffer)),
        "vis": Process(target=qt_visualizer, args=(shared_sensor_buffer, force_buffer, slip_buffer)),
        "gripper": Process(
            target=gripper_control_worker,
            args=(
                shared_sensor_buffer,
                force_buffer,
                slip_buffer,
                arm_move_shared,
                args.gripper_config,
            ),
        ),
    }
    for p in processes.values():
        p.start()

    try:
        processes["gripper"].join()
    except KeyboardInterrupt:
        print("Interrupted by user.")
    finally:
        for p in processes.values():
            p.terminate()
        time.sleep(2)
        for p in processes.values():
            p.kill()
        for p in processes.values():
            p.join(timeout=1)
        shared_sensor_buffer.close()
        force_buffer.close()
        slip_buffer.close()
        print("Demo completed. Resources cleaned up.")


if __name__ == "__main__":
    main()
