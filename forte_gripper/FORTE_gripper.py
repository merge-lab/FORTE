#!/usr/bin/env python3
from forte_gripper.dynamixel import *
from forte_gripper.command import SystemHandler, Controller
from forte_gripper.dynamixel.group_bulk_read import GroupBulkRead
from forte_gripper.dynamixel.group_bulk_write import GroupBulkWrite
import yaml
import time
import numpy as np
import pynput

# ------------------------------ #
# --- Constants and Addresses ---#
# ------------------------------ #
ADDR_GOAL_POSITION    = 116
LEN_GOAL_POSITION     = 4

ADDR_GOAL_CURRENT     = 102
LEN_GOAL_CURRENT      = 2

ADDR_GOAL_VELOCITY    = 104  # For velocity mode (4 bytes)
LEN_GOAL_VELOCITY     = 4

ADDR_PRESENT_ALL      = 126  # Present current (2 bytes), velocity (4 bytes), position (4 bytes)
LEN_FEEDBACK          = 10

ADDR_OPERATING_MODE   = 11  # Operating mode register for XM430

# New registers for limits (example addresses – verify with your datasheet)
ADDR_PROFILE_CURRENT  = 38   # 2 bytes (current limit)
ADDR_CW_ANGLE_LIMIT   = 48    # 4 bytes (CW position limit)
ADDR_CCW_ANGLE_LIMIT  = 52    # 4 bytes (CCW position limit)
ADDR_PROFILE_VELOCITY = 44  # 4 bytes (velocity limit)

# Mapping from our internal control mode to operating mode values.
# "impedance" is mapped to "impedance" which sends both position and current commands.
CONTROL_MODE_MAP = {
    "current": 0,
    "velocity": 1,
    "position": 3,
    "impedance": 5,
}

def convert_to_signed_16bit(val):
    return val - 0x10000 if val & 0x8000 else val

def convert_to_signed_32bit(val):
    # from -1024 to 1024
    return val - 0x100000000 if val & 0x80000000 else val

