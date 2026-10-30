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

from components.mqtt_wire import connect_packet, publish_packet, ping_packet, PacketReader


DNS_TIMEOUT_MS = 15000
DNS_CACHE_MS = 300000
CONNECT_TIMEOUT_MS = 10000
SESSION_TIMEOUT_MS = 15000
ACK_TIMEOUT_MS = 15000
PING_TIMEOUT_MS = 10000
MAX_BACKOFF_MS = 60000
IO_BYTES_PER_TICK = 256
MAX_PAYLOAD_BYTES = 2048
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
                                  'Invalid MQTT packet length', 'MQTT receive flood'):
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
        try:
            rows = self._sockets.getaddrinfo(target[0], target[1],
                                            self._sockets.AF_INET, self._sockets.SOCK_STREAM)
            for row in rows:
                if _ipv4(row[-1][0]) and row[-1][0] != '0.0.0.0':
                    info = (row[0], row[1], row[2], (row[-1][0], target[1]))
                    break
        except Exception:
            pass
        self._result = (generation, target, info)
        self._busy = False

    def resolve(self, host, port, now):
        target = (host, port)
        if self._cache is not None and self._cache[0] == target and now < self._cache_until:
            return self._cache[1]
        result, self._result = self._result, None
        if result is not None and result[0] == self._generation and result[1] == target:
            if result[2] is None:
                raise OSError('DNS resolution failed')
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
        except Exception:
            self._busy = False
            raise OSError('DNS worker unavailable')
        return None


class MqttTransport:
    def __init__(self, config, now_ms, socket_module=None, ssl_module=None,
                 resolver=None, poll_factory=None, collect=None):
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
        self._keepalive = config.get('keepalive', 60)
        if isinstance(self._keepalive, bool) or not isinstance(self._keepalive, int) or not 10 <= self._keepalive <= 600:
            raise ValueError('Invalid MQTT keepalive')
        # Validate/encode once. Never log or expose this credential-bearing frame.
        self._connect_frame = connect_packet(config['client_id'], config['username'],
                                             config['password'], self._keepalive, clean_session=True)
        self._resolver = resolver if resolver is not None else ThreadResolver(self._sockets)
        self._poll_factory = poll_factory if poll_factory is not None else select.poll
        self._reader = PacketReader()
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
        if use_context:
            # Keep the parsed trust chain across sessions. Re-parsing it while
            # reconnecting would create another certificate allocation peak.
            # Advertised context support must succeed; errors never downgrade.
            context = context_type(context_protocol)
            context.verify_mode = self._ssl.CERT_REQUIRED
            if context.verify_mode != self._ssl.CERT_REQUIRED:
                raise ValueError('Verified TLS context is unavailable')
            context.load_verify_locations(cadata=ca_data)
            self._ssl_context = context

    def publish(self, topic, payload):
        """Accept one immutable payload; keep it through reconnect until PUBACK."""
        if self._pending is not None or self._acked:
            return False
        if not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_PAYLOAD_BYTES:
            raise ValueError('Invalid MQTT telemetry payload')
        packet_id = self._next_packet_id % 65535 + 1
        # Validate topic and size before accepting responsibility for this message.
        publish_packet(topic, payload, packet_id)
        self._next_packet_id = packet_id
        self._pending = [topic, payload, packet_id, False]
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
                'nextRetryInMs': max(0, self._retry_at - self._now())}

    def _drop_socket(self):
        raw, stream, poller = self._raw, self._stream, self._poll
        self._raw = self._stream = self._poll = None
        self._tx = None
        self._tx_offset = 0
        self._tx_kind = None
        self._ack_deadline = self._ping_deadline = 0
        self._reader.reset()
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
        if self._ssl_context is not None:
            self._stream = self._ssl_context.wrap_socket(
                self._raw, server_hostname=self._server_hostname,
                do_handshake_on_connect=False)
        else:
            self._stream = self._ssl.wrap_socket(
                self._raw, cert_reqs=self._ssl.CERT_REQUIRED,
                cadata=self._config['ca_data'], server_hostname=self._server_hostname,
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
        for packet_type, _, body in self._reader.feed(data):
            if packet_type == 2:
                self._operation = 'connack'
                if self._state != 'mqtt_connect' or body[0] != 0 or self._tx is not None:
                    raise ValueError('Unexpected MQTT CONNACK')
                self._state = 'connected'
                self._last_error = None
                self._connections += 1
            elif packet_type == 4:
                packet_id = (body[0] << 8) | body[1]
                if (self._state == 'connected' and self._pending is not None and
                        self._ack_deadline and packet_id == self._pending[2]):
                    self._pending = None
                    self._ack_deadline = 0
                    self._acked = True
                    self._ack_count += 1
                    self._attempt = 0
            elif packet_type == 13:
                if self._state != 'connected':
                    raise ValueError('Unexpected MQTT PINGRESP')
                self._ping_deadline = 0
                self._attempt = 0

    def tick(self, connected, time_ready):
        """Do at most one TLS write and one TLS read; never wait for an ACK."""
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
            if self._state == 'connected' and self._tx is None:
                if self._pending is not None and not self._ack_deadline:
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
            self._fail('transport_failed', error)
