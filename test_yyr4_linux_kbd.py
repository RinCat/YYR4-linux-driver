# SPDX-License-Identifier: MIT
"""Regression tests for the YYR4 protocol and reconnecting daemon."""

import hashlib
import os
import signal
import stat
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
import serial
from evdev import UInputError, ecodes

import yyr4_linux_kbd as driver


@pytest.fixture
def hardware(monkeypatch):
    """Replace hardware boundaries while keeping the real driver logic."""
    ser = Mock(port="/dev/fake", in_waiting=0)
    monkeypatch.setattr(driver.serial, "Serial", Mock(return_value=ser))
    monkeypatch.setattr(driver, "DEVICE", ser.port)
    monkeypatch.setattr(driver, "CONTROL_PIPE", None)
    for name, value in (
        ("LIGHT_SWITCH", True),
        ("LIGHT_BRIGHTNESS", 100),
        ("LIGHT_MODE", 4),
        ("LIGHT_TIMEOUT", 10),
    ):
        monkeypatch.setattr(driver, name, value)
    monkeypatch.setattr(
        driver.list_ports,
        "comports",
        Mock(return_value=[SimpleNamespace(device=ser.port)]),
    )
    ui = Mock()
    monkeypatch.setattr(driver, "UInput", Mock(return_value=ui))
    monkeypatch.setattr(driver.time, "sleep", Mock())
    return ser, ui


@pytest.fixture
def stop_after_retry(monkeypatch):
    """Simulate Ctrl-C during the first reconnect delay."""

    def sleep(seconds):
        if seconds == driver.RETRY_DELAY:
            raise KeyboardInterrupt

    monkeypatch.setattr(driver.time, "sleep", Mock(side_effect=sleep))


def test_handshake_uses_decimal_byte_encoding():
    data = bytes([1, 2, 3, 4, 5])
    assert driver.protocol_digest(data) == bytes.fromhex("5994471abb")
    assert (
        driver.protocol_digest(bytes([0, 10, 255]))
        == hashlib.sha256(b"010255").digest()[:5]
    )


@pytest.mark.parametrize("bad_reply", [b"", b"short", b"\x00" * 10])
def test_handshake_retries_before_sending_password(hardware, monkeypatch, bad_reply):
    ser, _ = hardware
    challenge = bytes([1, 2, 3, 4, 5])
    reply_random = bytes([6, 7, 8, 9, 10])
    reply = bytes.fromhex("5994471abb") + reply_random
    monkeypatch.setattr(driver, "make_challenge", lambda: challenge)
    ser.read.side_effect = [bad_reply, reply]

    driver.handshake(ser, retries=2)

    password = hashlib.sha256(b"678910").digest()[:5]
    assert ser.write.call_args_list == [
        call(challenge),
        call(challenge),
        call(password),
    ]
    assert ser.read.call_args_list == [call(10), call(10)]
    assert ser.reset_input_buffer.call_count == 3


def test_handshake_failure_closes_port_before_retry(hardware, stop_after_retry, caplog):
    ser, ui = hardware
    ser.read.return_value = b""

    assert driver.main() == 0

    assert ser.write.call_count == 5
    ser.close.assert_called_once()
    driver.UInput.assert_not_called()
    ui.close.assert_not_called()
    assert "handshake timed out" in caplog.text
    driver.time.sleep.assert_called_with(driver.RETRY_DELAY)


@pytest.mark.parametrize(
    "switch,brightness,mode,timeout,light_hex,timeout_hex",
    [
        (True, 100, 4, 10, "0001640400", "040a000000"),
        (False, 0, 0, 0, "0000000000", "0400000000"),
        (True, 100, 9, 255, "0001640900", "04ff000000"),
    ],
)
def test_initialization_wire_format(
    hardware, monkeypatch, switch, brightness, mode, timeout, light_hex, timeout_hex
):
    ser, _ = hardware
    for name, value in (
        ("LIGHT_SWITCH", switch),
        ("LIGHT_BRIGHTNESS", brightness),
        ("LIGHT_MODE", mode),
        ("LIGHT_TIMEOUT", timeout),
    ):
        monkeypatch.setattr(driver, name, value)

    driver.validate_configuration()
    driver.send_init(ser, driver.LightSettings.configured())

    assert ser.write.call_args_list == [
        call(bytes.fromhex(light_hex)),
        call(bytes.fromhex(timeout_hex)),
    ]
    ser.reset_input_buffer.assert_called_once()


@pytest.mark.parametrize("split", range(1, 5))
def test_frames_preserve_partial_reads_and_batched_events(split):
    first = bytes.fromhex("0001020300")
    second = bytes.fromhex("0100020300")
    buffer = bytearray(first[:split])

    assert list(driver.frames(buffer)) == []
    assert buffer == first[:split]
    buffer.extend(first[split:] + second)
    assert list(driver.frames(buffer)) == [first, second]
    assert not buffer


def test_frames_skip_noise_and_invalid_flags():
    valid = bytes.fromhex("0001020300")
    noise = b"\xff" * 7 + bytes.fromhex("0002ffff00") + b"\xff" * 3
    buffer = bytearray(noise + valid + valid[:3])

    assert list(driver.frames(buffer)) == [valid]
    assert buffer == valid[:3]


def test_reader_waits_for_one_byte_then_drains_queued_data(hardware):
    ser, _ = hardware
    frame = bytes.fromhex("0001000000")

    def read(size):
        if size == 1:
            ser.in_waiting = len(frame) - 1
            return frame[:1]
        return frame[1:]

    ser.read.side_effect = read

    assert next(driver.read_frames(ser)) == frame
    assert ser.read.call_args_list == [call(1), call(4)]


def test_keys_stay_held_across_idle_reads(hardware, monkeypatch):
    ser, ui = hardware
    monkeypatch.setattr(driver, "handshake", Mock())
    down = bytes.fromhex("0001000000")
    up = bytes.fromhex("0000000000")

    def after_idle_read():
        assert ui.write.call_args_list == [call(ecodes.EV_KEY, ecodes.KEY_F13, 1)]
        return up

    reads = iter([down[:2], b"", down[2:], b"", after_idle_read, KeyboardInterrupt()])

    def read(_size):
        value = next(reads)
        if isinstance(value, BaseException):
            raise value
        return value() if callable(value) else value

    ser.read.side_effect = read

    assert driver.main() == 0

    assert ui.write.call_args_list == [
        call(ecodes.EV_KEY, ecodes.KEY_F13, 1),
        call(ecodes.EV_KEY, ecodes.KEY_F13, 0),
    ]
    ui.close.assert_called_once()
    ser.close.assert_called_once()


def test_duplicate_key_edges_and_shared_bindings(hardware, monkeypatch):
    _, ui = hardware
    monkeypatch.setitem(driver.EVENT_CODES, "A2", driver.EVENT_CODES["A1"])
    keyboard = driver.Keyboard()

    keyboard.emit("A1", True)
    keyboard.emit("A1", True)
    keyboard.emit("A2", True)
    keyboard.emit("A1", False)
    assert ui.method_calls == [
        call.write(ecodes.EV_KEY, ecodes.KEY_F13, 1),
        call.syn(),
        call.write(ecodes.EV_KEY, ecodes.KEY_F13, 0),
        call.syn(),
        call.write(ecodes.EV_KEY, ecodes.KEY_F13, 1),
        call.syn(),
    ]
    keyboard.emit("A2", False)
    keyboard.emit("A2", False)
    keyboard.close()

    assert ui.write.call_args_list == [
        call(ecodes.EV_KEY, ecodes.KEY_F13, 1),
        call(ecodes.EV_KEY, ecodes.KEY_F13, 0),
        call(ecodes.EV_KEY, ecodes.KEY_F13, 1),
        call(ecodes.EV_KEY, ecodes.KEY_F13, 0),
    ]


