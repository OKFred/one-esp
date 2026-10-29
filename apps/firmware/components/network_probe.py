"""A polled TCP-connect probe for MicroPython, without application traffic.

DNS resolution is synchronous on ESP32 and is cached until the target changes.
The clock callback must return monotonically increasing milliseconds (for
example DebugService.now), rather than wrapping raw ticks_ms values.
"""

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


CONNECT_TIMEOUT_MS = 2000
POLL_OUT = getattr(select, 'POLLOUT', 4)
POLL_ERRORS = (getattr(select, 'POLLERR', 8) | getattr(select, 'POLLHUP', 16) |
               getattr(select, 'POLLNVAL', 32))
PENDING_ERRORS = (getattr(errno, 'EINPROGRESS', 115),
                  getattr(errno, 'EALREADY', 114),
                  getattr(errno, 'EAGAIN', 11),
                  getattr(errno, 'EWOULDBLOCK', 11))
# ESP32's reduced errno module omits EISCONN; its newlib value is 127,
# whereas the Linux/internal errno fallback is 106 (an ESP32 address error).
ALREADY_CONNECTED = getattr(errno, 'EISCONN', 127 if sys.platform == 'esp32' else 106)


def _error_number(error):
    number = getattr(error, 'errno', None)
    return number if number is not None else (error.args[0] if error.args else None)


class NetworkProbe:
    def __init__(self, now_ms, socket_module=None, poll_factory=None):
        self._now = now_ms
        self._sockets = socket_module if socket_module is not None else socket
        self._poll_factory = poll_factory if poll_factory is not None else select.poll
        self._socket = None
        self._poll = None
        self._cached_target = None
        self._cached_info = None
        self._started_at = 0
        self._connect_started = 0
        self.busy = False

    def cancel(self):
        """Release an active probe without producing a sample; retain DNS cache."""
        active_socket, poller = self._socket, self._poll
        self._socket = None
        self._poll = None
        self.busy = False
        if poller is not None and active_socket is not None:
            try:
                poller.unregister(active_socket)
            except Exception:
                pass
        if active_socket is not None:
            try:
                active_socket.close()
            except Exception:
                pass

    def _finish(self, ok, completed_at=None):
        completed_at = self._now() if completed_at is None else completed_at
        result = {'ok': ok,
                  'ping': max(0, completed_at - self._connect_started) if ok else None,
                  'startedAtMs': self._started_at, 'completedAtMs': completed_at}
        self.cancel()
        return result

    def _connect(self):
        try:
            self._socket.connect(self._cached_info[-1])
            return True
        except OSError as error:
            number = _error_number(error)
            if number == ALREADY_CONNECTED:
                return True
            if number in PENDING_ERRORS:
                return None
            return False

    def start(self, host, port):
        """Start a probe, replacing an active one; return an immediate result or None."""
        self.cancel()
        self._started_at = self._now()
        self.busy = True
        try:
            target = (host, port)
            if target != self._cached_target:
                self._cached_target = target
                self._cached_info = None
            if self._cached_info is None:
                address = self._sockets.getaddrinfo(
                    host, port, 0, self._sockets.SOCK_STREAM)[0]
                self._cached_info = (address[0], address[1], address[2], address[-1])
            self._socket = self._sockets.socket(*self._cached_info[:3])
            self._socket.setblocking(False)
            # Keep connection latency separate from blocking DNS work.
            self._connect_started = self._now()
            connected = self._connect()
            if connected is not None:
                return self._finish(connected)
            self._poll = self._poll_factory()
            self._poll.register(self._socket, POLL_OUT | POLL_ERRORS)
        except Exception:
            return self._finish(False)
        return None

    def tick(self):
        """Poll once without waiting; return a completed result or None."""
        if not self.busy:
            return None
        now = self._now()
        if now - self._connect_started >= CONNECT_TIMEOUT_MS:
            return self._finish(False, now)
        try:
            for event in self._poll.poll(0):
                flags = event[1]
                if flags & POLL_ERRORS:
                    return self._finish(False)
                if flags & POLL_OUT:
                    # ESP32 v1.22 has neither getsockopt nor getpeername. Writable
                    # may also mean refused: repeat connect to the numeric address
                    # and require success/EISCONN, without writing any payload.
                    connected = self._connect()
                    if connected is not None:
                        return self._finish(connected)
        except Exception:
            return self._finish(False)
        return None
