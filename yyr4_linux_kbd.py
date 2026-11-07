#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""YYR4 CH340 serial-to-uinput daemon.

Edit the configuration and key mappings below. See README.md for setup,
runtime light commands, and the protocol.
"""

import fcntl
import hashlib
import logging
import math
import os
import secrets
import signal
import stat
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, closing, suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

import serial
from evdev import UInput, UInputError, ecodes
from serial.tools import list_ports

# Configuration

# Serial port path; None auto-detects the CH340 (1a86:7523).
DEVICE: str | None = None
# Print every raw event frame.
VERBOSE = False
# Named pipe for runtime light commands; None disables it. Its parent must
# exist. The daemon creates it with owner-only access and removes it on normal exit.
CONTROL_PIPE: str | None = str(
    Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    / "yyr4-control"
)

# Startup light settings. Each mode has a fixed color palette.
LIGHT_SWITCH = True
LIGHT_BRIGHTNESS = 50  # 0-100
LIGHT_MODE = 8  # 0-9
LIGHT_TIMEOUT = 10  # 0-255 idle minutes before light-off

# Seconds to wait before reconnecting after a lost port or failed handshake.
RETRY_DELAY = 5.0
# Seconds of silence before checking that the connected port is still present.
PRESENCE_INTERVAL = 5.0

VID, PID = 0x1A86, 0x7523
BAUD = 115200
SERIAL_TIMEOUT = 0.2
SERIAL_WRITE_TIMEOUT = 1.0
FRAME_LEN = 5
READ_SIZE = 64
CONTROL_READ_SIZE = 4096
MAX_COMMAND_LEN = 256
MODIFIER_KEYS = frozenset(
    {
        ecodes.KEY_LEFTCTRL,
        ecodes.KEY_RIGHTCTRL,
        ecodes.KEY_LEFTSHIFT,
        ecodes.KEY_RIGHTSHIFT,
        ecodes.KEY_LEFTALT,
        ecodes.KEY_RIGHTALT,
        ecodes.KEY_LEFTMETA,
        ecodes.KEY_RIGHTMETA,
    }
)
logger = logging.getLogger(__name__)

# Key mappings

# Physical layout: three rows of four keys, plus four knobs. L/R are rotation
# directions and P is a knob press.
# fmt: off
MATRIX_KEYS: dict[tuple[int, int], str] = {
    (0, 0): "A1",  (0, 1): "A2",  (0, 2): "A3",  (0, 3): "A4",
    (1, 0): "A5",  (1, 1): "A6",  (1, 2): "A7",  (1, 3): "A8",
    (2, 0): "A9",  (2, 1): "A10", (2, 2): "A11", (2, 3): "A12",
    (3, 0): "AL",  (3, 1): "AP",  (3, 2): "AR",
    (4, 0): "BL",  (4, 1): "BP",  (4, 2): "BR",
    (5, 0): "CL",  (5, 1): "CP",  (5, 2): "CR",
    (6, 0): "DL",  (6, 1): "DP",  (6, 2): "DR",
}
# fmt: on

# Key actions accept one code or a tuple/list (modifiers first) and follow down/up
# edges. Relative actions tick on down edges and accept optional modifiers.
# Use None to leave a control unassigned.
KeyCodes = tuple[int, ...] | list[int]
Action = (
    tuple[Literal["key"], int | KeyCodes]
    | tuple[Literal["rel"], int, int]
    | tuple[Literal["rel"], int, int, KeyCodes]
)
EVENT_CODES: dict[str, Action | None] = {
    "A1": ("key", ecodes.KEY_F13),
    "A2": ("key", ecodes.KEY_F14),
    "A3": ("key", ecodes.KEY_F15),
    "A4": ("key", ecodes.KEY_F16),
    "A5": ("key", ecodes.KEY_F17),
    "A6": ("key", ecodes.KEY_F18),
    "A7": ("key", ecodes.KEY_F19),
    "A8": ("key", ecodes.KEY_F20),
    "A9": ("key", ecodes.KEY_F21),
    "A10": ("key", ecodes.KEY_F22),
    "A11": ("key", ecodes.KEY_F23),
    "A12": ("key", ecodes.KEY_F24),
    "AL": ("key", ecodes.KEY_KPMINUS),
    "AR": ("key", ecodes.KEY_KPPLUS),
    "AP": ("key", ecodes.KEY_KPDOT),
    "BL": ("key", (ecodes.KEY_LEFTCTRL, ecodes.KEY_Z)),
    "BR": ("key", (ecodes.KEY_LEFTCTRL, ecodes.KEY_LEFTSHIFT, ecodes.KEY_Z)),
    "BP": ("key", (ecodes.KEY_LEFTCTRL, ecodes.KEY_Z)),
    "CL": ("key", ecodes.KEY_LEFTBRACE),
    "CR": ("key", ecodes.KEY_RIGHTBRACE),
    "CP": ("key", ecodes.KEY_F),
    "DL": ("key", (ecodes.KEY_LEFTSHIFT, ecodes.KEY_KP4)),
    "DR": ("key", (ecodes.KEY_LEFTSHIFT, ecodes.KEY_KP6)),
    "DP": ("key", (ecodes.KEY_LEFTSHIFT, ecodes.KEY_F)),
}


class DriverError(RuntimeError):
    """A device-discovery or protocol failure."""


@dataclass
class LightSettings:
    enabled: bool
    brightness: int
    mode: int
    timeout: int

    @classmethod
    def configured(cls) -> "LightSettings":
        return cls(LIGHT_SWITCH, LIGHT_BRIGHTNESS, LIGHT_MODE, LIGHT_TIMEOUT)

    def validate(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("LIGHT_SWITCH must be True or False")
        for name, value, maximum in (
            ("LIGHT_BRIGHTNESS", self.brightness, 100),
            ("LIGHT_MODE", self.mode, 9),
            ("LIGHT_TIMEOUT", self.timeout, 255),
        ):
            if not isinstance(value, int) or not 0 <= value <= maximum:
                raise ValueError(f"{name} must be an integer between 0 and {maximum}")

    def light_frame(self) -> bytes:
        return bytes([0x00, self.enabled, self.brightness, self.mode, 0x00])

    def timeout_frame(self) -> bytes:
        return bytes([0x04, self.timeout, 0x00, 0x00, 0x00])

    def command(self, command: str) -> bytes:
        """Apply one valid command and return its configuration frame."""
        words = command.split()
        if words in (["on"], ["off"]):
            field, value = "enabled", words[0] == "on"
        elif len(words) == 2 and words[0] in ("brightness", "mode", "timeout"):
            field, value = words[0], int(words[1], 0)
        else:
            raise ValueError(
                "expected on, off, brightness 0..100, mode 0..9, timeout 0..255"
            )
        updated = replace(self, **{field: value})
        updated.validate()
        setattr(self, field, value)
        return updated.timeout_frame() if field == "timeout" else updated.light_frame()


class ControlPipe:
    """A nonblocking FIFO, held open across board reconnects."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        try:
            os.mkfifo(self.path, mode=0o600)
        except FileExistsError:
            if not stat.S_ISFIFO(self.path.lstat().st_mode):
                raise ValueError(
                    f"CONTROL_PIPE already exists and is not a FIFO: {path}"
                ) from None
        self.fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            info = os.fstat(self.fd)
            if not stat.S_ISFIFO(info.st_mode):
                raise ValueError(f"CONTROL_PIPE is not a FIFO: {path}")
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.fd)
            raise
        self.identity = (info.st_dev, info.st_ino)
        self.buffer = bytearray()
        self.discarding = False

    def commands(self) -> Iterator[str]:
        """Read a bounded chunk and yield complete ASCII command lines."""
        try:
            self.buffer.extend(os.read(self.fd, CONTROL_READ_SIZE))
        except BlockingIOError:
            pass
        while b"\n" in self.buffer:
            line, _, remainder = self.buffer.partition(b"\n")
            self.buffer = remainder
            if self.discarding:
                self.discarding = False
                continue
            if len(line) > MAX_COMMAND_LEN:
                logger.warning(
                    "control command exceeds %d bytes; ignored", MAX_COMMAND_LEN
                )
                continue
            try:
                command = line.decode("ascii").strip()
            except UnicodeDecodeError:
                logger.warning("control command is not ASCII; ignored")
                continue
            if command:
                yield command
        if len(self.buffer) > MAX_COMMAND_LEN:
            logger.warning("control command exceeds %d bytes; ignored", MAX_COMMAND_LEN)
            self.buffer.clear()
            self.discarding = True

    def close(self) -> None:
        try:
            with suppress(FileNotFoundError):
                info = self.path.lstat()
                if (info.st_dev, info.st_ino) == self.identity:
                    self.path.unlink()
        finally:
            os.close(self.fd)