@pytest.mark.parametrize(
    "modifier",
    [
        ecodes.KEY_LEFTCTRL,
        ecodes.KEY_RIGHTCTRL,
        ecodes.KEY_LEFTSHIFT,
        ecodes.KEY_RIGHTSHIFT,
        ecodes.KEY_LEFTALT,
        ecodes.KEY_RIGHTALT,
        ecodes.KEY_LEFTMETA,
        ecodes.KEY_RIGHTMETA,
    ],
)
def test_shared_modifier_bindings_do_not_retrigger(hardware, monkeypatch, modifier):
    _, ui = hardware
    for name in ("A1", "A2"):
        monkeypatch.setitem(driver.EVENT_CODES, name, ("key", modifier))
    keyboard = driver.Keyboard()

    keyboard.emit("A1", True)
    keyboard.emit("A2", True)
    keyboard.emit("A1", False)
    assert ui.write.call_args_list == [call(ecodes.EV_KEY, modifier, 1)]
    keyboard.emit("A2", False)
    keyboard.close()

    assert ui.write.call_args_list == [
        call(ecodes.EV_KEY, modifier, 1),
        call(ecodes.EV_KEY, modifier, 0),
    ]


@pytest.mark.parametrize("release_original_first", [False, True])
def test_redo_retriggers_z_while_undo_is_held(hardware, release_original_first):
    _, ui = hardware
    keyboard = driver.Keyboard()

    keyboard.emit("BP", True)
    keyboard.emit("BR", True)
    keyboard.emit("BR", True)  # Duplicate down must not trigger another redo.
    keyboard.emit("BR", False)
    keyboard.emit("BR", True)
    for name in ("BP", "BR") if release_original_first else ("BR", "BP"):
        keyboard.emit(name, False)
    keyboard.close()

    releases = (
        [ecodes.KEY_Z, ecodes.KEY_LEFTSHIFT, ecodes.KEY_LEFTCTRL]
        if release_original_first
        else [ecodes.KEY_LEFTSHIFT, ecodes.KEY_Z, ecodes.KEY_LEFTCTRL]
    )
    assert ui.write.call_args_list == [
        call(ecodes.EV_KEY, ecodes.KEY_LEFTCTRL, 1),
        call(ecodes.EV_KEY, ecodes.KEY_Z, 1),
        call(ecodes.EV_KEY, ecodes.KEY_Z, 0),
        call(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 1),
        call(ecodes.EV_KEY, ecodes.KEY_Z, 1),
        call(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 0),
        call(ecodes.EV_KEY, ecodes.KEY_Z, 0),
        call(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 1),
        call(ecodes.EV_KEY, ecodes.KEY_Z, 1),
        *(call(ecodes.EV_KEY, code, 0) for code in releases),
    ]


def test_knobs_tick_only_on_down_edges(hardware, monkeypatch):
    _, ui = hardware
    monkeypatch.setitem(driver.EVENT_CODES, "AL", ("rel", ecodes.REL_WHEEL, 1))
    monkeypatch.setitem(driver.EVENT_CODES, "AR", ("rel", ecodes.REL_WHEEL, -1))
    monkeypatch.setitem(driver.EVENT_CODES, "CL", None)
    keyboard = driver.Keyboard()

    for frame in ("0001030000", "0000030000", "0001030200", "0000030200"):
        driver.handle_event(keyboard, bytes.fromhex(frame))
    keyboard.emit("CL", True)
    keyboard.emit("CL", False)
    keyboard.close()

    assert ui.write.call_args_list == [
        call(ecodes.EV_REL, ecodes.REL_WHEEL, 1),
        call(ecodes.EV_REL, ecodes.REL_WHEEL, -1),
    ]
    assert ui.syn.call_count == 2


def test_default_device_has_only_keyboard_capabilities(hardware):
    driver.Keyboard().close()

    capabilities = driver.UInput.call_args.args[0]
    assert set(capabilities) == {ecodes.EV_KEY}
    assert all(code < ecodes.BTN_MISC for code in capabilities[ecodes.EV_KEY])


def test_knob_b_emits_undo_and_redo_shortcuts(hardware):
    _, ui = hardware
    keyboard = driver.Keyboard()

    for frame in ("0001040000", "0000040000", "0001040200", "0000040200"):
        driver.handle_event(keyboard, bytes.fromhex(frame))
    keyboard.close()

    assert ui.write.call_args_list == [
        call(ecodes.EV_KEY, ecodes.KEY_LEFTCTRL, 1),
        call(ecodes.EV_KEY, ecodes.KEY_Z, 1),
        call(ecodes.EV_KEY, ecodes.KEY_Z, 0),
        call(ecodes.EV_KEY, ecodes.KEY_LEFTCTRL, 0),
        call(ecodes.EV_KEY, ecodes.KEY_LEFTCTRL, 1),
        call(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 1),
        call(ecodes.EV_KEY, ecodes.KEY_Z, 1),
        call(ecodes.EV_KEY, ecodes.KEY_Z, 0),
        call(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 0),
        call(ecodes.EV_KEY, ecodes.KEY_LEFTCTRL, 0),
    ]


@pytest.mark.parametrize("container", [tuple, list])
def test_key_combination_presses_modifiers_first_and_releases_them_last(
    hardware, monkeypatch, container
):
    _, ui = hardware
    keys = container((ecodes.KEY_LEFTCTRL, ecodes.KEY_LEFTSHIFT, ecodes.KEY_Z))
    monkeypatch.setitem(driver.EVENT_CODES, "A1", ("key", keys))
    keyboard = driver.Keyboard()

    keyboard.emit("A1", True)
    keyboard.emit("A1", True)
    keyboard.emit("A1", False)
    keyboard.emit("A1", False)
    keyboard.close()

    assert ui.write.call_args_list == [
        call(ecodes.EV_KEY, ecodes.KEY_LEFTCTRL, 1),
        call(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 1),
        call(ecodes.EV_KEY, ecodes.KEY_Z, 1),
        call(ecodes.EV_KEY, ecodes.KEY_Z, 0),
        call(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 0),
        call(ecodes.EV_KEY, ecodes.KEY_LEFTCTRL, 0),
    ]
    assert ui.syn.call_count == 2
    capabilities = driver.UInput.call_args.args[0]
    assert set(keys) <= set(capabilities[ecodes.EV_KEY])


