"""Unit tests for VoiceFlow TrayIcon, sleeping state, and auto-sleep lifecycle."""

import pytest
from unittest.mock import MagicMock, patch

from voiceflow.tray import TrayIcon, COLORS, _make_icon
from voiceflow.config import DEFAULT_CONFIG, Config, load_config


def test_config_auto_sleep():
    cfg = Config()
    assert cfg.auto_sleep_seconds == 120

    loaded = load_config()
    assert hasattr(loaded, "auto_sleep_seconds")
    assert loaded.auto_sleep_seconds >= 0


def test_load_env_keys():
    import os
    from unittest.mock import patch, mock_open
    from voiceflow.config import load_env_keys

    fake_bashrc = 'export DEEPGRAM_API_KEY="test_mock_deepgram_key"\n'
    orig = os.environ.pop("DEEPGRAM_API_KEY", None)
    try:
        with (
            patch("pathlib.Path.exists", return_value=True),
            patch("builtins.open", mock_open(read_data=fake_bashrc)),
        ):
            load_env_keys()
            assert os.environ.get("DEEPGRAM_API_KEY") == "test_mock_deepgram_key"
    finally:
        if orig is not None:
            os.environ["DEEPGRAM_API_KEY"] = orig
        else:
            os.environ.pop("DEEPGRAM_API_KEY", None)


def test_tray_icon_colors_contains_sleeping():
    assert "sleeping" in COLORS
    assert COLORS["sleeping"] == "#475569"


def test_make_icon_sleeping_state():
    icon = _make_icon(COLORS["sleeping"], is_sleeping=True, state="sleeping")
    assert icon is not None
    assert icon.size == (128, 128)


def test_make_icon_transcribing_state():
    icon = _make_icon(COLORS["transcribing"], state="transcribing")
    assert icon is not None
    assert icon.size == (128, 128)
    # Ensure non-transparent pixels are drawn (RGBA alpha > 0)
    assert any(b > 0 for b in icon.tobytes()[3::4])


def test_make_icon_error_state():
    icon = _make_icon(COLORS["error"], state="error")
    assert icon is not None
    assert icon.size == (128, 128)


def test_tray_icon_state_transitions():
    tray = TrayIcon()

    # Initial state
    assert tray._current_state == "active"

    # Transition to sleeping
    tray.set_state("sleeping")
    assert tray._current_state == "sleeping"

    # Transition to recording
    tray.set_state("recording")
    assert tray._current_state == "recording"

    # Transition to paused
    tray.set_state("paused")
    assert tray._current_state == "paused"


def test_tray_icon_sleep_action_callback():
    on_sleep_mock = MagicMock()
    tray = TrayIcon(on_sleep=on_sleep_mock)

    tray._sleep_action(None, None)
    on_sleep_mock.assert_called_once()


def test_tray_icon_toggle_action_callback():
    on_toggle_mock = MagicMock()
    tray = TrayIcon(on_toggle_active=on_toggle_mock)

    # Initial state is active: toggle turns it off
    tray._toggle_action(None, None)
    assert tray.is_active is False
    on_toggle_mock.assert_called_with(False)

    # Toggle turns it back on
    tray._toggle_action(None, None)
    assert tray.is_active is True
    on_toggle_mock.assert_called_with(True)


def test_tray_icon_trigger_action_callback():
    on_trigger_mock = MagicMock()
    tray = TrayIcon(on_trigger=on_trigger_mock)

    tray._trigger_action(None, None)
    on_trigger_mock.assert_called_once()


def test_osd_show_result_without_type_error():
    from voiceflow.osd import OSD

    osd = OSD(notification_mode="verbose")
    try:
        # Should not raise TypeError: arguments did not match any overloaded call
        osd.show_recording()
        osd.show_transcribing()
        osd.show_result("Test transcription result")
        osd.show_error("Test error")
        osd.hide()
    finally:
        osd.stop()


def test_tray_set_state_with_mocked_appindicator():
    tray = TrayIcon()
    mock_icon = MagicMock()
    mock_appind = MagicMock()
    mock_icon._appindicator = mock_appind
    tray._icon = mock_icon

    mock_glib = MagicMock()
    with patch.dict("sys.modules", {"gi": MagicMock(), "gi.repository": MagicMock(GLib=mock_glib)}):
        tray.set_state("transcribing")
        assert tray._current_state == "transcribing"
        assert mock_glib.idle_add.called

        tray.set_state("active")
        assert tray._current_state == "active"


def test_init_pystray_success():
    from voiceflow.tray import _init_pystray, _PYSTRAY_AVAILABLE

    assert _init_pystray() is True
    assert _PYSTRAY_AVAILABLE is True


def test_tray_start_thread_launches():
    tray = TrayIcon()
    with patch("threading.Thread") as mock_thread_cls:
        mock_thread = MagicMock()
        mock_thread_cls.return_value = mock_thread
        tray.start()
        mock_thread.start.assert_called_once()