def poll_control(
    control: ControlPipe, lights: LightSettings, ser: serial.Serial | None = None
) -> None:
    """Apply commands to the desired state and, when connected, to the board."""
    for command in control.commands():
        try:
            frame = lights.command(command)
        except ValueError as error:
            logger.warning("invalid control command %r: %s", command, error)
            continue
        if ser is not None:
            ser.write(frame)
        logger.info("lights: %s", command)


def validate_mappings() -> None:
    """Check that every matrix control has a well-formed action or None."""
    if not isinstance(EVENT_CODES, dict):
        raise TypeError("EVENT_CODES must be a dictionary")
    names = set(MATRIX_KEYS.values())
    unused = EVENT_CODES.keys() - names
    if unused:
        logger.warning(
            "EVENT_CODES names not in MATRIX_KEYS: %s",
            ", ".join(sorted(map(repr, unused))),
        )
    missing = names - EVENT_CODES.keys()
    if missing:
        raise ValueError(
            f"EVENT_CODES missing entries for: {', '.join(sorted(missing))}; "
            "use None to disable a control"
        )
    for name, action in EVENT_CODES.items():
        entry = f"EVENT_CODES[{name!r}]"
        if action is None:
            continue
        if not isinstance(action, (tuple, list)) or not action:
            raise TypeError(f"{entry}: action must be a nonempty tuple/list or None")
        kind = action[0]
        if kind not in ("key", "rel"):
            raise ValueError(
                f"{entry}: unknown action kind {kind!r}; expected 'key' or 'rel'"
            )
        if kind == "key":
            if len(action) != 2:
                raise ValueError(f"{entry}: key action must have 2 items")
            keys = (action[1],) if isinstance(action[1], int) else action[1]
            if not isinstance(keys, (tuple, list)):
                raise TypeError(f"{entry}: key codes must be an integer or tuple/list")
            if not keys:
                raise ValueError(
                    f"{entry}: key action needs at least one code; use None to disable"
                )
        else:
            if len(action) not in (3, 4):
                raise ValueError(f"{entry}: rel action must have 3 or 4 items")
            if type(action[1]) is not int or type(action[2]) is not int:
                raise TypeError(f"{entry}: relative code and amount must be integers")
            if not 0 <= action[1] <= ecodes.REL_MAX:
                raise ValueError(
                    f"{entry}: relative code must be between 0 and {ecodes.REL_MAX}"
                )
            keys = action[3] if len(action) == 4 else ()
            if not isinstance(keys, (tuple, list)):
                raise TypeError(f"{entry}: modifiers must be a tuple/list")
        if any(type(code) is not int for code in keys):
            raise TypeError(f"{entry}: key codes and modifiers must be integers")
        if any(not 1 <= code <= ecodes.KEY_MAX for code in keys):
            raise ValueError(
                f"{entry}: key codes and modifiers must be between 1 and {ecodes.KEY_MAX}"
            )