def test_overlapping_shortcuts_keep_shared_modifier_held(hardware, monkeypatch):
    _, ui = hardware
    monkeypatch.setitem(
        driver.EVENT_CODES, "A1", ("key", (ecodes.KEY_LEFTCTRL, ecodes.KEY_Z))
    )
    monkeypatch.setitem(
        driver.EVENT_CODES, "A2", ("key", (ecodes.KEY_LEFTCTRL, ecodes.KEY_Y))
    )
    keyboard = driver.Keyboard()

    keyboard.emit("A1", True)
    keyboard.emit("A2", True)
    keyboard.emit("A1", False)
    keyboard.emit("A2", False)
    keyboard.close()

    assert ui.write.call_args_list == [
        call(ecodes.EV_KEY, ecodes.KEY_LEFTCTRL, 1),
        call(ecodes.EV_KEY, ecodes.KEY_Z, 1),
        call(ecodes.EV_KEY, ecodes.KEY_Y, 1),
        call(ecodes.EV_KEY, ecodes.KEY_Z, 0),
        call(ecodes.EV_KEY, ecodes.KEY_Y, 0),
        call(ecodes.EV_KEY, ecodes.KEY_LEFTCTRL, 0),
    ]


@pytest.mark.parametrize("already_held", [False, True])
@pytest.mark.parametrize("container", [tuple, list])
def test_modified_wheel_preserves_held_modifiers(
    hardware, monkeypatch, already_held, container
):
    _, ui = hardware
    modifier = ecodes.KEY_LEFTCTRL
    monkeypatch.setitem(
        driver.EVENT_CODES, "AL", ("rel", ecodes.REL_WHEEL, 1, container((modifier,)))
    )
    if already_held:
        monkeypatch.setitem(driver.EVENT_CODES, "A1", ("key", modifier))
    keyboard = driver.Keyboard()
    if already_held:
        keyboard.emit("A1", True)
        ui.reset_mock()

    keyboard.emit("AL", True)
    keyboard.emit("AL", False)

    wheel = [call.write(ecodes.EV_REL, ecodes.REL_WHEEL, 1), call.syn()]
    expected = (
        wheel
        if already_held
        else [
            call.write(ecodes.EV_KEY, modifier, 1),
            call.syn(),
            *wheel,
            call.write(ecodes.EV_KEY, modifier, 0),
            call.syn(),
        ]
    )
    assert ui.method_calls == expected
    capabilities = driver.UInput.call_args.args[0]
    assert modifier in capabilities[ecodes.EV_KEY]
    assert ecodes.REL_WHEEL in capabilities[ecodes.EV_REL]
    keyboard.close()
    if already_held:
        assert ui.write.call_args_list[-1] == call(ecodes.EV_KEY, modifier, 0)


def test_modified_wheel_repeats_ticks_with_multiple_modifiers(hardware, monkeypatch):
    _, ui = hardware
    monkeypatch.setitem(
        driver.EVENT_CODES,
        "BL",
        ("rel", ecodes.REL_HWHEEL, -1, (ecodes.KEY_LEFTCTRL, ecodes.KEY_LEFTALT)),
    )
    keyboard = driver.Keyboard()

    keyboard.emit("BL", True)
    keyboard.emit("BL", True)
    keyboard.emit("BL", False)
    keyboard.close()

    tick = [
        call(ecodes.EV_KEY, ecodes.KEY_LEFTCTRL, 1),
        call(ecodes.EV_KEY, ecodes.KEY_LEFTALT, 1),
        call(ecodes.EV_REL, ecodes.REL_HWHEEL, -1),
        call(ecodes.EV_KEY, ecodes.KEY_LEFTALT, 0),
        call(ecodes.EV_KEY, ecodes.KEY_LEFTCTRL, 0),
    ]
    assert ui.write.call_args_list == tick * 2


def test_shutdown_releases_shortcut_keys_before_shared_modifiers(hardware, monkeypatch):
    _, ui = hardware
    monkeypatch.setitem(
        driver.EVENT_CODES, "A1", ("key", (ecodes.KEY_LEFTCTRL, ecodes.KEY_Z))
    )
    monkeypatch.setitem(
        driver.EVENT_CODES, "A2", ("key", (ecodes.KEY_LEFTCTRL, ecodes.KEY_Y))
    )
    keyboard = driver.Keyboard()
    keyboard.emit("A1", True)
    keyboard.emit("A2", True)
    ui.reset_mock()

    keyboard.close()

    assert ui.write.call_args_list == [
        call(ecodes.EV_KEY, ecodes.KEY_Y, 0),
        call(ecodes.EV_KEY, ecodes.KEY_Z, 0),
        call(ecodes.EV_KEY, ecodes.KEY_LEFTCTRL, 0),
    ]
    ui.close.assert_called_once()


def test_modified_wheel_releases_temporary_modifiers_when_write_fails(
    hardware, monkeypatch
):
    _, ui = hardware
    modifier = ecodes.KEY_LEFTALT
    monkeypatch.setitem(
        driver.EVENT_CODES, "AL", ("rel", ecodes.REL_WHEEL, 1, (modifier,))
    )
    keyboard = driver.Keyboard()

    def write(event_type, _code, _value):
        if event_type == ecodes.EV_REL:
            raise OSError("wheel write failed")

    ui.write.side_effect = write
    with pytest.raises(OSError, match="wheel write failed"):
        keyboard.emit("AL", True)
    keyboard.close()

    assert ui.write.call_args_list == [
        call(ecodes.EV_KEY, modifier, 1),
        call(ecodes.EV_REL, ecodes.REL_WHEEL, 1),
        call(ecodes.EV_KEY, modifier, 0),
    ]


def test_unmapped_cells_are_logged_without_input(hardware, caplog):
    _, ui = hardware
    keyboard = driver.Keyboard()

    driver.handle_event(keyboard, bytes.fromhex("0001ffff00"))
    keyboard.close()

    assert "unmapped cell 255,255: 0001ffff00" in caplog.text
    ui.write.assert_not_called()


def test_disconnect_releases_keys_and_closes_devices(
    hardware, monkeypatch, stop_after_retry, caplog
):
    ser, ui = hardware
    monkeypatch.setattr(driver, "handshake", Mock())
    ser.read.side_effect = [bytes.fromhex("0001000000"), OSError("disconnected")]

    assert driver.main() == 0

    assert ui.write.call_args_list == [
        call(ecodes.EV_KEY, ecodes.KEY_F13, 1),
        call(ecodes.EV_KEY, ecodes.KEY_F13, 0),
    ]
    assert ui.syn.call_count == 2
    ui.close.assert_called_once()
    ser.close.assert_called_once()
    assert "disconnected" in caplog.text
    driver.time.sleep.assert_called_with(driver.RETRY_DELAY)


