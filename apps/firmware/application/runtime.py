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
from components.time_sync import get_local_time
from components.network_probe import NetworkProbe
from components.clock_sync import ClockSync
from components.system_metrics import SystemMetrics


TCP_INTERVAL_MS = 60000
STAGE_RETRY_MS = 1000
STAGE_LOG_INTERVAL_MS = 30000
DISPLAY_INIT_RETRY_MS = 30000


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
                                    self.apply_config, self.schedule_restart, clock=time,
                                    metrics_provider=lambda interval: self.metrics.sample(interval))
        self.metrics = SystemMetrics(self.service.now, wlan=self.wlan)
        self.poller = select.poll()
        self.poller.register(sys.stdin, select.POLLIN)
        self.display = None
        self.wifi_started = None
        self.wifi_timeout = 20000
        self.wifi_values = None
        self.network_state = 'idle'
        self.trial_id = None
        self.restart_at = None
        self.maintenance_busy = None
        self.tcp_at = 0
        self.ntp_server = self.config.values['NTP_SERVER']
        self.tcp_target = (self.config.values['NETWORK_TEST_HOST'],
                           int(self.config.values['NETWORK_TEST_PORT']))
        self.probe = NetworkProbe(self.service.now)
        self.clock_sync = ClockSync(self.service.now)
        self.clock_sync.configure(self.ntp_server)
        self._next_network_job = 'ntp'
        self.ntp_synced = False
        self.tcp_ok = None
        self.tcp_ms = None
        self.tcp_samples = 0
        self.tcp_last_test_ms = None
        self.tcp_last_started_ms = None
        self.last_clock = -1000
        self._last_clock_view = None
        self._stage_retry_at = {}
        self._stage_log_at = {}
        self._display_import_at = 0
        self._wifi_initializing = False
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
        clock_status = self.clock_sync.status()
        return {'uptimeMs': self.service.now(), 'memFree': gc.mem_free(),
                'wifi': wifi, 'sensorEnabled': False,
                'clock': clock_status,
                'maintenancePaused': False,
                'maintenance': {'busy': self.maintenance_busy, 'clockSynced': clock_status['synced'],
                                'ntpPaused': False,
                                'tcpOk': self.tcp_ok, 'tcpMs': self.tcp_ms,
                                'tcpSamples': self.tcp_samples,
                                'tcpLastStartedMs': self.tcp_last_started_ms,
                                'tcpLastTestMs': self.tcp_last_test_ms,
                                'tcpIntervalMs': TCP_INTERVAL_MS,
                                'tcpNextInMs': max(0, self.tcp_at - self.service.now()),
                                'tcpInFlight': self.probe.busy}}

    def show(self, text):
        now = self.service.now()
        if now < self._stage_retry_at.get('display', 0):
            return False
        if not self.display:
            if now < self._display_import_at:
                return False
            self._display_import_at = now + DISPLAY_INIT_RETRY_MS
            try:
                self._load_display()
            except Exception as error:
                self._stage_failed('display', type(error).__name__)
                return False
        try:
            if self.display.screen_display(text) is False:
                self._stage_failed('display', 'Unavailable')
                return False
        except Exception as error:
            self._stage_failed('display', type(error).__name__)
            return False
        self._stage_retry_at.pop('display', None)
        return True

    def _load_display(self):
        from components import screen_display
        screen_display.set_observer(self.service.capture_oled)
        self.display = screen_display

    def _stage_failed(self, name, error_type):
        now = self.service.now()
        self._stage_retry_at[name] = now + STAGE_RETRY_MS
        if now - self._stage_log_at.get(name, -STAGE_LOG_INTERVAL_MS) >= STAGE_LOG_INTERVAL_MS:
            self._stage_log_at[name] = now
            # Exception messages may contain credentials, endpoints or input.
            # Fixed stage names and the exception class are sufficient to locate
            # a failed stage; failures in optional logging must not end the loop.
            try:
                self.service.log('error', 'Runtime ' + name + ' failed (' + error_type + '); retrying')
            except Exception:
                pass

    def _run_stage(self, name, operation):
        if self.service.now() < self._stage_retry_at.get(name, 0):
            return
        try:
            operation()
        except Exception as error:
            self._stage_failed(name, type(error).__name__)
        else:
            self._stage_retry_at.pop(name, None)

    def start_wifi(self, values, timeout_ms=20000):
        self.cancel_network_jobs()
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
        self.cancel_network_jobs()
        # Allow the accepted ACK to drain before resetting.
        self.restart_at = self.service.now() + 250

    def network_failure(self, reason):
        self.cancel_network_jobs()
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
                self.schedule_network_jobs()
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
            self.cancel_network_jobs()
            self.network_state = 'reconnecting'
            self.show('WiFi offline\nReconnecting...\nUSB debug ready')
            self.service.log('warn', 'WiFi disconnected; USB diagnostics remain available')
        elif self.network_state == 'reconnecting' and connected:
            self.network_state = 'connected'
            self.show('WiFi Connected!\n' + self.wlan.ifconfig()[0] + '\nUSB debug ready\nSensors disabled')
            self.service.log('info', 'WiFi connection restored')
            self.schedule_network_jobs()

    def schedule_network_jobs(self):
        now = self.service.now()
        self.clock_sync.request_sync()
        self.tcp_at = now + 1000
        self._last_clock_view = None

    def cancel_network_jobs(self):
        self.probe.cancel()
        self.clock_sync.cancel()
        self.maintenance_busy = None

    def clock_tick(self):
        now = self.service.now()
        if self.network_state != 'connected' or now - self.last_clock < 100:
            return
        self.last_clock = now
        if now < self._stage_retry_at.get('display', 0):
            return
        # Observe the RTC second boundary cheaply instead of refreshing at an
        # arbitrary one-second phase, which could leave displayed seconds late.
        # Formatting/I2C still occur only when the visible second/state changes.
        ip = self.wlan.ifconfig()[0]
        view = (time.time(), self.tcp_ok, self.tcp_ms, self.ntp_synced,
                self.config.values['TIMEZONE_HOURS'], ip)
        if view == self._last_clock_view:
            return
        local = get_local_time(float(self.config.values['TIMEZONE_HOURS']))
        stamp = local['formatted_simple_date_time'] if self.ntp_synced else 'RTC ' + local['formatted_time']
        tcp = ('TCP: ' + str(self.tcp_ms) + 'ms') if self.tcp_ok else (
            'TCP: failed' if self.tcp_ok is False else 'TCP: pending')
        if self.show('WiFi Connected!\n' + ip + '\n' + stamp + '\n' + tcp):
            self._last_clock_view = view

    def maintenance_tick(self):
        now = self.service.now()
        # Revision changes may be no-op saves. Only reset a job when its actual
        # endpoint changes, otherwise repeated form saves could cause probe bursts.
        ntp_server = self.config.values['NTP_SERVER']
        target = (self.config.values['NETWORK_TEST_HOST'],
                  int(self.config.values['NETWORK_TEST_PORT']))
        if ntp_server != self.ntp_server:
            self.ntp_server = ntp_server
            self.clock_sync.configure(ntp_server)
        if target != self.tcp_target:
            self.probe.cancel()
            self.maintenance_busy = None
            self.tcp_target = target
            self.tcp_at = now + 500
            self.tcp_ok, self.tcp_ms = None, None
        if self.restart_at is not None or self.network_state != 'connected' or not self.wlan.isconnected():
            self.cancel_network_jobs()
            return
        # Poll both jobs each loop. Neither a TCP handshake nor an NTP timeout
        # may prevent the other job, clock display or command receiver running.
        if self.clock_sync.busy:
            result = self.clock_sync.tick()
            if result is not None:
                self.record_clock_sync(result)
        if self.probe.busy:
            result = self.probe.tick()
            if result is not None:
                self.record_probe(result)

        ntp_due = not self.clock_sync.busy and now >= self.clock_sync.next_due_ms
        tcp_due = not self.probe.busy and now >= self.tcp_at
        # Only start one new job per iteration. First DNS resolution is still
        # synchronous, and alternating due jobs avoids priority starvation.
        if ntp_due and (not tcp_due or self._next_network_job == 'ntp'):
            self._next_network_job = 'tcp'
            result = self.clock_sync.start()
            if result is not None:
                self.record_clock_sync(result)
        elif tcp_due:
            self._next_network_job = 'ntp'
            # Start-to-start cadence, including failed DNS/connection attempts.
            # Missed intervals never trigger a burst of catch-up connections.
            self.tcp_at = now + TCP_INTERVAL_MS
            self.tcp_last_started_ms = now
            result = self.probe.start(target[0], target[1])
            if result is not None:
                self.record_probe(result)
        active = []
        if self.clock_sync.busy:
            active.append('ntp')
        if self.probe.busy:
            active.append('tcp')
        self.maintenance_busy = '+'.join(active) if active else None

    def record_clock_sync(self, result):
        self.ntp_synced = bool(self.clock_sync.status()['synced'])
        self.service.log('info' if result['ok'] else 'warn',
                         'Clock synchronized with fractional seconds' if result['ok']
                         else 'NTP synchronization failed; previous RTC retained')

    def record_probe(self, result):
        self.tcp_ok = bool(result['ok'])
        self.tcp_ms = result['ping'] if self.tcp_ok else None
        self.tcp_samples += 1
        self.tcp_last_test_ms = result['completedAtMs']
        self.service.log('info' if self.tcp_ok else 'warn',
                         'TCP connection test completed' if self.tcp_ok else 'TCP connection test failed')

    def poll_commands(self):
        # Bound per-loop work. Do not consume the UART through a second driver.
        for _ in range(512):
            if not self.poller.poll(0):
                break
            char = sys.stdin.read(1)
            if not char:
                break
            self.service.feed(char)

    def _network_step(self):
        try:
            if self._wifi_initializing:
                self.start_wifi(self.config.values)
                self._wifi_initializing = False
            else:
                self.network_tick()
        except OSError:
            self.network_failure('Connect failed')

    def _restart_step(self):
        if self.restart_at is not None and self.service.now() >= self.restart_at and not self.service._pending_acks:
            self.pins.restore_safe_values()
            machine.reset()

    def tick_once(self):
        # Optional network work, serial decoding and display errors must not
        # stop one another or drop the runtime into the REPL. Each failed stage
        # has a bounded retry cadence. KeyboardInterrupt/SystemExit still pass
        # through for mpremote and deliberate maintenance.
        self._run_stage('usb', self.poll_commands)
        self._run_stage('network', self._network_step)
        self._run_stage('maintenance', self.maintenance_tick)
        self._run_stage('clock', self.clock_tick)
        self._run_stage('telemetry', self.service.tick)
        self._run_stage('restart', self._restart_step)

    def run(self):
        # The receiver/hello exist before optional display initialization. A
        # failed display import retries at most once per 30 seconds via show().
        self.show('One ESP\nUSB debug ready\nSensors disabled')
        self._run_stage('startup', lambda: self.service.log(
            'info', 'Diagnostics ready; MQTT transport is reserved for a later release'))
        self._wifi_initializing = True
        while True:
            # Receive network timestamps before I2C/USB output adds latency.
            self.tick_once()
            time.sleep_ms(10)


def run():
    runtime = DiagnosticRuntime()
    try:
        runtime.run()
    finally:
        runtime.cancel_network_jobs()
        runtime.service.stop()
