"""
System tray icon for VoiceFlow.

States:
  🟢 Active/Idle   — waiting for hotkey / ready
  ⏸️ Turned Off   — paused, hotkey listener inactive
  🔴 Recording    — mic is active
  🟡 Transcribing — sending audio to STT
  ⚪ Error        — all providers failed

Menu:
  - Status header
  - 'Turn Off' / 'Turn On' (toggle active listening)
  - 'Quick Activate / Trigger' (instantly start recording without hotkey)
  - 'Provider Status'
  - 'Open Config'
  - 'Quit / Close Daemon'
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

_CACHE_DIR = Path.home() / ".cache" / "voiceflow" / "icons"

_PYSTRAY_AVAILABLE = False
pystray: Any = None
Image: Any = None
ImageDraw: Any = None


def _init_pystray() -> bool:
    global _PYSTRAY_AVAILABLE, pystray, Image, ImageDraw
    if _PYSTRAY_AVAILABLE:
        return True

    try:
        import pystray as _pystray
        from PIL import Image as _Image, ImageDraw as _ImageDraw

        pystray = _pystray
        Image = _Image
        ImageDraw = _ImageDraw

        # Monkeypatch pystray AppIndicator so that icons have proper .png filenames in a theme directory.
        # Without this, GNOME Shell's AppIndicator extension cannot locate the temp files and displays
        # image-loading-symbolic (fallback three dots "...").
        if hasattr(pystray, "_appindicator") and hasattr(pystray._appindicator, "Icon"):

            def _patched_update_icon(self):
                _CACHE_DIR.mkdir(parents=True, exist_ok=True)
                icon_name = getattr(self, "_state_name", "voiceflow_active")
                self._icon_path = str(_CACHE_DIR / f"{icon_name}.png")
                try:
                    with open(self._icon_path, "wb") as f:
                        self.icon.save(f, "PNG")
                except Exception as exc:
                    log.debug("Tray: error saving icon PNG: %s", exc)
                self._icon_valid = True
                if hasattr(self, "_appindicator") and self._appindicator:
                    try:
                        self._appindicator.set_icon_theme_path(str(_CACHE_DIR))
                        title = getattr(self, "title", "VoiceFlow")
                        self._appindicator.set_icon_full(icon_name, title)
                    except Exception as exc:
                        log.debug("Tray: appindicator update error: %s", exc)

            def _patched_update_fs_icon(self):
                _CACHE_DIR.mkdir(parents=True, exist_ok=True)
                icon_name = getattr(self, "_state_name", "voiceflow_active")
                self._icon_path = str(_CACHE_DIR / f"{icon_name}.png")
                try:
                    with open(self._icon_path, "wb") as f:
                        self.icon.save(f, "PNG")
                except Exception as exc:
                    log.debug("Tray: error saving icon PNG: %s", exc)
                self._icon_valid = True

            def _patched_remove_fs_icon(self):
                # Keep cached PNG icons on disk so GNOME Shell never encounters an unlinked file
                self._icon_valid = False

            def _patched_show(self):
                icon_name = getattr(self, "_state_name", "voiceflow_active")
                if hasattr(self, "_appindicator") and self._appindicator:
                    self._appindicator.set_icon_theme_path(str(_CACHE_DIR))
                    title = getattr(self, "title", "VoiceFlow")
                    self._appindicator.set_icon_full(icon_name, title)
                    self._appindicator.set_menu(
                        self._menu_handle or self._create_default_menu()
                    )
                    self._appindicator.set_title(self.title)
                    self._appindicator.set_status(
                        pystray._appindicator.AppIndicator.IndicatorStatus.ACTIVE
                    )

            pystray._appindicator.Icon._update_icon = pystray._appindicator.mainloop(
                _patched_update_icon
            )
            pystray._appindicator.Icon._update_fs_icon = _patched_update_fs_icon
            pystray._appindicator.Icon._remove_fs_icon = _patched_remove_fs_icon
            pystray._appindicator.Icon._show = pystray._appindicator.mainloop(
                _patched_show
            )

        _PYSTRAY_AVAILABLE = True
        return True
    except Exception as exc:
        log.debug("Tray: pystray backend not ready (%s) — will retry when desktop connects", exc)
        return False


_init_pystray()


COLORS = {
    "active": "#10B981",  # Emerald Green (ready)
    "idle": "#10B981",  # Emerald Green
    "sleeping": "#475569",  # Slate Blue-Grey (sleeping)
    "paused": "#6B7280",  # Slate Grey (turned off)
    "recording": "#EF4444",  # Crimson Red
    "transcribing": "#F59E0B",  # Amber Yellow (processing)
    "error": "#EF4444",  # Red Error
}


def _make_icon(
    color: str,
    is_paused: bool = False,
    is_sleeping: bool = False,
    state: str = "active",
    target_size: int = 128,
) -> "Image.Image":
    """Generate a crisp high-res tray icon with 4x supersampled antialiasing."""
    scale = 4
    size = target_size * scale
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    margin = 8 * scale
    # Draw base outer circle
    draw.ellipse([margin, margin, size - margin, size - margin], fill=color)

    sleeping = is_sleeping or (state == "sleeping")
    paused = is_paused or (state == "paused")

    if sleeping:
        # Subtle crescent moon and 'z' sleep indicator
        mcx, mcy = size // 2 - 4 * scale, size // 2
        r = 26 * scale
        draw.ellipse([mcx - r, mcy - r, mcx + r, mcy + r], fill="#E2E8F0")
        draw.ellipse(
            [mcx - r + 16 * scale, mcy - r - 8 * scale, mcx + r + 16 * scale, mcy + r - 8 * scale],
            fill=color,
        )
        # Crisp 'z'
        zx, zy = size // 2 + 16 * scale, size // 2 - 24 * scale
        draw.line([zx, zy, zx + 14 * scale, zy], fill="#E2E8F0", width=4 * scale)
        draw.line([zx + 14 * scale, zy, zx, zy + 14 * scale], fill="#E2E8F0", width=4 * scale)
        draw.line(
            [zx, zy + 14 * scale, zx + 14 * scale, zy + 14 * scale], fill="#E2E8F0", width=4 * scale
        )
    elif state == "transcribing":
        # Dedicated processing / transcribing audio waveform glyph
        cx, cy = size // 2, size // 2
        bar_w = 8 * scale
        bar_heights = [28 * scale, 48 * scale, 68 * scale, 48 * scale, 28 * scale]
        bar_spacing = 14 * scale
        total_w = len(bar_heights) * bar_w + (len(bar_heights) - 1) * (bar_spacing - bar_w)
        start_x = cx - total_w // 2 + bar_w // 2

        for i, h in enumerate(bar_heights):
            x = start_x + i * bar_spacing
            draw.rounded_rectangle(
                [x - bar_w // 2, cy - h // 2, x + bar_w // 2, cy + h // 2],
                radius=bar_w // 2,
                fill="#FFFFFF",
            )
    elif state == "error":
        cx, cy = size // 2, size // 2
        draw.line([cx, cy - 28 * scale, cx, cy + 8 * scale], fill="#FFFFFF", width=8 * scale)
        draw.ellipse(
            [cx - 4 * scale, cy + 18 * scale, cx + 4 * scale, cy + 26 * scale], fill="#FFFFFF"
        )
    else:
        # Inner microphone glyph
        cx, cy = size // 2, size // 2
        cw, ch = 20 * scale, 40 * scale
        mic_color = "#FFFFFF" if not paused else "#D1D5DB"
        draw.rounded_rectangle(
            [cx - cw // 2, cy - ch // 2 - 8 * scale, cx + cw // 2, cy + ch // 2 - 8 * scale],
            radius=10 * scale,
            fill=mic_color,
        )
        # Mic stand / arc
        draw.arc(
            [cx - 20 * scale, cy - 12 * scale, cx + 20 * scale, cy + 28 * scale],
            start=0,
            end=180,
            fill=mic_color,
            width=6 * scale,
        )
        # Stand pole
        draw.line([cx, cy + 28 * scale, cx, cy + 44 * scale], fill=mic_color, width=6 * scale)
        # Stand base
        draw.line(
            [cx - 16 * scale, cy + 44 * scale, cx + 16 * scale, cy + 44 * scale],
            fill=mic_color,
            width=6 * scale,
        )

        if paused:
            # Draw red slash across
            draw.line(
                [
                    margin + 12 * scale,
                    size - margin - 12 * scale,
                    size - margin - 12 * scale,
                    margin + 12 * scale,
                ],
                fill="#DC2626",
                width=10 * scale,
            )

    return img.resize((target_size, target_size), Image.Resampling.LANCZOS)


class TrayIcon:
    """System tray icon with active/paused/sleeping states and dictation toggle."""

    def __init__(
        self,
        on_toggle_active: Optional[Callable[[bool], Any]] = None,
        on_trigger: Optional[Callable[[], Any]] = None,
        on_quit: Optional[Callable[[], Any]] = None,
        on_sleep: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._on_toggle_active = on_toggle_active
        self._on_trigger = on_trigger
        self._on_sleep = on_sleep
        self._on_quit = on_quit

        self._icon: Any = None
        self._thread: Optional[threading.Thread] = None
        self._status_rows: list[dict] = []
        self._is_active: bool = True
        self._current_state: str = "active"

        if not _PYSTRAY_AVAILABLE:
            _init_pystray()
        if not _PYSTRAY_AVAILABLE:
            log.info("TrayIcon: pystray not yet available (will retry when desktop session connects)")

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="voiceflow-tray")
        self._thread.start()

    def stop(self) -> None:
        if self._icon:
            try:
                self._icon.stop()  # type: ignore[union-attr]
            except Exception:
                pass

    @property
    def is_active(self) -> bool:
        return self._is_active

    def set_active(self, active: bool) -> None:
        """Turn on or turn off listening."""
        self._is_active = active
        self.set_state("active" if active else "paused")

    def set_state(self, state: str) -> None:
        """state: 'sleeping' | 'active' | 'idle' | 'paused' | 'recording' | 'transcribing' | 'error'"""
        self._current_state = state
        if not self._icon or not _PYSTRAY_AVAILABLE:
            return

        is_paused = (state == "paused") or not self._is_active
        is_sleeping = state == "sleeping"
        color = COLORS.get(state, COLORS["active"])

        titles = {
            "active": "VoiceFlow: ACTIVE (Hold Right Ctrl to dictate)",
            "idle": "VoiceFlow: ACTIVE (Hold Right Ctrl to dictate)",
            "sleeping": "VoiceFlow: 💤 Sleeping (Hold Right Ctrl to wake & dictate)",
            "paused": "VoiceFlow: TURNED OFF (Listening paused)",
            "recording": "VoiceFlow: 🎤 Recording…",
            "transcribing": "VoiceFlow: ⚡ Transcribing / Processing…",
            "error": "VoiceFlow: ❌ Error",
        }
        title_str = titles.get(state, "VoiceFlow")
        icon_name = f"voiceflow_{state}"

        try:
            setattr(self._icon, "_state_name", icon_name)
            self._icon.icon = _make_icon(color, is_paused=is_paused, is_sleeping=is_sleeping, state=state)  # type: ignore[union-attr]
            self._icon.title = title_str  # type: ignore[union-attr]
            if hasattr(self._icon, "_appindicator") and getattr(self._icon, "_appindicator", None):
                indicator = getattr(self._icon, "_appindicator")

                def _do_update():
                    try:
                        indicator.set_icon_theme_path(str(_CACHE_DIR))
                        indicator.set_icon_full(icon_name, title_str)
                    except Exception as exc:
                        log.debug("Tray: indicator update error: %s", exc)
                    return False

                try:
                    from gi.repository import GLib

                    GLib.idle_add(_do_update)
                except Exception:
                    try:
                        indicator.set_icon_theme_path(str(_CACHE_DIR))
                        indicator.set_icon_full(icon_name, title_str)
                    except Exception:
                        pass
            self._icon.update_menu()  # type: ignore[union-attr]
        except Exception as exc:
            log.warning("TrayIcon set_state error: %s", exc)

    def update_provider_status(self, rows: list[dict]) -> None:
        self._status_rows = rows
        if self._icon:
            try:
                self._icon.update_menu()  # type: ignore[union-attr]
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Menu Actions
    # ------------------------------------------------------------------ #

    def _toggle_action(self, icon=None, item=None) -> None:
        self._is_active = not self._is_active
        self.set_active(self._is_active)
        if self._on_toggle_active:
            self._on_toggle_active(self._is_active)

    def _trigger_action(self, icon=None, item=None) -> None:
        if self._on_trigger:
            self._on_trigger()

    def _sleep_action(self, icon=None, item=None) -> None:
        if self._on_sleep:
            self._on_sleep()

    def _show_status(self, icon=None, item=None) -> None:
        rows = self._status_rows
        state_str = self._current_state.upper()
        msg = f"VoiceFlow Status: {state_str}\n\n"
        if rows:
            for r in rows:
                msg += f"{r.get('provider',''):12s}: {r.get('status',''):12s} (req: {r.get('requests',0)}, err: {r.get('errors',0)})\n"
        else:
            msg += "Groq (Active)"
        try:
            import subprocess

            subprocess.Popen(["notify-send", "VoiceFlow Status", msg], stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def _open_config(self, icon=None, item=None) -> None:
        config_path = os.path.expanduser("~/.config/voiceflow/config.toml")
        try:
            import subprocess

            subprocess.Popen(["xdg-open", str(config_path)], stderr=subprocess.DEVNULL)
        except Exception:
            try:
                import subprocess

                subprocess.Popen(["gedit", str(config_path)], stderr=subprocess.DEVNULL)
            except Exception:
                pass

    def _quit(self, icon=None, item=None) -> None:
        def _async_quit() -> None:
            self.stop()
            if self._on_quit:
                self._on_quit()

        threading.Thread(target=_async_quit, daemon=True, name="voiceflow-quit").start()

    # ------------------------------------------------------------------ #
    # Tray Mainloop
    # ------------------------------------------------------------------ #

    def _build_menu(self) -> "pystray.Menu":
        return pystray.Menu(  # type: ignore[union-attr]
            pystray.MenuItem(  # type: ignore[union-attr]
                lambda item=None: (
                    "⏹️ Stop Recording"
                    if self._current_state == "recording"
                    else "⚡ Wake & Record"
                ),
                self._trigger_action,
                default=True,
            ),
            pystray.MenuItem(  # type: ignore[union-attr]
                lambda item=None: (
                    "☀️ Wake Up" if self._current_state == "sleeping" else "💤 Put to Sleep"
                ),
                self._sleep_action,
            ),
            pystray.MenuItem(  # type: ignore[union-attr]
                lambda item=None: "Turn Off" if self._is_active else "Turn On",
                self._toggle_action,
            ),
            pystray.Menu.SEPARATOR,  # type: ignore[union-attr]
            pystray.MenuItem("Status", self._show_status),  # type: ignore[union-attr]
            pystray.MenuItem("Settings / Config", self._open_config),  # type: ignore[union-attr]
            pystray.Menu.SEPARATOR,  # type: ignore[union-attr]
            pystray.MenuItem("Quit VoiceFlow", self._quit),  # type: ignore[union-attr]
        )

    def _run(self) -> None:
        import time

        for attempt in range(15):
            if _PYSTRAY_AVAILABLE:
                break
            time.sleep(2)
            if _init_pystray():
                break

        if not _PYSTRAY_AVAILABLE:
            log.warning(
                "TrayIcon: could not initialize system tray indicator after waiting for desktop session. Tray icon disabled."
            )
            return

        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            color = COLORS.get(self._current_state, COLORS["active"])
            is_paused = (self._current_state == "paused") or not self._is_active
            is_sleeping = self._current_state == "sleeping"
            icon_name = f"voiceflow_{self._current_state}"
            icon_img = _make_icon(
                color, is_paused=is_paused, is_sleeping=is_sleeping, state=self._current_state
            )

            self._icon = pystray.Icon(  # type: ignore[union-attr]
                name="voiceflow",
                icon=icon_img,
                title="VoiceFlow: ACTIVE (Hold Right Ctrl to dictate)",
                menu=self._build_menu(),
            )
            setattr(self._icon, "_state_name", icon_name)
            if hasattr(self._icon, "_appindicator") and self._icon._appindicator:
                self._icon._appindicator.set_icon_theme_path(str(_CACHE_DIR))
                self._icon._appindicator.set_icon_full(icon_name, "VoiceFlow: ACTIVE")

            self._icon.run()  # type: ignore[union-attr]
        except Exception as exc:
            log.warning("TrayIcon: could not initialize system tray indicator: %s", exc)
            log.info(
                "TrayIcon: on Ubuntu Wayland, run: sudo apt install gir1.2-ayatanaappindicator3-0.1"
            )