def test_reconnect_rediscovers_port_and_keeps_virtual_keyboard(hardware, monkeypatch):
    first_ser, ui = hardware
    first_ser.port = "/dev/ttyUSB0"
    first_ser.read.side_effect = [bytes.fromhex("0001000000"), OSError("disconnected")]
    second_ser = Mock(port="/dev/ttyUSB1", in_waiting=0)
    reads = iter([bytes.fromhex("0001000100"), KeyboardInterrupt()])

    def second_read(_size):
        ui.close.assert_not_called()
        value = next(reads)
        if isinstance(value, BaseException):
            raise value
        return value

    second_ser.read.side_effect = second_read
    monkeypatch.setattr(driver, "DEVICE", None)
    driver.serial.Serial.side_effect = [first_ser, second_ser]
    driver.list_ports.comports.side_effect = [
        [SimpleNamespace(vid=driver.VID, pid=driver.PID, device=ser.port)]
        for ser in (first_ser, second_ser)
    ]
    handshake = Mock()
    monkeypatch.setattr(driver, "handshake", handshake)

    assert driver.main() == 0

    assert driver.serial.Serial.call_args_list == [
        call(
            "/dev/ttyUSB0",
            driver.BAUD,
            timeout=driver.SERIAL_TIMEOUT,
            write_timeout=driver.SERIAL_WRITE_TIMEOUT,
            exclusive=True,
        ),
        call(
            "/dev/ttyUSB1",
            driver.BAUD,
            timeout=driver.SERIAL_TIMEOUT,
            write_timeout=driver.SERIAL_WRITE_TIMEOUT,
            exclusive=True,
        ),
    ]
    assert handshake.call_args_list == [call(first_ser), call(second_ser)]
    for ser in (first_ser, second_ser):
        assert ser.write.call_args_list == [
            call(bytes.fromhex("0001640400")),
            call(bytes.fromhex("040a000000")),
        ]
        ser.close.assert_called_once()
    assert ui.write.call_args_list == [
        call(ecodes.EV_KEY, ecodes.KEY_F13, 1),
        call(ecodes.EV_KEY, ecodes.KEY_F13, 0),
        call(ecodes.EV_KEY, ecodes.KEY_F14, 1),
        call(ecodes.EV_KEY, ecodes.KEY_F14, 0),
    ]
    driver.UInput.assert_called_once()
    ui.close.assert_called_once()
    assert call(driver.RETRY_DELAY) in driver.time.sleep.call_args_list


@pytest.mark.parametrize("failure_stage", ["handshake", "init"])
def test_failed_first_connection_delays_virtual_keyboard_until_board_is_ready(
    hardware, monkeypatch, failure_stage
):
    first_ser, ui = hardware
    second_ser = Mock(port="/dev/fake", in_waiting=0)
    second_ser.read.side_effect = KeyboardInterrupt
    driver.serial.Serial.side_effect = [first_ser, second_ser]
    handshake = Mock()
    if failure_stage == "handshake":
        handshake.side_effect = [driver.DriverError("handshake failed"), None]
    else:
        first_ser.write.side_effect = OSError("initialization failed")
    monkeypatch.setattr(driver, "handshake", handshake)

    def create_ui(*_args, **_kwargs):
        first_ser.close.assert_called_once()
        assert handshake.call_count == 2
        assert second_ser.write.call_args_list == [
            call(bytes.fromhex("0001640400")),
            call(bytes.fromhex("040a000000")),
        ]
        second_ser.reset_input_buffer.assert_called_once()
        return ui

    driver.UInput.side_effect = create_ui

    assert driver.main() == 0

    first_ser.read.assert_not_called()
    second_ser.close.assert_called_once()
    driver.UInput.assert_called_once()
    ui.close.assert_called_once()
    assert driver.time.sleep.call_args_list.count(call(driver.RETRY_DELAY)) == 1


@pytest.mark.parametrize("failure_stage", ["open", "handshake", "init"])
def test_auto_detection_tries_next_ch340_after_failure(
    hardware, monkeypatch, failure_stage
):
    first_ser, ui = hardware
    first_ser.port = "/dev/ttyUSB0"
    second_ser = Mock(port="/dev/ttyUSB1", in_waiting=0)
    second_ser.read.side_effect = KeyboardInterrupt
    monkeypatch.setattr(driver, "DEVICE", None)
    driver.list_ports.comports.return_value = [
        SimpleNamespace(vid=driver.VID, pid=driver.PID, device=ser.port)
        for ser in (first_ser, second_ser)
    ]
    driver.serial.Serial.side_effect = [
        OSError("port busy") if failure_stage == "open" else first_ser,
        second_ser,
    ]
    handshake = Mock()
    if failure_stage == "handshake":
        handshake.side_effect = [driver.DriverError("not a YYR4"), None]
    elif failure_stage == "init":
        first_ser.write.side_effect = OSError("initialization failed")
    monkeypatch.setattr(driver, "handshake", handshake)

    def create_ui(*_args, **_kwargs):
        if failure_stage != "open":
            first_ser.close.assert_called_once()
        assert second_ser.write.call_args_list == [
            call(bytes.fromhex("0001640400")),
            call(bytes.fromhex("040a000000")),
        ]
        return ui

    driver.UInput.side_effect = create_ui

    assert driver.main() == 0

    assert [entry.args[0] for entry in driver.serial.Serial.call_args_list] == [
        first_ser.port,
        second_ser.port,
    ]
    assert driver.list_ports.comports.call_count == 2
    driver.UInput.assert_called_once()
    second_ser.close.assert_called_once()
    ui.close.assert_called_once()
    assert driver.time.sleep.call_args_list.count(call(driver.RETRY_DELAY)) == 1


def test_auto_detection_cycles_through_all_failed_candidates(hardware, monkeypatch):
    first_ser, _ = hardware
    first_ser.port = "/dev/ttyUSB0"
    second_ser = Mock(port="/dev/ttyUSB1", in_waiting=0)
    monkeypatch.setattr(driver, "DEVICE", None)
    driver.list_ports.comports.return_value = [
        SimpleNamespace(vid=driver.VID, pid=driver.PID, device=ser.port)
        for ser in (first_ser, second_ser)
    ]
    driver.serial.Serial.side_effect = [
        first_ser,
        second_ser,
        first_ser,
        KeyboardInterrupt(),
    ]
    monkeypatch.setattr(
        driver, "handshake", Mock(side_effect=driver.DriverError("not a YYR4"))
    )

    assert driver.main() == 0

    assert [entry.args[0] for entry in driver.serial.Serial.call_args_list] == [
        "/dev/ttyUSB0",
        "/dev/ttyUSB1",
        "/dev/ttyUSB0",
        "/dev/ttyUSB1",
    ]
    assert driver.list_ports.comports.call_count == 4
    assert first_ser.close.call_count == 2
    second_ser.close.assert_called_once()
    driver.UInput.assert_not_called()


def test_missing_board_is_retried(hardware, monkeypatch, caplog):
    _, ui = hardware
    find_port = Mock(
        side_effect=[driver.DriverError("board missing"), KeyboardInterrupt()]
    )
    monkeypatch.setattr(driver, "find_port", find_port)

    assert driver.main() == 0

    assert find_port.call_count == 2
    driver.serial.Serial.assert_not_called()
    driver.UInput.assert_not_called()
    ui.close.assert_not_called()
    driver.time.sleep.assert_called_once_with(driver.RETRY_DELAY)
    assert "board missing" in caplog.text


def test_serial_open_failure_is_retried(hardware, caplog):
    driver.serial.Serial.side_effect = [OSError("port busy"), KeyboardInterrupt()]

    assert driver.main() == 0

    assert driver.serial.Serial.call_count == 2
    driver.time.sleep.assert_called_once_with(driver.RETRY_DELAY)
    assert "port busy" in caplog.text


