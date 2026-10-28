"""Tests for TextInjector and WindowTracker."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from voiceflow.injector import TextInjector, DisplayEnvironment
from voiceflow.window_tracker import WindowTracker, WindowAnchor


class TestTextInjector:
    def test_init_detects_environment(self):
        injector = TextInjector()
        assert injector._env in (
            DisplayEnvironment.WAYLAND,
            DisplayEnvironment.X11,
            DisplayEnvironment.TTY,
        )

    @pytest.mark.asyncio
    async def test_inject_empty_string_noop(self):
        injector = TextInjector()
        res = await injector.inject("")
        assert res is True

    @pytest.mark.asyncio
    async def test_inject_atspi_accessible(self):
        injector = TextInjector()
        mock_acc = MagicMock()
        mock_acc.insert_text = MagicMock(return_value=True)

        with (
            patch("voiceflow.injector._ATSPI_AVAILABLE", True),
            patch.object(injector, "_inject_atspi", AsyncMock(return_value=True)) as mock_atspi,
        ):
            res = await injector.inject("hello world", target_accessible=mock_acc)
            assert res is True
            mock_atspi.assert_called_once_with("hello world", mock_acc)

    @pytest.mark.asyncio
    async def test_inject_terminal_routes_to_uinput_terminal(self):
        injector = TextInjector()
        mock_acc = MagicMock()
        mock_acc.get_role_name.return_value = "terminal"

        with (
            patch.object(injector, "_inject_atspi", AsyncMock(return_value=True)) as mock_atspi,
            patch.object(injector, "_set_clipboard", AsyncMock(return_value=True)),
            patch.object(
                injector, "_inject_uinput_paste", AsyncMock(return_value=True)
            ) as mock_uinput,
        ):
            injector._uinput = MagicMock()
            res = await injector.inject(
                "terminal text", target_accessible=mock_acc, is_terminal=True
            )
            assert res is True
            # For terminals, AT-SPI direct insert should be skipped!
            mock_atspi.assert_not_called()
            # UInput paste should be called with is_terminal=True
            mock_uinput.assert_called_once_with(True)

    @pytest.mark.asyncio
    async def test_inject_atspi_length_param(self):
        injector = TextInjector()
        mock_acc = MagicMock()
        mock_acc.insert_text = MagicMock(return_value=True)
        ok = await injector._inject_atspi("café crème 🚀", mock_acc)
        assert ok is True
        mock_acc.insert_text.assert_called_once_with(-1, "café crème 🚀", -1)

    @pytest.mark.asyncio
    async def test_inject_uinput_paste_releases_modifiers_on_cancel(self):
        import asyncio
        from evdev import ecodes

        injector = TextInjector()
        mock_ui = MagicMock()
        injector._uinput = mock_ui

        call_count = 0
        original_sleep = asyncio.sleep

        async def fake_sleep(dur):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise asyncio.CancelledError()
            await original_sleep(0.0001)

        with patch("asyncio.sleep", fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await injector._inject_uinput_paste(is_terminal=False)

        # Verify that release write calls (value=0) happened even after CancelledError
        writes = mock_ui.write.call_args_list
        release_calls = [c for c in writes if c[0][2] == 0]
        assert len(release_calls) >= 2


class TestWindowTracker:
    def test_capture_and_clear_anchor(self):
        tracker = WindowTracker()
        anchor = tracker.capture_anchor()
        assert anchor is not None
        assert isinstance(anchor.app_name, str)
        tracker.clear()
        assert tracker._anchor is None

    def test_window_anchor_is_terminal(self):
        mock_term = MagicMock()
        mock_term.get_role_name.return_value = "terminal"
        anchor1 = WindowAnchor(
            app_name="gnome-terminal-server", window_title="bash", accessible_component=mock_term
        )
        assert anchor1.is_terminal() is True

        mock_gui = MagicMock()
        mock_gui.get_role_name.return_value = "frame"
        anchor2 = WindowAnchor(
            app_name="google-chrome", window_title="Chrome", accessible_component=mock_gui
        )
        assert anchor2.is_terminal() is False

    @pytest.mark.asyncio
    async def test_restore_focus_noop_when_no_anchor(self):
        tracker = WindowTracker()
        tracker.clear()
        res = await tracker.restore_focus()
        assert res is False
