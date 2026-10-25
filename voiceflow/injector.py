"""
Text injector — types transcribed text directly into the focused text bar or terminal.

Strategies:
  1. AT-SPI2 Direct Injection:
     If the target widget implements AccessibleEditableText, directly calls
     `insert_text()` or `paste_text()` without needing synthetic keystrokes!
  2. evdev.UInput Virtual Keyboard:
     Creates a virtual hardware keyboard at /dev/uinput. Copies text to clipboard
     via wl-copy/xclip, then simulates instantaneous hardware Ctrl+V or Ctrl+Shift+V.
  3. ydotool fallback:
     If ydotool daemon is available on Wayland.
  4. xdotool fallback:
     On X11 / XWayland.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from typing import Any, Optional

from voiceflow.window_tracker import _ATSPI_AVAILABLE

log = logging.getLogger(__name__)

# Check evdev UInput availability
_UINPUT_AVAILABLE = False
try:
    import evdev
    from evdev import ecodes, UInput

    _UINPUT_AVAILABLE = True
except Exception:
    pass


class DisplayEnvironment:
    X11 = "x11"
    WAYLAND = "wayland"
    TTY = "tty"


def detect_environment() -> str:
    """Detect active display server."""
    if os.environ.get("WAYLAND_DISPLAY"):
        return DisplayEnvironment.WAYLAND
    if os.environ.get("DISPLAY"):
        return DisplayEnvironment.X11
    return DisplayEnvironment.TTY


class TextInjector:
    """Injects transcribed text directly into the focused application or text bar."""

    def __init__(self, method: str = "auto") -> None:
        if method == "auto":
            self._env = detect_environment()
        else:
            self._env = method

        self._uinput: Any = None
        self._init_uinput()
        log.info(
            "TextInjector: initialized (env=%s, uinput=%s)", self._env, self._uinput is not None
        )

    def _init_uinput(self) -> None:
        """Initialize persistent virtual keyboard if /dev/uinput is writable."""
        if not _UINPUT_AVAILABLE:
            return
        try:
            cap: dict[Any, Any] = {
                ecodes.EV_KEY: [
                    ecodes.KEY_LEFTCTRL,
                    ecodes.KEY_LEFTSHIFT,
                    ecodes.KEY_V,
                    ecodes.KEY_LEFTALT,
                    ecodes.KEY_TAB,
                    ecodes.KEY_ENTER,
                ]
            }
            self._uinput = UInput(cap, name="voiceflow-virtual-keyboard")
            log.debug("TextInjector: evdev.UInput virtual keyboard ready")
        except Exception as exc:
            log.debug("TextInjector: /dev/uinput not directly writable (%s)", exc)

    async def inject(
        self,
        text: str,
        target_accessible: Optional[object] = None,
        is_terminal: bool = False,
    ) -> bool:
        """
        Inject text into the active/target text bar.
        Tries AT-SPI direct injection first (for GUI widgets), then hardware virtual
        keyboard paste (Ctrl+Shift+V for terminals, Ctrl+V for GUIs), then ydotool/xdotool.
        """
        if not text or not text.strip():
            return True

        # Determine if destination is a terminal
        is_term = self._is_target_terminal(target_accessible, is_terminal_hint=is_terminal)
        log.debug("TextInjector: injecting %d chars (is_terminal=%s)", len(text), is_term)

        # Strategy 1: AT-SPI2 Direct Widget Insertion (GUI widgets only — terminals reject insert_text)
        if not is_term and target_accessible and _ATSPI_AVAILABLE:
            if await self._inject_atspi(text, target_accessible):
                log.info("TextInjector: ✅ Directly inserted text via AT-SPI2 into widget")
                return True

        # Always update system clipboard so text is ready for paste
        await self._set_clipboard(text)

        # Strategy 2: Virtual Hardware Keystroke via evdev.UInput (instantaneous)
        if self._uinput:
            if await self._inject_uinput_paste(is_term):
                log.info(
                    "TextInjector: ✅ Pasted directly via virtual keyboard (%s)",
                    "Ctrl+Shift+V" if is_term else "Ctrl+V",
                )
                return True

        # Strategy 3: ydotool on Wayland
        if shutil.which("ydotool"):
            if await self._inject_ydotool(is_term):
                log.info("TextInjector: ✅ Injected via ydotool")
                return True

        # Strategy 4: xdotool on X11 / XWayland
        if self._env == DisplayEnvironment.X11 or shutil.which("xdotool"):
            if await self._inject_xdotool(text):
                log.info("TextInjector: ✅ Injected via xdotool")
                return True

        # Strategy 5: Notify user as last resort
        log.warning("TextInjector: Direct keystroke injection unavailable — copied to clipboard")
        await self._notify("VoiceFlow", "📋 Text copied to clipboard — press Ctrl+V to paste")
        return False

    async def _inject_atspi(self, text: str, accessible: object) -> bool:
        """Directly insert text into an Accessible widget (zero keystrokes needed)."""
        try:
            # Check if object implements insert_text
            if hasattr(accessible, "insert_text"):
                # Try inserting at current caret position or end (-1)
                # Pass -1 for length so AT-SPI uses the full null-terminated string, handling UTF-8 properly
                ok = accessible.insert_text(-1, text, -1)
                if ok:
                    return True
        except Exception as exc:
            log.debug("TextInjector: Atspi insert_text failed: %s", exc)
        return False

    async def _inject_uinput_paste(self, is_terminal: bool) -> bool:
        """Synthesize hardware Ctrl+V or Ctrl+Shift+V using kernel virtual keyboard."""
        try:
            ui = self._uinput
            assert ui is not None

            # Small pause to let clipboard settle
            await asyncio.sleep(0.04)

            # Hold modifiers
            ui.write(ecodes.EV_KEY, ecodes.KEY_LEFTCTRL, 1)
            if is_terminal:
                ui.write(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 1)
            ui.write(ecodes.EV_KEY, ecodes.KEY_V, 1)
            ui.syn()

            try:
                await asyncio.sleep(0.04)
            finally:
                # Always release modifiers even if cancelled
                ui.write(ecodes.EV_KEY, ecodes.KEY_V, 0)
                if is_terminal:
                    ui.write(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 0)
                ui.write(ecodes.EV_KEY, ecodes.KEY_LEFTCTRL, 0)
                ui.syn()

            return True
        except Exception as exc:
            log.debug("TextInjector: uinput paste failed: %s", exc)
            return False

    async def _inject_ydotool(self, is_terminal: bool) -> bool:
        """Simulate paste keystroke using ydotool."""
        try:
            await asyncio.sleep(0.04)
            key_combo = "ctrl+shift+v" if is_terminal else "ctrl+v"
            proc = await asyncio.create_subprocess_exec(
                "ydotool",
                "key",
                "--key-delay",
                "25",
                key_combo,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            return proc.returncode == 0
        except Exception:
            return False

    async def _inject_xdotool(self, text: str) -> bool:
        """Simulate typing or paste via xdotool."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "xdotool",
                "type",
                "--clearmodifiers",
                "--delay",
                "0",
                "--",
                text,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            return proc.returncode == 0
        except Exception:
            return False

    async def _set_clipboard(self, text: str) -> bool:
        """Put text into the Wayland/X11 clipboard."""
        if shutil.which("wl-copy"):
            try:
                proc = await asyncio.create_subprocess_exec(
                    "wl-copy",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.communicate(input=text.encode("utf-8"))

                # Also populate primary selection (used by terminals and middle-click)
                try:
                    proc_p = await asyncio.create_subprocess_exec(
                        "wl-copy",
                        "--primary",
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await proc_p.communicate(input=text.encode("utf-8"))
                except Exception:
                    pass

                return proc.returncode == 0
            except Exception:
                pass

        if shutil.which("xclip"):
            try:
                proc = await asyncio.create_subprocess_exec(
                    "xclip",
                    "-selection",
                    "clipboard",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.communicate(input=text.encode("utf-8"))
                return proc.returncode == 0
            except Exception:
                pass

        return False

    def _is_target_terminal(self, accessible: Any = None, is_terminal_hint: bool = False) -> bool:
        """Check if target widget is a terminal emulator requiring Ctrl+Shift+V."""
        if is_terminal_hint:
            return True
        if not accessible:
            return False
        try:
            terms = (
                "terminal",
                "gnome-terminal",
                "alacritty",
                "kitty",
                "konsole",
                "wezterm",
                "xterm",
                "foot",
                "ptyxis",
                "tilix",
                "terminator",
            )
            role = (accessible.get_role_name() or "").lower()
            if any(t in role for t in terms):
                return True
            name = (accessible.get_name() or "").lower()
            if any(t in name for t in terms):
                return True

            # Walk parent tree to check if any container is a terminal
            curr = accessible
            depth = 0
            while curr and depth < 6:
                curr_role = (curr.get_role_name() or "").lower()
                curr_name = (curr.get_name() or "").lower()
                if any(t in curr_role or t in curr_name for t in terms):
                    return True
                curr = curr.get_parent()
                depth += 1
        except Exception:
            pass
        return False

    @staticmethod
    async def _notify(title: str, body: str) -> None:
        if shutil.which("notify-send"):
            try:
                proc = await asyncio.create_subprocess_exec(
                    "notify-send",
                    "--expire-time=2500",
                    "--app-name=VoiceFlow",
                    title,
                    body,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
            except Exception:
                pass