def test_exclusive_serial_contention_retries_before_starting_session(
    monkeypatch, caplog
):
    master, slave = os.openpty()
    try:
        port = os.ttyname(slave)
        monkeypatch.setattr(driver, "find_port", Mock(return_value=port))
        session = Mock(
            side_effect=AssertionError("a locked port must not start a session")
        )
        monkeypatch.setattr(driver, "session", session)
        monkeypatch.setattr(driver.time, "sleep", Mock(side_effect=KeyboardInterrupt))

        with serial.Serial(port, driver.BAUD, timeout=0, exclusive=True):
            with pytest.raises(KeyboardInterrupt):
                driver.run()
            session.assert_not_called()
            driver.time.sleep.assert_called_once_with(driver.RETRY_DELAY)
            assert port in caplog.text

        with serial.Serial(port, driver.BAUD, timeout=0, exclusive=True) as reopened:
            assert reopened.is_open
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize(
    "error",
    [OSError("initialization failed"), serial.SerialTimeoutException("Write timeout")],
)
def test_init_failure_closes_port_before_retry(
    hardware, monkeypatch, stop_after_retry, error
):
    ser, ui = hardware
    monkeypatch.setattr(driver, "handshake", Mock())
    ser.write.side_effect = error

    assert driver.main() == 0

    ser.close.assert_called_once()
    driver.UInput.assert_not_called()
    ui.close.assert_not_called()


def test_control_write_timeout_releases_keys_and_retries(
    hardware, monkeypatch, tmp_path, stop_after_retry, caplog
):
    ser, ui = hardware
    path = tmp_path / "control"
    monkeypatch.setattr(driver, "CONTROL_PIPE", str(path))
    monkeypatch.setattr(driver, "handshake", Mock())
    ser.write.side_effect = [5, 5, serial.SerialTimeoutException("Write timeout")]

    def read(_size):
        path.write_text("mode 9\n")
        return bytes.fromhex("0001000000")

    ser.read.side_effect = read

    assert driver.main() == 0

    assert ser.write.call_args_list[-1] == call(bytes.fromhex("0001640900"))
    assert ui.write.call_args_list == [
        call(ecodes.EV_KEY, ecodes.KEY_F13, 1),
        call(ecodes.EV_KEY, ecodes.KEY_F13, 0),
    ]
    ser.close.assert_called_once()
    ui.close.assert_called_once()
    driver.time.sleep.assert_called_with(driver.RETRY_DELAY)
    assert "Write timeout; retrying" in caplog.text
    assert not path.exists()


@pytest.mark.parametrize(
    "error", [PermissionError("uinput denied"), UInputError("failed")]
)
def test_uinput_setup_failure_exits_without_serial_retry(
    hardware, monkeypatch, tmp_path, caplog, error
):
    ser, _ = hardware
    original_handler = signal.getsignal(signal.SIGTERM)
    path = tmp_path / "control"
    monkeypatch.setattr(driver, "CONTROL_PIPE", str(path))
    monkeypatch.setattr(driver, "handshake", Mock())
    driver.UInput.side_effect = error

    assert driver.main() == 1

    driver.UInput.assert_called_once()
    driver.serial.Serial.assert_called_once()
    ser.close.assert_called_once()
    assert call(driver.RETRY_DELAY) not in driver.time.sleep.call_args_list
    assert "cannot create virtual keyboard (/dev/uinput)" in caplog.text
    assert not path.exists()
    assert signal.getsignal(signal.SIGTERM) == original_handler


@pytest.mark.parametrize("error_type", [OSError, UInputError])
@pytest.mark.parametrize("phase", ["press", "release"])
def test_uinput_event_failure_stops_without_serial_retry(
    hardware, monkeypatch, tmp_path, stop_after_retry, caplog, error_type, phase
):
    ser, ui = hardware
    path = tmp_path / "control"
    monkeypatch.setattr(driver, "CONTROL_PIPE", str(path))
    monkeypatch.setattr(driver, "handshake", Mock())
    ser.read.side_effect = [bytes.fromhex("0001000000"), OSError("board disconnected")]

    def write(_event_type, _code, value):
        if value == (1 if phase == "press" else 0):
            raise error_type(f"uinput {phase} failed")

    ui.write.side_effect = write

    assert driver.main() == 1

    driver.UInput.assert_called_once()
    driver.serial.Serial.assert_called_once()
    ser.close.assert_called_once()
    ui.close.assert_called_once()
    assert call(driver.RETRY_DELAY) not in driver.time.sleep.call_args_list
    assert f"uinput {phase} failed" in caplog.text
    assert not path.exists()


def test_uinput_close_runs_even_if_releasing_keys_fails(hardware):
    _, ui = hardware
    keyboard = driver.Keyboard()
    keyboard.emit("A1", True)
    ui.write.side_effect = OSError("uinput write failed")

    with pytest.raises(OSError, match="uinput write failed"):
        keyboard.close()

    ui.close.assert_called_once()


def test_watchdog_rejects_another_ch340_in_place_of_connected_port(
    hardware, monkeypatch
):
    ser, _ = hardware
    ser.read.return_value = b""
    driver.list_ports.comports.return_value = [
        SimpleNamespace(vid=driver.VID, pid=driver.PID, device="/dev/other")
    ]
    monkeypatch.setattr(driver.time, "monotonic", Mock(side_effect=[0, 5]))

    with pytest.raises(driver.DriverError, match="serial port disappeared: /dev/fake"):
        next(driver.read_frames(ser))


@pytest.mark.parametrize("use_alias", [False, True])
def test_watchdog_accepts_connected_port_and_checks_only_after_interval(
    hardware, monkeypatch, tmp_path, use_alias
):
    ser, _ = hardware
    port = tmp_path / "ttyUSB0"
    port.touch()
    if use_alias:
        alias = tmp_path / "by-id"
        alias.symlink_to(port)
        ser.port = str(alias)
    else:
        ser.port = str(port)
    driver.list_ports.comports.return_value = [SimpleNamespace(device=str(port))]
    frame = bytes.fromhex("0001000000")
    ser.read.side_effect = [b"", b"", b"", frame]
    monkeypatch.setattr(driver.time, "monotonic", Mock(side_effect=[0, 1, 4, 5, 5, 6]))

    assert next(driver.read_frames(ser)) == frame

    driver.list_ports.comports.assert_called_once()


@pytest.mark.parametrize("original_present", [False, True])
def test_watchdog_keeps_original_target_when_alias_changes(
    hardware, monkeypatch, tmp_path, original_present
):
    ser, _ = hardware
    original = tmp_path / "ttyUSB0"
    replacement = tmp_path / "ttyUSB1"
    alias = tmp_path / "by-id"
    original.touch()
    replacement.touch()
    alias.symlink_to(original)
    ser.port = str(alias)
    frame = bytes.fromhex("0001000000")

    def read(_size):
        alias.unlink()
        alias.symlink_to(replacement)
        ports = [SimpleNamespace(device=str(replacement))]
        if original_present:
            ports.append(SimpleNamespace(device=str(original)))
        else:
            original.unlink()
        driver.list_ports.comports.return_value = ports
        ser.read.side_effect = [frame]
        return b""

    ser.read.side_effect = read
    monkeypatch.setattr(driver.time, "monotonic", Mock(side_effect=[0, 5, 5, 6]))

    if original_present:
        assert next(driver.read_frames(ser)) == frame
    else:
        with pytest.raises(driver.DriverError, match="serial port disappeared"):
            next(driver.read_frames(ser))
    driver.list_ports.comports.assert_called_once()


