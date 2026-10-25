"""
Target Window Tracker for VoiceFlow.

Remembers the window and focused widget where dictation was initiated ("Anchor Mode").
If the user switches to another window while speaking (e.g. to read docs or view a reference),
VoiceFlow restores focus to the original starting window and text field before injecting text.

Uses the Linux AT-SPI2 accessibility architecture (preinstalled on Ubuntu GNOME).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from typing import Any, Optional

log = logging.getLogger(__name__)

# System daemons / compositors that should never be selected as user target windows
SYSTEM_DAEMONS = {
    "gnome-shell",
    "gsd-keyboard",
    "gsd-color",
    "gsd-media-keys",
    "gsd-power",
    "gsd-wacom",
    "ibus-extension-gtk3",
    "ibus-x11",
    "evolution-alarm-notify",
    "xdg-desktop-portal-gtk",
    "xdg-desktop-portal-gnome",
    "update-notifier",
    "gjs",
}

import ctypes
import ctypes.util

_C_LOG_HANDLER = None


def _suppress_glib_noise() -> None:
    """Permanently silence C-level GLib warnings from dbind and Atspi (e.g. GetItems missing cache)."""
    global _C_LOG_HANDLER
    if _C_LOG_HANDLER is not None:
        return
    try:
        glib_lib = ctypes.util.find_library("glib-2.0")
        if not glib_lib:
            return
        glib = ctypes.CDLL(glib_lib)
        GLOG_FUNC = ctypes.CFUNCTYPE(
            None, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p
        )

        def _null_handler(domain, level, message, user_data):
            pass

        _C_LOG_HANDLER = GLOG_FUNC(_null_handler)
        for domain in (b"dbind", b"Atspi", b"atk-bridge"):
            glib.g_log_set_handler(domain, 0xFF, _C_LOG_HANDLER, None)
    except Exception:
        pass


def _ensure_atspi_bus() -> None:
    """Ensure AT_SPI_BUS_ADDRESS is correctly configured to silence libdbind warnings."""
    if os.environ.get("AT_SPI_BUS_ADDRESS"):
        return
    try:
        uid = os.getuid()
        std_socket = f"/run/user/{uid}/at-spi/bus_0"
        if os.path.exists(std_socket):
            os.environ["AT_SPI_BUS_ADDRESS"] = f"unix:path={std_socket}"
            return
        out = subprocess.check_output(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                "org.a11y.Bus",
                "--object-path",
                "/org/a11y/bus",
                "--method",
                "org.a11y.Bus.GetAddress",
            ],
            stderr=subprocess.DEVNULL,
            timeout=0.5,
        ).decode()
        if "'" in out:
            os.environ["AT_SPI_BUS_ADDRESS"] = out.split("'")[1]
    except Exception:
        pass


_ATSPI_AVAILABLE = False
try:
    _suppress_glib_noise()
    _ensure_atspi_bus()
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    # Suppress C-level stderr during Atspi.init to eliminate libdbind warning
    old_stderr = os.dup(2)
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, 2)
        Atspi.init()
    finally:
        os.dup2(old_stderr, 2)
        os.close(devnull)
        os.close(old_stderr)

    _ATSPI_AVAILABLE = True
except Exception as exc:
    log.debug("WindowTracker: Atspi not available (%s)", exc)


async def simulate_alt_tab() -> bool:
    """Simulate a quick Alt+Tab to restore focus to the previous window."""
    try:
        from evdev import ecodes, UInput

        cap: dict[Any, Any] = {ecodes.EV_KEY: [ecodes.KEY_LEFTALT, ecodes.KEY_TAB]}
        with UInput(cap, name="voiceflow-window-switcher") as ui:
            ui.write(ecodes.EV_KEY, ecodes.KEY_LEFTALT, 1)
            ui.write(ecodes.EV_KEY, ecodes.KEY_TAB, 1)
            ui.syn()
            await asyncio.sleep(0.04)
            ui.write(ecodes.EV_KEY, ecodes.KEY_TAB, 0)
            ui.write(ecodes.EV_KEY, ecodes.KEY_LEFTALT, 0)
            ui.syn()
        return True
    except Exception:
        pass

    if shutil.which("xdotool"):
        try:
            proc = await asyncio.create_subprocess_exec(
                "xdotool",
                "key",
                "alt+Tab",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            return proc.returncode == 0
        except Exception:
            pass
    return False


class WindowAnchor:
    """Represents the application, window, and widget where speech started."""

    def __init__(
        self,
        app_name: str,
        window_title: str,
        accessible_component: Any = None,
        accessible_app: Any = None,
    ) -> None:
        self.app_name = app_name
        self.window_title = window_title
        self.accessible_component = accessible_component
        self.accessible_app = accessible_app

    def is_terminal(self) -> bool:
        """Check if this anchor represents a terminal emulator."""
        name = (self.app_name or "").lower()
        title = (self.window_title or "").lower()
        role = ""
        if self.accessible_component:
            try:
                role = (self.accessible_component.get_role_name() or "").lower()
            except Exception:
                pass

        if "terminal" in role:
            return True
        for term_name in (
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
            "urxvt",
            "st",
            "tabby",
            "contour",
            "blackbox",
            "warp",
        ):
            if term_name in name:
                return True
        return False

    def __repr__(self) -> str:
        role = None
        if self.accessible_component:
            try:
                role = getattr(self.accessible_component, "get_role_name", lambda: None)()
            except Exception:
                pass
        return f"<WindowAnchor app={self.app_name!r} title={self.window_title!r} role={role!r}>"


class WindowTracker:
    """Tracks active window focus and restores it upon dictation completion."""

    def __init__(self) -> None:
        self._anchor: Optional[WindowAnchor] = None

    def capture_anchor(self) -> Optional[WindowAnchor]:
        """
        Record the currently active window and focused component.
        Called at the exact moment recording begins.
        """
        if not _ATSPI_AVAILABLE:
            self._anchor = self._capture_anchor_fallback()
            return self._anchor

        try:
            desktop = Atspi.get_desktop(0)
            child_count = desktop.get_child_count()

            # Priority 1: Find the top-level user window with StateType.ACTIVE
            for i in range(child_count):
                app = desktop.get_child_at_index(i)
                if not app:
                    continue
                app_name = app.get_name() or ""
                if app_name in SYSTEM_DAEMONS:
                    continue

                for j in range(app.get_child_count()):
                    win = app.get_child_at_index(j)
                    if not win:
                        continue
                    try:
                        states = win.get_state_set()
                        if states.contains(Atspi.StateType.ACTIVE):
                            focused = self._find_focused_widget(win) or win
                            win_title = (
                                win.get_name() or self._get_parent_window_title(focused) or app_name
                            )
                            self._anchor = WindowAnchor(
                                app_name=app_name,
                                window_title=win_title,
                                accessible_component=focused,
                                accessible_app=app,
                            )
                            log.info("WindowTracker: captured active anchor -> %s", self._anchor)
                            return self._anchor
                    except Exception:
                        continue

            # Priority 2: Any non-system app with a FOCUSED child
            for i in range(child_count):
                app = desktop.get_child_at_index(i)
                if not app or (app.get_name() or "") in SYSTEM_DAEMONS:
                    continue
                focused = self._find_focused_widget(app)
                if focused:
                    app_name = app.get_name() or "UnknownApp"
                    win_title = self._get_parent_window_title(focused) or app_name
                    self._anchor = WindowAnchor(
                        app_name=app_name,
                        window_title=win_title,
                        accessible_component=focused,
                        accessible_app=app,
                    )
                    log.info("WindowTracker: captured focused fallback anchor -> %s", self._anchor)
                    return self._anchor

        except Exception as exc:
            log.debug("WindowTracker: failed to capture anchor via Atspi: %s", exc)

        self._anchor = self._capture_anchor_fallback()
        return self._anchor

    async def restore_focus(self) -> bool:
        """
        Refocus the anchor window if focus shifted during dictation.
        Returns True if focus was successfully restored or was already on anchor.
        """
        if not self._anchor:
            return False

        current_anchor = self._get_current_active()
        if current_anchor and current_anchor.app_name == self._anchor.app_name:
            log.debug("WindowTracker: already focused on anchor window (%s)", self._anchor.app_name)
            return True

        # SAFETY GUARD: If we cannot reliably identify the current window OR the anchor window
        # was just a generic fallback, NEVER simulate Alt+Tab. Doing so would kick the user
        # out of their active window into another app.
        if (
            not current_anchor
            or self._anchor.app_name in ("ActiveApp", "App", "UnknownApp", "")
            or current_anchor.app_name in ("ActiveApp", "App", "UnknownApp", "")
            or current_anchor.app_name in SYSTEM_DAEMONS
        ):
            log.debug(
                "WindowTracker: focus state uncertain or fallback anchor — leaving current window focused"
            )
            return True

        log.info(
            "WindowTracker: verified focus change from %s to %s — restoring focus to original window!",
            current_anchor.app_name,
            self._anchor.app_name,
        )

        # Method 1: Try Atspi Component.grab_focus()
        if self._anchor.accessible_component:
            try:
                comp = self._anchor.accessible_component.get_component_iface()
                if comp and hasattr(comp, "grab_focus"):
                    comp.grab_focus()
                    await asyncio.sleep(0.05)
                    log.debug("WindowTracker: restored focus via Atspi Component.grab_focus")
                    return True
            except Exception as exc:
                log.debug("WindowTracker: Atspi grab_focus failed: %s", exc)

        # Method 2: In GNOME / Wayland, simulate Alt+Tab ONLY when a real, verified switch away happened
        try:
            await simulate_alt_tab()
            await asyncio.sleep(0.08)
            log.debug("WindowTracker: restored focus via Alt+Tab toggle")
            return True
        except Exception as exc:
            log.debug("WindowTracker: Alt+Tab restoration failed: %s", exc)

        return False

    @property
    def current_anchor(self) -> Optional[WindowAnchor]:
        return self._anchor

    def clear(self) -> None:
        self._anchor = None

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _find_focused_widget(self, accessible: Any, depth: int = 0) -> Any:
        """Recursively find widget with FOCUSED state."""
        if depth > 10:
            return None
        try:
            state_set = accessible.get_state_set()
            if state_set.contains(Atspi.StateType.FOCUSED):
                return accessible

            for i in range(accessible.get_child_count()):
                child = accessible.get_child_at_index(i)
                if child:
                    res = self._find_focused_widget(child, depth + 1)
                    if res:
                        return res
        except Exception:
            pass
        return None

    def _get_parent_window_title(self, accessible: Any) -> str:
        """Climb the accessible tree to find the top-level window title."""
        curr = accessible
        while curr:
            try:
                role = curr.get_role_name()
                if role in ("frame", "window", "dialog"):
                    return curr.get_name() or ""
                curr = curr.get_parent()
            except Exception:
                break
        return ""

    def _get_current_active(self) -> Optional[WindowAnchor]:
        """Check what non-system window currently has focus right now."""
        if not _ATSPI_AVAILABLE:
            return None
        try:
            desktop = Atspi.get_desktop(0)
            for i in range(desktop.get_child_count()):
                app = desktop.get_child_at_index(i)
                if not app or (app.get_name() or "") in SYSTEM_DAEMONS:
                    continue
                for j in range(app.get_child_count()):
                    win = app.get_child_at_index(j)
                    if not win:
                        continue
                    try:
                        states = win.get_state_set()
                        if states.contains(Atspi.StateType.ACTIVE):
                            focused = self._find_focused_widget(win) or win
                            return WindowAnchor(
                                app_name=app.get_name() or "UnknownApp",
                                window_title=win.get_name() or "",
                                accessible_component=focused,
                                accessible_app=app,
                            )
                    except Exception:
                        continue
        except Exception:
            pass
        return None

    def _capture_anchor_fallback(self) -> WindowAnchor:
        """Fallback when Atspi is not active: check active window title via xdotool or proc."""
        try:
            res = (
                subprocess.check_output(
                    ["xdotool", "getactivewindow", "getwindowname"],
                    stderr=subprocess.DEVNULL,
                    timeout=0.2,
                )
                .decode()
                .strip()
            )
            return WindowAnchor(app_name="App", window_title=res)
        except Exception:
            return WindowAnchor(app_name="ActiveApp", window_title="ActiveWindow")
