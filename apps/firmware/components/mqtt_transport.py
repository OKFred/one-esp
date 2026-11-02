"""Bounded MQTT QoS 1 over verified, polled MicroPython mbedTLS sockets.

The application supplies a non-wrapping millisecond clock. DNS is the only
background operation: ESP32 getaddrinfo releases the GIL while lwIP waits.
There is at most one resolver worker, one connection and one pending message.
No exception text, broker identity, credentials, topic or payload enters status.
"""

import gc
import socket
import sys
try:
    import uselect as select
except ImportError:
    import select
try:
    import uerrno as errno
except ImportError:
    import errno

from components.mqtt_wire import (connect_packet, publish_packet, ping_packet, PacketReader,
                                  subscribe_packet, puback_packet, decode_publish)


DNS_TIMEOUT_MS = 15000
DNS_CACHE_MS = 300000
CONNECT_TIMEOUT_MS = 10000
SESSION_TIMEOUT_MS = 15000
ACK_TIMEOUT_MS = 15000
PING_TIMEOUT_MS = 10000
MAX_BACKOFF_MS = 60000
IO_BYTES_PER_TICK = 256
MAX_PAYLOAD_BYTES = 2048
MAX_COMMAND_REPLIES = 2
MAX_COMMAND_PUBACKS = 8
POLL_OUT = getattr(select, 'POLLOUT', 4)
POLL_ERRORS = (getattr(select, 'POLLERR', 8) | getattr(select, 'POLLHUP', 16) |
               getattr(select, 'POLLNVAL', 32))
WOULD_BLOCK = (getattr(errno, 'EAGAIN', 11), getattr(errno, 'EWOULDBLOCK', 11))
CONNECT_PENDING = WOULD_BLOCK + (getattr(errno, 'EINPROGRESS', 119 if sys.platform == 'esp32' else 115),
                                getattr(errno, 'EALREADY', 120 if sys.platform == 'esp32' else 114))
ALREADY_CONNECTED = getattr(errno, 'EISCONN', 127 if sys.platform == 'esp32' else 106)


def _error_number(error):
    number = getattr(error, 'errno', None)
    return number if number is not None else (error.args[0] if error.args else None)


def _dns_failure(reason, number=None):
    return OSError(number, reason) if type(number) is int else OSError(reason)


def _dns_failure_reason(error):
    for value in error.args:
        if isinstance(value, str) and value in ('dns_thread_start_failed', 'dns_lookup_failed'):
            return value
    return None


def _certificate_reason(message):
    # Only recognize fixed mbedTLS verify-info text. Never retain subjects,
    # hostnames or any other free-form exception content in diagnostics.
    certificate = False
    for line in message.split('\n'):
        line = line.strip()
        if line in ('The certificate validity has expired', 'The CRL is expired'):
            return 'certificate_verify_expired'
        if line in ('The certificate validity starts in the future', 'The CRL is from the future'):
            return 'certificate_verify_future'
        if line == 'The certificate Common Name (CN) does not match with the expected CN':
            return 'certificate_verify_cn_mismatch'
        if line in ('The certificate is not correctly signed by the trusted CA',
                    'The CRL is not correctly signed by the trusted CA'):
            return 'certificate_verify_not_trusted'
        if line == 'Certificate was missing':
            return 'certificate_verify_missing'
        if (line.startswith('The certificate ') or line.startswith('The CRL ') or
                line.startswith('Usage does not match the ') or
                line in ('Certificate verification was skipped',
                         'Other reason (can be used by verify callback)',
                         'Unknown reason (this should not happen)')):
            certificate = True
    return 'certificate_verify_other' if certificate else None


