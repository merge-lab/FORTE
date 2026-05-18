#!/usr/bin/python

"""
Title:
        Mobile Platform Command Functions

Description
        -generate a control input command from desired state
        -set and change a motor controller's state
        -set and publish a control input command
        -request and read state data

* Copyrighted by Mingyo Seo
* Created on October 13 2018
"""

import math
import yaml
import threading
from forte_gripper.dynamixel import *

# XM430:
#   counter_per_range: 4096
#   range_deg: 360.0
#   gear_ratio: 353.5
#   rpm_per_val: 0.229

class MessageHandler:
    VAL_ENABLE = 1
    VAL_DISABLE = 0

    CTLR_POSITION = 3  # velocity control
    CTLR_VELOCITY = 1  # position control
    CTLR_CURRENT = 0  # current control
    CTLR_IMPEDANCE = 5  # impedance control

    def __init__(self, protocol_ver, series):
        # with open("config/dynamixel.yaml", "r") as f:
        #     try:
        #         act_config = yaml.safe_load(f)[series]
        #     except yaml.YAMLError as exc:
        #         print(exc)

        counter_per_range = 4096
        range_deg = 360.0
        gear_ratio = 353.5
        rpm_per_val = 0.229

        # self.GEAR_RATIO = act_config["gear_ratio"]
        # self.RANGE_DEG = act_config["range_deg"]
        # self.COUNT_PER_RANGE = act_config["counter_per_range"]
        # self.RPM_PER_VAL = act_config["rpm_per_val"]

        self.GEAR_RATIO = gear_ratio
        self.RANGE_DEG = range_deg
        self.COUNT_PER_RANGE = counter_per_range
        self.RPM_PER_VAL = rpm_per_val
    

        if protocol_ver == 2:
            self.ADR_TORQUE_ENABLE = 64
            self.ADR_LED = 65
            self.ADR_OPERATING_MODE = 11
            self.ADR_PROF_VELOCITY = 112
            self.ADR_PROF_ACCELERATION = 108
            self.ADR_GOAL_CURRENT = 102
            self.ADR_GOAL_VELOCITY = 104
            self.ADR_GOAL_POSITION = 116
            self.ADR_PRESENT_ALL = 126
            self.ADR_PRESENT_POSITION = 132
            self.ADR_PRESENT_VELOCITY = 128
            self.ADR_PRESENT_CURRENT = 126
            self.ADR_VEL_I_GAIN = 76
            self.ADR_VEL_P_GAIN = 78

        elif protocol_ver == 1:
            self.ADR_TORQUE_ENABLE = 24
            self.ADR_LED = 25
            self.ADR_PROF_VELOCITY = 32
            self.ADR_GOAL_POSITION = 30
            self.ADR_PRESENT_POSITION = 36
            self.ADR_PRESENT_VELOCITY = 38
            self.ADR_PRESENT_CURRENT = 40

    def cur2cnt(self, cur):
        """
        convert desired motor current [A] to control command values [mA]
        arguments:
                        float [motor current in A]
        returns:
                        float [motor current in mA]
        """

        val = int(1000 * cur)
        return val

    def cnt2cur(self, cnt):
        """
        convert received values [mA] to current motor current [A]
        arguments:
                        float [motor current in mA]
        returns:
                        float [motor current in A]
        """

        val = 1.0e-3 * cnt
        return val

    def vel2cnt(self, vel):
        """
        convert desired angular velocity [rad/sec] to control command values [count]
        arguments:
                        float [angular velocity in rad/sec]
        returns:
                        float [anglular velocity in count]
        """

        val = int(30 * vel / (self.RPM_PER_VAL * math.pi))
        return val

    def cnt2vel(self, cnt):
        """
        convert received values [count] to current angular velocity [rad/sec]
        arguments:
                        float [anglular velocity in count]
        returns:
                        float [angular velocity in rad/sec]
        """

        val = self.RPM_PER_VAL * math.pi * cnt / 30.0
        return val

    def pos2cnt(self, pos):
        """
        convert desired angular position [rad] to control command values [count]
        arguments:
                        float [angular position in rad]
        returns:
                        float [anglular position in count]
        """

        val = (
            self.COUNT_PER_RANGE * 180 * pos / (self.RANGE_DEG * math.pi)
            + self.COUNT_PER_RANGE / 2
        )
        return int(val)

    def cnt2pos(self, cnt):
        """
        convert received values [count] to current angular position [rad]
        arguments:
                        float [anglular position in count]
        returns:
                        float [angular position in rad]
        """

        val = (
            (self.RANGE_DEG * math.pi)
            * (cnt - self.COUNT_PER_RANGE / 2)
            / (180 * self.COUNT_PER_RANGE)
        )
        return val


