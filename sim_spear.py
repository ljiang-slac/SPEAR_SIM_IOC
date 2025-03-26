#!/usr/bin/env python3

import caproto
from caproto import ChannelType
from caproto.server import PVGroup, pvproperty, run
import random
import time
import logging
import os
import smtplib
from email.mime.text import MIMEText
import math

# Configure logging to display INFO level messages
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global constants
INJECT_THRESHOLD = 495.0  # Beam current below this triggers injection (mA)
INJECT_RATE = 0.5         # Injection rate for Beam mode (mA/second)
INJECT_RATE_SEC = 20.0    # Injection rate for Down-to-Inject transition (mA/second)

class SpearSimulatorIOC(PVGroup):
    """
    A Caproto IOC simulating the SPEAR beam current and injection process.
    Simulates beam current decay, injection cycles, and machine states.
    """
    manual_state_set = False  # Tracks if state was manually set
    injecting = False         # Flag to track if injection is in progress
    last_state = 0            # Track the previous state for transition detection
    current_inject_rate = INJECT_RATE  # Dynamic injection rate

    # PV: Average beam current over 1 second
    beam_curr_avg = pvproperty(
        value=500.0,
        name='BeamCurrAvg',
        doc='SPEAR beam current averaged over 1 sec (mA)',
        units='mA',
        precision=2,
        record='ai'
    )

    # PV: Minimum current for frequent fills
    beam_curr_avg_min = pvproperty(
        value=50.0,
        name='BeamCurrAvgMin',
        doc='Minimum current above which frequent fill is possible (mA)',
        units='mA',
        precision=2,
        record='ai'
    )

    # PV: Desired target current for injection
    beam_curr_des = pvproperty(
        value=500.0,
        name='BeamCurrDes',
        doc='Target current for injection (mA)',
        units='mA',
        precision=2,
        record='ai'
    )

    # PV: Machine state (Beam, Inject, AccPhy, Down)
    state = pvproperty(
        name='State',
        doc='SPEAR machine mode',
        dtype=ChannelType.ENUM,
        enum_strings=['Beam', 'Inject', 'AccPhy', 'Down'],
        value=0,
        record='mbbi'
    )

    # PV: Injection state (No Injection, Beamline Wait, Injection)
    inject_state = pvproperty(
        name='InjectState',
        doc='SPEAR Injection status',
        dtype=ChannelType.ENUM,
        enum_strings=['No Injection', 'Beamline Wait', 'Injection'],
        value=0,
        record='mbbi'
    )

    # PV: Alarm status for unexpected state changes
    alarm = pvproperty(
        name='Alarm',
        doc='Alarm status for unexpected state changes',
        dtype=ChannelType.ENUM,
        enum_strings=['OK', 'WARNING', 'ERROR'],
        value=0,
        record='mbbi'
    )

    # New PV: Debug flag to manually control the injecting state
    debug_injecting = pvproperty(
        value=False,
        name='DebugInjecting',
        doc='Force injecting flag for manual control (True/False)',
        dtype=bool
    )

    @beam_curr_avg.startup
    async def beam_curr_avg(self, instance, async_lib):
        """Startup method for beam_curr_avg PV. Runs the main simulation loop."""
        logger.info("Simulation loop started")
        
        # Simulation parameters
        inject_delay = 0.000001  # Duration of Beamline Wait phase (seconds)
        inject_timer = 0.0       # Timer for injection phases
        sim_time = 0.0           # Simulation time for beam decay
        I_0 = 500.0              # Initial/target beam current (mA)
        tau = 31162.0            # Decay time constant (~360s from 500 to 495 mA)
        loop_interval = 0.001    # Loop update interval (seconds)
        inject_duration = (I_0 - INJECT_THRESHOLD) / INJECT_RATE  # Default duration for Beam mode

        logger.info(f"Default inject_duration: {inject_duration} seconds based on INJECT_RATE={INJECT_RATE} mA/s")

        while True:
            # Get current state values
            curr_state = self.state.value
            curr_inject_state = self.inject_state.value
            curr_avg = self.beam_curr_avg.value  # Start with the current PV value

            # Log current simulation status
            logger.info(f"Loop: State={curr_state} ({self.state.enum_strings[curr_state]}), "
                        f"InjectState={curr_inject_state} ({self.inject_state.enum_strings[curr_inject_state]}), "
                        f"BeamCurrAvg={curr_avg}, SimTime={sim_time}, InjectTimer={inject_timer}, Injecting={self.injecting}, "
                        f"InjectRate={self.current_inject_rate}")

            # Increment simulation time
            sim_time += loop_interval

            # Check for Down-to-Inject or Down-to-Beam transition
            if self.last_state == 3 and (curr_state == 1 or curr_state == 0) and self.manual_state_set:
                logger.info("Detected manual transition from Down to Inject/Beam, using INJECT_RATE_SEC")
                self.current_inject_rate = INJECT_RATE_SEC
                inject_duration = (I_0 - curr_avg) / INJECT_RATE_SEC  # Recalculate duration for faster rate
                self.injecting = True
                if curr_state == 0:  # If set to Beam, force Inject mode first
                    await self.state.write(1)
                logger.info(f"Set inject_duration to {inject_duration} seconds based on INJECT_RATE_SEC={INJECT_RATE_SEC} mA/s")

            if curr_state == 0:  # Beam mode: Apply exponential decay and check for injection
                self.injecting = False
                self.current_inject_rate = INJECT_RATE  # Reset to default rate
                inject_duration = (I_0 - INJECT_THRESHOLD) / INJECT_RATE
                # Apply decay
                curr_avg = I_0 * math.exp(-sim_time / tau)
                if curr_avg < INJECT_THRESHOLD and curr_inject_state == 0 and not self.manual_state_set:
                    logger.info(f"Current {curr_avg} mA below {INJECT_THRESHOLD} mA, initiating injection with INJECT_RATE={INJECT_RATE}")
                    self.injecting = True
                    await self.state.write(1)

            elif curr_state == 1:  # Inject mode: Handle injection phases
                if not self.injecting:
                    logger.warning("Inject mode detected without injecting flag; forcing back to Beam mode")
                    await self.inject_state.write(0)
                    await self.state.write(0)
                    self.injecting = False
                    continue

                if curr_inject_state == 0:  # No Injection -> Beamline Wait
                    logger.info("Starting injection: Moving to Beamline Wait")
                    await self.inject_state.write(1)
                    inject_timer = inject_delay
                elif curr_inject_state == 1:  # Beamline Wait phase
                    inject_timer -= loop_interval
                    logger.info(f"Beamline Wait, timer={inject_timer}")
                    if inject_timer <= 0:
                        logger.info("Switching to Injection")
                        await self.inject_state.write(2)
                        inject_timer = inject_duration
                elif curr_inject_state == 2:  # Injection phase
                    inject_timer -= loop_interval
                    logger.info(f"Injection, timer={inject_timer}, using rate={self.current_inject_rate}")
                    if curr_avg < I_0:
                        # Increase current at the current injection rate
                        curr_avg += self.current_inject_rate * loop_interval
                    curr_avg = min(curr_avg, I_0)  # Cap at target current
                    if inject_timer <= 0:
                        logger.info("Injection complete, forcing return to No Injection and Beam mode")
                        await self.inject_state.write(0)
                        await self.state.write(0)
                        self.injecting = False
                        self.manual_state_set = False
                        sim_time = 0.0  # Reset decay timer for a fresh cycle
                        logger.info(f"Returned to Beam mode with BeamCurrAvg={curr_avg}, SimTime={sim_time}")

            elif curr_state == 2:  # AccPhy mode: Slight random decay
                curr_avg -= random.uniform(-0.1, 0.5) * (loop_interval / 1.0)
                curr_avg = max(min_curr, curr_avg)
                if curr_inject_state != 0:
                    logger.info("AccPhy mode: Forcing InjectState to No Injection")
                    await self.inject_state.write(0)
                self.injecting = False

            elif curr_state == 3:  # Down mode: Current drops to 0
                curr_avg = 0.0
                if curr_inject_state != 0:
                    logger.info("Down mode: Forcing InjectState to No Injection")
                    await self.inject_state.write(0)
                self.injecting = False

            # Update the beam current PV with the calculated value
            await self.beam_curr_avg.write(curr_avg)

            # Random transition to Down mode with email alert
            if curr_inject_state == 0 and random.random() < 0.001 * (loop_interval / 1.0) and not self.manual_state_set and curr_state != 3:
                logger.warning("Random state transition to Down (3)")
                await self.state.write(3)
                logger.warning("Setting Alarm to WARNING due to Down state")
                await self.alarm.write(1)
                try:
                    msg = MIMEText("SPEAR Simulator has entered Down mode. Please manually set to Beam mode.")
                    msg['Subject'] = 'SPEAR Simulator Down Alert'
                    msg['From'] = 'spear_simulator@slac.stanford.edu'
                    msg['To'] = 'ljiang@slac.stanford.edu'
                    with smtplib.SMTP('smtp.slac.stanford.edu') as server:
                        server.send_message(msg)
                    logger.info("Email sent to operator (ljiang@slac.stanford.edu) via SLAC SMTP")
                except Exception as e:
                    logger.error(f"Failed to send email via SLAC SMTP: {e}")

            # Clear alarm if not in Down mode and not injecting
            elif curr_state != 3 and not self.injecting:
                await self.alarm.write(0)
                self.manual_state_set = False

            # Update last_state for next iteration
            self.last_state = curr_state

            # Sleep to maintain loop interval
            await async_lib.library.sleep(loop_interval)

    @beam_curr_des.putter
    async def beam_curr_des(self, instance, value):
        """Handle writes to BeamCurrDes PV, clamping value between 0 and 500 mA."""
        logger.info(f"Setting BeamCurrDes: requested={value}, current={instance.value}")
        new_value = max(0.0, min(value, 500.0))
        logger.info(f"BeamCurrDes set to {new_value}")
        return new_value

    @beam_curr_avg_min.putter
    async def beam_curr_avg_min(self, instance, value):
        """Handle writes to BeamCurrAvgMin PV, clamping value between 0 and 100 mA."""
        logger.info(f"Setting BeamCurrAvgMin: requested={value}, current={instance.value}")
        new_value = max(0.0, min(value, 100.0))
        logger.info(f"BeamCurrAvgMin set to {new_value}")
        return new_value

    @state.putter
    async def state(self, instance, value):
        """Handle writes to State PV, validating and converting input to valid enum index."""
        logger.info(f"Setting State: requested={value}, current={instance.value}")
        if isinstance(value, str):
            enum_strings = self.state.enum_strings
            try:
                value = enum_strings.index(value)
                logger.info(f"Converted string '{value}' to index {value}")
            except ValueError:
                logger.info(f"Invalid State string '{value}', reverting to {instance.value}")
                return instance.value
        if isinstance(value, (int, float)) and int(value) in range(4):
            # Allow Inject mode if transitioning from Down, even if not injecting
            if int(value) == 1 and not self.injecting and instance.value != 3:
                logger.info("Manual Inject not allowed unless injection is in progress or transitioning from Down")
                return instance.value
            logger.info(f"State set to {int(value)}")
            if int(value) == 1 and instance.value == 3:  # Manual Down-to-Inject transition
                self.injecting = True  # Set injecting flag to allow injection
                self.current_inject_rate = INJECT_RATE_SEC  # Use faster rate
            if int(value) != 1:  # Reset manual flag for non-Inject states
                self.manual_state_set = True
            await self.alarm.write(0)
            return int(value)
        logger.info(f"Invalid State value {value}, reverting to {instance.value}")
        return instance.value

    @inject_state.putter
    async def inject_state(self, instance, value):
        """Handle writes to InjectState PV, validating and converting input to valid enum index."""
        logger.info(f"Setting InjectState: requested={value}, current={instance.value}")
        if isinstance(value, str):
            enum_strings = self.inject_state.enum_strings
            try:
                value = enum_strings.index(value)
                logger.info(f"Converted string '{value}' to index {value}")
            except ValueError:
                logger.info(f"Invalid InjectState string '{value}', reverting to {instance.value}")
                return instance.value
        if isinstance(value, (int, float)) and int(value) in range(3):
            logger.info(f"InjectState set to {int(value)}")
            return int(value)
        logger.info(f"Invalid InjectState value {value}, reverting to {instance.value}")
        return instance.value

    @debug_injecting.putter
    async def debug_injecting(self, instance, value):
        """Handle writes to DebugInjecting PV to manually set the injecting flag."""
        logger.info(f"Setting DebugInjecting: requested={value}, current={instance.value}")
        self.injecting = bool(value)
        logger.info(f"Injecting flag set to {self.injecting}")
        return value

if __name__ == '__main__':
    """Main entry point: Start the IOC with a custom port and prefix."""
    os.environ['EPICS_CA_SERVER_PORT'] = '6688'
    ioc = SpearSimulatorIOC(prefix='SPEAR_SIM:')
    server_port = os.environ.get('EPICS_CA_SERVER_PORT', '5064')
    logger.info(f"Starting SPEAR Simulator IOC with prefix 'SPEAR_SIM:' on port {server_port}")
    run(ioc.pvdb, interfaces=['0.0.0.0'], log_pv_names=True)
