"""Tests for OSD module (mocked subprocess IPC & notification fallback)."""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch
import pytest

from voiceflow.osd import OSD


class TestOSD:
    def test_init_no_qt_env(self, monkeypatch):
        monkeypatch.setenv("VOICEFLOW_NO_QT", "1")
        with patch("subprocess.Popen") as mock_popen:
            osd = OSD(notification_mode="verbose")
            assert not osd._use_qt
            assert osd._proc is None
            mock_popen.assert_not_called()

    def test_init_verbose_spawns_process(self, monkeypatch):
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.delenv("VOICEFLOW_NO_QT", raising=False)
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.pid = 12345

        with (
            patch("voiceflow.osd._QT_AVAILABLE", True),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
        ):
            osd = OSD(notification_mode="verbose", position="top-right", dismiss_after_ms=1500)
            assert osd._use_qt is True
            assert osd._proc is mock_proc
            mock_popen.assert_called_once()
            args = mock_popen.call_args[0][0]
            assert "voiceflow.osd" in args
            assert "--position" in args and "top-right" in args
            assert "--dismiss-ms" in args and "1500" in args

    def test_commands_sent_to_subprocess(self, monkeypatch):
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.delenv("VOICEFLOW_NO_QT", raising=False)
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.stdin = io.StringIO()

        with (
            patch("voiceflow.osd._QT_AVAILABLE", True),
            patch("subprocess.Popen", return_value=mock_proc),
        ):
            osd = OSD(notification_mode="verbose")
            osd.show_recording()
            osd.show_transcribing()
            osd.show_result("hello world")
            osd.show_error("something failed")
            osd.hide()

            commands = mock_proc.stdin.getvalue().splitlines()
            assert commands == [
                "RECORDING",
                "TRANSCRIBING",
                "RESULT hello world",
                "ERROR something failed",
                "HIDE",
            ]

    def test_errors_only_mode_suppresses_transcription_and_result(self, monkeypatch):
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.delenv("VOICEFLOW_NO_QT", raising=False)
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.stdin = io.StringIO()

        with (
            patch("voiceflow.osd._QT_AVAILABLE", True),
            patch("subprocess.Popen", return_value=mock_proc),
        ):
            osd = OSD(notification_mode="errors_only")
            osd.show_recording()
            osd.show_transcribing()
            osd.show_result("ignored")
            osd.show_error("fatal error")

            commands = mock_proc.stdin.getvalue().splitlines()
            assert commands == ["ERROR fatal error"]

    def test_send_cmd_handles_broken_pipe_and_falls_back(self, monkeypatch):
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.delenv("VOICEFLOW_NO_QT", raising=False)
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_stdin = MagicMock()
        mock_stdin.write.side_effect = BrokenPipeError("Pipe broken")
        mock_proc.stdin = mock_stdin

        with (
            patch("voiceflow.osd._QT_AVAILABLE", True),
            patch("subprocess.Popen", return_value=mock_proc),
            patch.object(OSD, "_notify_send") as mock_notify,
        ):
            osd = OSD(notification_mode="verbose")
            assert osd._use_qt is True

            osd.show_recording()
            assert osd._use_qt is False
            mock_notify.assert_called_once_with("🎤 Recording…")

    def test_stop_terminates_subprocess(self, monkeypatch):
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.delenv("VOICEFLOW_NO_QT", raising=False)
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdin.closed = False

        with (
            patch("voiceflow.osd._QT_AVAILABLE", True),
            patch("subprocess.Popen", return_value=mock_proc),
        ):
            osd = OSD(notification_mode="verbose")
            osd.stop()

            mock_proc.stdin.write.assert_called_with("QUIT\n")
            mock_proc.stdin.flush.assert_called_once()
            mock_proc.stdin.close.assert_called_once()
            mock_proc.wait.assert_called_once()
            assert osd._proc is None
            assert not osd._use_qt

    def test_notify_send_invokes_binary(self):
        with (
            patch("shutil.which", return_value="/usr/bin/notify-send"),
            patch("subprocess.Popen") as mock_popen,
        ):
            OSD._notify_send("Test Notification")
            mock_popen.assert_called_once()
            cmd = mock_popen.call_args[0][0]
            assert cmd[0] == "notify-send"
            assert "Test Notification" in cmd
