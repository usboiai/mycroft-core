# Copyright 2019 Mycroft AI Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""Define the enclosure interface for Mark II devices."""
import threading
import time
import typing

from mycroft.client.enclosure.base import Enclosure
from mycroft.enclosure.hardware_enclosure import HardwareEnclosure
from mycroft.messagebus.message import Message
from mycroft.util.hardware_capabilities import EnclosureCapabilities
from mycroft.util.log import LOG

from .activities import (
    AccessPointActivity,
    InternetConnectActivity,
    NetworkConnectActivity,
    SystemClockSyncActivity,
)

SERVICES = ("audio", "skills", "speech")


class TemperatureMonitorThread(threading.Thread):
    def __init__(self, fan_obj, led_obj, pal_obj):
        self.fan_obj = fan_obj
        self.led_obj = led_obj
        self.pal_obj = pal_obj
        self.exit_flag = False
        threading.Thread.__init__(self)

    def run(self):
        LOG.debug("temperature monitor thread started")
        while not self.exit_flag:
            time.sleep(60)
            LOG.debug(f"CPU temperature is {self.fan_obj.get_cpu_temp()}")

            # TODO make this ratiometric
            current_temperature = self.fan_obj.get_cpu_temp()
            if current_temperature < 50.0:
                # anything below 122F we are fine
                self.fan_obj.set_fan_speed(0)
                LOG.debug("Fan turned off")
                self.led_obj._set_led(10, self.pal_obj.BLUE)
                continue

            if 50.0 < current_temperature < 60.0:
                # 122 - 140F we run fan at 25%
                self.fan_obj.set_fan_speed(25)
                LOG.debug("Fan set to 25%")
                self.led_obj._set_led(10, self.pal_obj.MAGENTA)
                continue

            if 60.0 < current_temperature <= 70.0:
                # 140 - 160F we run fan at 50%
                self.fan_obj.set_fan_speed(50)
                LOG.debug("Fan set to 50%")
                self.led_obj._set_led(10, self.pal_obj.BURNT_ORANGE)
                continue

            if current_temperature > 70.0:
                # > 160F we run fan at 100%
                self.fan_obj.set_fan_speed(100)
                LOG.debug("Fan set to 100%")
                self.led_obj._set_led(10, self.pal_obj.RED)
                continue


class PulseLedThread(threading.Thread):
    def __init__(self, led_obj, pal_obj):
        self.led_obj = led_obj
        self.pal_obj = pal_obj
        self.exit_flag = False
        self.color_tup = self.pal_obj.MYCROFT_GREEN
        self.delay = 0.1
        self.brightness = 100
        self.step_size = 5
        self.tmp_leds = []
        threading.Thread.__init__(self)

    def run(self):
        LOG.debug("pulse thread started")
        for x in range(0, 10):
            self.tmp_leds.append(self.color_tup)

        self.led_obj.brightness = self.brightness / 100
        self.led_obj.set_leds(self.tmp_leds)

        while not self.exit_flag:

            if (self.brightness + self.step_size) > 100:
                self.brightness = self.brightness - self.step_size
                self.step_size = self.step_size * -1

            elif (self.brightness + self.step_size) < 0:
                self.brightness = self.brightness - self.step_size
                self.step_size = self.step_size * -1

            else:
                self.brightness += self.step_size

            self.led_obj.brightness = self.brightness / 100
            self.led_obj.set_leds(self.tmp_leds)

            time.sleep(self.delay)

        LOG.debug("pulse thread stopped")
        self.led_obj.brightness = 1.0
        self.led_obj.fill(self.pal_obj.BLACK)