class FORTE_gripper:
    def __init__(self, config_path):
        # ------------------------------ #
        # --- Load Configuration ---     #
        # ------------------------------ #
        self.config = yaml.safe_load(open(config_path))
        
        # ------------------------------ #
        # --- Initialize System Handler ---
        # ------------------------------ #
        self.sys_hdlr = SystemHandler(self.config["system"])
        
        # Initialize per‑finger controllers using individual configurations.
        self.ctrl_r = Controller(self.sys_hdlr, self.config["gripper"]["finger_right"])
        self.ctrl_l = Controller(self.sys_hdlr, self.config["gripper"]["finger_left"])
        
        # Create a dictionary mapping motor IDs to their respective Controller.
        self.ctrl_dict = {
            self.config["gripper"]["finger_right"]["id"]: self.ctrl_r,
            self.config["gripper"]["finger_left"]["id"]: self.ctrl_l
        }

        # ------------------------------------------------------- #
        # --- Setup GroupBulkWrite Instances for each parameter ---
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
        for finger in [self.config["gripper"]["finger_right"], self.config["gripper"]["finger_left"]]:
            motor_id = finger["id"]
            if not self.group_bulk_read_state.addParam(motor_id, ADDR_PRESENT_ALL, LEN_FEEDBACK):
                print(f"[ID:{motor_id}] GroupBulkRead addParam failed")
                exit()


        # Open communication port
        self.sys_hdlr.open()

        
        # --------------------------------------------------------- #
        # --- Set Limits: Torque, Position, and Velocity -------- #
        # --------------------------------------------------------- #
        # Use the right finger's configuration as representative.
        finger_right = self.config["gripper"]["finger_right"]
        
        # Torque limit is given in N*m; convert to current limit in mA using a torque constant (e.g., 1.78 N*m/A).
        torque_limit_Nm = finger_right["torque_limit"]
        current_limit_mA = int((torque_limit_Nm / 1.78 / 2.69) * 1000)
        # current_limit_unit is 2.69 mA for dynamixel XM430-W350-R
        
        pos_min = finger_right["position_min"]   # Normalized minimum
        pos_max = finger_right["position_max"]   # Normalized maximum
        profile_vel = finger_right["profile_velocity"]  # in rad/s, dynamixel XM430-W350-R unit is 0.229 [rev/min] = 0.00478 [rad/s]
        profile_vel_value = int(profile_vel / 0.00478)

        # # --------------------------------------------------- #
        # # --- Set Control Mode (map impedance accordingly) -- #
        # # --------------------------------------------------- #
        # mode = finger_right["control_mode"]
        # if mode == "impedance":
        #     mode = "impedance"
        # self.control_mode = mode
        # self.switch_control_mode(self.control_mode)
        self.control_mode = finger_right["control_mode"]


    # --------------------------------------------------- #
    # --- Conversion Functions -------------------------- #
    # --------------------------------------------------- #
    def _position_to_raw(self, position):
        return int((position + 1.0) * 2048)

    def _current_to_ma(self, current):
        return int(current * 1000)

    def _velocity_to_raw(self, velocity):
        return int(velocity * 1023)

    # --------------------------------------------------- #
    # --- State-to-Command Conversion ------------------- #
    # --------------------------------------------------- #
    def state2cmd(self, state: dict):
        """
        Converts a command state dictionary into raw command data.
        The expected keys depend on the current control mode:
          - In "velocity" mode: state should contain "velocity": [right, left].
          - In "current" mode: state should contain "current": [right, left].
          - In "impedance" mode: state should contain "pos" and "current".
        """
        if self.control_mode == "velocity":
            velocities = state["velocity"]
            goal_velocities = {
                self.config["gripper"]["finger_right"]["id"]: self._velocity_to_raw(velocities[0]),
                self.config["gripper"]["finger_left"]["id"]: self._velocity_to_raw(-velocities[1]),
            }
            return goal_velocities
        elif self.control_mode == "current":
            currents = state["current"]
            current_setpoints = {
                self.config["gripper"]["finger_right"]["id"]: self._current_to_ma(-currents[0]),
                self.config["gripper"]["finger_left"]["id"]: self._current_to_ma(currents[1]),
            }
            return current_setpoints
        elif self.control_mode == "impedance":
            pos = state["pos"]
            current = state["current"]
            goal_positions = {
                self.config["gripper"]["finger_right"]["id"]: self._position_to_raw(pos[0]),
                self.config["gripper"]["finger_left"]["id"]: self._position_to_raw(-pos[1]),
            }
            current_setpoints = {
                self.config["gripper"]["finger_right"]["id"]: self._current_to_ma(-current[0]),
                self.config["gripper"]["finger_left"]["id"]: self._current_to_ma(current[1]),
            }
            return goal_positions, current_setpoints
        else:
            raise ValueError("Unsupported control mode")

    # --------------------------------------------------- #
    # --- Command Sending Function ---------------------- #
    # --------------------------------------------------- #
    def send_cmd(self, cmd_data):
        """
        Sends the prepared command to the Dynamixels based on the control mode.
        """
        if self.control_mode == "velocity":
            # Send goal velocity command
            for dxl_id, vel_value in cmd_data.items():
                param_goal_velocity = [
                    vel_value & 0xFF,
                    (vel_value >> 8) & 0xFF,
                    (vel_value >> 16) & 0xFF,
                    (vel_value >> 24) & 0xFF,
                ]
                # Use the appropriate controller's packetHandler
                ctrl = self.ctrl_dict[dxl_id]
                if not self.group_bulk_write_position.addParam(
                    dxl_id, ADDR_GOAL_VELOCITY, LEN_GOAL_VELOCITY, param_goal_velocity
                ):
                    print(f"[ID:{dxl_id}] GroupBulkWrite (velocity) addParam failed")
                    exit()
            dxl_comm_result = self.group_bulk_write_position.txPacket()
            if dxl_comm_result != COMM_SUCCESS:
                print("Group Bulk Write (velocity) failed: " +
                      self.sys_hdlr.packetHandler.getTxRxResult(dxl_comm_result))
            self.group_bulk_write_position.clearParam()
        elif self.control_mode == "current":
            # Send goal current command
            for dxl_id, current_value in cmd_data.items():
                param_goal_current = [
                    current_value & 0xFF,
                    (current_value >> 8) & 0xFF,
                ]
                ctrl = self.ctrl_dict[dxl_id]
                if not self.group_bulk_write_current.addParam(
                    dxl_id, ADDR_GOAL_CURRENT, LEN_GOAL_CURRENT, param_goal_current
                ):
                    print(f"[ID:{dxl_id}] GroupBulkWrite (current) addParam failed")
                    exit()
            dxl_comm_result = self.group_bulk_write_current.txPacket()
            if dxl_comm_result != COMM_SUCCESS:
                print("Group Bulk Write (current) failed: " +
                      self.sys_hdlr.packetHandler.getTxRxResult(dxl_comm_result))
            self.group_bulk_write_current.clearParam()
        elif self.control_mode == "impedance":
            # Send both position and current commands
            goal_positions, current_setpoints = cmd_data
            for dxl_id, goal_value in goal_positions.items():
                param_goal_position = [
                    goal_value & 0xFF,
                    (goal_value >> 8) & 0xFF,
                    (goal_value >> 16) & 0xFF,
                    (goal_value >> 24) & 0xFF,
                ]
                ctrl = self.ctrl_dict[dxl_id]
                if not self.group_bulk_write_position.addParam(
                    dxl_id, ADDR_GOAL_POSITION, LEN_GOAL_POSITION, param_goal_position
                ):
                    print(f"[ID:{dxl_id}] GroupBulkWrite (position) addParam failed")
                    exit()
            for dxl_id, current_value in current_setpoints.items():
                param_goal_current = [
                    current_value & 0xFF,
                    (current_value >> 8) & 0xFF,
                ]
                ctrl = self.ctrl_dict[dxl_id]
                if not self.group_bulk_write_current.addParam(
                    dxl_id, ADDR_GOAL_CURRENT, LEN_GOAL_CURRENT, param_goal_current
                ):
                    print(f"[ID:{dxl_id}] GroupBulkWrite (current) addParam failed")
                    exit()
            dxl_comm_result = self.group_bulk_write_position.txPacket()
            if dxl_comm_result != COMM_SUCCESS:
                print("Group Bulk Write (position) failed: " +
                      self.sys_hdlr.packetHandler.getTxRxResult(dxl_comm_result))
            dxl_comm_result = self.group_bulk_write_current.txPacket()
            if dxl_comm_result != COMM_SUCCESS:
                print("Group Bulk Write (current) failed: " +
                      self.sys_hdlr.packetHandler.getTxRxResult(dxl_comm_result))
            self.group_bulk_write_position.clearParam()
            self.group_bulk_write_current.clearParam()
        else:
            raise ValueError("Unsupported control mode")

    # --------------------------------------------------- #
    # --- Feedback Reading Function --------------------- #
    # --------------------------------------------------- #
    def read_state(self):
        """
        Reads feedback from the Dynamixels. The returned dictionary always contains:
            - "current": in Amperes (unit: 2.29 MA)
            - "velocity": in radians per second
            - "position": normalized (-1.0 to 1.0)
        """
        dxl_comm_result = self.group_bulk_read_state.txRxPacket()
        if dxl_comm_result != COMM_SUCCESS:
            print("Group Bulk Read error: " +
                  self.sys_hdlr.packetHandler.getTxRxResult(dxl_comm_result))
            return None
        state = {}
        for finger in [self.config["gripper"]["finger_right"], self.config["gripper"]["finger_left"]]:
            motor_id = finger["id"]
            current_raw = self.group_bulk_read_state.getData(motor_id, ADDR_PRESENT_ALL, 2) # 2.29 [mA] = 0.00229 [A]
            current_val = convert_to_signed_16bit(current_raw) / 1000.0
            velocity_val = self.group_bulk_read_state.getData(motor_id, ADDR_PRESENT_ALL + 2, 4) # 0.229 [rev/min] = 0.00478 [rad/s]
            velocity_val = convert_to_signed_32bit(velocity_val)
            velocity_rads = velocity_val * 0.00478
            position_val = self.group_bulk_read_state.getData(motor_id, ADDR_PRESENT_ALL + 6, 4)
            # only keep 4096 values which are the lower 12 bits
            position_val = position_val & 0xFFF
            position_val = position_val / 2048.0 - 1.0 # +-1.0 normalized
            state[motor_id] = {
                "current": current_val,
                "velocity": velocity_rads,
                "position": position_val,
            }
        return state

    # --------------------------------------------------- #
    # --- High-Level Step Function ---------------------- #
    # --------------------------------------------------- #
    def step(self, cmd_state):
        cmd_data = self.state2cmd(cmd_state)
        self.send_cmd(cmd_data)
        return self.read_state()

    # --------------------------------------------------- #
    # --- Control Mode Switching Function --------------- #
    # --------------------------------------------------- #
    def switch_control_mode(self, new_mode: str):
        if new_mode not in CONTROL_MODE_MAP:
            raise ValueError("Unsupported control mode. Choose 'velocity', 'current', or 'impedance'.")
        for finger in [self.config["gripper"]["finger_right"], self.config["gripper"]["finger_left"]]:
            motor_id = finger["id"]
            result = self.ctrl_dict[finger["id"]].switch_control_mode(new_mode)
            if result != True:
                print(f"[ID:{motor_id}] Failed to switch control mode to {new_mode}")
            else:
                print(f"[ID:{motor_id}] Switched control mode to {new_mode} (value: {new_mode})")
        self.control_mode = new_mode

    # --------------------------------------------------- #
    # --- Utility Functions ----------------------------- #
    # --------------------------------------------------- #
    def open_gripper(self):
        # For a default open command, use impedance mode command structure.
        cmd_state = {"pos": [0.2, 0.2], "current": [0.02, 0.02]}
        return self.step(cmd_state)

    def enable(self):
        self.ctrl_r.enable()
        self.ctrl_l.enable()

    def disable(self):
        self.ctrl_r.disable()
        time.sleep(0.1)
        self.ctrl_l.disable()

    def shutdown(self):
        # self.sys_hdlr.close()
        self.group_bulk_read_state.clearParam()
        self.group_bulk_write_position.clearParam()
        self.group_bulk_write_current.clearParam()

        self.disable()
        self.sys_hdlr.close()
        

