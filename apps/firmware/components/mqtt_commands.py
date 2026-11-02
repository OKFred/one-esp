"""Small, expiring RS485 configuration inbox; no GPIO or credential commands.

The caller supplies verified broker routing and a recently synchronized Unix
clock. Responses and duplicate fingerprints are bounded; raw requests are not
kept, logged, or included in diagnostics. Persistence/revision CAS belongs to
the shared settings service, including replay rejection after this RAM cache.
"""

try:
    import ujson as json
except ImportError:
    import json
try:
    import uhashlib as hashlib
except ImportError:
    import hashlib


MAX_COMMAND_BYTES = 2048
MAX_REPLY_BYTES = 1024
MAX_CACHE_ENTRIES = 8
MAX_CACHE_BYTES = 4096
ENTRY_OVERHEAD = 256
MAX_EXPIRY_SECONDS = 300


def utc_seconds(value):
    """Parse UTC ISO time at second precision, independent of MicroPython's epoch."""
    if isinstance(value, str) and 22 <= len(value) <= 27 and value[19] == '.' and value[-1] == 'Z':
        fraction = value[20:-1]
        if any(char < '0' or char > '9' for char in fraction):
            raise ValueError('Invalid command expiration')
        value = value[:19] + 'Z'
    if (not isinstance(value, str) or len(value) != 20 or
            value[4] != '-' or value[7] != '-' or value[10] != 'T' or
            value[13] != ':' or value[16] != ':' or value[19] != 'Z'):
        raise ValueError('Invalid command expiration')
    digits = value[:4] + value[5:7] + value[8:10] + value[11:13] + value[14:16] + value[17:19]
    if any(char < '0' or char > '9' for char in digits):
        raise ValueError('Invalid command expiration')
    year, month, day = int(value[:4]), int(value[5:7]), int(value[8:10])
    hour, minute, second = int(value[11:13]), int(value[14:16]), int(value[17:19])
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    months = (31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    if (not 2024 <= year <= 2099 or not 1 <= month <= 12 or
            not 1 <= day <= months[month - 1] or hour > 23 or minute > 59 or second > 59):
        raise ValueError('Invalid command expiration')
    previous = year - 1
    days = (365 * (year - 1970) + previous // 4 - 1969 // 4 -
            previous // 100 + 1969 // 100 + previous // 400 - 1969 // 400)
    for length in months[:month - 1]:
        days += length
    return (days + day - 1) * 86400 + hour * 3600 + minute * 60 + second


def _identifier(value):
    return (isinstance(value, str) and 1 <= len(value) <= 64 and
            all('a' <= c <= 'z' or 'A' <= c <= 'Z' or '0' <= c <= '9' or c in '_-' for c in value))


class CommandProcessor:
    def __init__(self, handler, clock):
        self._handler = handler
        self._clock = clock
        self._cache = []
        self._bytes = 0
        self._received = 0
        self._rejected = 0
        self._duplicates = 0
        self._bucket_second = None
        self._bucket_count = 0

    def _find(self, identifier):
        for entry in self._cache:
            if entry['id'] == identifier:
                return entry
        return None

    def _room(self, needed, keep=None):
        while (self._bytes + needed > MAX_CACHE_BYTES or
               (keep is None and len(self._cache) >= MAX_CACHE_ENTRIES)):
            evict = next((entry for entry in self._cache if entry is not keep and
                          not entry['pending'] and not entry['dirty']), None)
            if evict is None:
                return False
            self._cache.remove(evict)
            self._bytes -= evict['cost']
        return True

    def _encode(self, identifier, response):
        if type(response) is not dict or type(response.get('ok')) is not bool:
            response = {'ok': False, 'error': 'Remote configuration failed'}
        value = {'v': 1, 'id': identifier, 'ok': response['ok']}
        if isinstance(response.get('error'), str):
            value['error'] = response['error'][:160]
        if type(response.get('result')) is dict:
            value['result'] = response['result']
        try:
            data = json.dumps(value).encode('utf-8')
            if len(data) <= MAX_REPLY_BYTES:
                return data
        except (TypeError, ValueError):
            pass
        return json.dumps({'v': 1, 'id': identifier, 'ok': False,
                           'error': 'Remote response unavailable'}).encode('utf-8')

    def _reply(self, entry, response, final=False):
        data = self._encode(entry['id'], response)
        extra = len(data) - len(entry['response'])
        if extra > 0 and not self._room(extra, entry):
            data = self._encode(entry['id'], {'ok': False, 'error': 'Remote response cache full'})
            extra = len(data) - len(entry['response'])
        entry['response'] = data
        entry['cost'] += extra
        self._bytes += extra
        result = response.get('result') if type(response) is dict else None
        stage = result.get('stage') if type(result) is dict else None
        entry['pending'] = not final and stage in ('testing', 'accepted', 'queued')
        entry['final'] = final or not entry['pending']
        entry['dirty'] = True

    def receive(self, payload, retained=False):
        self._received += 1
        if retained or not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_COMMAND_BYTES:
            self._rejected += 1
            return False
        try:
            command = json.loads(str(payload, 'utf-8'))
            if (type(command) is not dict or set(command) != {'v', 'id', 'command', 'args', 'expiresAtUtc'} or
                    type(command.get('v')) is not int or command['v'] != 1 or not _identifier(command.get('id')) or
                    command.get('command') not in ('sensor.config.get', 'sensor.config.set') or
                    type(command.get('args')) is not dict):
                raise ValueError('Invalid remote command')
            args = command['args']
            if command['command'] == 'sensor.config.get':
                if args:
                    raise ValueError('Invalid remote arguments')
            elif (set(args) != {'revision', 'values'} or type(args.get('revision')) is not int or
                  args['revision'] < 0 or type(args.get('values')) is not dict):
                raise ValueError('Invalid remote arguments')
            identifier = command['id']
            digest = hashlib.sha256(payload).digest()
            cached = self._find(identifier)
            if cached is not None:
                if cached['digest'] != digest:
                    self._rejected += 1
                    return False
                self._duplicates += 1
                cached['dirty'] = True
                return True
            try:
                now, synchronized = self._clock() if callable(self._clock) else (None, False)
            except Exception:
                raise ValueError('Remote clock is not ready')
            if type(now) is not int or synchronized is not True:
                raise ValueError('Remote clock is not ready')
            expires = utc_seconds(command['expiresAtUtc'])
            if not now < expires <= now + MAX_EXPIRY_SECONDS:
                raise ValueError('Remote command expired')
            if self._bucket_second != now:
                self._bucket_second, self._bucket_count = now, 0
            if self._bucket_count >= 4:
                raise ValueError('Remote command rate exceeded')
            cost = ENTRY_OVERHEAD + len(identifier) + len(digest)
            # Reserve room for a fixed failure reply before any setting changes.
            if not self._room(cost + 192):
                raise ValueError('Remote command cache full')
            entry = {'id': identifier, 'digest': digest, 'response': b'',
                     'cost': cost, 'pending': True, 'dirty': False, 'final': False}
            self._cache.append(entry)
            self._bytes += cost
            self._bucket_count += 1

            def finish(response):
                if self._find(identifier) is entry and not entry['final']:
                    self._reply(entry, response, final=True)

            try:
                response = self._handler(command, finish)
            except Exception:
                response = {'ok': False, 'error': 'Remote configuration failed'}
            if not entry['final']:
                self._reply(entry, response)
            return True
        except (ValueError, TypeError, KeyError, UnicodeError):
            self._rejected += 1
            return False

    def drain(self, enqueue):
        """Offer at most one cached response per tick; queue pressure loses no final ACK."""
        for entry in self._cache:
            if entry['dirty'] and entry['response']:
                if enqueue(entry['id'], entry['response']):
                    entry['dirty'] = False
                return

    def status(self):
        return {'received': self._received, 'rejected': self._rejected,
                'duplicates': self._duplicates, 'cached': len(self._cache),
                'pendingResponses': sum(1 for entry in self._cache if entry['dirty'])}
