# YYR4 Linux Driver

**I do not recommend buying the YYR4 keypad.** It is a CH340 USB serial
device, not a USB HID keyboard, and it does not run QMK firmware. It requires
a driver running on every computer to produce keyboard input. Key mappings
depend on that driver; you cannot configure the keypad once and use it
elsewhere as a standalone keyboard.

This Linux driver turns its 12 keys and four knobs into a virtual
keyboard through uinput and reconnects automatically after unplugging.
The virtual keyboard is created after the board's first successful handshake
and light initialization, then stays registered across serial reconnects.
Failure to create it stops the daemon with an error.

## Run

Requires Python 3.10+ and read/write access to the board's serial port
and `/dev/uinput`.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python yyr4_linux_kbd.py
```

Stop with Ctrl-C or `kill -TERM <pid>`; both release held keys and clean up
the control pipe. There are no command-line options.

## Configuration

Edit the constants in [yyr4_linux_kbd.py](yyr4_linux_kbd.py), then restart:

```python
DEVICE = None  # Auto-detect; or "/dev/ttyUSB0"
LIGHT_SWITCH = True
LIGHT_BRIGHTNESS = 50  # 0–100
LIGHT_MODE = 8
LIGHT_TIMEOUT = 10  # 0–255 idle minutes before light-off
```

Set `VERBOSE = True` to log raw event frames.

`CONTROL_PIPE` defaults to `$XDG_RUNTIME_DIR/yyr4-control`, or
`/run/user/<uid>/yyr4-control` if the variable is unset. Set it to another
path or `None` to disable runtime control. The parent directory must exist;
desktop logins normally create the user runtime directory.

## Runtime light control

Start the daemon first; it creates the named pipe. Send commands as the
same user:

```sh
yyr4_pipe="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/yyr4-control"
echo 'on' > "$yyr4_pipe"
echo 'off' > "$yyr4_pipe"
echo 'brightness 70' > "$yyr4_pipe"
echo 'mode 9' > "$yyr4_pipe"
echo 'timeout 10' > "$yyr4_pipe"
```

Commands use the ranges above. Settings survive board reconnects; restarting
the daemon restores the constants. Color follows the selected mode.

Light modes:

| Mode | Name | English |
|---|---|---|
| 0 | 七彩呼吸 | Rainbow breathing |
| 1 | 繁星点点 | Twinkling stars |
| 2 | 赛博朋克 | Cyberpunk |
| 3 | 全彩单击 | Full-color click |
| 4 | 全彩随机 | Full-color random |
| 5 | 全彩流星 | Full-color meteor |
| 6 | 全彩渐变 | Full-color gradient |
| 7 | 紫色渐变 | Purple gradient |
| 8 | 多色渐变 | Multicolor gradient |
| 9 | 五彩斑斓 | Vibrant colors |

## Key mapping

Edit `MATRIX_KEYS` for matrix coordinates and `EVENT_CODES` for actions.

A1–A12 send F13–F24. The knob defaults use Blender's standard keymap:

| Knob | Turn left | Turn right | Press |
|---|---|---|---|
| A | Zoom out (Numpad−) | Zoom in (Numpad+) | Frame Selected (Numpad.) |
| B | Undo (Ctrl+Z) | Redo (Ctrl+Shift+Z) | Undo (Ctrl+Z) |
| C | Smaller brush (`[`) | Larger brush (`]`) | Adjust brush size (F) |
| D | Roll view left (Shift+Numpad4) | Roll view right (Shift+Numpad6) | Adjust brush strength (Shift+F) |

A/B have tactile detents; C/D turn smoothly. All four report discrete left/right
events. Brush controls require Sculpt/Paint mode. Keep the pointer over the
relevant Blender viewport; CP/DP open adjustment tools where you can type a
value and confirm with Enter.

Every control has an explicit entry; `None` disables it. The default mapping
sends only keyboard shortcuts. Edit entries to match your application's keymap;
wheel actions are available for custom mappings.
Malformed actions and missing control entries are rejected at startup before
devices are opened. Names absent from `MATRIX_KEYS` produce a warning.

| Action | Mapping value |
|---|---|
| Single key | `("key", ecodes.KEY_B)` |
| Ctrl+Z | `("key", (ecodes.KEY_LEFTCTRL, ecodes.KEY_Z))` |
| Ctrl+Z using a list | `("key", [ecodes.KEY_LEFTCTRL, ecodes.KEY_Z])` |
| Ctrl+Shift+Z | `("key", (ecodes.KEY_LEFTCTRL, ecodes.KEY_LEFTSHIFT, ecodes.KEY_Z))` |
| Left/right bracket | `("key", ecodes.KEY_LEFTBRACE)` / `("key", ecodes.KEY_RIGHTBRACE)` |
| Vertical wheel | `("rel", ecodes.REL_WHEEL, 1)` |
| Horizontal wheel | `("rel", ecodes.REL_HWHEEL, 1)` |
| Alt+wheel | `("rel", ecodes.REL_WHEEL, 1, (ecodes.KEY_LEFTALT,))` |

Key actions stay held until the control is released. Key combinations and
wheel modifiers accept tuples or lists; put modifiers first in a combination.
Wheel actions tick on down events; use `-1` for the opposite direction.
Optional wheel modifiers apply only during that tick. Shared modifiers stay
held; pressing another control with the same non-modifier key retriggers it.
Restart the daemon after editing.

**KDE:** in Keyboard → Key Bindings (Advanced on older versions), enable
Configure keyboard options → Function keys → **Use F13–F24 as usual
function keys** (`fkeys:basic_13-24`). Otherwise some F keys trigger
microphone or touchpad actions.

## USB protocol

The Linux transport is CH340 USB serial: VID/PID `1a86:7523`,
115200 baud, 8N1. Frame bytes are hexadecimal; numeric ranges are decimal.

### Handshake

Define the five-byte digest as:

```python
import hashlib


def digest(data):
    return hashlib.sha256("".join(str(b) for b in data).encode("ascii")).digest()[:5]
```

Decimal byte values are joined without separators: `01 02 ff` becomes
`"12255"`.

1. Host sends `01` followed by four random bytes.
2. Board replies with `digest(host_message)` followed by five random bytes
   (10 bytes total).
3. Host sends `digest(board_random_bytes)`.

### Frames

After the handshake, each message is five bytes:

| Direction | Bytes | Meaning |
|---|---|---|
| Board → host | `TYPE DD RR CC 00` | Type `00` or `01`; `DD` = `01` down, `00` up; row `RR`, column `CC` |
| Host → board | `00 SS BB MM 00` | Switch `SS` = `00`/`01`; brightness `BB` = 0–100; mode `MM` = 0–9 |
| Host → board | `04 TT 00 00 00` | Idle light timeout `TT` = 0–255 minutes |

Configuration frames can be sent after authentication. For example,
`00 01 32 09 00` enables mode 9 at brightness 50; `04 0a 00 00 00`
sets a 10-minute timeout.

Matrix coordinates:

```text
       col 0  col 1  col 2  col 3
row 0   A1     A2     A3     A4
row 1   A5     A6     A7     A8
row 2   A9     A10    A11    A12
row 3   AL     AP     AR
row 4   BL     BP     BR
row 5   CL     CP     CR
row 6   DL     DP     DR
```

Knob L/R events arrive as down/up pairs. Reads may split or combine frames;
retain partial bytes and process each complete five-byte event. The driver
resynchronizes by discarding leading bytes until the type, flag, and trailing
zero are valid.

## License

[MIT](LICENSE).