if __name__ == "__main__":
    time_start = time.perf_counter()
    gripper = FORTE_gripper("configs/actuator/FORTE_gripper.yaml")
    gripper.enable()
    time_list = []
    
    open_pos = 0.25
    cmd_state = {"pos": [open_pos, open_pos], "current": [0.2, 0.2]}
    state = gripper.read_state()
    right_id = gripper.config["gripper"]["finger_right"]["id"]
    left_id = gripper.config["gripper"]["finger_left"]["id"]
    r_pos = state[right_id]["position"]
    l_pos = state[left_id]["position"]
    start_pos = np.max([abs(r_pos), abs(l_pos)])
    open_steps = np.linspace(start_pos, open_pos, 20)
    for pos in open_steps:
        cmd_state["pos"] = [pos, pos]
        state = gripper.step(cmd_state)
    
    # wait for pynput keyboard input
    def on_press(key):
        if key == pynput.keyboard.Key.esc:
            print("Exiting...")
            gripper.shutdown()
            return False
        elif key == pynput.keyboard.Key.space:
            print("Opening gripper...")
            open_pos = 0.48
            open_pos = 0.215
            open_pos = 0.17
            cmd_state = {"pos": [open_pos, open_pos], "current": [0.05, 0.05]}
            state = gripper.read_state()
            right_id = gripper.config["gripper"]["finger_right"]["id"]
            left_id = gripper.config["gripper"]["finger_left"]["id"]
            r_pos = state[right_id]["position"]
            l_pos = state[left_id]["position"]
            start_pos = np.max([abs(r_pos), abs(l_pos)])
            open_steps = np.linspace(start_pos, open_pos, 2)
            for pos in open_steps:
                cmd_state["pos"] = [pos, pos]
                state = gripper.step(cmd_state)
            return True
        elif key == pynput.keyboard.Key.caps_lock:
            print("Closing gripper...")
            close_pos = 0.156
            # close_pos = 0.154
            # close_pos = 0.152 # o ball
            # close_pos = 0.075 # raspberry
            close_pos = 0.140 # CUPCAKE
            # close_pos = 0.120
            close_pos = 0.15

            #
            cmd_state = {"pos": [close_pos, close_pos], "current": [0.1, 0.1]}
            state = gripper.read_state()
            right_id = gripper.config["gripper"]["finger_right"]["id"]
            left_id = gripper.config["gripper"]["finger_left"]["id"]
            r_pos = state[right_id]["position"]
            l_pos = state[left_id]["position"]
            start_pos = np.max([abs(r_pos), abs(l_pos)])
            close_steps = np.linspace(start_pos, close_pos, 30)
            for pos in close_steps:
                cmd_state["pos"] = [pos, pos]
                state = gripper.step(cmd_state)
            return True
        elif key == pynput.keyboard.Key.alt_r:
            print("Closing gripper...")
            close_pos = 0.156
            # close_pos = 0.154
            # close_pos = 0.152 # o ball
            # close_pos = 0.075 # raspberry
            close_pos = 0.140 # CUPCAKE
            # close_pos = 0.120
            close_pos = 0.09

            #
            cmd_state = {"pos": [close_pos, close_pos], "current": [0.15, 0.15]}
            state = gripper.read_state()
            right_id = gripper.config["gripper"]["finger_right"]["id"]
            left_id = gripper.config["gripper"]["finger_left"]["id"]
            r_pos = state[right_id]["position"]
            l_pos = state[left_id]["position"]
            start_pos = np.max([abs(r_pos), abs(l_pos)])
            close_steps = np.linspace(start_pos, close_pos, 30)
            for pos in close_steps:
                cmd_state["pos"] = [pos, pos]
                state = gripper.step(cmd_state)
            return True
    
    with pynput.keyboard.Listener(on_press=on_press) as listener:
        while listener.running:
            pass
        listener.join()
        exit(0)
    

    gripper.switch_control_mode("current")
    import threading

    def command_loop():
        counter = 0
        while not stop_event.is_set():
            # current = np.sin(counter) * 0.05 + 0.05
            current = + 0.05
            counter += 0.005 * np.pi
            # 40 step cycle
            cmd_state = {"current": [current, current]}
            # cmd_state = {"current": [0.02, 0.02]}
            obs_state = gripper.step(cmd_state)
            time.sleep(0.01)

    stop_event = threading.Event()
    cmd_thread = threading.Thread(target=command_loop)
    cmd_thread.start()

    try:
        while cmd_thread.is_alive():
            cmd_thread.join(timeout=0.1)
    except KeyboardInterrupt:
        stop_event.set()
        cmd_thread.join()
    finally:
        time.sleep(1)
        gripper.shutdown()
        print("Gripper shutdown")

    # finally:
    #     gripper.shutdown()
