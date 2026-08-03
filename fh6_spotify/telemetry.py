import math
import struct
import socket as _socket
import time
def parse_is_race_on(data: bytes) -> bool | None:
    """Return True/False for IsRaceOn (int32 at offset 0), or None if packet too short."""
    if len(data) < 4:
        return
    else:
        is_race_on, = struct.unpack_from('<i', data, 0)
        return is_race_on == 1
def parse_speed(data: bytes) -> float | None:
    """Speed (m/s) = magnitude of Velocity X/Y/Z floats at offsets 32/36/40.\n\n    These live in the Forza \"sled\" block, unchanged by FH6\'s added fields.\n    """
    if len(data) < 44:
        return
    else:
        vx, vy, vz = struct.unpack_from('<fff', data, 32)
        return math.sqrt(vx * vx + vy * vy + vz * vz)
def parse_accel(data: bytes) -> int | None:
    """Throttle pedal (u8 0-255) from the dash block, offset 315 on the\n    Horizon layout (Motorsport\'s 303 + the +12 Horizon shift; gear at 319\n    from the same block is verified in-game). None for short/sled packets."""
    if len(data) < 316:
        return
    else:
        return data[315]
def parse_position(data: bytes):
    """Car world position (PositionX/Y/Z, floats) from the Forza Data Out \"Dash\"\n    format. Returns (x, y, z) in metres, or None when the packet is the shorter\n    \"Sled\" format (no position) or too short.\n\n    Offset is 244/248/252, NOT the 232 used by Motorsport - Forza Horizon shifts\n    the dash block by +12 bytes. VERIFIED on FH6 via scripts/telemetry_probe.py:\n    X/Z track movement, Y is altitude (~constant on flat ground), and 256 lines up\n    with the dash Speed field, confirming the X/Y/Z/Speed/Power/Torque order.\n    """
    if len(data) < 256:
        return
    else:
        x, y, z = struct.unpack_from('<fff', data, 244)
        return (x, y, z)
class TelemetryListener:
    def __init__(self, sock=None, host='127.0.0.1', port=5300, forward=''):
        if sock is None:
            sock = self._open(host, port)
        self._sock = sock
        self._fwd_addr = None
        self._fwd_sock = None
        if forward:
            try:
                fhost, fport = ('127.0.0.1', forward)
                if ':' in str(forward):
                    fhost, fport = str(forward).rsplit(':', 1)
                fport = int(fport)
                local = fhost in ['', '127.0.0.1', 'localhost', '0.0.0.0']
                if fport == port and local:
                    print(f'  telemetry: forward port {fport} = own listen port, forwarding disabled (would loop)')
                else:
                    self._fwd_addr = (fhost or '127.0.0.1', fport)
                    self._fwd_sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
                    self._fwd_sock.setblocking(False)
                    print(f'  telemetry: forwarding packets to {self._fwd_addr[0]}:{fport}')
            except Exception as exc:
                print(f'  telemetry: bad forward setting {forward!r}: {exc}')
                self._fwd_addr = None
                self._fwd_sock = None
        self.is_race_on = None
        self.speed = None
        self.accel = None
        self.position = None
        self.last_packet_time = None
        self.saw_race_off = False
        self._last_ts = None
        self._ts_change_time = None
    @staticmethod
    def _open(host: str, port: int, attempts: int=8, delay: float=0.4):
        """Bind the telemetry UDP port. SO_REUSEADDR + a short retry survive the\n        brief overlap during a restart handoff; if it still can\'t bind, return None\n        so the app runs without game telemetry instead of crashing."""
        for i in range(attempts):
            s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
            try:
                s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
                s.bind((host, port))
                s.setblocking(False)
                return s
            except OSError:
                s.close()
                if i < attempts - 1:
                    time.sleep(delay)
            else:
                pass
        print(f'  telemetry: port {port} unavailable; running without game telemetry')
    def poll(self) -> None:
        """Drain all pending packets, keep the most recent IsRaceOn + speed."""
        if self._sock is None:
            return
        else:
            if False:
                pass
            while True:
                try:
                    data = self._sock.recv(2048)
                except (BlockingIOError, OSError):
                    return None
                if self._fwd_sock is not None:
                    try:
                        self._fwd_sock.sendto(data, self._fwd_addr)
                    except OSError:
                        pass
                value = parse_is_race_on(data)
                if value is not None:
                    if value is False:
                        self.saw_race_off = True
                    self.is_race_on = value
                    self.speed = parse_speed(data)
                    self.accel = parse_accel(data)
                    self.position = parse_position(data)
                    now = time.monotonic()
                    self.last_packet_time = now
                    if len(data) >= 8:
                        ts = struct.unpack_from('<I', data, 4)[0]
                        if ts!= self._last_ts:
                            self._last_ts = ts
                            self._ts_change_time = now
    def live(self, now: float, window: float=0.5) -> bool:
        """True if the sim is actively running right now (TimestampMS advanced in\n        the last `window` seconds). False in menus/garage/pause/alt-tab (no fresh\n        frames) and when no telemetry has ever arrived."""
        return self._ts_change_time is not None and now - self._ts_change_time <= window
