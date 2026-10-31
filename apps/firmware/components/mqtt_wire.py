"""Bounded MQTT 3.1.1 codec, usable on MicroPython without dependencies.

Command subscriptions are explicitly enabled; publisher-only callers stay strict.
Protocol errors contain fixed descriptions, never field values or payloads.
"""


MAX_PACKET_BYTES = 4096
MAX_PACKETS_PER_FEED = 16


def _integer(value, minimum, maximum, description):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(description)
    return value


def _u16(value):
    return bytes((value >> 8, value & 255))


def _text(value, allow_empty=False):
    if not isinstance(value, str) or (not value and not allow_empty) or len(value) > MAX_PACKET_BYTES:
        raise ValueError('Invalid MQTT text field')
    for character in value:
        code = ord(character)
        if (code == 0 or 0xD800 <= code <= 0xDFFF or
                0xFDD0 <= code <= 0xFDEF or code & 0xFFFF in (0xFFFE, 0xFFFF)):
            raise ValueError('Invalid MQTT UTF-8 field')
    try:
        data = value.encode('utf-8')
    except UnicodeError:
        raise ValueError('Invalid MQTT UTF-8 field')
    if len(data) > MAX_PACKET_BYTES:
        raise ValueError('MQTT field is too large')
    return data


def _binary(value):
    if isinstance(value, str):
        try:
            value = value.encode('utf-8')
        except UnicodeError:
            raise ValueError('Invalid MQTT binary field')
    if not isinstance(value, (bytes, bytearray)) or len(value) > MAX_PACKET_BYTES:
        raise ValueError('Invalid MQTT binary field')
    return bytes(value)


def _remaining_length(length):
    encoded = bytearray()
    while True:
        digit = length & 127
        length >>= 7
        encoded.append(digit | (128 if length else 0))
        if not length:
            return bytes(encoded)


def _check_packet_size(body_length):
    header_length = 1 + len(_remaining_length(body_length))
    if body_length + header_length > MAX_PACKET_BYTES:
        raise ValueError('MQTT packet is too large')


def _frame(header, body):
    _check_packet_size(len(body))
    return bytes((header,)) + _remaining_length(len(body)) + body


def connect_packet(client_id, username=None, password=None, keepalive=60, clean_session=True):
    """Build CONNECT without a will; passwords are MQTT binary fields."""
    _integer(keepalive, 0, 65535, 'Invalid MQTT keepalive')
    if type(clean_session) is not bool:
        raise ValueError('Invalid MQTT clean-session flag')
    if password is not None and username is None:
        raise ValueError('MQTT password requires a username')
    identity = _text(client_id)
    user = _text(username, True) if username is not None else None
    secret = _binary(password) if password is not None else None
    body_length = 12 + len(identity)
    if user is not None:
        body_length += 2 + len(user)
    if secret is not None:
        body_length += 2 + len(secret)
    _check_packet_size(body_length)
    flags = (2 if clean_session else 0) | (128 if user is not None else 0) | (64 if secret is not None else 0)
    body = b'\x00\x04MQTT\x04' + bytes((flags,)) + _u16(keepalive) + _u16(len(identity)) + identity
    if user is not None:
        body += _u16(len(user)) + user
    if secret is not None:
        body += _u16(len(secret)) + secret
    return _frame(0x10, body)


def publish_packet(topic, payload, packet_id, duplicate=False):
    """Build one non-retained QoS 1 PUBLISH using the caller's exact bytes."""
    _integer(packet_id, 1, 65535, 'Invalid MQTT packet identifier')
    if type(duplicate) is not bool:
        raise ValueError('Invalid MQTT duplicate flag')
    name = _text(topic)
    if '+' in topic or '#' in topic:
        raise ValueError('MQTT publish topic cannot contain wildcards')
    if not isinstance(payload, (bytes, bytearray)):
        raise ValueError('MQTT payload must be bytes')
    _check_packet_size(4 + len(name) + len(payload))
    body = _u16(len(name)) + name + _u16(packet_id) + bytes(payload)
    return _frame(0x3A if duplicate else 0x32, body)


def ping_packet():
    return b'\xc0\x00'


def subscribe_packet(topic, packet_id):
    """Subscribe to one exact topic at QoS 1, without wildcard authority."""
    _integer(packet_id, 1, 65535, 'Invalid MQTT packet identifier')
    name = _text(topic)
    if '+' in topic or '#' in topic:
        raise ValueError('MQTT command topic cannot contain wildcards')
    return _frame(0x82, _u16(packet_id) + _u16(len(name)) + name + b'\x01')


