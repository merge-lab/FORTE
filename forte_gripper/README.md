# Gripper Control Class

This repository provides a Python-based control class for Dynamixel-based grippers (specifically the XM430 series) with multiple control modes available: velocity, current, and current-based position control. The `Gripper` class abstracts the process of configuring, commanding, and reading the state of the gripper through efficient group bulk read/write operations.

## Features

- **Multiple Control Modes:**  
  - **Velocity Control (mode 1):** Command target velocities.  
  - **Current Control (mode 0):** Command target currents.  
  - **Current-based Position Control (mode 5):** Command a combination of target position and current.

- **Safe Mode Switching:**  
  Switch between control modes by disabling the motors, updating the operating mode register, and re-enabling the motors.

- **Efficient Communication:**  
  Uses group bulk read and write for fast, synchronous communication with the motors.

- **Configurable:**  
  Reads system parameters and control mode settings from a YAML configuration file.

