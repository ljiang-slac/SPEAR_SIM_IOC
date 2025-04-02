#!/usr/bin/env python3

import os
import time
from ophyd import EpicsSignalRO
import matplotlib.pyplot as plt
import numpy as np

# Set EPICS Channel Access environment variables
os.environ['EPICS_CA_SERVER_PORT'] = '6688'
os.environ['EPICS_CA_ADDR_LIST'] = 'localhost'
os.environ['EPICS_CA_AUTO_ADDR_LIST'] = 'NO'

# Define the EPICS PV for BeamCurrAvg
beam_curr_avg = EpicsSignalRO('SPEAR_SIM:BeamCurrAvg', name='beam_curr_avg', auto_monitor=True)

# Connect to the PV
try:
    print("Connecting to SPEAR_SIM:BeamCurrAvg...")
    beam_curr_avg.wait_for_connection(timeout=5)
    print(f"Connected! Initial value: {beam_curr_avg.get():.2f} mA")
except Exception as e:
    print(f"Failed to connect to PV: {e}")
    exit(1)

# Data storage
data = {'time': [], 'beam_curr_avg': []}

# Set up live plotting
plt.ion()  # Enable interactive mode
fig, ax = plt.subplots(figsize=(10, 6))
line, = ax.plot([], [], 'b-o', label='BeamCurrAvg')
ax.set_xlabel('Time (s)')
ax.set_ylabel('Beam Current (mA)')
ax.set_title('SPEAR Beam Current vs Time (Live)')
ax.grid(True)
ax.legend()
plt.show(block=False)

# Define the scan: monitor BeamCurrAvg for 60 seconds
num_points = 600
dwell_time = 1.0

# Manual scan on main thread
print(f"Starting scan of BeamCurrAvg for {num_points} points at {dwell_time} s intervals...")
start_time = time.time()
for i in range(num_points):
    value = beam_curr_avg.get()
    timestamp = time.time()
    data['time'].append(timestamp)
    data['beam_curr_avg'].append(value)
    print(f"Point {i+1}: beam_curr_avg={value:.2f} mA")  # Manual table-like output
    
    # Update plot
    relative_time = [(t - start_time) for t in data['time']]
    line.set_xdata(relative_time)
    line.set_ydata(data['beam_curr_avg'])
    ax.relim()
    ax.autoscale_view()
    fig.canvas.draw()
    fig.canvas.flush_events()
    time.sleep(dwell_time)

# Final plot adjustments
plt.ioff()
plt.savefig('beam_curr_vs_time_final.png')

# Histogram
fig, ax = plt.subplots(figsize=(10, 6))
ax.hist(data['beam_curr_avg'], bins=20, color='skyblue', edgecolor='black')
ax.set_xlabel('Beam Current (mA)')
ax.set_ylabel('Frequency')
ax.set_title('Distribution of BeamCurrAvg')
ax.grid(True)
plt.savefig('beam_curr_distribution.png')
plt.show()

# Statistics
beam_curr_array = np.array(data['beam_curr_avg'])
print(f"BeamCurrAvg Statistics:")
print(f"  Mean: {np.mean(beam_curr_array):.2f} mA")
print(f"  Std Dev: {np.std(beam_curr_array):.2f} mA")
print(f"  Min: {np.min(beam_curr_array):.2f} mA")
print(f"  Max: {np.max(beam_curr_array):.2f} mA")

# Cleanup
beam_curr_avg.destroy()
print("Disconnected from SPEAR_SIM:BeamCurrAvg")