class Controller:
    """
    set a controller's configuration and execute commands
    """

    def __init__(self, sys_handler, config):
        """
        configure the Dynamixel's ID, protocols and control mode
        arguments:
                int [target ID], bool [protocol version], bool [control mode]
        """

        self.id = config["id"]
        self.ctrl_mode = config["control_mode"]
        self.fdb_mode = config["feedback_mode"]
        self.portHandler = sys_handler.portHandler
        self.packetHandler = sys_handler.packetHandler
        self.portLock = sys_handler.portLock
        self.sys_handler = sys_handler
        self.msgHandler = MessageHandler(
            protocol_ver=sys_handler.protocol_ver, series=config["series"]
        )
        self.config = config
        self._logging_error = True

        self.activated = False

        self._init_mode(config)

    def _init_mode(self, config):
        self.stateRef = 0.0
        self.stateFdb = 0.0
        if self.ctrl_mode == "position":
            self.valMax = self._state2cnt(config["position_max"], mode="position")
            self.valMin = self._state2cnt(config["position_min"], mode="position")
            self.valProfVel = self._state2cnt(
                config["profile_velocity"], mode="velocity"
            )
        elif self.ctrl_mode == "velocity":
            self.valMax = self._state2cnt(config["velocity_limit"], mode="velocity")
            self.valMin = self._state2cnt(-config["velocity_limit"], mode="velocity")
        elif self.ctrl_mode == "current":
            self.valMax = self._state2cnt(config["torque_limit"], mode="current")
            self.valMin = self._state2cnt(-config["torque_limit"], mode="current")
        elif self.ctrl_mode == "impedance":
            self.valMax = [
                self._state2cnt(config["position_max"], mode="position"),
                self._state2cnt(config["torque_limit"], mode="current"),
            ]
            self.valMin = [
                self._state2cnt(config["position_min"], mode="position"),
                self._state2cnt(-config["torque_limit"], mode="current"),
            ]
            self.valProfVel = self._state2cnt(
                config["profile_velocity"], mode="velocity"
            )
            self.stateRef = [0.0, 0.0]

        self.valRef = self._state2cnt(self.stateRef, mode=self.ctrl_mode)
        self.valFdb = 0

        def _set_val(fcn, val):
            return self.packetHandler.write1ByteTxRx(
                self.portHandler, self.id, fcn, val
            )
        
        def _write2ByteTxRx(fcn, val):
            return self.packetHandler.write2ByteTxRx(
                self.portHandler, self.id, fcn, val
            )

        if self.sys_handler.protocol_ver == self.sys_handler.PROTOCOL_VER_1:
            sys.stdout.write("\rSelected Protocol 1.0\r\n")

            assert self.ctrl_mode == self.CTLR_POSITION, (
                "Only Position Control is supported in Protocol 1.0"
            )

            def _control_val(fcn, val):
                return self.packetHandler.write2ByteTxRx(
                    self.portHandler, self.id, fcn, val
                )

            def _request_val(fcn):
                return self.packetHandler.read2ByteTxRx(self.portHandler, self.id, fcn)

        elif self.sys_handler.protocol_ver == self.sys_handler.PROTOCOL_VER_2:
            # sys.stdout.write("\rSelected Protocol 2.0\r\n")

            if self.ctrl_mode == "current":

                def _control_val(fcn, val):
                    return self.packetHandler.write2ByteTxRx(
                        self.portHandler, self.id, fcn, val
                    )
            elif self.ctrl_mode == "impedance":

                def _control_val(fcn, val):
                    if type(fcn) == list and type(val) == list:
                        pos_results = self.packetHandler.write4ByteTxRx(
                            self.portHandler, self.id, fcn[0], val[0]
                        )
                        cur_results = self.packetHandler.write2ByteTxRx(
                            self.portHandler, self.id, fcn[1], val[1]
                        )
                        return pos_results[0] or cur_results[0], pos_results[1]
                    else:
                        return self.packetHandler.write4ByteTxRx(
                            self.portHandler, self.id, fcn, val
                        )
            else:

                def _control_val(fcn, val):
                    return self.packetHandler.write4ByteTxRx(
                        self.portHandler, self.id, fcn, val
                    )

            if self.fdb_mode == "current":

                def _request_val(fcn):
                    return self.packetHandler.read2ByteTxRx(
                        self.portHandler, self.id, fcn
                    )
            elif self.fdb_mode in ["position", "velocity"]:

                def _request_val(fcn):
                    return self.packetHandler.read4ByteTxRx(
                        self.portHandler, self.id, fcn
                    )
            else:

                def _request_val(fcn):
                    return self.packetHandler.readTxRx(
                        self.portHandler, self.id, fcn, 10
                    )

        self._set_val = _set_val
        self._write2ByteTxRx = _write2ByteTxRx
        self._control_val = _control_val
        self._request_val = _request_val

    def _comm_result(self, comm_result, error):
        if comm_result != COMM_SUCCESS:
            if self._logging_error:
                sys.stdout.write(
                    "\r%s\r\n" % self.packetHandler.getTxRxResult(comm_result)
                )
            return False

        elif error != 0:
            if self._logging_error:
                sys.stdout.write(
                    "\r%s\r\n" % self.packetHandler.getRxPacketError(error)
                )
            return False

        return True

    def _state2cnt(self, state, mode="position"):
        if mode == "position":
            return self.msgHandler.pos2cnt(state)
        elif mode == "velocity":
            return self.msgHandler.vel2cnt(state)
        elif mode == "current":
            return self.msgHandler.cur2cnt(state)
        elif mode == "impedance":
            return [
                self.msgHandler.pos2cnt(state[0]),
                self.msgHandler.cur2cnt(state[1]),
            ]

    def _cnt2state(self, cnt, mode="position"):
        if mode == "position":
            return self.msgHandler.cnt2pos(cnt)
        elif mode == "velocity":
            return self.msgHandler.cnt2vel(cnt)
        elif mode == "current":
            return self.msgHandler.cnt2cur(cnt)
        elif mode == "impedance":
            return [self.msgHandler.cnt2pos(cnt[0]), self.msgHandler.cnt2cur(cnt[1])]

    def _msgSet(self, fcn, val):
        self.portLock.acquire()
        comm_result, error = self._set_val(fcn, val)
        self.portLock.release()

        return self._comm_result(comm_result, error)

    def _msgControl(self, fcn, val):
        self.portLock.acquire()
        comm_result, error = self._control_val(fcn, val)
        self.portLock.release()

        return self._comm_result(comm_result, error)

    def _msgRequest(self, fcn):
        self.portLock.acquire()
        results = self._request_val(fcn)
        self.valFdb, comm_result, error = results
        self.portLock.release()
        return self._comm_result(comm_result, error)

    def enable(self):
        """
        enable the Dynamixel
        returns:
                bool [success]
        """

        # Set Operating Mode
        if self.ctrl_mode == "position":
            if not self._msgSet(
                self.msgHandler.ADR_OPERATING_MODE, self.msgHandler.CTLR_POSITION
            ):
                return False
        elif self.ctrl_mode == "impedance":
            if not self._msgSet(
                self.msgHandler.ADR_OPERATING_MODE, self.msgHandler.CTLR_IMPEDANCE
            ):
                return False
        elif self.ctrl_mode == "velocity":
            if not self._msgSet(
                self.msgHandler.ADR_OPERATING_MODE, self.msgHandler.CTLR_VELOCITY
            ):
                return False
        elif self.ctrl_mode == "current":
            if not self._msgSet(
                self.msgHandler.ADR_OPERATING_MODE, self.msgHandler.CTLR_CURRENT
            ):
                return False

        sys.stdout.write("\rSelected Mode: {}\r\n".format(self.ctrl_mode))

        # Set Profile Velocity
        if self.ctrl_mode in ["position", "impedance"]:
            if not self._msgControl(
                self.msgHandler.ADR_PROF_VELOCITY,
                self.msgHandler.vel2cnt(self.valProfVel),
            ):
                return False
            sys.stdout.write("\rProfile velocity: {}\r\n".format(self.valProfVel))

        # Set Profile Acceleration
        # Trapozoidal profile
        if self.ctrl_mode in ["position", "impedance"]:
            if not self._msgControl(
                self.msgHandler.ADR_PROF_ACCELERATION, 3
            ):
                return False
            sys.stdout.write("\rProfile acceleration: {}\r\n".format(2))
        elif self.ctrl_mode == "velocity":
            if not self._msgControl(
                self.msgHandler.ADR_PROF_ACCELERATION, 50
            ):
                return False
            sys.stdout.write("\rProfile acceleration: {}\r\n".format(2))
            
        # Set control gain for velocity control
        if self.ctrl_mode == "velocity":
            if not self._write2ByteTxRx(
                self.msgHandler.ADR_VEL_I_GAIN, 0
            ):
                return False
            sys.stdout.write("\rVelocity I gain: {}\r\n".format(0))
            if not self._write2ByteTxRx(
                self.msgHandler.ADR_VEL_P_GAIN, 7
            ):
                return False

        # Enable Dynamixel Torque
        if not self._msgSet(
            self.msgHandler.ADR_TORQUE_ENABLE, self.msgHandler.VAL_ENABLE
        ):
            return False
        # Turn on LED
        if not self._msgSet(self.msgHandler.ADR_LED, self.msgHandler.VAL_ENABLE):
            return False

        self.activated = True
        sys.stdout.write("\rDynamixel %d has been successfully enabled\r\n" % self.id)
        return True

    def switch_control_mode(self, mode):
        """
        switch the Dynamixel's control mode
        arguments:
                string [control mode]
        returns:
                bool [success]
        """
        self.disable()
        if mode in ["position", "velocity", "current", "impedance"]:
            self.ctrl_mode = mode
        else:
            sys.stdout.write("\rInvalid control mode\r\n")
            return False
        self._init_mode(self.config)
        self.enable()
        return True

    def disable(self):
        """
        disable the Dynamixel
        returns:
                bool [success]
        """

        # Disable Dynamixel Torque
        if not self._msgSet(
            self.msgHandler.ADR_TORQUE_ENABLE, self.msgHandler.VAL_DISABLE
        ):
            return False

        # Turn off LED
        if not self._msgSet(self.msgHandler.ADR_LED, self.msgHandler.VAL_DISABLE):
            return False

        self.activated = False
        sys.stdout.write("\rDynamixel %d has been successfully disabled\r\n" % self.id)

        return True

    def update(self, input_value):
        """
        set the Dynamixel's control setpoint in pulse with the given trajectory in radian
        arguments:
                float [trajectory in radian]
        returns:
                bool [success]
        """

        self.stateRef = input_value
        self.valRef = self._state2cnt(input_value, mode=self.ctrl_mode)

        return True

    def val_clip(self):
        """
        clip the control setpoint in pulse
        """

        val_cmd = max(min(self.valRef, self.valMax), self.valMin)

        return val_cmd

    def control(self):
        """
        control the motors with respect to the setpoint in pulse
        returns:
                bool [success]
        """

        # val_cmd = max(min(self.valRef, self.valMax), self.valMin)

        if self.ctrl_mode == "position":
            adr_cmd = self.msgHandler.ADR_GOAL_POSITION
        elif self.ctrl_mode == "velocity":
            adr_cmd = self.msgHandler.ADR_GOAL_VELOCITY
        elif self.ctrl_mode == "current":
            adr_cmd = self.msgHandler.ADR_GOAL_CURRENT
        elif self.ctrl_mode == "impedance":
            adr_cmd = [
                self.msgHandler.ADR_GOAL_POSITION,
                self.msgHandler.ADR_GOAL_CURRENT,
            ]
        else:
            return False

        val_cmd = self.val_clip()

        # Write goal position
        return self._msgControl(adr_cmd, val_cmd)

    def request(self):
        """
        request and receive the Dynamixel's state data in pulse
        returns:
                bool [success]
        """

        if self.fdb_mode == "position":
            adr_cmd = self.msgHandler.ADR_PRESENT_POSITION
        elif self.fdb_mode == "velocity":
            adr_cmd = self.msgHandler.ADR_PRESENT_VELOCITY
        elif self.fdb_mode == "current":
            adr_cmd = self.msgHandler.ADR_PRESENT_CURRENT
        else:
            adr_cmd = self.msgHandler.ADR_PRESENT_ALL

        # Read present position
        if self._msgRequest(adr_cmd):
            if self.fdb_mode == "all":
                valFdbCur = int.from_bytes(self.valFdb[0:2], byteorder="big")
                valFdbVel = int.from_bytes(self.valFdb[2:6], byteorder="big")
                valFdbPos = int.from_bytes(self.valFdb[6:10], byteorder="big")
                if valFdbCur & 0x8000:
                    valFdbCur -= 0x10000
                if valFdbVel & 0x8000:
                    valFdbVel -= 0x10000
                if valFdbPos & 0x80000000:
                    valFdbPos -= 0x100000000
                self.stateFdb = [
                    self._cnt2state(valFdbCur, mode="current"),
                    self._cnt2state(valFdbVel, mode="velocity"),
                    self._cnt2state(valFdbPos, mode="position"),
                ]
            else:
                if self.fdb_mode == "current" and (self.valFdb & 0x8000):
                    self.valFdb -= 0x10000
                elif self.valFdb & 0x80000000:
                    self.valFdb -= 0x100000000
                self.stateFdb = self._cnt2state(self.valFdb, mode=self.fdb_mode)
            return True
        else:
            return False


class SystemHandler:
    PROTOCOL_VER_1 = 1
    PROTOCOL_VER_2 = 2

    def __init__(self, config):
        self.port_name = config["port_name"]
        self.baudrate = config["baudrate"]
        self.protocol_ver = config["protocol_ver"]

        self.portHandler = PortHandler(self.port_name)
        self.packetHandler = PacketHandler(self.protocol_ver)

        self.portLock = threading.Lock()

    def open(self):
        # Open port
        if self.portHandler.openPort():
            sys.stdout.write("\rSucceeded to open the port\r\n")
        else:
            sys.stdout.write("\rFailed to open the port\r\n")
            quit()

        # Set port baudrate
        if self.portHandler.setBaudRate(self.baudrate):
            sys.stdout.write("\rSucceeded to change the baudrate\r\n")
        else:
            sys.stdout.write("\rFailed to change the baudrate\r\n")
            quit()

    def close(self):
        self.portLock.acquire()

        if self.portHandler.clearPort():
            sys.stdout.write("\rSucceeded to clear the port\r\n")
        self.portLock.release()

        self.portHandler.closePort()
        sys.stdout.write("\rSucceeded to close the port\r\n")