class ChaseLedThread(threading.Thread):
    def __init__(self, led_obj, background_color, foreground_color):
        self.led_obj = led_obj
        self.bkgnd_col = background_color
        self.fgnd_col = foreground_color
        self.exit_flag = False
        self.color_tup = foreground_color
        self.delay = 0.1
        tmp_leds = []
        for indx in range(0, 10):
            tmp_leds.append(self.bkgnd_col)

        self.led_obj.set_leds(tmp_leds)
        threading.Thread.__init__(self)

    def run(self):
        LOG.debug("chase thread started")
        chase_ctr = 0
        while not self.exit_flag:
            chase_ctr += 1
            LOG.debug(f"chase thread {chase_ctr}")
            for x in range(0, 10):
                self.led_obj.set_led(x, self.fgnd_col)
                time.sleep(self.delay)
                self.led_obj.set_led(x, self.bkgnd_col)
            if chase_ctr > 10:
                self.exit_flag = True

        LOG.debug("chase thread stopped")
        self.led_obj.fill((0, 0, 0))


class EnclosureMark2(Enclosure):
    force_system_clock_update = True

    def __init__(self):
        super().__init__()
        self.display_bus_client = None
        self.finished_loading = False
        self.active_screen = "loading"
        self.paused_screen = None
        self.is_pairing = False
        self.active_until_stopped = None
        self.reserved_led = 10
        self.mute_led = 11
        self.chaseLedThread = None
        self.pulseLedThread = None
        self.ready_services = set()
        self.is_paired = False

        self.system_volume = 0.5  # pulse audio master system volume
        # if you want to do anything with the system volume
        # (ala pulseaudio, etc) do it here!
        self.current_volume = 0.5  # hardware/board level volume

        # TODO these need to come from a config value
        self.hardware = HardwareEnclosure("Mark2", "sj201r4")
        self.hardware.client_volume_handler = self.async_volume_handler

        # start the temperature monitor thread
        self.temperatureMonitorThread = TemperatureMonitorThread(
            self.hardware.fan, self.hardware.leds, self.hardware.palette
        )
        self.temperatureMonitorThread.start()

        self.hardware.leds.set_leds(
            [
                self.hardware.palette.BLACK,
                self.hardware.palette.BLACK,
                self.hardware.palette.BLACK,
                self.hardware.palette.BLACK,
                self.hardware.palette.BLACK,
                self.hardware.palette.BLACK,
                self.hardware.palette.BLACK,
                self.hardware.palette.BLACK,
                self.hardware.palette.BLACK,
                self.hardware.palette.BLACK,
            ]
        )

        self.hardware.leds._set_led_with_brightness(
            self.reserved_led, self.hardware.palette.MAGENTA, 0.5
        )

        # set mute led based on reality
        mute_led_color = self.hardware.palette.GREEN
        if self.hardware.switches.SW_MUTE == 1:
            mute_led_color = self.hardware.palette.RED

        self.hardware.leds._set_led_with_brightness(
            self.mute_led, mute_led_color, 1.0
        )

        self.default_caps = EnclosureCapabilities()

    def run(self):
        """Make it so."""
        super().run()
        self._define_event_handlers()
        self._find_initialized_services()

    def async_volume_handler(self, vol):
        """Report changed Mark II hardware volume.

        This does not set the volume, only reports it on the bus.

        Args:
            vol (int or float): the target volume 0-10
                                Note if a float < 1.0 is provided this will be
                                treated as a percentage eg 0.9 = 90% volume.
        """
        if vol >= 1.0:
            vol = vol / 10
        self.current_volume = vol
        LOG.info(f"Async set volume to {self.current_volume}")
        # notify anybody listening on the bus who cares
        self.bus.emit(
            Message(
                "hardware.volume",
                {"volume": self.current_volume},
                context={"source": ["enclosure"]},
            )
        )

    def _define_event_handlers(self):
        """Assigns methods to act upon message bus events."""
        for service in SERVICES:
            self.bus.on(
                f"{service}.initialize.ended", self.handle_service_initialized
            )
        self.bus.on("mycroft.volume.set", self.on_volume_set)
        self.bus.on("mycroft.volume.get", self.on_volume_get)
        self.bus.on("mycroft.volume.duck", self.on_volume_duck)
        self.bus.on("mycroft.volume.unduck", self.on_volume_unduck)
        self.bus.on(
            "recognizer_loop:record_begin", self.handle_start_recording
        )
        self.bus.on("recognizer_loop:record_end", self.handle_stop_recording)
        self.bus.on("recognizer_loop:audio_output_end", self.handle_end_audio)
        self.bus.on(
            "mycroft.speech.recognition.unknown", self.handle_end_audio
        )
        self.bus.on("mycroft.stop.handled", self.handle_end_audio)
        self.bus.on("mycroft.capabilities.get", self.on_capabilities_get)
        self.bus.on("mycroft.started", self.handle_mycroft_started)

        # Request messages to detect network/internet
        self.bus.on("hardware.detect-network", self._handle_detect_network)
        self.bus.on("hardware.detect-internet", self._handle_detect_internet)

        self.bus.on("hardware.network-detected", self._handle_network_detected)
        self.bus.on(
            "hardware.internet-detected", self._handle_internet_connected
        )

        self.bus.on(
            "hardware.awconnect.create-ap", self._handle_create_access_point
        )
        self.bus.on(
            "server-connect.authenticated", self.handle_server_authenticated
        )

    def handle_start_recording(self, message):
        LOG.debug("Gathering speech stuff")
        if self.pulseLedThread is None:
            self.pulseLedThread = PulseLedThread(
                self.hardware.leds, self.hardware.palette
            )
            self.pulseLedThread.start()

    def handle_stop_recording(self, message):
        background_color = self.hardware.palette.BLUE
        foreground_color = self.hardware.palette.BLACK
        LOG.debug("Got spoken stuff")
        if self.pulseLedThread is not None:
            self.pulseLedThread.exit_flag = True
            self.pulseLedThread.join()
            self.pulseLedThread = None
        if self.chaseLedThread is None:
            self.chaseLedThread = ChaseLedThread(
                self.hardware.leds, background_color, foreground_color
            )
            self.chaseLedThread.start()

    def handle_end_audio(self, message):
        LOG.debug("Finished playing audio")
        if self.chaseLedThread is not None:
            self.chaseLedThread.exit_flag = True
            self.chaseLedThread.join()
            self.chaseLedThread = None

    def on_volume_duck(self, message):
        # TODO duck it anyway using set vol
        # LOG.warning("Mark2 volume duck deprecated! use volume set instead.")
        # TODO make configurable 'duck_vol'
        # self.hardware.hardware_volume.set_volume(float(0.1))
        # Use amixer in volume skill to avoid AGC issue.
        pass

    def on_volume_unduck(self, message):
        # TODO duck it anyway using set vol
        # LOG.warning("Mark2 volume unduck deprecated!
        #              use volume set instead.")
        # self.hardware.hardware_volume.set_volume(float(self.current_volume))
        # Use amixer in volume skill to avoid AGC issue.
        pass

    def on_volume_set(self, message):
        self.current_volume = message.data.get("percent", self.current_volume)
        LOG.info(f"Setting volume to {self.current_volume}")
        self.hardware.hardware_volume.set_volume(float(self.current_volume))

        # notify anybody listening on the bus who cares
        self.bus.emit(
            Message(
                "hardware.volume",
                {"volume": self.current_volume},
                context={"source": ["enclosure"]},
            )
        )

    def on_volume_get(self, message):
        self.current_volume = self.hardware.hardware_volume.get_volume()
        if self.current_volume > 1.0:
            self.current_volume = self.current_volume / 10
        LOG.info(f"Current volume {self.current_volume}")
        self.bus.emit(
            message.response(
                data={"percent": self.current_volume, "muted": False}
            )
        )

    def on_capabilities_get(self, message):
        LOG.info("Enclosure capabilities requested")
        self.bus.emit(
            message.response(
                data={
                    "default": self.default_caps.caps,
                    "extra": self.hardware.capabilities,
                    "board_type": self.hardware.board_type,
                    "leds": self.hardware.leds.capabilities,
                    "volume": self.hardware.hardware_volume.capabilities,
                    "switches": self.hardware.switches.capabilities,
                }
            )
        )

    def handle_service_initialized(self, message: Message):
        """Apply a service ready message to the mycroft ready aggregation

        Args:
            message: The event that triggered this method
        """
        service = message.msg_type.split(".")[0]
        LOG.info(f"{service.title()} service has been initialized")
        self._check_all_services_initialized(service)

    def _find_initialized_services(self):
        """Checks for services initialized before message bus connection.

        This handles a race condition where a service could have finished its
        initialization processing before this service is ready to accept
        messages from the core bus.
        """
        for service in SERVICES:
            if service not in self.ready_services:
                response = self.bus.wait_for_response(
                    Message("mycroft.{}.is_ready".format(service))
                )
                if response and response.data["status"]:
                    LOG.info(f"{service.title()} service has been initialized")
                    self._check_all_services_initialized(service)

    def _check_all_services_initialized(self, service: str):
        """Determines if all services have finished initialization.

        Post-initialization processing cannot happen on any service until
        all services have finished their initialization

        Args:
            service: name of the service that reported ready.
        """
        self.ready_services.add(service)
        if all(service in self.ready_services for service in SERVICES):
            LOG.info("All Mycroft services are initialized.")
            self.bus.emit(Message("mycroft.started"))

    def handle_mycroft_started(self, _):
        """Executes logic that depends on all services being initialized."""
        LOG.info("Muting microphone during start up.")
        self.bus.emit(Message("mycroft.mic.mute"))
        self._remove_service_init_handlers()
        self.bus.emit(Message("hardware.detect-network"))

    def _remove_service_init_handlers(self):
        """Deletes the event handlers for services initialized."""
        for service in SERVICES:
            self.bus.remove(
                f"{service}.initialize.ended", self.handle_service_initialized
            )

    def _handle_create_access_point(self, _message=None):
        """Communicate with awconnect container to create Mycroft access point"""
        self._create_access_point()

    def _handle_detect_network(self, _message=None):
        """Request to detect network"""
        self._detect_network()

    def _handle_detect_internet(self, _message=None):
        """Request to detect internet"""
        self._detect_internet()

    def _handle_network_detected(self, _message=None):
        """Detect internet once network is connected"""
        self.bus.emit(Message("hardware.detect-internet"))

    def _handle_internet_connected(self, _message=None):
        """Executes logic that depends on an internet connection.

        The first thing that has to happen after the internet connection is
        established is to synchronize the system clock.  If the system clock
        time is too far away from the actual time, issues like SSL errors on
        API calls can occur.

        The mycroft.internet-ready event indicates that any logic that needs
        to use the internet can now be executed.
        """
        if self.force_system_clock_update:
            self._synchronize_system_clock()
        self.bus.emit(Message("mycroft.internet-ready"))

    def _update_system(self):
        """Skips system update using Admin service.

        The Mark II uses an external vendor, Pantacor, to manage software
        updates.  Mycroft Core does not control when Pantacor updates are
        performed.
        """
        pass

    def _detect_network(self):
        """Check network connectivity over DBus"""
        dbus_config = self.config.get("dbus", {})
        bus_address = dbus_config.get("bus_address")

        network_activity = NetworkConnectActivity(
            "hardware.network-detection",
            self.bus,
            dbus_address=bus_address,
        )
        network_activity.run()

    def _detect_internet(self):
        """Check internet connectivity with network_utils"""
        internet_activity = InternetConnectActivity(
            "hardware.internet-detection",
            self.bus,
        )
        internet_activity.run()

    def _synchronize_system_clock(self):
        """Waits for the system clock to be synchronized with a NTP service."""
        sync_activity = SystemClockSyncActivity(
            "hardware.clock-sync", self.bus
        )
        sync_activity.run()

    def _create_access_point(self):
        """Request access point creation from awconnect"""
        ap_activity = AccessPointActivity("network.access-point", self.bus)
        ap_activity.run_background()

    def handle_server_authenticated(self, _):
        LOG.info("Server authentication successful")
        LOG.info("Activating microphone")
        self.bus.emit(Message("mycroft.mic.unmute"))
        LOG.info("Device is ready for user interactions")
        self.bus.emit(Message("mycroft.ready"))

    def terminate(self):
        self.hardware.leds._set_led(10, (0, 0, 0))  # blank out reserved led
        self.hardware.leds._set_led(11, (0, 0, 0))  # BUG set to real value!
        self.hardware.terminate()