def test_run_opens_resolved_target_before_alias_changes_during_handshake(
    hardware, monkeypatch, tmp_path, stop_after_retry, caplog
):
    ser, ui = hardware
    original = tmp_path / "ttyUSB0"
    replacement = tmp_path / "ttyUSB1"
    alias = tmp_path / "by-id"
    original.touch()
    replacement.touch()
    alias.symlink_to(original)
    monkeypatch.setattr(driver, "DEVICE", str(alias))

    def open_serial(port, *_args, **_kwargs):
        ser.port = port
        return ser

    def handshake(_ser):
        alias.unlink()
        alias.symlink_to(replacement)
        original.unlink()

    driver.serial.Serial.side_effect = open_serial
    monkeypatch.setattr(driver, "handshake", handshake)
    ser.read.return_value = b""
    driver.list_ports.comports.return_value = [SimpleNamespace(device=str(replacement))]
    monkeypatch.setattr(driver.time, "monotonic", Mock(side_effect=[0, 5]))

    assert driver.main() == 0

    assert driver.serial.Serial.call_args.args[0] == str(original)
    assert f"serial port disappeared: {original}" in caplog.text
    driver.time.sleep.assert_called_with(driver.RETRY_DELAY)
    ser.close.assert_called_once()
    ui.close.assert_called_once()


@pytest.mark.parametrize(
    "name,value",
    [
        ("DEVICE", ""),
        ("DEVICE", 42),
        ("CONTROL_PIPE", ""),
        ("CONTROL_PIPE", 42),
        ("LIGHT_SWITCH", "false"),
        ("LIGHT_BRIGHTNESS", -1),
        ("LIGHT_BRIGHTNESS", 101),
        ("LIGHT_BRIGHTNESS", 1.5),
        ("LIGHT_MODE", -1),
        ("LIGHT_MODE", 10),
        ("LIGHT_TIMEOUT", -1),
        ("LIGHT_TIMEOUT", 256),
        ("RETRY_DELAY", 0),
        ("RETRY_DELAY", -1),
        ("RETRY_DELAY", float("nan")),
        ("RETRY_DELAY", float("inf")),
        ("PRESENCE_INTERVAL", 0),
        ("PRESENCE_INTERVAL", "5"),
    ],
)
def test_invalid_configuration_exits_before_opening_devices(
    hardware, monkeypatch, caplog, name, value
):
    monkeypatch.setattr(driver, name, value)

    assert driver.main() == 1

    driver.serial.Serial.assert_not_called()
    assert f"invalid configuration: {name}" in caplog.text


@pytest.mark.parametrize(
    "action,message",
    [
        ("key", "nonempty tuple/list"),
        (42, "nonempty tuple/list"),
        ((), "nonempty tuple/list"),
        (("Key", ecodes.KEY_Z), "unknown action kind"),
        (("wheel", ecodes.REL_WHEEL, 1), "unknown action kind"),
        ((["key"], ecodes.KEY_Z), "unknown action kind"),
        (("key",), "must have 2 items"),
        (("key", ecodes.KEY_Z, ecodes.KEY_LEFTCTRL), "must have 2 items"),
        (("key", "KEY_Z"), "integer or tuple/list"),
        (("key", 1.5), "integer or tuple/list"),
        (("key", None), "integer or tuple/list"),
        (("key", ()), "at least one code"),
        (("key", []), "at least one code"),
        (("key", True), "must be integers"),
        (("key", (ecodes.KEY_LEFTCTRL, "Z")), "must be integers"),
        (("key", [ecodes.KEY_LEFTCTRL, False]), "must be integers"),
        (("key", -1), "between 1 and"),
        (("key", ecodes.KEY_RESERVED), "between 1 and"),
        (("key", (ecodes.KEY_LEFTCTRL, ecodes.KEY_RESERVED)), "between 1 and"),
        (("key", [ecodes.KEY_RESERVED]), "between 1 and"),
        (("key", ecodes.KEY_MAX + 1), "between 1 and"),
        (("rel",), "must have 3 or 4 items"),
        (("rel", ecodes.REL_WHEEL), "must have 3 or 4 items"),
        (("rel", ecodes.REL_WHEEL, 1, (), "extra"), "must have 3 or 4 items"),
        (("rel", "REL_WHEEL", 1), "code and amount must be integers"),
        (("rel", True, 1), "code and amount must be integers"),
        (("rel", ecodes.REL_WHEEL, 1.5), "code and amount must be integers"),
        (("rel", ecodes.REL_WHEEL, False), "code and amount must be integers"),
        (("rel", -1, 1), "relative code must be between"),
        (("rel", ecodes.REL_MAX + 1, 1), "relative code must be between"),
        (
            ("rel", ecodes.REL_WHEEL, 1, ecodes.KEY_LEFTCTRL),
            "modifiers must be a tuple/list",
        ),
        (("rel", ecodes.REL_WHEEL, 1, None), "modifiers must be a tuple/list"),
        (("rel", ecodes.REL_WHEEL, 1, ["CTRL"]), "must be integers"),
        (("rel", ecodes.REL_WHEEL, 1, (False,)), "must be integers"),
        (("rel", ecodes.REL_WHEEL, 1, [-1]), "between 1 and"),
        (("rel", ecodes.REL_WHEEL, 1, (ecodes.KEY_RESERVED,)), "between 1 and"),
        (("rel", ecodes.REL_WHEEL, 1, [ecodes.KEY_RESERVED]), "between 1 and"),
        (("rel", ecodes.REL_WHEEL, 1, (ecodes.KEY_MAX + 1,)), "between 1 and"),
    ],
)
def test_invalid_mapping_fails_before_creating_pipe_or_devices(
    hardware, monkeypatch, tmp_path, caplog, action, message
):
    monkeypatch.setitem(driver.EVENT_CODES, "A1", action)
    monkeypatch.setattr(driver, "CONTROL_PIPE", str(tmp_path / "control"))
    pipe = Mock()
    monkeypatch.setattr(driver, "ControlPipe", pipe)

    assert driver.main() == 1

    assert "invalid configuration: EVENT_CODES['A1']" in caplog.text
    assert message in caplog.text
    pipe.assert_not_called()
    driver.serial.Serial.assert_not_called()
    driver.UInput.assert_not_called()
    driver.time.sleep.assert_not_called()


@pytest.mark.parametrize("name", ["A1", "BL", "DP", "EXTRA"])
def test_missing_matrix_control_mapping_is_rejected(
    hardware, monkeypatch, caplog, name
):
    if name == "EXTRA":
        monkeypatch.setitem(driver.MATRIX_KEYS, (7, 0), name)
    else:
        monkeypatch.delitem(driver.EVENT_CODES, name)

    assert driver.main() == 1

    assert "EVENT_CODES missing entries" in caplog.text
    assert name in caplog.text
    assert "use None to disable" in caplog.text
    driver.serial.Serial.assert_not_called()
    driver.UInput.assert_not_called()
    driver.time.sleep.assert_not_called()


def test_event_codes_must_be_a_dictionary(hardware, monkeypatch, caplog):
    monkeypatch.setattr(driver, "EVENT_CODES", [])

    assert driver.main() == 1

    assert "EVENT_CODES must be a dictionary" in caplog.text
    driver.serial.Serial.assert_not_called()
    driver.UInput.assert_not_called()


