import unittest
from unittest.mock import MagicMock, patch
from voiceflow.hotkey import HotkeyListener, HotkeyMode


class TestHotkeyEvdev(unittest.TestCase):
    def test_device_filtering_by_hotkey(self):
        """Only devices having the specific key_code in EV_KEY capabilities should be monitored."""
        listener = HotkeyListener(hotkey="KEY_RIGHTCTRL")

        dev_keyboard = MagicMock()
        dev_keyboard.path = "/dev/input/event8"
        dev_keyboard.name = "Real Keyboard"
        dev_keyboard.capabilities.return_value = {1: [97, 28, 1]}  # 97 is KEY_RIGHTCTRL

        dev_mouse = MagicMock()
        dev_mouse.path = "/dev/input/event22"
        dev_mouse.name = "Mouse"
        dev_mouse.capabilities.return_value = {1: [272, 273]}  # Mouse buttons only

        with patch("evdev.list_devices", return_value=["/dev/input/event8", "/dev/input/event22"]):
            with patch(
                "evdev.InputDevice",
                side_effect=lambda p: dev_keyboard if p == "/dev/input/event8" else dev_mouse,
            ):
                kbs = listener._get_keyboards(97)
                self.assertIn(dev_keyboard, kbs)
                self.assertNotIn(dev_mouse, kbs)
                dev_mouse.close.assert_called_once()

    def test_evdev_loop_removes_dead_device_on_oserror(self):
        """When a device throws OSError (disconnect/HUP), _evdev_loop must remove and close it."""
        listener = HotkeyListener(hotkey="KEY_RIGHTCTRL")

        dead_kb = MagicMock()
        dead_kb.fd = 10
        dead_kb.path = "/dev/input/event10"
        dead_kb.name = "Unplugged Device"
        dead_kb.read.side_effect = OSError(19, "No such device")

        def stop_after_one_select(*args, **kwargs):
            listener.stop()
            return ([dead_kb.fd], [], [])

        with (
            patch("evdev.ecodes.KEY_RIGHTCTRL", 97),
            patch.object(listener, "_get_keyboards", return_value=[dead_kb]),
            patch("select.select", side_effect=stop_after_one_select),
        ):
            listener._evdev_loop()

        dead_kb.close.assert_called()

    def test_evdev_loop_retains_device_on_blockingioerror(self):
        """When a device throws BlockingIOError (buffer drained / EOF), it must NOT be removed or closed."""
        listener = HotkeyListener(hotkey="KEY_RIGHTCTRL")

        kb = MagicMock()
        kb.fd = 10
        kb.path = "/dev/input/event2"
        kb.name = "AT Translated Set 2 keyboard"
        kb.read.side_effect = BlockingIOError(11, "Resource temporarily unavailable")

        iterations = 0

        def select_mock(*args, **kwargs):
            nonlocal iterations
            iterations += 1
            if iterations == 1:
                return ([kb.fd], [], [])
            # On second iteration, verify kb was NOT closed/removed during the first iteration
            self.assertEqual(kb.close.call_count, 0)
            listener.stop()
            return ([], [], [])

        with (
            patch("evdev.ecodes.KEY_RIGHTCTRL", 97),
            patch.object(listener, "_get_keyboards", return_value=[kb]),
            patch("select.select", side_effect=select_mock),
        ):
            listener._evdev_loop()

    def test_device_filtering_ignores_virtual_injection_devices(self):
        """Virtual injection devices like ydotoold and voiceflow-virtual-keyboard must be excluded."""
        listener = HotkeyListener(hotkey="KEY_RIGHTCTRL")

        dev_hw = MagicMock()
        dev_hw.path = "/dev/input/event2"
        dev_hw.name = "AT Translated Set 2 keyboard"
        dev_hw.capabilities.return_value = {1: [97, 28, 1]}

        dev_ydotool = MagicMock()
        dev_ydotool.path = "/dev/input/event18"
        dev_ydotool.name = "ydotoold virtual device"
        dev_ydotool.capabilities.return_value = {1: [97, 28, 1]}

        dev_voiceflow = MagicMock()
        dev_voiceflow.path = "/dev/input/event19"
        dev_voiceflow.name = "voiceflow-virtual-keyboard"
        dev_voiceflow.capabilities.return_value = {1: [97, 28, 1]}

        with patch("evdev.list_devices", return_value=["/dev/input/event2", "/dev/input/event18", "/dev/input/event19"]):
            with patch(
                "evdev.InputDevice",
                side_effect=lambda p: (
                    dev_hw if p == "/dev/input/event2" else (dev_ydotool if p == "/dev/input/event18" else dev_voiceflow)
                ),
            ):
                kbs = listener._get_keyboards(97)
                self.assertIn(dev_hw, kbs)
                self.assertNotIn(dev_ydotool, kbs)
                self.assertNotIn(dev_voiceflow, kbs)
                dev_ydotool.close.assert_called_once()
                dev_voiceflow.close.assert_called_once()

    def test_reset_recording_state(self):
        """reset_recording_state must set _recording back to False."""
        listener = HotkeyListener(hotkey="KEY_RIGHTCTRL", mode=HotkeyMode.TOGGLE)
        listener._recording = True
        listener.reset_recording_state()
        self.assertFalse(listener._recording)


if __name__ == "__main__":
    unittest.main()
