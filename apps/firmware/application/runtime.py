"""Device runtime for USB diagnostics and explicitly configured telemetry."""

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
from components.debug_service import DebugService, MAX_LINE_BYTES
from components.time_sync import get_local_time
from components.network_probe import NetworkProbe
from components.clock_sync import ClockSync
from components.system_metrics import SystemMetrics


TCP_INTERVAL_MS = 60000
STAGE_RETRY_MS = 1000
STAGE_LOG_INTERVAL_MS = 30000
DISPLAY_INIT_RETRY_MS = 30000
MQTT_INIT_RETRY_MS = 30000
MQTT_INIT_MAX_RETRY_MS = 300000
MQTT_GC_THRESHOLD_BYTES = 8192


class DiagnosticRuntime:
    def __init__(self, mqtt_config_path='.mqtt.json', sensor_config_path='.sensor.json',
                 command_input=None):
        self._mqtt_gc_previous = None
        # Collect temporary diagnostics before split heaps grow into memory
        # needed by Wi-Fi/TLS. Waiting until MQTT construction is too late.
        self._set_mqtt_gc_threshold()
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
        # A text read(1) can wait for UTF-8 continuation bytes after POLLIN.
        # Explicit binary adapters also support host tests without real stdin.
        self._command_input = (getattr(sys.stdin, 'buffer', None)
                               if command_input is None else command_input)
        self._command_line = bytearray()
        self._command_overflow = False
        self.command_input_available = False
        self.poller = None
        if callable(getattr(self._command_input, 'read', None)):
            try:
                self.poller = select.poll()
                self.poller.register(self._command_input, select.POLLIN)
                self.command_input_available = True
            except Exception:
                pass
        if not self.command_input_available:
            self._disable_command_input()
        self.display = None
        self.wifi_started = None
        self.wifi_timeout = 20000
        self.wifi_values = None
        self.network_state = 'idle'
        self.network_reason = None
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
        self.mqtt = None
        self._mqtt_config_path = mqtt_config_path
        self._mqtt_initialized = False
        self._mqtt_enabled = None
        self._mqtt_retry_at = 0
        self._mqtt_retry_delay = MQTT_INIT_RETRY_MS
        self._mqtt_state = 'disabled'
        self.sensor = None
        self._sensor_config_path = sensor_config_path
        self._sensor_requested = None
        self._sensor_state = 'disabled'
        self._sensor_retry_at = 0
        self._sensor_observation = None
        self._sensor_interval_ms = 60000
        self._sensor_gate_until = self.service.now() + 20000
        self._mqtt_init_stage = 'not_started'
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
        mqtt_status = self.mqtt.status() if self.mqtt else {
            'state': self._mqtt_state, 'initializationStage': self._mqtt_init_stage}
        # The ACK already carries the original UUID/time/hash. Avoid duplicating
        # two large summaries in every one-second USB status frame.
        mqtt_status.pop('lastSample', None)
        return {'uptimeMs': self.service.now(), 'memFree': gc.mem_free(),
                'usbCommandInputAvailable': self.command_input_available,
                'wifi': wifi, 'sensorEnabled': self.sensor is not None,
                'environment': self.sensor.status() if self.sensor else {
                    'enabled': self._sensor_requested is True, 'phase': self._sensor_state,
                    'samples': 0, 'failures': 0, 'lastError': None,
                    'readAtMs': None, 'ageMs': None, 'stale': True,
                    'intervalMs': 60000, 'nextReadInMs': 0,
                    'temperatureC': None, 'relativeHumidityPercent': None},
                'clock': clock_status,
                'mqtt': mqtt_status,
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
        self.network_reason = None
        self.wifi_values = dict(values)
        self.show(self.task_screen())
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
        self.network_reason = reason
        self.show(self.task_screen())
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
                self.show(self.task_screen())
                self.schedule_network_jobs()
                self.service.log('info', 'WiFi connected; diagnostics ready')
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
            self.show(self.task_screen())
            self.service.log('warn', 'WiFi disconnected; USB diagnostics remain available')
        elif self.network_state == 'reconnecting' and connected:
            self.network_state = 'connected'
            self.show(self.task_screen())
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

    def task_screen(self):
        # Four physical 16-character rows: job, observation, delivery, time.
        # Build this only at a visible second/state change, not every loop.
        environment = (self.sensor is not None or self._sensor_requested is True or
                       self._sensor_state in ('configuration_error', 'initialization_retry'))
        mqtt = self.mqtt.status() if self.mqtt else None
        transport = (mqtt.get('transport') or {}) if mqtt else {}
        mqtt_problem = self._mqtt_state in ('configuration_error', 'initialization_retry')
        title = 'Env telemetry' if environment else (
            'System telemetry' if self._mqtt_enabled or mqtt_problem or (mqtt and mqtt.get('enabled'))
            else 'USB diagnostics')
        if self.sensor:
            sensor = self.sensor.status()
            temperature, humidity = sensor.get('temperatureC'), sensor.get('relativeHumidityPercent')
            if sensor.get('lastError'):
                detail = 'Sensor read err'
            elif sensor.get('stale') and temperature is not None:
                detail = 'Sensor stale'
            elif temperature is not None and humidity is not None:
                detail = 'T:%.1fC H:%d%%' % (temperature, int(humidity + 0.5))
            else:
                detail = 'Reading sensor' if sensor.get('phase') in ('drain', 'tx', 'rx') else 'Awaiting reading'
        elif environment:
            detail = 'Sensor cfg error' if self._sensor_state == 'configuration_error' else (
                'Sensor retry' if self._sensor_state == 'initialization_retry' else 'Starting sensor')
        else:
            detail = 'Sensor disabled'

        if self.network_state == 'failed':
            delivery = 'WiFi timeout' if self.network_reason in ('Connect timeout', 'Disconnect timeout') else (
                self.network_reason or 'WiFi failed')
        elif self.network_state == 'reconnecting':
            delivery = 'WiFi offline'
        elif self.network_state in ('disconnecting', 'connecting'):
            delivery = 'WiFi connecting'
        elif self.network_state != 'connected':
            delivery = 'WiFi pending'
        elif mqtt and mqtt.get('enabled'):
            if not mqtt.get('clockReady'):
                delivery = 'Syncing clock'
            elif transport.get('connected'):
                delivery = 'MQTT: pending' if mqtt.get('pending') else 'MQTT: sent ' + str(mqtt.get('acknowledged', 0))
            else:
                delivery = 'MQTT reconnect' if transport.get('phase') == 'backoff' else 'MQTT connecting'
        elif self._mqtt_enabled or mqtt_problem:
            delivery = {'waiting_clock': 'Syncing clock',
                        'configuration_error': 'MQTT config err',
                        'initialization_retry': 'MQTT retry'}.get(self._mqtt_state, 'Starting MQTT')
        elif environment:
            delivery = 'Local sampling'
        else:
            delivery = ('TCP: ' + str(self.tcp_ms) + 'ms') if self.tcp_ok else (
                'TCP: failed' if self.tcp_ok is False else 'TCP: pending')
        # Errors take the heading as soon as the owning network stage notices
        # them. A failed probe describes that test, not all Internet traffic.
        if self.network_state == 'failed':
            title, detail = 'WiFi failed', delivery
            delivery = 'Edit WiFi config'
        elif self.network_state == 'reconnecting':
            detail, title, delivery = title, 'WiFi offline', 'Reconnecting...'
        elif self.network_state == 'connected':
            if self.tcp_ok is False:
                title = 'TCP test failed'
                detail = ('MQTT online' if transport.get('connected') else 'MQTT offline') if mqtt and mqtt.get('enabled') else 'WiFi online'
                retry = max(0, self.tcp_at - self.service.now())
                delivery = 'Testing TCP...' if self.probe.busy or not retry else 'Retry in %ds' % ((retry + 999) // 1000)
            elif mqtt_problem:
                title = 'MQTT setup error'
                detail = 'Config invalid' if self._mqtt_state == 'configuration_error' else 'Setup retry'
            elif (mqtt and mqtt.get('enabled') and
                  not transport.get('connected') and
                  (transport.get('phase') in ('backoff', 'unavailable') or
                   transport.get('connections', 0) > 0 or mqtt.get('transportFailures', 0) > 0)):
                title = 'MQTT offline'
                detail = 'Upload queued' if mqtt.get('pending') else 'Connection lost'
                retry = transport.get('nextRetryInMs', 0)
                delivery = 'Retry in %ds' % ((retry + 999) // 1000) if retry else 'Reconnecting...'
        local = get_local_time(float(self.config.values['TIMEZONE_HOURS']))
        stamp = local['formatted_simple_date_time'] if self.ntp_synced else 'RTC ' + local['formatted_time']
        # Keep overflow out of adjacent rows, especially the final clock row.
        return '\n'.join((title[:16], detail[:16], delivery[:16], stamp[:16]))

    def clock_tick(self):
        now = self.service.now()
        if now - self.last_clock < 100:
            return
        self.last_clock = now
        if now < self._stage_retry_at.get('display', 0):
            return
        # Observe the RTC second boundary cheaply instead of refreshing at an
        # arbitrary one-second phase, which could leave displayed seconds late.
        # Formatting/I2C still occur only when the visible second/state changes.
        client = getattr(self.mqtt, 'client', None)
        view = (time.time(), self.tcp_ok, self.tcp_ms, self.ntp_synced,
                self.config.values['TIMEZONE_HOURS'], self.network_state,
                self._mqtt_state, getattr(client, 'connected', None), self.probe.busy)
        if view == self._last_clock_view:
            return
        if self.show(self.task_screen()):
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

    def _disable_command_input(self):
        self.command_input_available = False
        self._command_input = None
        self._command_line = bytearray()
        self._command_overflow = False
        self.service.stop()
        self.service.log('error', 'USB binary command input unavailable; live debug disabled')

    def _command_byte(self, value):
        if value == 10:
            raw, overflow = self._command_line, self._command_overflow
            self._command_line = bytearray()
            self._command_overflow = False
            if not overflow:
                try:
                    # MicroPython bytearray lacks bytes.decode on some ports.
                    line = str(raw, 'utf-8')
                except (UnicodeError, ValueError):
                    self.service.log('warn', 'Invalid UTF-8 command frame ignored')
                else:
                    self.service.feed(line)
                    self.service.feed('\n')
        elif not self._command_overflow:
            if len(self._command_line) >= MAX_LINE_BYTES:
                self._command_line = bytearray()
                self._command_overflow = True
            else:
                self._command_line.append(value)

    def poll_commands(self):
        if not self.command_input_available:
            return
        errors = (getattr(select, 'POLLERR', 8) | getattr(select, 'POLLHUP', 16)
                  | getattr(select, 'POLLNVAL', 32))
        # Bound bytes per loop, and never ask the stream for unavailable bytes.
        for _ in range(512):
            ready = False
            for event in self.poller.poll(0):
                flags = event[1]
                if flags & select.POLLIN and not flags & errors:
                    ready = True
                    break
            if not ready:
                break
            chunk = self._command_input.read(1)
            if not chunk:
                break
            if not isinstance(chunk, (bytes, bytearray)) or len(chunk) != 1:
                self._disable_command_input()
                break
            self._command_byte(chunk[0])

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
            self.close_sensor()
            self.pins.restore_safe_values()
            machine.reset()

    def _load_sensor_options(self):
        try:
            import ujson as json
        except ImportError:
            import json
        try:
            with open(self._sensor_config_path) as stream:
                raw = stream.read(513)
        except OSError as error:
            if error.args and error.args[0] == 2:
                self._sensor_requested = False
                return
            raise
        if len(raw) > 512:
            raise ValueError('Sensor configuration is too large')
        settings = json.loads(raw)
        if (type(settings) is not dict or any(key not in ('enabled', 'interval_seconds') for key in settings)
                or type(settings.get('enabled', False)) is not bool):
            raise ValueError('Invalid sensor configuration')
        enabled, interval = settings.get('enabled', False), settings.get('interval_seconds', 60)
        if enabled and (type(interval) is not int or not 5 <= interval <= 3600):
            raise ValueError('Invalid sensor interval')
        self._sensor_interval_ms = interval * 1000 if enabled else 60000
        self._sensor_requested = enabled

    def _sensor_wait_for_mqtt(self):
        if self.network_state == 'failed' or self.service.now() >= self._sensor_gate_until:
            return False
        if self._mqtt_enabled is None:
            try:
                self._mqtt_enabled = self._mqtt_enabled_flag()
            except Exception:
                return False  # A bad MQTT config must not disable local reads.
        if not self._mqtt_enabled:
            return False
        client = getattr(self.mqtt, 'client', None)
        if getattr(client, 'connected', False):
            return False
        self._sensor_state = 'waiting_resources'
        return True

    def sensor_tick(self):
        if not self.sensor:
            if self._sensor_requested is False or self.service.now() < self._sensor_retry_at:
                return
            try:
                if self._sensor_requested is None:
                    self._load_sensor_options()
                if not self._sensor_requested or self._sensor_wait_for_mqtt():
                    return
                # Wi-Fi/TLS reserve native heap first. Config parse temporaries
                # have left their helper scope before importing the driver.
                gc.collect()
                from components.temperature_humidity import TemperatureHumidityReader
                self.sensor = TemperatureHumidityReader(self.service.now, interval_ms=self._sensor_interval_ms)
                for gpio in (14, 15):
                    self.pins.descriptions[gpio].update(
                        mode='uart', owner='RS485 temperature/humidity', source='configured',
                        reason='UART1 RX' if gpio == 14 else 'UART1 TX; strapping pin at reset')
                self.pins.descriptions[13].update(
                    mode='output', owner='RS485 direction', source='commanded', value=0,
                    reason='DE and /RE share GPIO13; low receives; no user GPIO writes')
            except ValueError:
                self._sensor_requested = False
                self._sensor_state = 'configuration_error'
                raise
            except Exception:
                self._sensor_retry_at = self.service.now() + 30000
                self._sensor_state = 'initialization_retry'
                raise
        observation = self.sensor.tick()
        self.pins.descriptions[13]['value'] = 1 if self.sensor.phase == 'tx' else 0
        if observation is None:
            return
        # Timestamp and capture at the physical read, never at a later retry.
        metrics = {'temperature_c': observation['temperatureC'],
                   'humidity_pct': observation['relativeHumidityPercent']}
        cpu = self.metrics.capture_temperature()
        if cpu is not None:
            metrics['cpu_temperature_c'] = cpu
        if not self._mqtt_clock_ready():
            self._sensor_observation = None
            return
        parts = time.gmtime()
        stamp = '%04d-%02d-%02dT%02d:%02d:%02dZ' % parts[:6]
        self._sensor_observation = {
            'sample': observation['sample'], 'readAtMs': observation['readAtMs'],
            'readTimeUtc': stamp, 'metrics': metrics, 'source': {'driver': 'rs485.ascii.READ'}}

    def _sensor_sample(self):
        if self.sensor is None or self._sensor_observation is None:
            return None
        reading = self.sensor.reading()
        if (reading is None or reading['sample'] != self._sensor_observation['sample']
                or reading['readAtMs'] != self._sensor_observation['readAtMs']):
            return None
        return self._sensor_observation

    def close_sensor(self):
        sensor, self.sensor = self.sensor, None
        self._sensor_requested = False
        self._sensor_observation = None
        self._sensor_state = 'disabled'
        if sensor is not None:
            sensor.close()

    def _mqtt_enabled_flag(self):
        # ConfigStore already needs JSON. This short-lived parse retains only
        # one boolean; it must not import MQTT, read the CA or start a socket.
        try:
            os.stat(self._mqtt_config_path)
        except OSError as error:
            if error.args and error.args[0] == 2:
                return False
            raise
        try:
            import ujson as json
        except ImportError:
            import json
        with open(self._mqtt_config_path) as stream:
            settings = stream.read(4097)
        if len(settings) > 4096:
            raise ValueError('MQTT configuration is too large')
        settings = json.loads(settings)
        if type(settings) is not dict or type(settings.get('enabled', False)) is not bool:
            raise ValueError('Invalid MQTT enabled flag')
        return settings.get('enabled', False)

    def _mqtt_clock_ready(self):
        quality = self.clock_sync.status()
        age = quality.get('lastSyncAgeMs')
        # Certificate validity and observation timestamps require a recent
        # successful NTP result, not merely a plausible reset/holdover RTC.
        if quality.get('synced') is not True or type(age) is not int or not 0 <= age <= 600000:
            return False
        try:
            parts = time.gmtime()
            return (len(parts) >= 6 and 2024 <= parts[0] <= 9999 and
                    1 <= parts[1] <= 12 and 1 <= parts[2] <= 31 and
                    0 <= parts[3] < 24 and 0 <= parts[4] < 60 and 0 <= parts[5] < 60)
        except Exception:
            return False

    def _mqtt_init_failed(self, client, permanent):
        self._restore_mqtt_gc_threshold()
        if client:
            try:
                client.close()
            except Exception:
                pass
        if permanent:
            self._mqtt_enabled = False
            self._mqtt_initialized = True
            self._mqtt_state = 'configuration_error'
        else:
            self._mqtt_state = 'initialization_retry'
            self._mqtt_retry_at = self.service.now() + self._mqtt_retry_delay
            self._mqtt_retry_delay = min(MQTT_INIT_MAX_RETRY_MS, self._mqtt_retry_delay * 2)
        # Collect only at the failed setup boundary, not in periodic telemetry.
        try:
            gc.collect()
        except Exception:
            pass

    def _set_mqtt_gc_threshold(self):
        # This is an allocation-triggered GC heuristic, not a memory reserve.
        # Install it only after MQTT construction, and own only our change.
        if self._mqtt_gc_previous is not None:
            return
        threshold = getattr(gc, 'threshold', None)
        if not callable(threshold):
            return
        try:
            previous = threshold()
        except Exception:
            return
        if type(previous) is not int or previous < -1:
            return
        self._mqtt_gc_previous = previous
        try:
            threshold(MQTT_GC_THRESHOLD_BYTES)
        except Exception:
            # A port may reject this API. Try restoring even if the setter
            # raised after changing its value; never block USB or the clock.
            self._restore_mqtt_gc_threshold()

    def _restore_mqtt_gc_threshold(self):
        previous = self._mqtt_gc_previous
        self._mqtt_gc_previous = None
        if previous is None:
            return
        try:
            threshold = getattr(gc, 'threshold', None)
            if callable(threshold):
                threshold(previous)
        except Exception:
            pass

    def mqtt_tick(self):
        now = self.service.now()
        if not self.mqtt:
            if self._mqtt_enabled is False:
                self._restore_mqtt_gc_threshold()
                return
            if now < self._mqtt_retry_at:
                return
            if self._mqtt_enabled is None:
                try:
                    self._mqtt_enabled = self._mqtt_enabled_flag()
                except ValueError:
                    self._mqtt_init_failed(None, True)
                    raise
                except Exception:
                    self._mqtt_init_failed(None, False)
                    raise
                if not self._mqtt_enabled:
                    self._mqtt_initialized = True
                    self._mqtt_state = 'disabled'
                    self._restore_mqtt_gc_threshold()
                    return
            if not self.wlan.isconnected():
                self._mqtt_state = 'waiting_network'
                return
            if not self._mqtt_clock_ready():
                self._mqtt_state = 'waiting_clock'
                return
            # Association and the first NTP exchange get memory priority.
            # Release the probe parse and construction temporaries only here.
            client = None
            try:
                gc.collect()
                self._mqtt_init_stage = 'import_telemetry'
                from components.mqtt_telemetry import load_config, TemperatureTelemetry
                self._mqtt_init_stage = 'load_config'
                config = load_config(self._mqtt_config_path)
                if not config or not config.get('enabled'):
                    self._mqtt_enabled = False
                    self._mqtt_initialized = True
                    self._mqtt_state = 'disabled'
                    self._restore_mqtt_gc_threshold()
                    gc.collect()
                    return
                self._mqtt_init_stage = 'import_transport'
                from components.mqtt_transport import MqttTransport
                self._mqtt_init_stage = 'construct_client'
                client = MqttTransport(config, self.service.now)
                publisher_args = {'logger': self.service.log}
                if self._sensor_requested:
                    publisher_args['sample_provider'] = self._sensor_sample
                self._mqtt_init_stage = 'construct_publisher'
                publisher = TemperatureTelemetry(config, client, self.service.now, **publisher_args)
                del config
                gc.collect()
                self.mqtt = publisher
                self._mqtt_init_stage = 'ready'
                self._mqtt_initialized = True
                self._mqtt_retry_delay = MQTT_INIT_RETRY_MS
                self._set_mqtt_gc_threshold()
            except ValueError:
                self._mqtt_init_failed(client, True)
                raise
            except Exception:
                self._mqtt_init_failed(client, False)
                raise
        connected = self.wlan.isconnected()
        self.mqtt.tick(connected, self._mqtt_clock_ready() if connected else False)

    def debug_telemetry_tick(self):
        client = getattr(self.mqtt, 'client', None)
        if getattr(client, 'handshaking', False):
            self.service.tick(quiet=True)
        else:
            self.service.tick()

    def tick_once(self):
        # Optional network work, serial decoding and display errors must not
        # stop one another or drop the runtime into the REPL. Each failed stage
        # has a bounded retry cadence. KeyboardInterrupt/SystemExit still pass
        # through for mpremote and deliberate maintenance.
        self._run_stage('usb', self.poll_commands)
        self._run_stage('sensor', self.sensor_tick)
        if self.sensor is not None and self.sensor.phase in ('draining', 'tx'):
            # Four UART bytes take about 4.2ms. Receive before a fast reply;
            # OLED and TLS wait at most the driver's 100ms TX deadline.
            self._run_stage('restart', self._restart_step)
            return
        self._run_stage('network', self._network_step)
        self._run_stage('maintenance', self.maintenance_tick)
        self._run_stage('clock', self.clock_tick)
        self._run_stage('telemetry', self.debug_telemetry_tick)
        self._run_stage('mqtt', self.mqtt_tick)
        self._run_stage('restart', self._restart_step)

    def run(self):
        # The receiver/hello exist before optional display initialization. A
        # failed display import retries at most once per 30 seconds via show().
        self.show('One ESP\nUSB debug ready\nDiagnostics ready')
        self._run_stage('startup', lambda: self.service.log(
            'info', 'Diagnostics ready; private MQTT telemetry is optional'))
        self._wifi_initializing = True
        while True:
            # Receive network timestamps before I2C/USB output adds latency.
            self.tick_once()
            time.sleep_ms(1 if self.sensor is not None and self.sensor.phase in ('draining', 'tx') else 10)


def run():
    runtime = DiagnosticRuntime()
    try:
        runtime.run()
    finally:
        try:
            runtime.cancel_network_jobs()
        finally:
            try:
                if runtime.mqtt:
                    runtime.mqtt.close()
            finally:
                try:
                    runtime._restore_mqtt_gc_threshold()
                finally:
                    try:
                        runtime.close_sensor()
                    finally:
                        runtime.service.stop()