@pytest.mark.parametrize("action", [None, ("key", ecodes.KEY_B)])
def test_unused_mapping_names_warn_without_rejecting_configuration(
    hardware, monkeypatch, caplog, action
):
    monkeypatch.setitem(driver.EVENT_CODES, "a1", action)

    driver.validate_configuration()

    assert "EVENT_CODES names not in MATRIX_KEYS" in caplog.text
    assert "'a1'" in caplog.text
    assert any(record.levelname == "WARNING" for record in caplog.records)
    driver.serial.Serial.assert_not_called()
    driver.UInput.assert_not_called()


@pytest.mark.parametrize(
    "action",
    [
        None,
        ("key", 1),
        ("key", ecodes.KEY_B),
        ("key", (ecodes.KEY_LEFTCTRL, ecodes.KEY_Z)),
        ("key", [ecodes.KEY_LEFTCTRL, ecodes.KEY_Z]),
        ("rel", ecodes.REL_WHEEL, 1),
        ("rel", ecodes.REL_WHEEL, -1),
        ("rel", ecodes.REL_X, -1),
        ("rel", ecodes.REL_HWHEEL, -1, (ecodes.KEY_LEFTSHIFT,)),
        ("rel", ecodes.REL_WHEEL, 1, [ecodes.KEY_LEFTCTRL]),
        ("rel", ecodes.REL_WHEEL, 1, ()),
        ("rel", ecodes.REL_WHEEL, 1, []),
    ],
)
def test_valid_mapping_forms_are_accepted(hardware, monkeypatch, action):
    monkeypatch.setitem(driver.EVENT_CODES, "A1", action)

    driver.validate_configuration()

    driver.serial.Serial.assert_not_called()
    driver.UInput.assert_not_called()


def test_port_discovery_and_override(monkeypatch):
    ports = [
        SimpleNamespace(vid=None, pid=None, device="/dev/unrelated"),
        SimpleNamespace(vid=driver.VID, pid=driver.PID, device="/dev/ttyUSB0"),
        SimpleNamespace(vid=driver.VID, pid=driver.PID, device="/dev/ttyUSB1"),
        SimpleNamespace(vid=driver.VID, pid=driver.PID, device="/dev/ttyUSB2"),
    ]
    list_ports = Mock(return_value=ports)
    monkeypatch.setattr(driver.list_ports, "comports", list_ports)
    monkeypatch.setattr(driver, "DEVICE", "/dev/custom")
    assert driver.find_port() == "/dev/custom"
    assert driver.find_port("/dev/ttyUSB0") == "/dev/custom"
    list_ports.assert_not_called()
    monkeypatch.setattr(driver, "DEVICE", None)
    assert driver.find_port() == "/dev/ttyUSB0"
    assert driver.find_port("/dev/ttyUSB0") == "/dev/ttyUSB1"
    assert driver.find_port("/dev/ttyUSB1") == "/dev/ttyUSB2"
    assert driver.find_port("/dev/ttyUSB2") == "/dev/ttyUSB0"

    list_ports.return_value = list(reversed(ports))
    assert driver.find_port("/dev/ttyUSB1") == "/dev/ttyUSB2"

    list_ports.return_value = [ports[-1]]
    assert driver.find_port("/dev/ttyUSB0") == "/dev/ttyUSB2"
    assert driver.find_port("/dev/ttyUSB2") == "/dev/ttyUSB2"

    list_ports.return_value = []
    with pytest.raises(driver.DriverError, match="no CH340"):
        driver.find_port()


@pytest.fixture
def control(tmp_path):
    pipe = driver.ControlPipe(str(tmp_path / "control"))
    try:
        yield pipe
    finally:
        pipe.close()


@pytest.mark.parametrize(
    "command,expected",
    [
        ("on", "0001320100"),
        ("off", "0000320100"),
        ("brightness 0", "0001000100"),
        ("brightness 100", "0001640100"),
        ("mode 0", "0001320000"),
        ("mode 9", "0001320900"),
        ("mode 0x9", "0001320900"),
        ("timeout 0", "0400000000"),
        ("timeout 255", "04ff000000"),
    ],
)
def test_light_commands_produce_expected_frames(command, expected):
    lights = driver.LightSettings(True, 50, 1, 10)
    assert lights.command(command) == bytes.fromhex(expected)


@pytest.mark.parametrize(
    "command",
    [
        "",
        "color red",
        "brightness -1",
        "brightness 101",
        "brightness nope",
        "mode -1",
        "mode 10",
        "mode",
        "timeout -1",
        "timeout 256",
        "on now",
    ],
)
def test_invalid_light_commands_leave_state_unchanged(command):
    lights = driver.LightSettings(True, 50, 1, 10)
    with pytest.raises(ValueError):
        lights.command(command)
    assert lights == driver.LightSettings(True, 50, 1, 10)


def test_control_pipe_reads_partial_and_batched_lines(control):
    assert list(control.commands()) == []
    assert stat.S_ISFIFO(control.path.stat().st_mode)
    assert stat.S_IMODE(control.path.stat().st_mode) == 0o600

    with control.path.open("wb", buffering=0) as writer:
        writer.write(b"mode")
        assert list(control.commands()) == []
        writer.write(b" 9\n\nbrightness 20\n")

    assert list(control.commands()) == ["mode 9", "brightness 20"]
    assert list(control.commands()) == []


def test_control_pipe_accepts_shell_echo(control):
    subprocess.run(
        ["sh", "-c", 'echo "mode 9" > "$1"', "sh", str(control.path)],
        check=True,
        timeout=2,
    )
    assert list(control.commands()) == ["mode 9"]


def test_control_pipe_discards_malformed_and_oversized_lines(control, caplog):
    with control.path.open("wb", buffering=0) as writer:
        writer.write(b"\xff\n" + b"x" * 300)
        assert list(control.commands()) == []
        writer.write(b"mode 9\non\n")

    assert list(control.commands()) == ["on"]
    assert "not ASCII" in caplog.text
    assert "exceeds 256 bytes" in caplog.text


def test_control_pipe_refuses_existing_files_and_preserves_content(tmp_path):
    path = tmp_path / "control"
    path.write_text("keep this")

    with pytest.raises(ValueError, match="not a FIFO"):
        driver.ControlPipe(str(path))

    assert path.read_text() == "keep this"


def test_control_pipe_reuses_stale_fifo_and_cleans_up(tmp_path):
    path = tmp_path / "control"
    os.mkfifo(path, mode=0o600)
    pipe = driver.ControlPipe(str(path))
    pipe.close()
    assert not path.exists()


def test_control_pipe_refuses_second_listener(control):
    with pytest.raises(BlockingIOError):
        driver.ControlPipe(str(control.path))
    assert control.path.exists()


def test_control_pipe_close_preserves_replacement_file(tmp_path):
    path = tmp_path / "control"
    pipe = driver.ControlPipe(str(path))
    path.unlink()
    path.write_text("replacement")
    pipe.close()
    assert path.read_text() == "replacement"


def test_invalid_control_command_does_not_block_next_command(hardware, control, caplog):
    ser, _ = hardware
    lights = driver.LightSettings(True, 50, 1, 10)
    control.path.write_text("mode 10\nbrightness 25\n")

    driver.poll_control(control, lights, ser)

    ser.write.assert_called_once_with(bytes.fromhex("0001190100"))
    assert lights == driver.LightSettings(True, 25, 1, 10)
    assert "invalid control command" in caplog.text
    ser.reset_input_buffer.assert_not_called()


