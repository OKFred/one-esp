"""Transport-neutral diagnostics and command handling, compatible with MicroPython."""

try:
    import ujson as json
except ImportError:
    import json
try:
    import ubinascii as binascii
except ImportError:
    import binascii
import time


WIRE_PREFIX = '@one-esp '
MAX_LINE_BYTES = 8192
MAX_LOG_BYTES = 4096
MAX_LOGS = 32
LOG_OVERHEAD_BYTES = 64
MAX_ACKS = 16
NORMAL_BYTES_PER_SECOND = 7000
PRIORITY_BYTES_PER_SECOND = 2048
SNAPSHOT_PIN_CHUNK = 6
SNAPSHOT_INTERVAL_MS = 2000
PROFILES = {'full': (2, 10), 'lite': (1, 1)}


class DebugService:
    def __init__(self, writer, device_id, boot_id, status_provider, pins,
                 config_store, config_apply=None, restart=None, clock=None,
                 metrics_provider=None):
        self.writer = writer
        self.device_id = device_id
        self.boot_id = boot_id
        self.status_provider = status_provider
        self.metrics_provider = metrics_provider
        self.pins = pins
        self.config = config_store
        self.config_apply = config_apply
        self.restart = restart
        self.clock = clock or time
        self._raw_tick = self.clock.ticks_ms()
        self._uptime = 0
        self.seq = 0
        self.enabled = False
        self.profile = 'full'
        self.oled_fps, self.gpio_hz = PROFILES['full']
        self.expires_at = 0
        self.logs = []
        self.log_bytes = 0
        self.dropped_logs = 0
        self.dropped_events = 0
        self._last_status = -1000
        self._last_pins = -1000
        self._last_oled = -1000
        self._last_debug = -1000
        self._last_metrics = -15000
        self._debug_pending = False
        self._last_pin_rows = {}
        self._oled = {'width': 128, 'height': 64, 'format': 'MONO_VLSB',
                      'buffer': '', 'available': False, 'text': ''}
        self._oled_revision = 0
        self._sent_oled_revision = -1
        self._acks = {}
        self._ack_order = []
        self._pending_acks = []
        self._line = ''
        self._line_bytes = 0
        self._line_overflow = False
        self._budget_started = 0
        self._budget_bytes = 0
        self._priority_bytes = 0
        self._snapshot_pending = False
        self._snapshot_stage = 0
        self._snapshot_rows = None
        self._snapshot_pin_index = 0
        self._snapshot_finished = -SNAPSHOT_INTERVAL_MS

    def now(self):
        raw = self.clock.ticks_ms()
        self._uptime += max(0, self.clock.ticks_diff(raw, self._raw_tick))
        self._raw_tick = raw
        return self._uptime

    def _redact(self, message):
        message = str(message)
        candidates = [self.config.values]
        if self.config.pending:
            candidates.append(self.config.pending)
        for values in candidates:
            secret = values.get('PASSWORD')
            if secret:
                message = message.replace(secret, '[redacted]')
        return message

    def emit(self, event_type, data, priority=False):
        now = self.now()
        if now - self._budget_started >= 1000:
            self._budget_started, self._budget_bytes = now, 0
            self._priority_bytes = 0
        event = {'v': 1, 'deviceId': self.device_id, 'bootId': self.boot_id,
                 'seq': self.seq + 1, 'ts': now, 'type': event_type, 'data': data}
        encoded = WIRE_PREFIX + json.dumps(event) + '\n'
        size = len(encoded.encode())
        used = self._priority_bytes if priority else self._budget_bytes
        limit = PRIORITY_BYTES_PER_SECOND if priority else NORMAL_BYTES_PER_SECOND
        if size > MAX_LINE_BYTES or used + size > limit:
            self.dropped_events += 1
            return False
        try:
            self.writer(encoded)
        except OSError:
            self.dropped_events += 1
            return False
        if priority:
            self._priority_bytes += size
        else:
            self._budget_bytes += size
        self.seq += 1
        return True

    def hello(self, priority=True):
        capabilities = ['usb', 'oled', 'pins', 'logs', 'debug', 'snapshot',
                        'config', 'restart', 'gpio-allowlist', 'debug-renew']
        if self.metrics_provider:
            capabilities.append('metrics')
        return self.emit('hello', {
            'firmware': 'one-esp-debug/1.0', 'board': 'esp32-oled-096',
            'capabilities': capabilities,
        }, priority)

    def _metrics_tick(self, now):
        interval = 5000 if self.profile == 'full' else 15000
        try:
            data = self.metrics_provider(interval)
            if self.emit('metrics', data):
                self._last_metrics = now
                return True
        except Exception:
            # Metrics are optional. Allocation/probe failures must not stop the
            # application or leave the paced snapshot stuck on this stage.
            self.dropped_events += 1
            self._last_metrics = now
            return True
        return False

    def debug_state(self):
        return {'enabled': self.enabled, 'profile': self.profile,
                'expiresInMs': max(0, self.expires_at - self.now()) if self.enabled else 0,
                'oledFps': self.oled_fps, 'gpioHz': self.gpio_hz,
                'droppedLogs': self.dropped_logs, 'droppedEvents': self.dropped_events}

    def log(self, level, message):
        if level not in ('debug', 'info', 'warn', 'error'):
            level = 'info'
        # Truncate before retaining; neither secrets nor unbounded messages enter the queue.
        entry = {'level': level, 'message': self._redact(message)[:400]}
        size = len(entry['message'].encode()) + LOG_OVERHEAD_BYTES
        while self.logs and (len(self.logs) >= MAX_LOGS or self.log_bytes + size > MAX_LOG_BYTES):
            self._pop_log()
            self.dropped_logs += 1
        self.logs.append(entry)
        self.log_bytes += size

    def _pop_log(self):
        entry = self.logs.pop(0)
        self.log_bytes -= len(entry['message'].encode()) + LOG_OVERHEAD_BYTES

    def capture_oled(self, state):
        available = bool(state.get('available'))
        raw = state.get('buffer', b'')
        encoded = binascii.b2a_base64(raw).strip().decode() if available else ''
        text = self._redact(state.get('text', ''))[:256]
        if encoded != self._oled['buffer'] or text != self._oled['text'] or available != self._oled['available']:
            self._oled = {'width': 128, 'height': 64, 'format': 'MONO_VLSB',
                          'buffer': encoded, 'available': available, 'text': text}
            self._oled_revision += 1

    def snapshot(self):
        # Requests coalesce into one paced snapshot. Never synchronously dump
        # several kilobytes while UART0's receive buffer is not being drained.
        self._snapshot_pending = True

    def _snapshot_tick(self, now):
        if not self._snapshot_pending:
            return False
        if now - self._snapshot_finished < SNAPSHOT_INTERVAL_MS:
            return True
        sent = False
        if self._snapshot_stage == 0:
            sent = self.hello(False)
        elif self._snapshot_stage == 1:
            sent = self.emit('status', self.status_provider())
            if sent:
                self._last_status = now
        elif self._snapshot_stage == 2:
            sent = self.emit('debug', self.debug_state())
            if sent:
                self._last_debug = now
        elif self._snapshot_stage == 3:
            if self._snapshot_rows is None:
                self._snapshot_rows = self.pins.snapshot()
                self._snapshot_pin_index = 0
            start = self._snapshot_pin_index
            chunk = self._snapshot_rows[start:start + SNAPSHOT_PIN_CHUNK]
            # A refresh is split across independent serial frames. Never clear
            # known pins in this boot if a later fragment is lost; the host
            # already resets its map when the device boot/session changes.
            sent = self.emit('pins', {'pins': chunk, 'partial': True})
            if sent:
                self._snapshot_pin_index += len(chunk)
                if self._snapshot_pin_index < len(self._snapshot_rows):
                    return True
                self._last_pin_rows = {row['gpio']: row for row in self._snapshot_rows}
                self._last_pins = now
                self._snapshot_rows = None
        elif self._snapshot_stage == 4:
            sent = self.emit('oled', self._oled)
            if sent:
                self._sent_oled_revision = self._oled_revision
                self._last_oled = now
        else:
            sent = self._metrics_tick(now)
        if sent:
            if self._snapshot_stage >= (5 if self.metrics_provider else 4):
                self._snapshot_pending = False
                self._snapshot_stage = 0
                self._snapshot_finished = now
                return True
            self._snapshot_stage += 1
        return True

    def stop(self):
        self._debug_pending = self._debug_pending or self.enabled
        self.enabled = False
        self.expires_at = 0
        self.pins.restore_safe_values()

    def acknowledge(self, command_id, ok, result=None, error=None):
        data = {'id': command_id, 'ok': ok}
        if result is not None:
            data['result'] = result
        if error is not None:
            data['error'] = self._redact(error)
        if command_id not in self._acks:
            self._ack_order.append(command_id)
        self._acks[command_id] = data
        while len(self._ack_order) > MAX_ACKS:
            evicted = self._ack_order.pop(0)
            del self._acks[evicted]
            if evicted in self._pending_acks:
                self._pending_acks.remove(evicted)
        self._queue_ack(command_id)

    def _queue_ack(self, command_id):
        if command_id not in self._pending_acks:
            self._pending_acks.append(command_id)

    def _send_ack(self, command_id):
        if self.emit('ack', self._acks[command_id], True):
            if command_id in self._pending_acks:
                self._pending_acks.remove(command_id)
            return True
        return False

    def _number(self, value, name, minimum, maximum):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
            raise ValueError('Invalid ' + name)
        if value < minimum:
            raise ValueError('Invalid ' + name)
        return min(value, maximum)

    def handle(self, command):
        command_id = command.get('id') if type(command) is dict else None
        if not isinstance(command_id, str) or not 1 <= len(command_id) <= 128:
            return
        if command_id in self._acks:
            self._queue_ack(command_id)
            return
        try:
            if command.get('v') != 1:
                raise ValueError('Unsupported protocol version')
            name = command.get('command')
            args = command.get('args', {})
            if type(args) is not dict:
                raise ValueError('Invalid command arguments')
            if name not in ('debug.set', 'snapshot.get', 'config.get'):
                if command.get('bootId') != self.boot_id:
                    raise ValueError('Device rebooted; refresh the snapshot')
                if not self.enabled or self.now() >= self.expires_at:
                    self.stop()
                    raise ValueError('Enable a debug session first')
            elif 'bootId' in command and command['bootId'] != self.boot_id:
                raise ValueError('Device rebooted; refresh the snapshot')
            if name == 'debug.set':
                if type(args.get('enabled')) is not bool:
                    raise ValueError('enabled must be a boolean')
                if args['enabled']:
                    profile = args.get('profile', 'full')
                    if profile not in PROFILES:
                        raise ValueError('Unsupported debug profile')
                    max_oled, max_gpio = PROFILES[profile]
                    ttl = self._number(args.get('ttlSeconds', 600), 'TTL', 1, 1800)
                    oled_fps = self._number(args.get('oledFps', max_oled), 'OLED frequency', 0.1, max_oled)
                    gpio_hz = self._number(args.get('gpioHz', max_gpio), 'GPIO frequency', 0.1, max_gpio)
                    self.profile, self.oled_fps, self.gpio_hz = profile, oled_fps, gpio_hz
                    self.enabled, self.expires_at = True, self.now() + int(ttl * 1000)
                    self.acknowledge(command_id, True, self.debug_state())
                    self.snapshot()
                else:
                    self.stop()
                    self.acknowledge(command_id, True, self.debug_state())
            elif name == 'debug.renew':
                # The protected-command gate above requires the current boot
                # and an active lease. A delayed renewal cannot restart debug.
                ttl = self._number(args.get('ttlSeconds'), 'TTL', 1, 1800)
                self.expires_at = self.now() + int(ttl * 1000)
                self.acknowledge(command_id, True, self.debug_state())
                self._debug_pending = True
            elif name == 'snapshot.get':
                self.snapshot()
                self.acknowledge(command_id, True, {'stage': 'queued'})
            elif name == 'config.get':
                self.acknowledge(command_id, True, self.config.public())
            elif name == 'config.set':
                wifi_changed = self.config.begin(args.get('values'), args.get('revision'))
                if wifi_changed:
                    if self.config_apply is None:
                        self.config.rollback()
                        raise ValueError('Network trial is unavailable')
                    self.acknowledge(command_id, True, {'stage': 'testing', 'timeoutSeconds': 60})
                    self.config_apply(command_id, self.config.pending)
                else:
                    result = self.config.commit()
                    result['stage'] = 'applied'
                    self.acknowledge(command_id, True, result)
            elif name == 'gpio.write':
                self.acknowledge(command_id, True, self.pins.write(args.get('gpio'), args.get('value')))
            elif name == 'device.restart':
                if self.restart is None:
                    raise ValueError('Restart unavailable')
                self.acknowledge(command_id, True, {'stage': 'accepted'})
                self.restart()
            else:
                raise ValueError('Unsupported command')
        except ValueError as error:
            # Our validation errors contain field descriptions, never values.
            self.acknowledge(command_id, False, error=str(error))
        except (TypeError, KeyError, OSError):
            self.acknowledge(command_id, False, error='Command failed; check arguments and device state')

    def feed(self, chunk):
        for char in chunk:
            if char == '\n':
                line, overflow = self._line.rstrip('\r'), self._line_overflow
                self._line, self._line_bytes, self._line_overflow = '', 0, False
                if overflow or not line.startswith(WIRE_PREFIX):
                    continue
                try:
                    self.handle(json.loads(line[len(WIRE_PREFIX):]))
                except (ValueError, TypeError):
                    self.log('warn', 'Invalid debug command frame ignored')
            elif not self._line_overflow:
                self._line_bytes += len(char.encode())
                if self._line_bytes > MAX_LINE_BYTES:
                    self._line, self._line_overflow = '', True
                else:
                    self._line += char

    def tick(self):
        now = self.now()
        # Safety expiry precedes all transmission, including a queued ACK.
        if self.enabled and now >= self.expires_at:
            self.stop()
        if self._pending_acks and self._send_ack(self._pending_acks[0]):
            return
        if self._debug_pending and self.emit('debug', self.debug_state()):
            self._debug_pending = False
            self._last_debug = now
            return
        if self._snapshot_tick(now):
            return
        if not self.enabled:
            return
        status_interval = 1000 if self.profile == 'full' else 5000
        if now - self._last_status >= status_interval:
            if self.emit('status', self.status_provider()):
                self._last_status = now
                return
        if now - self._last_debug >= 5000:
            if self.emit('debug', self.debug_state()):
                self._last_debug = now
                return
        # A noisy input can remain due on every slow UART loop. Keep the latest
        # screen ahead of GPIO deltas so a running clock cannot be starved.
        if self._oled_revision != self._sent_oled_revision and now - self._last_oled >= 1000 / self.oled_fps:
            if self.emit('oled', self._oled):
                self._sent_oled_revision = self._oled_revision
                self._last_oled = now
                return
        # Cached metrics get their own low-frequency frame, rather than adding
        # several hundred bytes to every status/OLED update. No idle sampling.
        if self.metrics_provider and now - self._last_metrics >= (5000 if self.profile == 'full' else 15000):
            if self._metrics_tick(now):
                return
        if now - self._last_pins >= 1000 / self.gpio_hz:
            rows = self.pins.snapshot()
            # Sampling cadence is independent of successful transmission.
            # Retain the last sent rows on failure so the next sample retries.
            self._last_pins = now
            changed = [row for row in rows if self._last_pin_rows.get(row['gpio']) != row]
            if changed and self.emit('pins', {'pins': changed, 'partial': True}):
                self._last_pin_rows = {row['gpio']: row for row in rows}
                return
        for _ in range(min(2, len(self.logs))):
            entry = self.logs[0]
            if self.profile == 'lite' and entry['level'] not in ('warn', 'error'):
                self._pop_log()
            elif self.emit('log', entry):
                self._pop_log()
                return
            else:
                break
