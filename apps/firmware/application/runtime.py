"""USB diagnostic runtime. Disconnected sensor peripherals are never started."""

import gc
import sys
import time
import machine
import network
try:
    import uselect as select
except ImportError:
    import select
try:
    import ubinascii as binascii
except ImportError:
    import binascii
import os

from components.board_profile import DEBUG_OUTPUTS, PinRegistry
from components.device_config import ConfigStore
from components.debug_service import DebugService
from components.time_sync import time_sync, get_local_time
from components.socket_test import socket_test


MAINTENANCE_INTERVAL_MS = 600000


class DiagnosticRuntime:
    def __init__(self):
        self.config = ConfigStore()
        self.wlan = network.WLAN(network.STA_IF)
        # No Pin.IN/Pin.OUT initialization here: 36 and 39 are input-only pads.
        inputs = {gpio: machine.Pin(gpio) for gpio in (36, 39)}
        self.pins = PinRegistry(inputs, DEBUG_OUTPUTS)
        device_id = binascii.hexlify(machine.unique_id()).decode()
        boot_id = binascii.hexlify(os.urandom(8)).decode()
        self.service = DebugService(sys.stdout.write, device_id, boot_id,
                                    self.status, self.pins, self.config,
                                    self.apply_config, self.schedule_restart, clock=time)
        self.poller = select.poll()
        self.poller.register(sys.stdin, select.POLLIN)
        self.display = None
        self.wifi_started = None
        self.wifi_timeout = 20000
        self.wifi_values = None
        self.network_state = 'idle'
        self.trial_id = None
        self.restart_at = None
        self.maintenance_at = 0
        self.maintenance_phase = 'ntp'
        self.maintenance_busy = None
        self.maintenance_revision = self.config.revision
        self.ntp_synced = False
        self.tcp_ok = None
        self.tcp_ms = None
        self.last_clock = -1000
        self.service.hello()

    def status(self):
        try:
            connected, status = self.wlan.isconnected(), self.wlan.status()
        except OSError:
            connected, status = False, -1
        wifi = {'connected': connected, 'status': status,
                'ssid': self.config.values.get('SSID', ''), 'phase': self.network_state}
        if connected:
            try:
                wifi['ip'] = self.wlan.ifconfig()[0]
                wifi['rssi'] = self.wlan.status('rssi')
            except OSError:
                pass
        return {'uptimeMs': self.service.now(), 'memFree': gc.mem_free(),
                'wifi': wifi, 'sensorEnabled': False,
                'maintenancePaused': self.service.enabled,
                'maintenance': {'busy': self.maintenance_busy, 'clockSynced': self.ntp_synced,
                                'tcpOk': self.tcp_ok, 'tcpMs': self.tcp_ms}}

    def show(self, text):
        if self.display:
            try:
                self.display.screen_display(text)
            except OSError:
                self.service.log('error', 'OLED update failed')

    def start_wifi(self, values, timeout_ms=20000):
        self.wifi_timeout = timeout_ms
        self.wifi_started = self.service.now()
        self.network_state = 'disconnecting'
        self.wifi_values = dict(values)
        self.show('Connecting WiFi\nPlease wait...')
        ssid = values.get('SSID', '')
        if not ssid:
            self.network_failure('SSID missing')
            return
        try:
            self.wlan.active(True)
            self.wlan.config(reconnects=0)
            self.wlan.disconnect()
        except OSError:
            self.network_failure('Connect failed')

    def apply_config(self, command_id, candidate):
        self.trial_id = command_id
        self.service.log('info', 'Testing staged WiFi configuration; previous settings remain saved')
        self.start_wifi(candidate, 60000)

    def schedule_restart(self):
        # Allow the accepted ACK to drain before resetting.
        self.restart_at = self.service.now() + 250

    def network_failure(self, reason):
        self.wifi_started = None
        try:
            self.wlan.disconnect()
        except OSError:
            pass
        self.network_state = 'failed'
        self.show('WiFi failed\n' + reason + '\nEdit .env\nRestart to retry')
        self.service.log('warn', 'WiFi failed: ' + reason)
        if self.trial_id:
            command_id, self.trial_id = self.trial_id, None
            self.config.rollback()
            self.service.acknowledge(command_id, False,
                                     {'stage': 'rolled-back', 'revision': self.config.revision},
                                     'New WiFi configuration failed; previous configuration restored')
            self.service.log('info', 'Restoring previous WiFi configuration')
            self.start_wifi(self.config.values)

    def network_tick(self):
        connected = self.wlan.isconnected()
        if self.wifi_started is not None:
            # WLAN.disconnect() is asynchronous. An old association cannot
            # confirm a trial: first observe disconnected, then issue connect,
            # and only a later iteration may accept the candidate association.
            if self.network_state == 'disconnecting':
                if self.service.now() - self.wifi_started >= self.wifi_timeout:
                    self.network_failure('Disconnect timeout' if connected else 'Connect timeout')
                elif not connected:
                    self.wlan.connect(self.wifi_values.get('SSID', ''), self.wifi_values.get('PASSWORD', ''))
                    self.network_state = 'connecting'
                return
            if connected:
                self.wifi_started = None
                self.network_state = 'connected'
                self.wlan.config(reconnects=-1)
                if self.trial_id:
                    command_id, self.trial_id = self.trial_id, None
                    try:
                        result = self.config.commit()
                        result['stage'] = 'applied'
                        self.service.acknowledge(command_id, True, result)
                    except OSError:
                        self.config.rollback()
                        self.service.acknowledge(command_id, False,
                                                 error='Configuration save failed; reconnecting previous network')
                        self.start_wifi(self.config.values)
                        return
                self.show('WiFi Connected!\n' + self.wlan.ifconfig()[0] + '\nUSB debug ready\nSensors disabled')
                self.maintenance_at = self.service.now() + 1000
                self.maintenance_phase = 'ntp'
                self.service.log('info', 'WiFi connected; sensor acquisition remains disabled')
                self.service.emit('status', self.status(), True)
            elif self.service.now() - self.wifi_started >= self.wifi_timeout:
                status = self.wlan.status()
                if status in (network.STAT_WRONG_PASSWORD, 15, 204):
                    reason = 'Check password'
                elif status == network.STAT_NO_AP_FOUND:
                    reason = 'AP not found'
                elif status in (network.STAT_IDLE, network.STAT_CONNECTING):
                    reason = 'Connect timeout'
                else:
                    reason = 'Connect failed'
                self.network_failure(reason)
                self.service.emit('status', self.status(), True)
        elif self.network_state == 'connected' and not connected:
            self.network_state = 'reconnecting'
            self.show('WiFi offline\nReconnecting...\nUSB debug ready')
            self.service.log('warn', 'WiFi disconnected; USB diagnostics remain available')
        elif self.network_state == 'reconnecting' and connected:
            self.network_state = 'connected'
            self.show('WiFi Connected!\n' + self.wlan.ifconfig()[0] + '\nUSB debug ready\nSensors disabled')
            self.service.log('info', 'WiFi connection restored')
            self.maintenance_at = self.service.now() + 1000
            self.maintenance_phase = 'ntp'

    def clock_tick(self):
        now = self.service.now()
        if self.network_state != 'connected' or now - self.last_clock < 1000:
            return
        self.last_clock = now
        local = get_local_time(float(self.config.values['TIMEZONE_HOURS']))
        stamp = local['formatted_simple_date_time'] if self.ntp_synced else 'RTC ' + local['formatted_time']
        tcp = ('TCP: ' + str(self.tcp_ms) + 'ms') if self.tcp_ok else (
            'TCP: failed' if self.tcp_ok is False else 'TCP: pending')
        self.show('WiFi Connected!\n' + self.wlan.ifconfig()[0] + '\n' + stamp + '\n' + tcp)

    def maintenance_tick(self):
        now = self.service.now()
        if self.config.revision != self.maintenance_revision:
            self.maintenance_revision = self.config.revision
            self.maintenance_at = now + 500
            self.maintenance_phase = 'ntp'
        if self.service.enabled or self.network_state != 'connected' or now < self.maintenance_at:
            return
        # Run only one bounded socket job per iteration. These libraries use
        # synchronous DNS, which may still delay USB until resolution returns.
        self.maintenance_busy = self.maintenance_phase
        try:
            if self.maintenance_phase == 'ntp':
                self.ntp_synced = time_sync(self.config.values['NTP_SERVER'], timeout=2)
                self.service.log('info' if self.ntp_synced else 'warn',
                                 'Clock synchronized' if self.ntp_synced else 'NTP synchronization failed')
                self.maintenance_phase = 'tcp'
                self.maintenance_at = self.service.now() + 100
            else:
                result = socket_test({'host': self.config.values['NETWORK_TEST_HOST'],
                                      'port': int(self.config.values['NETWORK_TEST_PORT'])}, timeout=2)
                self.tcp_ok = bool(result.get('ok'))
                self.tcp_ms = result.get('data', {}).get('ping') if self.tcp_ok else None
                self.service.log('info' if self.tcp_ok else 'warn',
                                 'TCP connection test completed' if self.tcp_ok else 'TCP connection test failed')
                self.maintenance_phase = 'ntp'
                self.maintenance_at = self.service.now() + MAINTENANCE_INTERVAL_MS
        except OSError:
            self.service.log('warn', 'Network maintenance failed')
            self.maintenance_phase = 'ntp'
            self.maintenance_at = self.service.now() + MAINTENANCE_INTERVAL_MS
        finally:
            self.maintenance_busy = None

    def poll_commands(self):
        # Bound per-loop work. Do not consume the UART through a second driver.
        for _ in range(512):
            if not self.poller.poll(0):
                break
            char = sys.stdin.read(1)
            if not char:
                break
            self.service.feed(char)

    def run(self):
        # Construct the receiver and emit hello before importing display code.
        try:
            from components import screen_display
            self.display = screen_display
            screen_display.set_observer(self.service.capture_oled)
        except (OSError, ImportError):
            self.service.log('error', 'OLED unavailable; USB diagnostics remain available')
        self.show('One ESP\nUSB debug ready\nSensors disabled')
        self.service.log('info', 'Diagnostics ready; MQTT transport is reserved for a later release')
        self.start_wifi(self.config.values)
        while True:
            self.poll_commands()
            try:
                self.network_tick()
            except OSError:
                self.network_failure('Connect failed')
            self.service.tick()
            self.clock_tick()
            self.maintenance_tick()
            if self.restart_at is not None and self.service.now() >= self.restart_at and not self.service._pending_acks:
                self.pins.restore_safe_values()
                machine.reset()
            time.sleep_ms(10)


def run():
    runtime = DiagnosticRuntime()
    try:
        runtime.run()
    finally:
        runtime.service.stop()