def test_control_commands_update_lights_while_keys_remain_held(
    hardware, control, monkeypatch
):
    ser, ui = hardware
    lights = driver.LightSettings.configured()
    driver.send_init(ser, lights)

    def write_command():
        control.path.write_text("mode 9\n")
        return b""

    def after_update():
        assert ui.write.call_args_list == [call(ecodes.EV_KEY, ecodes.KEY_F13, 1)]
        assert ser.write.call_args_list[-1] == call(bytes.fromhex("0001640900"))
        return bytes.fromhex("0000000000")

    reads = iter(
        [bytes.fromhex("0001000000"), write_command, after_update, KeyboardInterrupt()]
    )

    def read(_size):
        value = next(reads)
        if isinstance(value, BaseException):
            raise value
        return value() if callable(value) else value

    ser.read.side_effect = read
    keyboard = driver.Keyboard()
    with pytest.raises(KeyboardInterrupt):
        driver.session(ser, keyboard, lights, control)

    assert lights.mode == 9
    assert ui.write.call_args_list == [
        call(ecodes.EV_KEY, ecodes.KEY_F13, 1),
        call(ecodes.EV_KEY, ecodes.KEY_F13, 0),
    ]
    ser.reset_input_buffer.assert_called_once()
    ui.close.assert_not_called()
    keyboard.close()
    ui.close.assert_called_once()


def test_offline_commands_are_applied_by_next_initialization(hardware, control):
    ser, _ = hardware
    lights = driver.LightSettings.configured()
    control.path.write_text("mode 9\nbrightness 25\noff\ntimeout 0\n")

    driver.poll_control(control, lights)
    ser.write.assert_not_called()
    driver.send_init(ser, lights)

    assert ser.write.call_args_list == [
        call(bytes.fromhex("0000190900")),
        call(bytes.fromhex("0400000000")),
    ]


def test_desired_state_survives_failed_serial_write(hardware, control):
    ser, _ = hardware
    lights = driver.LightSettings.configured()
    control.path.write_text("mode 9\nbrightness 25\n")
    ser.write.side_effect = OSError("disconnected")

    with pytest.raises(OSError, match="disconnected"):
        driver.poll_control(control, lights, ser)

    assert lights.mode == 9
    ser.write.reset_mock(side_effect=True)
    driver.poll_control(control, lights)
    driver.send_init(ser, lights)
    assert ser.write.call_args_list[0] == call(bytes.fromhex("0001190900"))


def test_daemon_cleans_up_control_pipe_on_interrupt(hardware, monkeypatch, tmp_path):
    path = tmp_path / "control"
    monkeypatch.setattr(driver, "CONTROL_PIPE", str(path))
    run = Mock(side_effect=KeyboardInterrupt)
    monkeypatch.setattr(driver, "run", run)

    assert driver.main() == 0

    run.assert_called_once()
    assert isinstance(run.call_args.args[0], driver.ControlPipe)
    driver.UInput.assert_not_called()
    assert not path.exists()


def test_sigterm_releases_keys_and_closes_all_resources(
    hardware, monkeypatch, tmp_path
):
    ser, ui = hardware
    original_handler = signal.getsignal(signal.SIGTERM)
    path = tmp_path / "control"
    monkeypatch.setattr(driver, "CONTROL_PIPE", str(path))
    monkeypatch.setattr(driver, "handshake", Mock())

    def read(_size):
        assert path.exists()
        if ui.write.called:
            assert callable(signal.getsignal(signal.SIGTERM))
            signal.raise_signal(signal.SIGTERM)
            pytest.fail("SIGTERM did not interrupt the session")
        return bytes.fromhex("0001000000")

    ser.read.side_effect = read

    assert driver.main() == 0

    assert ui.write.call_args_list == [
        call(ecodes.EV_KEY, ecodes.KEY_F13, 1),
        call(ecodes.EV_KEY, ecodes.KEY_F13, 0),
    ]
    ui.close.assert_called_once()
    ser.close.assert_called_once()
    assert not path.exists()
    assert signal.getsignal(signal.SIGTERM) == original_handler


def test_sigterm_during_reconnect_delay_stops_without_retrying(
    hardware, monkeypatch, tmp_path
):
    _, ui = hardware
    original_handler = signal.getsignal(signal.SIGTERM)
    path = tmp_path / "control"
    monkeypatch.setattr(driver, "CONTROL_PIPE", str(path))
    find_port = Mock(side_effect=driver.DriverError("board missing"))
    monkeypatch.setattr(driver, "find_port", find_port)

    def stop(_seconds):
        assert callable(signal.getsignal(signal.SIGTERM))
        signal.raise_signal(signal.SIGTERM)
        pytest.fail("SIGTERM did not interrupt the reconnect delay")

    driver.time.sleep.side_effect = stop

    assert driver.main() == 0

    find_port.assert_called_once()
    driver.time.sleep.assert_called_once_with(driver.RETRY_DELAY)
    driver.serial.Serial.assert_not_called()
    driver.UInput.assert_not_called()
    ui.close.assert_not_called()
    assert not path.exists()
    assert signal.getsignal(signal.SIGTERM) == original_handler


def test_daemon_rejects_control_path_collision_before_opening_serial(
    hardware, monkeypatch, tmp_path, caplog
):
    original_handler = signal.getsignal(signal.SIGTERM)
    path = tmp_path / "control"
    path.write_text("keep this")
    monkeypatch.setattr(driver, "CONTROL_PIPE", str(path))

    assert driver.main() == 1

    driver.serial.Serial.assert_not_called()
    assert path.read_text() == "keep this"
    assert "control pipe error" in caplog.text
    assert "CONTROL_PIPE" in caplog.text
    assert str(path) in caplog.text
    assert signal.getsignal(signal.SIGTERM) == original_handler


def test_missing_runtime_directory_reports_control_pipe_path(
    hardware, monkeypatch, tmp_path, caplog
):
    path = tmp_path / "missing-runtime" / "yyr4-control"
    original_handler = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(driver, "CONTROL_PIPE", str(path))

    assert driver.main() == 1

    assert "control pipe error" in caplog.text
    assert "CONTROL_PIPE" in caplog.text
    assert str(path) in caplog.text
    assert not path.exists()
    driver.serial.Serial.assert_not_called()
    driver.UInput.assert_not_called()
    driver.time.sleep.assert_not_called()
    assert signal.getsignal(signal.SIGTERM) == original_handler


@pytest.mark.parametrize(
    "error", [PermissionError("permission denied"), BlockingIOError("pipe locked")]
)
def test_control_pipe_open_error_reports_path_even_without_filename(
    hardware, monkeypatch, tmp_path, caplog, error
):
    path = tmp_path / "control"
    monkeypatch.setattr(driver, "CONTROL_PIPE", str(path))
    monkeypatch.setattr(driver, "ControlPipe", Mock(side_effect=error))

    assert driver.main() == 1

    assert "control pipe error" in caplog.text
    assert "CONTROL_PIPE" in caplog.text
    assert str(path) in caplog.text
    assert str(error) in caplog.text
    driver.serial.Serial.assert_not_called()
    driver.UInput.assert_not_called()
