"""
On-screen display (OSD) overlay — shows recording/transcribing status.

A small floating bar at the bottom/top of the screen (like Wispr Flow's UI):
  🔴 Recording...  →  ⚙️ Transcribing...  →  ✅ [text preview]

Architecture:
  - When Qt is available and a desktop display exists, the OSD GUI runs in
    an isolated helper subprocess (`python -m voiceflow.osd`).
    This guarantees QApplication runs strictly on its own main thread,
    preventing any GLib context conflicts, thread affinity crashes, or
    static destruction segfaults in the main daemon or test runner.
  - The daemon communicates with the helper via a lightweight stdin pipe.
  - If Qt is not available or in headless environments, it falls back
    gracefully to desktop notifications via notify-send.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import threading
from typing import Optional

log = logging.getLogger(__name__)

_QT_AVAILABLE = False
try:
    from PyQt6.QtWidgets import QApplication, QLabel, QWidget, QVBoxLayout
    from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QThread
    from PyQt6.QtGui import QFont, QColor, QPainter, QPainterPath

    _QT_AVAILABLE = True
except ImportError:
    pass


class OSDState:
    IDLE = "idle"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    RESULT = "result"


class OSD:
    """
    On-screen display overlay for VoiceFlow status.
    Thread-safe — all public methods can be called from any thread/coroutine.
    """

    def __init__(
        self,
        position: str = "bottom-center",
        dismiss_after_ms: int = 2000,
        notification_mode: str = "minimal",
        show_errors: bool = True,
    ) -> None:
        self._position = position
        self._dismiss_ms = dismiss_after_ms
        self._mode = notification_mode  # "errors_only" | "minimal" | "verbose"
        self._show_errors = show_errors
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._use_qt = False

        has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        no_qt_env = bool(os.environ.get("VOICEFLOW_NO_QT"))

        if _QT_AVAILABLE and has_display and not no_qt_env:
            try:
                cmd = [
                    sys.executable,
                    "-m",
                    "voiceflow.osd",
                    "--position",
                    self._position,
                    "--dismiss-ms",
                    str(self._dismiss_ms),
                ]
                self._proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
                self._use_qt = True
                log.debug("OSD: started isolated Qt GUI subprocess (PID %d)", self._proc.pid)
            except Exception as exc:
                log.warning(
                    "OSD: failed to spawn Qt subprocess (%s), using notify-send fallback", exc
                )
                self._proc = None
                self._use_qt = False
        else:
            log.debug("OSD: using notify-send fallback (mode=%s)", self._mode)

    # ------------------------------------------------------------------ #
    # Public API — all thread-safe
    # ------------------------------------------------------------------ #

    def show_recording(self) -> None:
        if self._mode == "verbose":
            self._send_or_notify("RECORDING", "🎤 Recording…")

    def show_transcribing(self) -> None:
        if self._mode == "verbose":
            self._send_or_notify("TRANSCRIBING", "⚙️  Transcribing…")

    def show_result(self, text: str) -> None:
        if self._mode == "errors_only":
            return
        preview = text[:80] + ("…" if len(text) > 80 else "")
        self._send_or_notify(f"RESULT {preview}", f"✅  {preview}")

    def show_error(self, message: str) -> None:
        if not self._show_errors:
            log.warning("OSD show_error suppressed per configuration: %s", message)
            return
        self._send_or_notify(f"ERROR {message}", f"❌  {message}")

    def hide(self) -> None:
        self._send_cmd("HIDE")

    def stop(self) -> None:
        """Cleanly terminate the OSD subprocess."""
        with self._lock:
            if self._proc:
                try:
                    if self._proc.stdin and not self._proc.stdin.closed:
                        self._proc.stdin.write("QUIT\n")
                        self._proc.stdin.flush()
                        self._proc.stdin.close()
                    self._proc.wait(timeout=1.0)
                except Exception:
                    try:
                        self._proc.terminate()
                        self._proc.wait(timeout=0.5)
                    except Exception:
                        pass
                self._proc = None
            self._use_qt = False

    # ------------------------------------------------------------------ #
    # Internal IPC / Fallback Helpers
    # ------------------------------------------------------------------ #

    def _send_cmd(self, command: str) -> bool:
        with self._lock:
            if not self._use_qt or not self._proc or not self._proc.stdin:
                return False
            if self._proc.poll() is not None:
                self._use_qt = False
                return False
            try:
                self._proc.stdin.write(command + "\n")
                self._proc.stdin.flush()
                return True
            except Exception as exc:
                log.debug("OSD: error sending command to subprocess: %s", exc)
                self._use_qt = False
                return False

    def _send_or_notify(self, command: str, fallback_text: str) -> None:
        if not self._send_cmd(command):
            self._notify_send(fallback_text)

    @staticmethod
    def _notify_send(text: str) -> None:
        if shutil.which("notify-send"):
            try:
                subprocess.Popen(
                    [
                        "notify-send",
                        "--expire-time=2000",
                        "--app-name=VoiceFlow",
                        "VoiceFlow",
                        text,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass


# ------------------------------------------------------------------ #
# Qt Subprocess Implementation (Runs strictly on process main thread)
# ------------------------------------------------------------------ #

if _QT_AVAILABLE:

    class _OSDWindow(QWidget):  # type: ignore[misc]
        """Frameless, always-on-top floating OSD bar."""

        def __init__(self, position: str = "bottom-center") -> None:
            super().__init__()
            self._position = position
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.Tool
                | Qt.WindowType.BypassWindowManagerHint
                | Qt.WindowType.WindowDoesNotAcceptFocus
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

            layout = QVBoxLayout(self)
            layout.setContentsMargins(16, 8, 16, 8)
            self._label = QLabel("")
            self._label.setFont(QFont("Sans Serif", 14, QFont.Weight.Medium))
            self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(self._label)

            self._bg_color = "#1A1A2E"
            self._text_color = "#FFFFFF"
            self.setFixedHeight(52)

        def set_text(self, text: str, text_color: str, bg_color: str) -> None:
            self._text_color = text_color
            self._bg_color = bg_color
            self._label.setText(text)
            self._label.setStyleSheet(f"color: {text_color}; background: transparent;")
            self.adjustSize()
            self.setFixedHeight(52)
            self._reposition()
            self.update()

        def _reposition(self) -> None:
            screen = QApplication.primaryScreen()
            if not screen:
                return
            geom = screen.geometry()
            w = max(self.width(), 400)
            self.setFixedWidth(w)
            if self._position == "bottom-center":
                x = geom.x() + (geom.width() - w) // 2
                y = geom.y() + geom.height() - 80
            elif self._position == "top-center":
                x = geom.x() + (geom.width() - w) // 2
                y = geom.y() + 20
            else:
                x = geom.x() + (geom.width() - w) // 2
                y = geom.y() + geom.height() - 80
            self.move(x, y)

        def paintEvent(self, event) -> None:  # pylint: disable=unused-argument
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            path = QPainterPath()
            path.addRoundedRect(0.0, 0.0, float(self.width()), float(self.height()), 12.0, 12.0)
            painter.fillPath(path, QColor(self._bg_color))
            painter.end()

    class _StdinReaderThread(QThread):
        """Worker thread to read commands from stdin and emit them to the Qt GUI thread."""

        command_received = pyqtSignal(str)

        def run(self) -> None:
            try:
                for line in sys.stdin:
                    clean = line.strip()
                    if clean:
                        self.command_received.emit(clean)
                    if clean == "QUIT":
                        break
            except Exception:
                pass


def _run_osd_process() -> None:
    """Entry point when executed as `python -m voiceflow.osd`."""
    if not _QT_AVAILABLE:
        sys.exit(1)

    parser = argparse.ArgumentParser(description="VoiceFlow OSD subprocess")
    parser.add_argument("--position", default="bottom-center")
    parser.add_argument("--dismiss-ms", type=int, default=2000)
    args = parser.parse_args()

    app = QApplication(sys.argv)
    win = _OSDWindow(position=args.position)
    dismiss_timer = QTimer()
    dismiss_timer.setSingleShot(True)
    dismiss_timer.timeout.connect(win.hide)

    def on_command(cmd: str) -> None:
        dismiss_timer.stop()
        if cmd == "RECORDING":
            win.set_text("🎤 Recording…", "#E53935", "#FFEBEE")
            win.show()
            win.raise_()
        elif cmd == "TRANSCRIBING":
            win.set_text("⚙️  Transcribing…", "#1565C0", "#E3F2FD")
            win.show()
            win.raise_()
        elif cmd.startswith("RESULT "):
            text = cmd[7:]
            win.set_text(text, "#2E7D32", "#E8F5E9")
            win.show()
            win.raise_()
            dismiss_timer.start(args.dismiss_ms)
        elif cmd.startswith("ERROR "):
            text = cmd[6:]
            win.set_text(text, "#BF360C", "#FBE9E7")
            win.show()
            win.raise_()
            dismiss_timer.start(args.dismiss_ms)
        elif cmd == "HIDE":
            win.hide()
        elif cmd == "QUIT":
            win.close()
            app.quit()

    reader = _StdinReaderThread()
    reader.command_received.connect(on_command)
    reader.start()

    app.exec()
    reader.wait(500)
    sys.exit(0)


if __name__ == "__main__":
    _run_osd_process()
