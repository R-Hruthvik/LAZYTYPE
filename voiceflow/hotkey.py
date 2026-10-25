"""
Global hotkey listener — works on X11 AND Wayland (and TTY).

Strategy:
  Primary: evdev — reads /dev/input/event* directly from the kernel.
    Works on X11, Wayland, and raw TTY. Requires user in 'input' group.
  Fallback: pynput — X11 only, no group setup needed.

Default hotkey: Right Ctrl (KEY_RIGHTCTRL) — push-to-talk mode.
  Hold = recording starts
  Release = recording stops → transcription begins

Toggle mode: press once to start, press again to stop.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from enum import Enum, auto
from typing import Any, Callable

log = logging.getLogger(__name__)

# Callbacks
StartCallback = Callable[[], Any]
StopCallback = Callable[[], Any]


class HotkeyMode(Enum):
    PUSH_TO_TALK = auto()  # hold key to record
    TOGGLE = auto()  # press once to start, again to stop


class HotkeyListener:
    """
    System-wide global hotkey listener.

    Tries evdev first (works everywhere), falls back to pynput (X11 only).
    """

    def __init__(
        self,
        hotkey: str = "KEY_RIGHTCTRL",
        mode: HotkeyMode = HotkeyMode.PUSH_TO_TALK,
        on_start: StartCallback | None = None,
        on_stop: StopCallback | None = None,
    ) -> None:
        self._hotkey = hotkey
        self._mode = mode
        self._on_start = on_start
        self._on_stop = on_stop
        self._recording = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start the hotkey listener in a background thread."""
        self._loop = loop
        self._stop_event.clear()

        # Try evdev first
        if self._try_evdev():
            log.info(
                "HotkeyListener: using evdev backend (hotkey=%s, mode=%s)",
                self._hotkey,
                self._mode.name,
            )
            self._thread = threading.Thread(
                target=self._evdev_loop, daemon=True, name="voiceflow-hotkey-evdev"
            )
        else:
            log.info("HotkeyListener: evdev unavailable — falling back to pynput")
            self._thread = threading.Thread(
                target=self._pynput_loop, daemon=True, name="voiceflow-hotkey-pynput"
            )

        self._thread.start()

    def stop(self) -> None:
        """Signal the listener thread to stop."""
        self._stop_event.set()

    def reset_recording_state(self) -> None:
        """Reset internal recording toggle state to False."""
        self._recording = False

    # ------------------------------------------------------------------ #
    # evdev backend
    # ------------------------------------------------------------------ #

    def _get_keyboards(self, key_code: int | None = None) -> list[Any]:
        """Find accessible devices. If key_code is specified, only return devices that support that key."""
        try:
            from evdev import InputDevice, list_devices, ecodes
        except ImportError:
            return []

        keyboards: list[Any] = []
        for path in list_devices():
            dev = None
            try:
                dev = InputDevice(path)
                name = (getattr(dev, "name", "") or "").lower()
                # Skip synthetic virtual devices (e.g. ydotoold, voiceflow virtual keyboard, uinput)
                if any(v in name for v in ("ydotool", "voiceflow", "uinput")):
                    dev.close()
                    continue

                caps = dev.capabilities().get(ecodes.EV_KEY, ())
                if key_code is not None:
                    if key_code in caps:
                        keyboards.append(dev)
                    else:
                        dev.close()
                elif caps:
                    keyboards.append(dev)
                else:
                    dev.close()
            except (PermissionError, OSError):
                if dev:
                    try:
                        dev.close()
                    except Exception:
                        pass
        return keyboards

    def _try_evdev(self) -> bool:
        """Check if evdev is available and we have read access to input devices."""
        try:
            import evdev  # noqa: F401
            from evdev import ecodes

            key_code = getattr(ecodes, self._hotkey, None)
            if key_code is None:
                log.error("HotkeyListener: unknown evdev key name %r — check config", self._hotkey)
                return False
            kbs = self._get_keyboards(key_code)
            if kbs:
                for dev in kbs:
                    try:
                        dev.close()
                    except Exception:
                        pass
                return True
            log.warning(
                "HotkeyListener: evdev permission denied or no device with %s found on /dev/input/event*",
                self._hotkey,
            )
            log.warning("   -> On Wayland, to enable the global hotkey (Right Ctrl), run:")
            log.warning(
                "      sudo usermod -aG input $USER && sudo setfacl -m u:$USER:rw /dev/input/event*"
            )
            log.warning(
                "   -> Alternatively, use 'voiceflow --toggle' in any Ubuntu Custom Shortcut!"
            )
            return False
        except ImportError:
            return False

    def _evdev_loop(self) -> None:
        """Background thread: monitor all keyboards via evdev."""
        import select
        import time
        from evdev import ecodes

        # Map hotkey name to evdev key code
        key_code = getattr(ecodes, self._hotkey, None)
        if key_code is None:
            log.error("HotkeyListener: unknown evdev key name %r — check config", self._hotkey)
            return

        keyboards = self._get_keyboards(key_code)
        if not keyboards:
            log.warning("HotkeyListener: no devices with key %s found via evdev", self._hotkey)

        log.debug("HotkeyListener: monitoring %d device(s) for %s", len(keyboards), self._hotkey)

        last_scan_time = time.monotonic()
        while not self._stop_event.is_set():
            now = time.monotonic()
            # If no keyboards available (throttled to 2.0s) or every 10s, refresh device list to handle hotplugging/sleep
            should_rescan = (not keyboards and (now - last_scan_time > 2.0)) or (
                now - last_scan_time > 10.0
            )
            if should_rescan:
                last_scan_time = now
                current_paths = {kb.path for kb in keyboards}
                new_kbs = self._get_keyboards(key_code)
                new_paths = {kb.path for kb in new_kbs}
                if new_paths != current_paths:
                    for kb in keyboards:
                        try:
                            kb.close()
                        except Exception:
                            pass
                    keyboards = new_kbs
                    log.debug(
                        "HotkeyListener: refreshed device list (%d device(s) for %s)",
                        len(keyboards),
                        self._hotkey,
                    )
                else:
                    for kb in new_kbs:
                        try:
                            kb.close()
                        except Exception:
                            pass

            if not keyboards:
                time.sleep(1.0)
                continue

            fds = {kb.fd: kb for kb in keyboards}
            try:
                readable, _, _ = select.select(list(fds.keys()), [], [], 0.5)
            except (ValueError, OSError):
                time.sleep(0.1)
                continue

            dead_kbs = []
            for fd in readable:
                kb = fds.get(fd)
                if not kb:
                    continue
                try:
                    for event in kb.read():
                        if event.type == ecodes.EV_KEY and event.code == key_code:
                            self._handle_key_event(event.value)
                except BlockingIOError:
                    # Normal EOF for non-blocking read when device event buffer is drained
                    pass
                except OSError as exc:
                    log.debug(
                        "HotkeyListener: device error on %s (%s) — removing: %s",
                        getattr(kb, "path", fd),
                        getattr(kb, "name", "unknown"),
                        exc,
                    )
                    dead_kbs.append(kb)

            if dead_kbs:
                for dead in dead_kbs:
                    if dead in keyboards:
                        keyboards.remove(dead)
                    try:
                        dead.close()
                    except Exception:
                        pass

        for kb in keyboards:
            try:
                kb.close()
            except Exception:
                pass

    def _handle_key_event(self, value: int) -> None:
        """
        value=1: key pressed
        value=0: key released
        value=2: key held (repeat) — ignore
        """
        if value == 2:  # auto-repeat, ignore
            return

        if self._mode == HotkeyMode.PUSH_TO_TALK:
            if value == 1 and not self._recording:  # pressed
                self._recording = True
                self._fire(self._on_start)
            elif value == 0 and self._recording:  # released
                self._recording = False
                self._fire(self._on_stop)

        elif self._mode == HotkeyMode.TOGGLE:
            if value == 1:  # press only (not release)
                if not self._recording:
                    self._recording = True
                    self._fire(self._on_start)
                else:
                    self._recording = False
                    self._fire(self._on_stop)

    # ------------------------------------------------------------------ #
    # pynput backend (X11 fallback)
    # ------------------------------------------------------------------ #

    def _pynput_loop(self) -> None:
        """Use pynput for X11 environments without evdev access."""
        try:
            from pynput import keyboard

            # Parse hotkey string to pynput Key
            pynput_key = self._hotkey_to_pynput(self._hotkey)

            def on_press(key):
                if self._stop_event.is_set():
                    return False  # stop listener
                try:
                    if key == pynput_key:
                        if self._mode == HotkeyMode.PUSH_TO_TALK:
                            if not self._recording:
                                self._recording = True
                                self._fire(self._on_start)
                        elif self._mode == HotkeyMode.TOGGLE:
                            if not self._recording:
                                self._recording = True
                                self._fire(self._on_start)
                            else:
                                self._recording = False
                                self._fire(self._on_stop)
                except Exception:  # pylint: disable=broad-except
                    pass

            def on_release(key):
                if self._stop_event.is_set():
                    return False
                try:
                    if key == pynput_key and self._recording:
                        if self._mode == HotkeyMode.PUSH_TO_TALK:
                            self._recording = False
                            self._fire(self._on_stop)
                        elif self._mode == HotkeyMode.TOGGLE:
                            # Toggle: release doesn't stop — only next press does
                            pass
                except Exception:  # pylint: disable=broad-except
                    pass

            with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
                while not self._stop_event.is_set():
                    import time

                    time.sleep(0.1)
                listener.stop()

        except Exception as exc:  # pylint: disable=broad-except
            log.exception("HotkeyListener: pynput loop crashed: %s", exc)

    def _hotkey_to_pynput(self, key_name: str):
        """Convert evdev-style key name to pynput Key enum."""
        from pynput import keyboard

        mapping = {
            "KEY_RIGHTCTRL": keyboard.Key.ctrl_r,
            "KEY_LEFTCTRL": keyboard.Key.ctrl_l,
            "KEY_RIGHTALT": keyboard.Key.alt_r,
            "KEY_LEFTALT": keyboard.Key.alt_l,
            "KEY_RIGHTSHIFT": keyboard.Key.shift_r,
            "KEY_LEFTSHIFT": keyboard.Key.shift_l,
            "KEY_CAPSLOCK": keyboard.Key.caps_lock,
            "KEY_F1": keyboard.Key.f1,
            "KEY_F2": keyboard.Key.f2,
            # ... add more as needed
        }
        return mapping.get(key_name, keyboard.Key.ctrl_r)

    # ------------------------------------------------------------------ #
    # Thread → asyncio bridge
    # ------------------------------------------------------------------ #

    def _fire(self, callback: Callable | None) -> None:
        """Call a callback safely from the listener thread into the asyncio loop."""
        if callback is None or self._loop is None:
            return
        if asyncio.iscoroutinefunction(callback):
            asyncio.run_coroutine_threadsafe(callback(), self._loop)
        else:
            self._loop.call_soon_threadsafe(callback)