def validate_configuration() -> None:
    """Reject invalid settings before entering the reconnect loop."""
    if DEVICE is not None and (not isinstance(DEVICE, str) or not DEVICE):
        raise ValueError("DEVICE must be a serial port path or None")
    if CONTROL_PIPE is not None and (
        not isinstance(CONTROL_PIPE, str) or not CONTROL_PIPE
    ):
        raise ValueError("CONTROL_PIPE must be a FIFO path or None")
    LightSettings.configured().validate()
    for name, value in (
        ("RETRY_DELAY", RETRY_DELAY),
        ("PRESENCE_INTERVAL", PRESENCE_INTERVAL),
    ):
        if (
            not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{name} must be a positive, finite number of seconds")
    validate_mappings()


def protocol_digest(data: bytes) -> bytes:
    """Hash concatenated decimal byte values for either handshake message."""
    encoded = "".join(str(value) for value in data).encode("ascii")
    return hashlib.sha256(encoded).digest()[:FRAME_LEN]


def make_challenge() -> bytes:
    return b"\x01" + secrets.token_bytes(FRAME_LEN - 1)


def find_port(previous: str | None = None) -> str:
    """Honor DEVICE, otherwise rotate through the currently connected CH340s."""
    if DEVICE is not None:
        return DEVICE
    ports = sorted(
        port.device
        for port in list_ports.comports()
        if (port.vid, port.pid) == (VID, PID)
    )
    if not ports:
        raise DriverError(
            "no CH340 (1a86:7523) serial port found; is the board plugged in?"
        )
    if previous is not None and previous in ports:
        return ports[(ports.index(previous) + 1) % len(ports)]
    return ports[0]


def handshake(ser: serial.Serial, retries: int = 5) -> None:
    """Authenticate the board, retrying incomplete or unexpected replies."""
    for attempt in range(1, retries + 1):
        ser.reset_input_buffer()
        challenge = make_challenge()
        ser.write(challenge)
        reply = ser.read(2 * FRAME_LEN)
        if len(reply) == 2 * FRAME_LEN and reply[:FRAME_LEN] == protocol_digest(
            challenge
        ):
            password = protocol_digest(reply[FRAME_LEN:])
            ser.write(password)
            time.sleep(0.05)
            ser.reset_input_buffer()
            logger.info("handshake ok (attempt %d)", attempt)
            return
        logger.debug(
            "handshake attempt %d failed: %s", attempt, reply.hex() or "nothing"
        )
        time.sleep(0.1)
    raise DriverError("handshake timed out; is the board running the YYR4 firmware?")


def send_init(ser: serial.Serial, lights: LightSettings) -> None:
    """Send the configured light-settings and light-timeout frames."""
    ser.write(lights.light_frame())
    time.sleep(0.02)
    ser.write(lights.timeout_frame())
    time.sleep(0.02)
    ser.reset_input_buffer()


def frames(buffer: bytearray) -> Iterator[bytes]:
    """Consume complete event frames, retaining a partial trailing frame.

    Both accepted type bytes (00 and 01) are retained. Drop one byte on invalid
    headers, flags or terminators to search for the next frame.
    """
    while len(buffer) >= FRAME_LEN:
        if buffer[0] in (0, 1) and buffer[1] in (0, 1) and buffer[4] == 0:
            frame = bytes(buffer[:FRAME_LEN])
            del buffer[:FRAME_LEN]
            yield frame
        else:
            del buffer[0]


def read_frames(
    ser: serial.Serial,
    presence_interval: float = PRESENCE_INTERVAL,
    on_poll: Callable[[], None] | None = None,
) -> Iterator[bytes]:
    """Frame the stream and check the connected port after prolonged silence."""
    connected_path = Path(ser.port).resolve()
    buffer = bytearray()
    last_seen = time.monotonic()
    while True:
        if on_poll is not None:
            on_poll()
        # Wait for one byte when idle, then drain queued data without waiting
        # for a full READ_SIZE buffer or a serial timeout on every key press.
        chunk = ser.read(min(ser.in_waiting or 1, READ_SIZE))
        if chunk:
            last_seen = time.monotonic()
            buffer.extend(chunk)
            yield from frames(buffer)
        elif time.monotonic() - last_seen >= presence_interval:
            if not any(
                Path(port.device).resolve() == connected_path
                for port in list_ports.comports()
            ):
                raise DriverError(f"serial port disappeared: {ser.port}")
            last_seen = time.monotonic()


def action_keys(action: Action) -> tuple[int, ...]:
    """Return a shortcut's keys or a relative action's temporary modifiers."""
    if action[0] == "key":
        keys = (action[1],) if isinstance(action[1], int) else action[1]
    else:
        keys = action[3] if len(action) == 4 else ()
    return tuple(dict.fromkeys(keys))


class Keyboard:
    """Translate physical controls into a virtual device's input events."""

    def __init__(self) -> None:
        actions = [action for action in EVENT_CODES.values() if action is not None]
        capabilities = {
            ecodes.EV_KEY: sorted(
                {code for action in actions for code in action_keys(action)}
            ),
            ecodes.EV_REL: sorted(
                {action[1] for action in actions if action[0] == "rel"}
            ),
        }
        capabilities = {
            event_type: codes for event_type, codes in capabilities.items() if codes
        }
        self.ui = UInput(
            capabilities, name="YYR4 Virtual Keyboard", vendor=VID, product=PID
        )
        self.down: dict[str, tuple[int, ...]] = {}

    def _held_keys(self) -> tuple[int, ...]:
        return tuple(
            dict.fromkeys(code for keys in self.down.values() for code in keys)
        )

    def _write_keys(self, keys: tuple[int, ...], value: int) -> None:
        if keys:
            for code in keys:
                self.ui.write(ecodes.EV_KEY, code, value)
            self.ui.syn()

    def emit(self, name: str, pressed: bool) -> None:
        if not pressed:
            released = self.down.pop(name, None)
            if released is not None:
                held = self._held_keys()
                self._write_keys(
                    tuple(code for code in reversed(released) if code not in held), 0
                )
            return
        if name in self.down:
            return
        action = EVENT_CODES.get(name)
        if action is None:
            return
        keys = action_keys(action)
        held = self._held_keys()
        if action[0] == "rel":
            temporary = tuple(code for code in keys if code not in held)
            try:
                self._write_keys(temporary, 1)
                self.ui.write(ecodes.EV_REL, action[1], action[2])
                self.ui.syn()
            finally:
                self._write_keys(tuple(reversed(temporary)), 0)
            return
        # Retrigger shared non-modifiers so a second control gets a fresh press.
        # Keep shared modifiers held until their last control is released.
        self.down[name] = keys
        retrigger = tuple(
            code for code in keys if code in held and code not in MODIFIER_KEYS
        )
        self._write_keys(tuple(reversed(retrigger)), 0)
        self._write_keys(
            tuple(code for code in keys if code not in held or code in retrigger), 1
        )

    def release_all(self) -> None:
        """Release held keys at session end or daemon shutdown."""
        if self.down:
            self._write_keys(tuple(reversed(self._held_keys())), 0)
            self.down.clear()

    def close(self) -> None:
        try:
            self.release_all()
        finally:
            self.ui.close()


def handle_event(keyboard: Keyboard, frame: bytes) -> None:
    _, flag, row, column, _ = frame
    name = MATRIX_KEYS.get((row, column))
    if name is None:
        logger.warning("unmapped cell %d,%d: %s", row, column, frame.hex())
        return
    keyboard.emit(name, bool(flag))


def session(
    ser: serial.Serial,
    keyboard: Keyboard,
    lights: LightSettings,
    control: ControlPipe | None = None,
) -> None:
    """Read an initialized board's events, releasing keys when the session ends."""
    try:
        logger.info("ready")
        on_poll = (
            None if control is None else lambda: poll_control(control, lights, ser)
        )
        for frame in read_frames(ser, PRESENCE_INTERVAL, on_poll):
            logger.debug("<< %s", frame.hex())
            try:
                handle_event(keyboard, frame)
            except OSError as error:
                raise UInputError(
                    f"cannot write virtual keyboard events: {error}"
                ) from error
    finally:
        try:
            keyboard.release_all()
        except OSError as error:
            raise UInputError(
                f"cannot release virtual keyboard keys: {error}"
            ) from error


def run(control: ControlPipe | None = None) -> None:
    """Initialize the board, create uinput once, and retain it across reconnects."""
    lights = LightSettings.configured()
    keyboard = None
    previous_port = None
    with ExitStack() as stack:
        while True:
            try:
                if control is not None:
                    poll_control(control, lights)
                port = find_port(previous_port)
                previous_port = port
                # Bind this session to the target even if a by-id alias changes.
                port = str(Path(port).resolve())
                logger.info("opening %s at %d baud", port, BAUD)
                with closing(
                    serial.Serial(
                        port,
                        BAUD,
                        timeout=SERIAL_TIMEOUT,
                        write_timeout=SERIAL_WRITE_TIMEOUT,
                        exclusive=True,
                    )
                ) as ser:
                    handshake(ser)
                    send_init(ser, lights)
                    if keyboard is None:
                        try:
                            keyboard = stack.enter_context(closing(Keyboard()))
                        except (OSError, UInputError) as error:
                            raise UInputError(
                                f"cannot create virtual keyboard (/dev/uinput): {error}"
                            ) from error
                    session(ser, keyboard, lights, control)
            except (DriverError, OSError) as error:
                logger.warning("%s; retrying in %gs", error, RETRY_DELAY)
                time.sleep(RETRY_DELAY)


def main() -> int:
    logging.basicConfig(
        level=logging.DEBUG if VERBOSE else logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
    )
    try:
        validate_configuration()
    except (TypeError, ValueError) as error:
        logger.error("invalid configuration: %s", error)
        return 1
    try:
        with ExitStack() as stack:
            previous_handler = signal.signal(signal.SIGTERM, signal.default_int_handler)
            stack.callback(signal.signal, signal.SIGTERM, previous_handler)
            control = None
            if CONTROL_PIPE is not None:
                try:
                    control = stack.enter_context(closing(ControlPipe(CONTROL_PIPE)))
                except (OSError, ValueError) as error:
                    logger.error(
                        "control pipe error (CONTROL_PIPE=%r): %s", CONTROL_PIPE, error
                    )
                    return 1
                logger.info("control pipe: %s", CONTROL_PIPE)
            run(control)
    except KeyboardInterrupt:
        logger.info("stopped")
    except (OSError, UInputError, ValueError) as error:
        logger.error("fatal error: %s", error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
