#!/usr/bin/env python3
from forte_gripper.dynamixel import *
from forte_gripper.command import SystemHandler, Controller
from forte_gripper.dynamixel.group_bulk_read import GroupBulkRead
from forte_gripper.dynamixel.group_bulk_write import GroupBulkWrite
import yaml
import time
import numpy as np

# (Ensure these constants are defined appropriately)
ADDR_GOAL_POSITION = 116
LEN_GOAL_POSITION = 4
ADDR_GOAL_CURRENT = 102
LEN_GOAL_CURRENT = 2
ADDR_PRESENT_ALL = (
    126  # Present current (2 bytes), velocity (4 bytes), position (4 bytes)
)
LEN_FEEDBACK = 10


def convert_to_signed_16bit(val):
    return val - 0x10000 if val & 0x8000 else val


class Gripper:
    def __init__(self, config_path):
        # ------------------------------ #
        # --- Initialize Handlers ---    #
        # ------------------------------ #
        self.config = yaml.safe_load(open(config_path))
        self.sys_hdlr = SystemHandler(self.config["system"])
        self.ctrl_r = Controller(self.sys_hdlr, self.config["gripper"]["finger_right"])
        self.ctrl_l = Controller(self.sys_hdlr, self.config["gripper"]["finger_left"])

        # Open port
        self.sys_hdlr.open()

        # ------------------------------------------------------- #
        # --- Setup GroupBulkWrite Instances for each parameter ---#
        # ------------------------------------------------------- #
        self.group_bulk_write_position = GroupBulkWrite(
            self.sys_hdlr.portHandler, self.sys_hdlr.packetHandler
        )
        self.group_bulk_write_current = GroupBulkWrite(
            self.sys_hdlr.portHandler, self.sys_hdlr.packetHandler
        )

        # ------------------------------------- #
        # --- Setup GroupBulkRead Instance ---  #
        # ------------------------------------- #
        self.group_bulk_read_state = GroupBulkRead(
            self.sys_hdlr.portHandler, self.sys_hdlr.packetHandler
        )
        for finger in [
            self.config["gripper"]["finger_right"],
            self.config["gripper"]["finger_left"],
        ]:
            motor_id = finger["id"]
            if not self.group_bulk_read_state.addParam(
                motor_id, ADDR_PRESENT_ALL, LEN_FEEDBACK
            ):
                print(f"[ID:{motor_id}] GroupBulkRead addParam failed")
                exit()

    def open_gripper(self):
        cmd_state = {
            "pos": [0.2, 0.2],
            "current": [0.02, 0.02],
        }
        obs_state = self.step(cmd_state)
        return obs_state

    # --- Conversion Functions --- #
    # ------------------------------ #
    def _position_to_raw(self, position):
        return int((position + 1.0) * 2048)

    def _current_to_ma(self, current):
        return int(current * 1000)

    def state2cmd(self, state: dict):
        pos = state["pos"]
        current = state["current"]
        goal_positions = {
            self.config["gripper"]["finger_right"]["id"]: self._position_to_raw(pos[0]),
            self.config["gripper"]["finger_left"]["id"]: self._position_to_raw(-pos[1]),
        }
        current_setpoints = {
            self.config["gripper"]["finger_right"]["id"]: self._current_to_ma(
                -current[0]
            ),
            self.config["gripper"]["finger_left"]["id"]: self._current_to_ma(
                current[1]
            ),
        }
        return goal_positions, current_setpoints

    def send_cmd(self, goal_positions, current_setpoints):
        for dxl_id, goal_value in goal_positions.items():
            # Pack 32-bit goal position into 4 little-endian bytes
            param_goal_position = [
                goal_value & 0xFF,
                (goal_value >> 8) & 0xFF,
                (goal_value >> 16) & 0xFF,
                (goal_value >> 24) & 0xFF,
            ]
            # Pack 16-bit current setpoint into 2 little-endian bytes
            param_goal_current = [
                current_setpoints[dxl_id] & 0xFF,
                (current_setpoints[dxl_id] >> 8) & 0xFF,
            ]

            if not self.group_bulk_write_position.addParam(
                dxl_id, ADDR_GOAL_POSITION, LEN_GOAL_POSITION, param_goal_position
            ):
                print(f"[ID:{dxl_id}] GroupBulkWrite (position) addParam failed")
                exit()
            if not self.group_bulk_write_current.addParam(
                dxl_id, ADDR_GOAL_CURRENT, LEN_GOAL_CURRENT, param_goal_current
            ):
                print(f"[ID:{dxl_id}] GroupBulkWrite (current) addParam failed")
                exit()

        # Transmit both packets
        dxl_comm_result = self.group_bulk_write_position.txPacket()
        if dxl_comm_result != COMM_SUCCESS:
            print(
                "Group Bulk Write (position) failed: "
                + self.sys_hdlr.packetHandler.getTxRxResult(dxl_comm_result)
            )
        dxl_comm_result = self.group_bulk_write_current.txPacket()
        if dxl_comm_result != COMM_SUCCESS:
            print(
                "Group Bulk Write (current) failed: "
                + self.sys_hdlr.packetHandler.getTxRxResult(dxl_comm_result)
            )

        # Clear parameters for next use
        self.group_bulk_write_position.clearParam()
        self.group_bulk_write_current.clearParam()

    def read_state(self):
        dxl_comm_result = self.group_bulk_read_state.txRxPacket()
        if dxl_comm_result != COMM_SUCCESS:
            print(
                "Group Bulk Read error: "
                + self.sys_hdlr.packetHandler.getTxRxResult(dxl_comm_result)
            )
            return None

        state = {}
        for finger in [
            self.config["gripper"]["finger_right"],
            self.config["gripper"]["finger_left"],
        ]:
            motor_id = finger["id"]
            current_raw = self.group_bulk_read_state.getData(
                motor_id, ADDR_PRESENT_ALL, 2
            )
            current_val = convert_to_signed_16bit(current_raw) / 1000.0  # Amperes

            velocity_val = self.group_bulk_read_state.getData(
                motor_id, ADDR_PRESENT_ALL + 2, 4
            )

            position_val = self.group_bulk_read_state.getData(
                motor_id, ADDR_PRESENT_ALL + 6, 4
            )
            position_val = position_val / 2048.0 - 1.0

            state[motor_id] = {
                "current": current_val,
                "velocity": velocity_val,
                "position": position_val,
            }
        return state

    def step(self, cmd_state):
        time_start = time.perf_counter()
        goal_positions, current_setpoints = self.state2cmd(cmd_state)
        self.send_cmd(goal_positions, current_setpoints)
        time_cmd = time.perf_counter()
        obs_state = self.read_state()
        time_read = time.perf_counter()
        # if time_read - time_start > 0.033:
        #     print(f"cmd: {time_cmd - time_start}, read: {time_read - time_cmd}")
        return obs_state

    def enable(self):
        self.ctrl_r.enable()
        self.ctrl_l.enable()

    def disable(self):
        self.ctrl_r.disable()
        self.ctrl_l.disable()

    def shutdown(self):
        self.disable()
        self.sys_hdlr.close()


if __name__ == "__main__":
    time_start = time.perf_counter()
    gripper = Gripper("config/legato.yaml")
    gripper.enable()
    time_init = time.perf_counter()
    time_list = []
    for i in range(1000):
        cmd_state = {
            "pos": [0.2, 0.2],
            "current": [0.02, 0.02],
        }
        obs_state = gripper.step(cmd_state)
        time_list.append(time.perf_counter())
        print(f"[{i}]")

    print("time_stats:")
    print(f"init: {time_init - time_start}")
    print(f"step mean: {np.mean(np.diff(time_list))}")
    print(f"step std: {np.std(np.diff(time_list))}")
    print(f"step min: {np.min(np.diff(time_list))}")
    print(
        f"step max: {np.max(np.diff(time_list))} index: {np.argmax(np.diff(time_list))}"
    )

    gripper.shutdown()