def _failure_details(phase, operation, reason, error):
    # Exception messages may include private values. Retain only fixed labels
    # and a numeric errno; even an exception's class name needs an allowlist.
    error_type = None
    number = None
    if error is not None:
        name = type(error).__name__
        error_type = name if name in ('OSError', 'ValueError', 'MemoryError',
                                      'TypeError', 'RuntimeError') else 'Exception'
        candidate = _error_number(error)
        if type(candidate) is int:
            number = candidate
        if isinstance(error, ValueError):
            message = error.args[0] if error.args else None
            reason = 'value_error'
            if isinstance(message, str):
                if message == 'invalid cert':
                    reason = 'invalid_cert'
                elif message == 'MQTT connection refused':
                    reason = 'connack_refused'
                elif message in ('Unexpected MQTT CONNACK', 'Unexpected MQTT PINGRESP',
                                  'Invalid MQTT CONNACK', 'Invalid MQTT PUBACK identifier',
                                  'MQTT input must be bytes', 'Unexpected MQTT packet header',
                                  'Invalid MQTT remaining length', 'MQTT packet is too large',
                                  'Invalid MQTT packet length', 'MQTT receive flood',
                                  'Unexpected MQTT SUBACK', 'Invalid MQTT SUBACK',
                                  'Invalid MQTT command PUBLISH', 'Invalid MQTT command topic',
                                  'Unexpected MQTT command PUBLISH', 'Invalid MQTT packet identifier',
                                  'MQTT command acknowledgement queue full'):
                    reason = 'malformed_packet'
                else:
                    certificate_reason = _certificate_reason(message)
                    if certificate_reason is not None:
                        reason = certificate_reason
    return {'phase': phase, 'operation': operation, 'errorType': error_type,
            'errno': number, 'reason': reason}


def _ipv4(value):
    if not isinstance(value, str):
        return False
    parts = value.split('.')
    return len(parts) == 4 and all(part and all('0' <= c <= '9' for c in part)
                                   and len(part) <= 3 and int(part) <= 255 for part in parts)


class ThreadResolver:
    """Single-flight DNS with tagged results and a five-minute cache.

    A timed-out native lookup cannot be killed safely. invalidate() makes its
    result obsolete, but a second worker is never started until it exits.
    Only this resolver owns its worker; callbacks never touch MQTT sockets.
    """

    def __init__(self, socket_module=None, thread_module=None):
        self._sockets = socket_module if socket_module is not None else socket
        self._threads = thread_module
        self._busy = False
        self._result = None
        self._generation = 0
        self._cache = None
        self._cache_until = 0

    def invalidate(self):
        self._generation += 1
        self._cache = None
        self._result = None

    def _lookup(self, target, generation):
        info = None
        number = None
        try:
            rows = self._sockets.getaddrinfo(target[0], target[1],
                                            self._sockets.AF_INET, self._sockets.SOCK_STREAM)
            for row in rows:
                if _ipv4(row[-1][0]) and row[-1][0] != '0.0.0.0':
                    info = (row[0], row[1], row[2], (row[-1][0], target[1]))
                    break
        except Exception as error:
            candidate = _error_number(error)
            if type(candidate) is int:
                number = candidate
        self._result = (generation, target, info, number)
        self._busy = False

    def resolve(self, host, port, now):
        target = (host, port)
        if self._cache is not None and self._cache[0] == target and now < self._cache_until:
            return self._cache[1]
        result, self._result = self._result, None
        if result is not None and result[0] == self._generation and result[1] == target:
            if result[2] is None:
                raise _dns_failure('dns_lookup_failed', result[3])
            self._cache = (target, result[2])
            self._cache_until = now + DNS_CACHE_MS
            return result[2]
        if self._busy:
            return None
        if self._threads is None:
            import _thread
            self._threads = _thread
        self._busy = True
        try:
            self._threads.start_new_thread(self._lookup, (target, self._generation))
        except Exception as error:
            self._busy = False
            raise _dns_failure('dns_thread_start_failed', _error_number(error))
        return None