def puback_packet(packet_id):
    _integer(packet_id, 1, 65535, 'Invalid MQTT packet identifier')
    return b'\x40\x02' + _u16(packet_id)


def decode_publish(flags, body):
    """Validate an incoming QoS 0/1 PUBLISH; keep payload bytes undecoded."""
    qos = (flags >> 1) & 3
    if qos not in (0, 1) or (qos == 0 and flags & 8) or len(body) < 2:
        raise ValueError('Invalid MQTT command PUBLISH')
    length = (body[0] << 8) | body[1]
    end = 2 + length
    if not length or end + (2 if qos else 0) > len(body):
        raise ValueError('Invalid MQTT command topic')
    try:
        topic = str(body[2:end], 'utf-8')
    except (UnicodeError, ValueError):
        raise ValueError('Invalid MQTT command topic')
    _text(topic)
    if '+' in topic or '#' in topic:
        raise ValueError('Invalid MQTT command topic')
    packet_id = None
    if qos:
        packet_id = (body[end] << 8) | body[end + 1]
        _integer(packet_id, 1, 65535, 'Invalid MQTT packet identifier')
        end += 2
    return topic, body[end:], packet_id, qos, bool(flags & 1)


def disconnect_packet():
    return b'\xe0\x00'


class PacketReader:
    """Incrementally decode CONNACK, PUBACK and PINGRESP with bounded storage.

    max_packet_bytes includes the fixed header. More than 16 packets in one
    feed call is rejected as a flood for this publish-only client. A protocol
    error clears all partial state; callers should close the connection.
    """

    def __init__(self, max_packet_bytes=MAX_PACKET_BYTES, allow_commands=False):
        self._limit = _integer(max_packet_bytes, 2, MAX_PACKET_BYTES, 'Invalid MQTT packet limit')
        if type(allow_commands) is not bool:
            raise ValueError('Invalid MQTT command parser flag')
        self._allow_commands = allow_commands
        self.reset()

    def reset(self):
        self._header = None
        self._length = None
        self._length_value = 0
        self._length_digits = 0
        self._body = bytearray()

    def _complete(self):
        packet_type = self._header >> 4
        body = bytes(self._body)
        if packet_type == 2:
            if body[0] & 0xFE or body[1] > 5 or (body[1] and body[0]):
                raise ValueError('Invalid MQTT CONNACK')
            if body[1]:
                raise ValueError('MQTT connection refused')
        elif packet_type == 4 and not (body[0] or body[1]):
            raise ValueError('Invalid MQTT PUBACK identifier')
        elif packet_type == 9:
            if not (body[0] or body[1]) or body[2] not in (0, 1, 2, 128):
                raise ValueError('Invalid MQTT SUBACK')
        elif packet_type == 3:
            decode_publish(self._header & 15, body)
        packet = (packet_type, self._header & 15, body)
        self.reset()
        return packet

    def feed(self, data):
        packets = []
        try:
            if not isinstance(data, (bytes, bytearray)):
                raise ValueError('MQTT input must be bytes')
            for byte in data:
                if self._header is None:
                    if (byte not in (0x20, 0x40, 0xD0) and
                            not (self._allow_commands and byte in (0x30, 0x31, 0x32, 0x33, 0x3A, 0x3B, 0x90))):
                        raise ValueError('Unexpected MQTT packet header')
                    self._header = byte
                elif self._length is None:
                    self._length_value += (byte & 127) << (7 * self._length_digits)
                    self._length_digits += 1
                    if self._length_digits > 4 or (self._length_digits == 4 and byte & 128):
                        raise ValueError('Invalid MQTT remaining length')
                    if self._length_value + 1 + self._length_digits > self._limit:
                        raise ValueError('MQTT packet is too large')
                    if byte & 128:
                        continue
                    if self._length_digits > 1 and byte == 0:
                        raise ValueError('Invalid MQTT remaining length')
                    self._length = self._length_value
                    packet_type = self._header >> 4
                    expected = 0 if self._header == 0xD0 else (3 if packet_type == 9 else 2)
                    if ((packet_type != 3 and self._length != expected) or
                            (packet_type == 3 and self._length < 3)):
                        raise ValueError('Invalid MQTT packet length')
                    if not self._length:
                        packets.append(self._complete())
                else:
                    self._body.append(byte)
                    if len(self._body) == self._length:
                        packets.append(self._complete())
                if len(packets) > MAX_PACKETS_PER_FEED:
                    raise ValueError('MQTT receive flood')
        except ValueError:
            self.reset()
            raise
        return packets