class MqttTransport:
    def __init__(self, config, now_ms, socket_module=None, ssl_module=None,
                 resolver=None, poll_factory=None, collect=None,
                 command_handler=None, command_clock=None, command_active=True):
        self._config = config
        self._now = now_ms
        self._sockets = socket_module if socket_module is not None else socket
        self._collect = gc.collect if collect is None else collect
        if ssl_module is None:
            try:
                import ssl
            except ImportError:
                import ussl as ssl
            ssl_module = ssl
        self._ssl = ssl_module
        context_type = getattr(ssl_module, 'SSLContext', None)
        context_protocol = getattr(ssl_module, 'PROTOCOL_TLS_CLIENT', None)
        use_context = callable(context_type) and context_protocol is not None
        if (not hasattr(ssl_module, 'CERT_REQUIRED') or
                (not use_context and not callable(getattr(ssl_module, 'wrap_socket', None)))):
            raise ValueError('Verified nonblocking TLS is unavailable')
        host = config.get('server_hostname') or config.get('host')
        ca_data = config.get('ca_data')
        if not isinstance(host, str) or not host or '\x00' in host:
            raise ValueError('TLS server hostname is required')
        if not isinstance(ca_data, bytes) or not 1 <= len(ca_data) <= 8192:
            raise ValueError('TLS CA certificate is required')
        if config.get('broker_ip') is not None and not _ipv4(config['broker_ip']):
            raise ValueError('MQTT numeric endpoint must be IPv4')
        self._server_hostname = host
        self._ca_data = ca_data
        self._keepalive = config.get('keepalive', 60)
        if isinstance(self._keepalive, bool) or not isinstance(self._keepalive, int) or not 10 <= self._keepalive <= 600:
            raise ValueError('Invalid MQTT keepalive')
        # Validate/encode once. Never log or expose this credential-bearing frame.
        self._connect_frame = connect_packet(config['client_id'], config['username'],
                                             config['password'], self._keepalive, clean_session=True)
        self._resolver = resolver if resolver is not None else ThreadResolver(self._sockets)
        self._poll_factory = poll_factory if poll_factory is not None else select.poll
        self._commands_enabled = config.get('commands_enabled', False) is True
        self._commands_active = command_active is True
        self._commands = None
        self._command_topic = self._reply_topic = None
        if all(isinstance(config.get(key), str) and config.get(key) for key in ('site_id', 'gateway_id', 'device_id')):
            prefix = ('iot/v1/' + config['site_id'] + '/' + config['gateway_id'] +
                      '/devices/' + config['device_id'] + '/commands')
            # The codec rejects wildcard or invalid UTF-8 authority.
            subscribe_packet(prefix, 1)
            self._command_topic, self._reply_topic = prefix, prefix + '/ack'
        if self._commands_enabled and callable(command_handler) and self._command_topic:
            from components.mqtt_commands import CommandProcessor
            self._commands = CommandProcessor(command_handler, command_clock)
        self._command_state = ('awaiting_connection' if self._commands else
                               'unavailable' if self._commands_enabled else 'disabled')
        self._reader = PacketReader(allow_commands=self._commands is not None)
        self._subscribe_id = None
        self._subscribe_sent = False
        self._subscribe_deadline = 0
        self._command_pubacks = []
        self._reply_queue = []
        self._reply_inflight = None
        self._reply_deadline = 0
        self._reply_ack_count = 0
        self._wrong_command_topics = 0
        self._blocked_commands = 0
        self._raw = None
        self._stream = None
        self._poll = None
        self._address = None
        self._state = 'waiting_network'
        self._deadline = 0
        self._retry_at = 0
        self._failures = 0
        self._attempt = 0
        self._connections = 0
        self._last_error = None
        self._last_failure = None
        self._operation = None
        self._last_tx_at = 0
        self._tx = None
        self._tx_offset = 0
        self._tx_kind = None
        self._tx_deadline = 0
        self._pending = None
        self._next_packet_id = 0
        self._ack_deadline = 0
        self._ping_deadline = 0
        self._acked = False
        self._ack_count = 0
        self._publish_count = 0
        self._ssl_context = None
        self._use_context = use_context
        self._handoff_complete = False

    def _context(self):
        if self._ssl_context is None:
            # Let DNS finish and TCP connect before reserving TLS native memory.
            # Keep the parsed trust chain afterwards, including across retries.
            # Advertised context support must succeed; errors never downgrade.
            context = self._ssl.SSLContext(self._ssl.PROTOCOL_TLS_CLIENT)
            context.verify_mode = self._ssl.CERT_REQUIRED
            if context.verify_mode != self._ssl.CERT_REQUIRED:
                raise ValueError('Verified TLS context is unavailable')
            context.load_verify_locations(cadata=self._ca_data)
            self._ssl_context = context
        return self._ssl_context

    def publish(self, topic, payload):
        """Accept one immutable payload; keep it through reconnect until PUBACK."""
        if self._pending is not None or self._acked:
            return False
        if not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_PAYLOAD_BYTES:
            raise ValueError('Invalid MQTT telemetry payload')
        packet_id = self._packet_id()
        # Validate topic and size before accepting responsibility for this message.
        publish_packet(topic, payload, packet_id)
        self._pending = [topic, payload, packet_id, False]
        return True

    def _packet_id(self):
        reserved = (self._pending[2] if self._pending else None,
                    self._reply_inflight[2] if self._reply_inflight else None,
                    self._subscribe_id)
        for _ in range(4):
            self._next_packet_id = self._next_packet_id % 65535 + 1
            if self._next_packet_id not in reserved:
                return self._next_packet_id
        raise ValueError('MQTT packet identifiers unavailable')

    def release_context(self):
        """Free an inactive TLS context without discarding pending data."""
        if self._raw is not None or self._stream is not None:
            raise ValueError('MQTT connection must be closed before releasing TLS context')
        self._ssl_context = None
        self._collect_once()

    def prepare_pending_transfer_from(self, previous):
        """Validate/allocate before configuration commit; leave both senders intact."""
        if (previous is self or not self.connected or self._pending is not None or
                self._reply_inflight is not None or self._reply_queue or
                (self._commands is not None and self._commands.status()['cached']) or
                previous._raw is not None or previous._stream is not None or previous._handoff_complete):
            raise ValueError('MQTT pending transfer is unavailable')
        if (previous._reply_inflight or previous._reply_queue) and self._reply_topic != previous._reply_topic:
            raise ValueError('MQTT control identity changed')
        commands = previous._commands if self._command_topic == previous._command_topic else None
        # All sums and the empty replacement queue are allocated here, before
        # ownership moves. Large counters may use heap-backed MicroPython ints.
        return (self, previous, previous._pending, previous._acked, previous._next_packet_id,
                self._publish_count + previous._publish_count,
                self._ack_count + previous._ack_count,
                self._failures + previous._failures,
                self._connections + previous._connections,
                self._reply_ack_count + previous._reply_ack_count,
                previous._reply_inflight, previous._reply_queue, [], commands)

    def transfer_pending_from(self, previous, prepared=None):
        """Adopt one paused sender; the prepared commit phase only assigns references."""
        token = self.prepare_pending_transfer_from(previous) if prepared is None else prepared
        if (type(token) is not tuple or len(token) != 14 or token[0] is not self or
                token[1] is not previous or previous._pending is not token[2] or
                previous._reply_queue is not token[11] or previous._handoff_complete or
                not self.connected or self._pending is not None or self._reply_inflight is not None or
                self._reply_queue or previous._raw is not None or previous._stream is not None):
            raise ValueError('MQTT pending transfer preparation changed')
        self._pending = token[2]
        previous._pending = None
        self._acked = token[3]
        previous._acked = False
        self._next_packet_id = token[4]
        self._publish_count = token[5]
        self._ack_count = token[6]
        self._failures = token[7]
        self._connections = token[8]
        self._reply_ack_count = token[9]
        self._reply_inflight = token[10]
        previous._reply_inflight = None
        self._reply_queue = token[11]
        previous._reply_queue = token[12]
        self._reply_deadline = 0
        if token[13] is not None:
            self._commands = token[13]
            previous._commands = None
        previous._handoff_complete = True
        previous._state = 'transferred'

    def set_commands_active(self, active):
        """A candidate can test SUBACK while configuration execution is paused."""
        if type(active) is not bool:
            raise ValueError('Invalid MQTT command activation')
        self._commands_active = active

    def _enqueue_reply(self, identifier, payload):
        for index, entry in enumerate(self._reply_queue):
            if entry[0] == identifier:
                self._reply_queue[index] = (identifier, payload)
                return True
        if len(self._reply_queue) >= MAX_COMMAND_REPLIES:
            return False
        self._reply_queue.append((identifier, payload))
        return True

    def take_ack(self):
        acknowledged, self._acked = self._acked, False
        return acknowledged

    @property
    def connected(self):
        return self._state == 'connected'

    @property
    def handshaking(self):
        return self._state in ('resolving', 'connecting', 'mqtt_connect')

    def status(self):
        return {'phase': self._state, 'connected': self._state == 'connected',
                'inflight': self._pending is not None, 'acks': self._ack_count,
                'publishes': self._publish_count, 'failures': self._failures,
                'connections': self._connections, 'lastError': self._last_error,
                'lastFailure': dict(self._last_failure) if self._last_failure else None,
                'nextRetryInMs': max(0, self._retry_at - self._now()),
                'commands': dict({'configured': self._commands_enabled,
                                  'active': self._commands_active and self._commands_enabled and self._commands is not None,
                                  'state': self._command_state,
                                  'replyPending': len(self._reply_queue) + bool(self._reply_inflight),
                                  'replyAcknowledged': self._reply_ack_count,
                                  'wrongTopics': self._wrong_command_topics,
                                  'blocked': self._blocked_commands},
                                 **(self._commands.status() if self._commands else {}))}

    def _drop_socket(self):
        raw, stream, poller = self._raw, self._stream, self._poll
        self._raw = self._stream = self._poll = None
        self._state = 'waiting_network'
        self._tx = None
        self._tx_offset = 0
        self._tx_kind = None
        self._ack_deadline = self._ping_deadline = 0
        self._reply_deadline = self._subscribe_deadline = 0
        self._subscribe_id = None
        self._subscribe_sent = False
        while self._command_pubacks:
            self._command_pubacks.pop()
        self._command_state = ('awaiting_connection' if self._commands_enabled and self._commands else
                               'unavailable' if self._commands_enabled else 'disabled')
        if poller is not None and raw is not None:
            try:
                poller.unregister(raw)
            except Exception:
                pass
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
        # A failed deferred mbedTLS handshake may detach its raw socket before
        # raising. Closing only the TLS wrapper would then leak the TCP socket.
        if raw is not None:
            try:
                raw.close()
            except Exception:
                pass
        # Reset can allocate a tiny parser buffer. Native handles must already
        # be closed even when memory pressure prevents that allocation.
        self._reader.reset()

    def close(self):
        """Release the connection; retained pending data may retry on later ticks."""
        self._drop_socket()
        self._collect_once()
        self._state = 'waiting_network'
        self._retry_at = 0

    def _collect_once(self):
        # Collection can return entirely unused split-heap areas to ESP-IDF.
        # Keep this at connection boundaries, never in the per-tick I/O path.
        try:
            self._collect()
        except Exception:
            pass

    def _fail(self, reason, error=None):
        phase, operation = self._state, self._operation
        self._drop_socket()
        self._collect_once()
        self._last_error = reason
        self._last_failure = _failure_details(phase, operation, reason, error)
        self._failures += 1
        self._retry_at = self._now() + min(MAX_BACKOFF_MS, 1000 * (1 << min(self._attempt, 6)))
        self._attempt += 1
        self._state = 'backoff'

    def _tcp_ready(self):
        self._operation = 'connect'
        try:
            self._raw.connect(self._address)
            return True
        except OSError as error:
            code = _error_number(error)
            if code == ALREADY_CONNECTED:
                return True
            if code in CONNECT_PENDING:
                return False
            raise

    def _wrap(self):
        # Unsupported certificate/hostname/deferred-handshake options fail closed.
        # MicroPython mbedTLS advances the handshake through read/write EAGAIN.
        self._collect_once()
        self._operation = 'wrap'
        if self._use_context:
            self._stream = self._context().wrap_socket(
                self._raw, server_hostname=self._server_hostname,
                do_handshake_on_connect=False)
        else:
            self._stream = self._ssl.wrap_socket(
                self._raw, cert_reqs=self._ssl.CERT_REQUIRED,
                cadata=self._ca_data, server_hostname=self._server_hostname,
                do_handshake=False)
        self._stream.setblocking(False)
        self._state = 'mqtt_connect'
        self._deadline = self._now() + SESSION_TIMEOUT_MS
        self._queue(self._connect_frame, 'connect')

    def _open(self, info):
        self._operation = 'open'
        self._address = info[-1]
        self._raw = self._sockets.socket(*info[:3])
        self._raw.setblocking(False)
        self._deadline = self._now() + CONNECT_TIMEOUT_MS
        self._state = 'connecting'
        self._poll = self._poll_factory()
        self._poll.register(self._raw, POLL_OUT | POLL_ERRORS)
        if self._tcp_ready():
            self._wrap()

    def _queue(self, packet, kind):
        self._tx = packet
        self._tx_offset = 0
        self._tx_kind = kind
        self._tx_deadline = self._now() + SESSION_TIMEOUT_MS

    def _write_once(self):
        if self._tx is None:
            return
        self._operation = 'write'
        end = min(len(self._tx), self._tx_offset + IO_BYTES_PER_TICK)
        try:
            count = self._stream.write(memoryview(self._tx)[self._tx_offset:end])
        except OSError as error:
            if _error_number(error) in WOULD_BLOCK:
                return
            raise
        if count is None:
            return
        if not isinstance(count, int) or count <= 0 or count > end - self._tx_offset:
            raise OSError('MQTT stream closed')
        self._last_tx_at = self._now()
        self._tx_offset += count
        if self._tx_offset == len(self._tx):
            kind = self._tx_kind
            self._tx = self._tx_kind = None
            self._tx_offset = 0
            if kind == 'publish':
                self._publish_count += 1
                self._ack_deadline = self._now() + ACK_TIMEOUT_MS
            elif kind == 'ping':
                self._ping_deadline = self._now() + PING_TIMEOUT_MS
            elif kind == 'subscribe':
                self._subscribe_sent = True
                self._subscribe_deadline = self._now() + ACK_TIMEOUT_MS
            elif kind == 'command_reply':
                self._reply_deadline = self._now() + ACK_TIMEOUT_MS

    def _read_once(self):
        self._operation = 'read'
        try:
            data = self._stream.read(IO_BYTES_PER_TICK)
        except OSError as error:
            if _error_number(error) in WOULD_BLOCK:
                return
            raise
        if data is None:
            return
        if not data:
            raise OSError('MQTT stream closed')
        self._operation = 'packet_decode'
        for packet_type, flags, body in self._reader.feed(data):
            if packet_type == 2:
                self._operation = 'connack'
                if self._state != 'mqtt_connect' or body[0] != 0 or self._tx is not None:
                    raise ValueError('Unexpected MQTT CONNACK')
                self._state = 'connected'
                self._last_error = None
                self._connections += 1
                if self._commands_enabled and self._commands:
                    self._subscribe_id = self._packet_id()
                    self._command_state = 'subscribing'
            elif packet_type == 4:
                packet_id = (body[0] << 8) | body[1]
                if (self._state == 'connected' and self._reply_inflight is not None and
                        self._reply_deadline and packet_id == self._reply_inflight[2]):
                    self._reply_inflight = None
                    self._reply_deadline = 0
                    self._reply_ack_count += 1
                elif (self._state == 'connected' and self._pending is not None and
                        self._ack_deadline and packet_id == self._pending[2]):
                    self._pending = None
                    self._ack_deadline = 0
                    self._acked = True
                    self._ack_count += 1
                    self._attempt = 0
            elif packet_type == 9:
                packet_id = (body[0] << 8) | body[1]
                if (not self.connected or not self._subscribe_sent or
                        packet_id != self._subscribe_id or body[2] == 2):
                    raise ValueError('Unexpected MQTT SUBACK')
                self._subscribe_id = None
                self._subscribe_deadline = 0
                self._command_state = 'denied' if body[2] == 128 else 'ready'
            elif packet_type == 3:
                if not self.connected:
                    raise ValueError('Unexpected MQTT command PUBLISH')
                topic, payload, packet_id, qos, retained = decode_publish(flags, body)
                if qos:
                    if packet_id not in self._command_pubacks:
                        if len(self._command_pubacks) >= MAX_COMMAND_PUBACKS:
                            raise ValueError('MQTT command acknowledgement queue full')
                        self._command_pubacks.append(packet_id)
                if (self._commands_enabled and self._commands_active and self._commands and
                        self._command_state == 'ready' and topic == self._command_topic):
                    self._commands.receive(payload, retained)
                elif topic != self._command_topic:
                    self._wrong_command_topics += 1
                else:
                    self._blocked_commands += 1
            elif packet_type == 13:
                if self._state != 'connected':
                    raise ValueError('Unexpected MQTT PINGRESP')
                self._ping_deadline = 0
                self._attempt = 0

    def tick(self, connected, time_ready):
        """Do at most one TLS write and one TLS read; never wait for an ACK."""
        if self._handoff_complete:
            return
        now = self._now()
        if not connected or not time_ready:
            if self._raw is not None or self._stream is not None or self._tx is not None:
                self._drop_socket()
            self._state = 'waiting_network' if not connected else 'waiting_time'
            self._retry_at = 0
            return
        if self._state in ('waiting_network', 'waiting_time', 'backoff'):
            if now < self._retry_at:
                return
            self._state = 'resolving'
            self._deadline = now + DNS_TIMEOUT_MS
        try:
            if self._state == 'resolving':
                self._operation = 'resolve'
                if now >= self._deadline:
                    self._resolver.invalidate()
                    self._fail('dns_timeout')
                    return
                endpoint = self._config.get('broker_ip')
                info = ((self._sockets.AF_INET, self._sockets.SOCK_STREAM, 0,
                         (endpoint, self._config.get('port', 8883))) if endpoint else
                        self._resolver.resolve(self._config['host'], self._config.get('port', 8883), now))
                if info is not None:
                    self._open(info)
                return
            if self._state == 'connecting':
                if now >= self._deadline:
                    self._fail('connect_timeout')
                    return
                for _, flags in self._poll.poll(0):
                    if flags & POLL_ERRORS:
                        self._fail('connect_failed')
                        return
                    if flags & POLL_OUT and self._tcp_ready():
                        self._wrap()
                        return
                return
            if self._state == 'mqtt_connect' and now >= self._deadline:
                self._fail('tls_or_connack_timeout')
                return
            if self._tx is not None and now >= self._tx_deadline:
                self._fail('write_timeout')
                return
            if self._ack_deadline and now >= self._ack_deadline:
                self._fail('ack_timeout')
                return
            if self._ping_deadline and now >= self._ping_deadline:
                self._fail('ping_timeout')
                return
            if self._subscribe_deadline and now >= self._subscribe_deadline:
                self._fail('subscription_timeout')
                return
            if self._reply_deadline and now >= self._reply_deadline:
                self._fail('command_reply_timeout')
                return
            if self.connected and self._commands:
                self._commands.drain(self._enqueue_reply)
            if self._state == 'connected' and self._tx is None:
                if self._command_pubacks:
                    self._queue(puback_packet(self._command_pubacks.pop(0)), 'command_puback')
                elif self._subscribe_id is not None and not self._subscribe_sent:
                    self._queue(subscribe_packet(self._command_topic, self._subscribe_id), 'subscribe')
                elif self._subscribe_id is None and self._reply_inflight is not None and not self._reply_deadline:
                    _, payload, packet_id, attempted = self._reply_inflight
                    self._queue(publish_packet(self._reply_topic, payload, packet_id, duplicate=attempted), 'command_reply')
                    self._reply_inflight[3] = True
                elif self._subscribe_id is None and self._reply_inflight is None and self._reply_queue:
                    identifier, payload = self._reply_queue.pop(0)
                    packet_id = self._packet_id()
                    self._reply_inflight = [identifier, payload, packet_id, True]
                    self._queue(publish_packet(self._reply_topic, payload, packet_id), 'command_reply')
                elif self._subscribe_id is None and self._pending is not None and not self._ack_deadline:
                    topic, payload, packet_id, attempted = self._pending
                    self._queue(publish_packet(topic, payload, packet_id, duplicate=attempted), 'publish')
                    self._pending[3] = True
                elif not self._ping_deadline and now - self._last_tx_at >= self._keepalive * 500:
                    self._queue(ping_packet(), 'ping')
            self._write_once()
            self._read_once()
        except ValueError as error:
            self._fail('tls_or_protocol_failed', error)
        except Exception as error:
            reason = _dns_failure_reason(error) if self._state == 'resolving' else None
            self._fail(reason or 'transport_failed', error)
