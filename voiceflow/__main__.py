"""
VoiceFlow main daemon entry point.

Usage:
    voiceflow                  # Start live voice tracking daemon in background/foreground
    voiceflow --toggle         # Quick activate / trigger dictation (or via custom shortcut)
    voiceflow --pause          # Turn off listening (pause)
    voiceflow --resume         # Turn on listening (activate)
    voiceflow --status         # Check if daemon is alive and view provider health
    voiceflow --stop           # Gracefully stop the running daemon
    voiceflow --test           # Quick 3s mic test via Groq
    voiceflow --list-devices   # List available audio input devices
    voiceflow --verbose        # Debug logging

Trigger methods:
    1. Global Hotkey: Hold Right Ctrl anywhere (push-to-talk)
    2. Quick Activate: 'voiceflow --toggle' (bind to any shortcut in Ubuntu Keyboard Settings)
    3. Top-right Ubuntu tray menu: Click 'Quick Dictate' or toggle 'Turn Off / Turn On'
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import tempfile
from pathlib import Path

log = logging.getLogger("voiceflow")


def _get_socket_path() -> Path:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir and Path(runtime_dir).is_dir():
        return Path(runtime_dir) / "voiceflow.sock"
    user_run = Path(f"/run/user/{os.getuid()}")
    if user_run.is_dir():
        return user_run / "voiceflow.sock"
    return Path(tempfile.gettempdir()) / f"voiceflow-{os.getuid()}.sock"


SOCKET_PATH = _get_socket_path()


def _get_lock_path() -> Path:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir and Path(runtime_dir).is_dir():
        return Path(runtime_dir) / "voiceflow.lock"
    user_run = Path(f"/run/user/{os.getuid()}")
    if user_run.is_dir():
        return user_run / "voiceflow.lock"
    return Path(tempfile.gettempdir()) / f"voiceflow-{os.getuid()}.lock"


LOCK_PATH = _get_lock_path()


def _acquire_instance_lock() -> int | None:
    """
    Acquire an exclusive advisory lock on LOCK_PATH via fcntl.flock.
    Returns the open file descriptor if acquired, or None if another instance is running.
    The OS kernel automatically releases this lock if the process exits or terminates.
    """
    import fcntl

    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        return fd
    except (BlockingIOError, OSError):
        return None


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(level)

    # Avoid duplicate handlers on re-entry
    if not root.handlers:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        root.addHandler(console_handler)

        try:
            log_dir = Path.home() / ".config" / "voiceflow"
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(
                str(log_dir / "voiceflow.log"), mode="a", encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except Exception:
            pass

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("faster_whisper").setLevel(logging.WARNING)


def _build_provider_chain(config):
    """Instantiate configured providers and return a ProviderChain."""
    from voiceflow.chain import ProviderChain
    from voiceflow.config import load_env_keys
    from voiceflow.providers.base import STTProvider

    load_env_keys()

    providers: list[STTProvider] = []
    for name in config.provider_priority:
        try:
            if name == "groq":
                from voiceflow.providers.groq_provider import GroqProvider

                providers.append(GroqProvider())
            elif name == "nvidia":
                from voiceflow.providers.nvidia_provider import NvidiaProvider

                providers.append(NvidiaProvider())
            elif name == "deepgram":
                from voiceflow.providers.deepgram_provider import DeepgramProvider

                providers.append(DeepgramProvider())
            elif name == "assemblyai":
                from voiceflow.providers.assemblyai_provider import AssemblyAIProvider

                providers.append(AssemblyAIProvider())
            elif name == "local":
                try:
                    import faster_whisper  # noqa: F401
                    from voiceflow.providers.local_provider import LocalProvider

                    providers.append(LocalProvider(model_size=config.local_model))
                except ImportError:
                    log.info("Local provider skipped (faster-whisper not installed in environment)")
        except Exception as exc:
            log.debug("Skipping provider %r: %s", name, exc)

    if not providers:
        log.error("No providers available! Ensure GROQ_API_KEY is set in ~/.bashrc")
        sys.exit(1)

    log.info("Active providers: %s", [p.name for p in providers])
    return ProviderChain(
        providers,
        cooldown_after_errors=config.cooldown_after_errors,
        default_cooldown_seconds=config.cooldown_seconds,
    )


# ------------------------------------------------------------------ #
# IPC Client (send commands to running daemon)
# ------------------------------------------------------------------ #


def _send_ipc_command(command: str) -> bool:
    """Send a command (e.g. 'toggle', 'pause', 'resume', 'stop', 'status') to running daemon."""
    import socket

    if not SOCKET_PATH.exists():
        print("❌ VoiceFlow daemon is NOT running.")
        print("   Start it with: python -m voiceflow")
        return False

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(10.0)
            client.connect(str(SOCKET_PATH))
            client.sendall(command.encode() + b"\n")
            response = client.recv(4096).decode().strip()
            if response:
                print(response)
        return True
    except ConnectionRefusedError:
        print("⚠️ Stale socket found — daemon is not responding. Removing socket.")
        SOCKET_PATH.unlink(missing_ok=True)
        return False
    except Exception as exc:
        print(f"❌ Could not communicate with daemon: {exc}")
        return False


# ------------------------------------------------------------------ #
# Test Mode
# ------------------------------------------------------------------ #


async def _run_test_mode(config) -> None:
    """Quick test: record 3 seconds from mic and transcribe via Groq."""
    from voiceflow.audio.recorder import AudioRecorder

    chain = _build_provider_chain(config)

    print("\n🎤  VoiceFlow Quick Test")
    print("━" * 40)
    print(f"  API Key (Groq): {'✓ set' if os.environ.get('GROQ_API_KEY') else '✗ NOT SET'}")
    print(f"  Active provider: {chain.active_provider()}")
    print("\n  Recording for 3 seconds… speak now!\n")

    chunks: list[bytes] = []
    recorded_frames = 0
    max_frames = int(
        3.0 * config.sample_rate / (config.sample_rate * config.chunk_duration_ms / 1000)
    )

    with AudioRecorder(
        device_index=config.device_index, sample_rate=config.sample_rate
    ) as recorder:
        async for chunk in recorder.stream():
            chunks.append(chunk)
            recorded_frames += 1
            bar = "█" * int(recorded_frames / max_frames * 20)
            print(f"\r  [{bar:<20s}] {recorded_frames}/{max_frames} frames", end="", flush=True)
            if recorded_frames >= max_frames:
                recorder.stop()
                break

    print("\n\n  ⚙️  Transcribing via", chain.active_provider(), "…\n")
    audio = b"".join(chunks)
    try:
        text = await chain.transcribe(audio, config.language)
        print(f"  📝  Transcript: {text!r}")
        print("\n  ✅  Test passed!" if text else "\n  ⚠️  Empty transcript — try speaking louder")
    except Exception as exc:
        print(f"\n  ❌  Transcription failed: {exc}")
        sys.exit(1)


# ------------------------------------------------------------------ #
# Daemon Mode
# ------------------------------------------------------------------ #


async def _run_daemon(config) -> None:
    """Main daemon: background hotkey/IPC listener + audio capture + STT + injection."""
    from voiceflow.audio.recorder import AudioRecorder
    from voiceflow.audio.vad import VAD
    from voiceflow.hotkey import HotkeyListener, HotkeyMode
    from voiceflow.injector import TextInjector
    from voiceflow.streaming import StreamingSession
    from voiceflow.osd import OSD
    from voiceflow.tray import TrayIcon
    from voiceflow.window_tracker import WindowTracker

    chain = _build_provider_chain(config)
    injector = TextInjector(method=config.inject_method)
    window_tracker = WindowTracker()
    osd = (
        OSD(
            position=config.osd_position,
            dismiss_after_ms=config.osd_dismiss_after_ms,
            notification_mode=config.notification_mode,
        )
        if config.osd_enabled
        else None
    )

    # State tracking
    is_active = True  # True = listening for hotkey/triggers, False = paused
    is_sleeping = True  # True = sleeping (idle, 0% CPU), False = awake / recording
    is_recording = False
    session: StreamingSession | None = None
    recorder: AudioRecorder | None = None
    hotkey: HotkeyListener | None = None
    loop = asyncio.get_running_loop()
    record_lock = asyncio.Lock()
    stop_event = asyncio.Event()
    sleep_timer_task: asyncio.Task | None = None
    feed_task: asyncio.Task | None = None
    auto_sleep_seconds = getattr(config, "auto_sleep_seconds", 120)

    def _cancel_sleep_timer() -> None:
        nonlocal sleep_timer_task
        if sleep_timer_task and not sleep_timer_task.done():
            sleep_timer_task.cancel()
            sleep_timer_task = None

    def _go_to_sleep() -> None:
        nonlocal is_sleeping
        if not is_active or is_recording or is_sleeping:
            return
        is_sleeping = True
        log.info("💤 VoiceFlow: auto-sleep timeout reached — entering sleeping idle mode")
        if tray:
            tray.set_state("sleeping")

    def _wake_up() -> None:
        nonlocal is_sleeping
        if is_sleeping:
            is_sleeping = False
            log.info("☀️ VoiceFlow: woke up from sleep")
            if tray:
                tray.set_state("active")
        _start_sleep_timer()

    def _toggle_sleep() -> None:
        nonlocal is_sleeping
        if is_sleeping:
            _wake_up()
        else:
            _cancel_sleep_timer()
            _go_to_sleep()

    def _start_sleep_timer() -> None:
        nonlocal sleep_timer_task
        _cancel_sleep_timer()
        if auto_sleep_seconds <= 0 or not is_active:
            return

        async def _sleep_countdown():
            try:
                await asyncio.sleep(auto_sleep_seconds)
                _go_to_sleep()
            except asyncio.CancelledError:
                pass

        sleep_timer_task = asyncio.create_task(_sleep_countdown())

    async def _handle_transcript_async(text: str, is_final: bool, metrics: dict) -> None:
        nonlocal is_recording, recorder, session
        try:
            async with record_lock:
                is_recording = False
                if recorder:
                    try:
                        recorder.close()
                    except Exception:
                        pass
                    recorder = None
                if session:
                    session = None
                if hotkey:
                    hotkey.reset_recording_state()

            if not text:
                err = metrics.get("error")
                if err:
                    log.warning("Transcription resulted in error: %s", err)
                    if osd:
                        osd.show_error(f"STT: {err}")
                    if tray:
                        tray.set_state("error")
                else:
                    if tray:
                        tray.set_state("active" if is_active else "paused")
                window_tracker.clear()
                _start_sleep_timer()
                return

            provider = metrics.get("provider") or chain.active_provider()
            stt_ms = metrics.get("stt_latency_ms", 0)
            audio_s = metrics.get("audio_duration_s", 0)
            total_s = metrics.get("total_duration_s", 0)

            log.info(
                "⚡ Transcript [%s] in %dms (audio: %.1fs, total: %.1fs): %r",
                provider,
                stt_ms,
                audio_s,
                total_s,
                text,
            )

            if is_final:
                # 1. Restore focus to original text bar if user switched windows while speaking
                try:
                    await window_tracker.restore_focus()
                except Exception as exc:
                    log.debug("Error restoring focus: %s", exc)

                # 2. Inject text directly into the target text bar or terminal
                try:
                    anchor = window_tracker.current_anchor
                    target_acc = anchor.accessible_component if anchor else None
                    is_term = anchor.is_terminal() if anchor else False
                    await injector.inject(text, target_accessible=target_acc, is_terminal=is_term)
                except Exception as exc:
                    log.error("Error during text injection: %s", exc)
                finally:
                    window_tracker.clear()

                # 3. Update tray status and restore active state
                if tray:
                    try:
                        tray.set_state("active" if is_active else "paused")
                        tray.update_provider_status(chain.status_table())
                    except Exception as exc:
                        log.warning("Error updating tray after transcript: %s", exc)

                # Start the 2-minute auto-sleep timer
                _start_sleep_timer()

        except Exception as exc:
            log.exception("Unhandled error in _handle_transcript_async: %s", exc)
            if tray:
                try:
                    tray.set_state("active" if is_active else "paused")
                except Exception:
                    pass

    def _on_transcript(text: str, is_final: bool, metrics: dict | None = None) -> None:
        metrics = metrics or {}
        fut = asyncio.run_coroutine_threadsafe(
            _handle_transcript_async(text, is_final, metrics), loop
        )

        def _check_fut_error(f):
            exc = f.exception()
            if exc:
                log.error("Unhandled exception in _handle_transcript_async: %s", exc, exc_info=exc)

        fut.add_done_callback(_check_fut_error)

    async def _start_recording() -> None:
        nonlocal session, recorder, is_recording, is_sleeping, feed_task
        async with record_lock:
            if not is_active:
                log.debug("VoiceFlow is currently turned off (paused) — ignoring trigger")
                return
            if is_recording:
                return

            # Cancel auto-sleep timer while actively recording
            _cancel_sleep_timer()
            if is_sleeping:
                is_sleeping = False
                log.info("☀️ VoiceFlow: woke up from sleep for dictation")

            is_recording = True
            log.info("🎤 Recording started…")

            # Capture target window anchor asynchronously without blocking audio capture start
            loop.run_in_executor(None, window_tracker.capture_anchor)

            if osd:
                osd.show_recording()
            if tray:
                tray.set_state("recording")

            recorder = AudioRecorder(
                device_index=config.device_index, sample_rate=config.sample_rate
            )
            recorder.open()

            # In Push-to-Talk mode, user holds the key until finished!
            # Silence auto-cutoff ONLY applies in hands-free / toggle mode.
            if config.mode == "push_to_talk":
                silence_timeout_s = 0.0  # Never cut off while user holds key
            else:
                silence_timeout_s = (config.silence_timeout_ms or 2000) / 1000.0

            session = StreamingSession(
                chain=chain,
                on_transcript=_on_transcript,
                language=config.language,
                sample_rate=config.sample_rate,
                max_duration_s=config.max_window_seconds,
                silence_timeout_s=silence_timeout_s,
            )
            await session.start()

            current_recorder = recorder

            async def _feed():
                vad = VAD(aggressiveness=config.vad_aggressiveness) if config.vad_enabled else None
                try:
                    async for chunk in current_recorder.stream():
                        if not session or not session.is_running:
                            break
                        is_speech = vad.is_speech(chunk) if vad else True
                        session.add_chunk(chunk, is_speech=is_speech)
                finally:
                    if current_recorder:
                        current_recorder.stop()

            feed_task = asyncio.create_task(_feed())

    async def _stop_recording() -> None:
        nonlocal session, recorder, is_recording, feed_task
        async with record_lock:
            if not is_recording:
                return

            is_recording = False
            if hotkey:
                hotkey.reset_recording_state()
            current_recorder = recorder
            current_session = session
            current_feed = feed_task
            recorder = None
            session = None
            feed_task = None

        log.info("⚙️ Transcribing…")
        if osd:
            osd.show_transcribing()
        if tray:
            tray.set_state("transcribing")

        try:
            if current_recorder:
                current_recorder.stop()
            if current_feed and not current_feed.done():
                try:
                    await asyncio.wait_for(current_feed, timeout=0.5)
                except Exception:
                    pass
            if current_recorder:
                current_recorder.close()
            if current_session:
                await current_session.stop()
        except Exception as exc:
            log.error("Error during stop/transcribe: %s", exc)
            if tray:
                tray.set_state("active" if is_active else "paused")
            if osd:
                osd.show_error("Transcription error")

    async def _toggle_recording() -> None:
        """Toggle recording on/off (for click or shortcut trigger)."""
        if is_recording:
            await _stop_recording()
        else:
            await _start_recording()

    def _set_active_state(active: bool) -> None:
        nonlocal is_active, is_sleeping
        is_active = active
        log.info("VoiceFlow state: %s", "ACTIVE" if is_active else "TURNED OFF")
        if not is_active:
            _cancel_sleep_timer()
            if is_recording:
                asyncio.run_coroutine_threadsafe(_stop_recording(), loop)
            if tray:
                tray.set_state("paused")
            if osd:
                osd.show_result("VoiceFlow Turned Off")
        else:
            is_sleeping = True
            if tray:
                tray.set_state("sleeping")
            if osd:
                osd.show_result("VoiceFlow Ready (Sleeping)")

    def _handle_quit() -> None:
        log.info("Quit requested — stopping daemon...")
        # If running as a systemd user service, stop the systemd service
        # so systemd knows the stop was intentional and will not trigger Restart=on-failure
        service_active = False
        try:
            import subprocess

            res = subprocess.run(
                ["systemctl", "--user", "is-active", "voiceflow.service"],
                capture_output=True,
                text=True,
                timeout=1.0,
            )
            if res.stdout.strip() == "active":
                service_active = True
        except Exception:
            pass

        if service_active or "INVOCATION_ID" in os.environ:
            try:
                import subprocess

                subprocess.Popen(["systemctl", "--user", "stop", "voiceflow.service"])
                return
            except Exception:
                pass

        loop.call_soon_threadsafe(stop_event.set)

    # Tray icon setup
    tray = None
    if config.tray_enabled:
        tray = TrayIcon(
            on_toggle_active=_set_active_state,
            on_trigger=lambda: asyncio.run_coroutine_threadsafe(_toggle_recording(), loop),
            on_sleep=lambda: loop.call_soon_threadsafe(_toggle_sleep),
            on_quit=_handle_quit,
        )
        tray.start()
        # Default state is sleeping (idle, 0% CPU)
        tray.set_state("sleeping")

    # Hotkey listener setup
    mode = HotkeyMode.TOGGLE if config.mode == "toggle" else HotkeyMode.PUSH_TO_TALK
    hotkey = HotkeyListener(
        hotkey=config.hotkey,
        mode=mode,
        on_start=lambda: asyncio.run_coroutine_threadsafe(_start_recording(), loop),
        on_stop=lambda: asyncio.run_coroutine_threadsafe(_stop_recording(), loop),
    )
    hotkey.start(loop)

    # ------------------------------------------------------------------ #
    # IPC Server for instant toggle / status / stop commands
    # ------------------------------------------------------------------ #
    async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            data = await reader.readline()
            cmd = data.decode().strip()

            if cmd == "toggle":
                await _toggle_recording()
                state = "recording" if is_recording else "stopped"
                writer.write(f"VoiceFlow: Dictation {state}\n".encode())
            elif cmd == "pause":
                _set_active_state(False)
                writer.write(b"VoiceFlow: TURNED OFF (Listening paused)\n")
            elif cmd == "resume":
                _set_active_state(True)
                writer.write(b"VoiceFlow: TURNED ON (Active)\n")
            elif cmd == "sleep":
                _cancel_sleep_timer()
                _go_to_sleep()
                writer.write(b"VoiceFlow: SLEEPING\n")
            elif cmd == "wake":
                _wake_up()
                writer.write(b"VoiceFlow: AWAKE\n")
            elif cmd == "status":
                if not is_active:
                    state_str = "TURNED OFF"
                elif is_sleeping:
                    state_str = "SLEEPING (0% CPU, idle)"
                else:
                    state_str = "AWAKE"
                status_lines = [
                    f"VoiceFlow Daemon: RUNNING (PID {os.getpid()})",
                    f"State: {state_str}",
                    f"Currently Recording: {'YES' if is_recording else 'NO'}",
                    f"Active Provider: {chain.active_provider()}",
                ]
                writer.write(("\n".join(status_lines) + "\n").encode())
            elif cmd == "stop":
                writer.write(b"VoiceFlow: Stopping daemon...\n")
                await writer.drain()
                _handle_quit()
            else:
                writer.write(b"Unknown command\n")
            await writer.drain()
        except Exception as exc:
            log.debug("IPC client error: %s", exc)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    SOCKET_PATH.unlink(missing_ok=True)
    server = await asyncio.start_unix_server(_handle_client, path=str(SOCKET_PATH))
    try:
        os.chmod(str(SOCKET_PATH), 0o600)
    except Exception:
        pass
    log.debug("IPC server listening on %s", SOCKET_PATH)

    _print_banner(config, chain)

    try:
        loop.add_signal_handler(signal.SIGTERM, stop_event.set)
        loop.add_signal_handler(signal.SIGINT, stop_event.set)
        await stop_event.wait()
    finally:
        server.close()
        await server.wait_closed()
        SOCKET_PATH.unlink(missing_ok=True)
        hotkey.stop()
        if tray:
            tray.stop()
        if osd:
            osd.stop()
        log.info("VoiceFlow stopped gracefully.")


def _print_banner(config, chain) -> None:
    from voiceflow.injector import detect_environment

    env = detect_environment()
    print()
    print("  ╔═══════════════════════════════════════════════════╗")
    print("  ║            VoiceFlow Daemon 🎤                    ║")
    print("  ╚═══════════════════════════════════════════════════╝")
    print(f"  ● Status:    RUNNING (PID {os.getpid()})")
    print(f"  ● Display:   {env.upper()}")
    print(f"  ● Provider:  {chain.active_provider()} (Groq Whisper Turbo)")
    print(f"  ● Hotkey:    {config.hotkey} [{config.mode}]")
    print()
    print("  [Triggers]")
    print("  1. Push-to-talk:  Hold Right Ctrl anywhere (apps, coding agents, terminals)")
    print("  2. Quick Toggle:  Run 'python -m voiceflow --toggle' in any shortcut")
    print("  3. Top-Right Tray: Click icon to Turn Off / Turn On / Dictate / Quit")
    print()
    print("  Press Ctrl+C or run 'python -m voiceflow --stop' to shut down.\n")


def _install_autostart() -> None:
    """Install global launcher, XDG desktop autostart entry, and systemd user service."""
    import stat
    import subprocess

    home = Path.home()
    local_bin = home / ".local" / "bin"
    autostart_dir = home / ".config" / "autostart"
    systemd_dir = home / ".config" / "systemd" / "user"
    log_dir = home / ".config" / "voiceflow"

    local_bin.mkdir(parents=True, exist_ok=True)
    autostart_dir.mkdir(parents=True, exist_ok=True)
    systemd_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    repo_dir = Path(__file__).resolve().parent.parent
    launcher = local_bin / "voiceflow"
    launcher_content = f"""#!/usr/bin/env bash
# VoiceFlow Global Launcher
# Loads ~/.bashrc environment variables (API keys) and runs VoiceFlow via virtualenv

if [ -f "$HOME/.bashrc" ]; then
    eval "$(grep -E '^\\s*export\\s+([A-Za-z0-9_]*API_KEY[A-Za-z0-9_]*)=' "$HOME/.bashrc" 2>/dev/null)"
    source "$HOME/.bashrc" 2>/dev/null || true
fi

REPO_DIR="{repo_dir}"
VENV_PYTHON="$REPO_DIR/.venv/bin/python"

if [ ! -x "$VENV_PYTHON" ]; then
    VENV_PYTHON="$HOME/.local/share/voiceflow/venv/bin/python"
fi

LOG_FILE="$HOME/.config/voiceflow/voiceflow.log"
mkdir -p "$HOME/.config/voiceflow"

if [ -x "$VENV_PYTHON" ]; then
    cd "$REPO_DIR" 2>/dev/null || true
    if [ -t 1 ] || [ $# -gt 0 ]; then
        exec "$VENV_PYTHON" -m voiceflow "$@"
    else
        exec "$VENV_PYTHON" -m voiceflow "$@" >> "$LOG_FILE" 2>&1
    fi
else
    if [ -t 1 ] || [ $# -gt 0 ]; then
        exec python3 -m voiceflow "$@"
    else
        exec python3 -m voiceflow "$@" >> "$LOG_FILE" 2>&1
    fi
fi
"""
    launcher.write_text(launcher_content)
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    desktop_file = autostart_dir / "voiceflow.desktop"
    desktop_content = f"""[Desktop Entry]
Type=Application
Name=VoiceFlow
GenericName=Speech-to-Text Daemon
Comment=Fast system-wide speech-to-text sleeping daemon
Exec={launcher}
Icon=audio-input-microphone
Terminal=false
Categories=Utility;AudioVideo;Audio;Accessibility;
StartupNotify=false
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=2
X-systemd-skip=true
"""
    desktop_file.write_text(desktop_content)

    service_content = f"""[Unit]
Description=VoiceFlow — System-wide STT Sleeping Daemon
After=default.target pipewire.service pipewire-pulse.service
Wants=pipewire.service pipewire-pulse.service

[Service]
Type=simple
ExecStart={launcher}
Restart=always
RestartSec=3
KillMode=mixed
TimeoutStopSec=5
SuccessExitStatus=0 143 15

[Install]
WantedBy=default.target
"""
    service_file.write_text(service_content)

    # Enable systemd user unit if systemctl is available
    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, capture_output=True)
        subprocess.run(
            ["systemctl", "--user", "enable", "voiceflow.service"], check=False, capture_output=True
        )
    except Exception:
        pass

    print("✅ VoiceFlow autostart successfully configured!")
    print(f"   • Launcher:   {launcher}")
    print(f"   • Autostart:  {desktop_file}")
    print(f"   • Systemd:    {service_file}")
    print(f"   • Log File:   {log_dir / 'voiceflow.log'}")
    print(
        "\nVoiceFlow will now start automatically upon system login and stop cleanly on shutdown."
    )


def _uninstall_autostart() -> None:
    """Remove desktop autostart entry and systemd service."""
    import subprocess

    home = Path.home()
    (home / ".config" / "autostart" / "voiceflow.desktop").unlink(missing_ok=True)
    service_file = home / ".config" / "systemd" / "user" / "voiceflow.service"
    if service_file.exists():
        try:
            subprocess.run(
                ["systemctl", "--user", "stop", "voiceflow.service"],
                check=False,
                capture_output=True,
            )
            subprocess.run(
                ["systemctl", "--user", "disable", "voiceflow.service"],
                check=False,
                capture_output=True,
            )
            subprocess.run(
                ["systemctl", "--user", "daemon-reload"], check=False, capture_output=True
            )
        except Exception:
            pass
        service_file.unlink(missing_ok=True)
    print("✅ VoiceFlow autostart removed. VoiceFlow will no longer start on login.")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="voiceflow",
        description="Free WhisperFlow alternative — system-wide STT daemon for Ubuntu Linux",
    )
    parser.add_argument("--test", action="store_true", help="Record 3s and test Groq transcription")
    parser.add_argument(
        "--toggle",
        action="store_true",
        help="Quick toggle dictation (start/stop) on running daemon",
    )
    parser.add_argument("--pause", action="store_true", help="Turn off / pause voice tracking")
    parser.add_argument("--resume", action="store_true", help="Turn on / resume voice tracking")
    parser.add_argument(
        "--sleep", action="store_true", help="Put the daemon to sleep (idle mode, 0%% CPU)"
    )
    parser.add_argument("--wake", action="store_true", help="Wake up the daemon from sleep")
    parser.add_argument(
        "--status", action="store_true", help="Check daemon status and provider health"
    )
    parser.add_argument("--stop", action="store_true", help="Stop the running VoiceFlow daemon")
    parser.add_argument(
        "--install-autostart",
        action="store_true",
        help="Configure VoiceFlow to start on login and stop on shutdown",
    )
    parser.add_argument(
        "--uninstall-autostart", action="store_true", help="Remove VoiceFlow from login autostart"
    )
    parser.add_argument("--list-devices", action="store_true", help="List audio input devices")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    args = parser.parse_args()

    # Handle autostart setup commands
    if args.install_autostart:
        _install_autostart()
        return
    if args.uninstall_autostart:
        _uninstall_autostart()
        return

    # Handle IPC commands first
    if args.toggle:
        _send_ipc_command("toggle")
        return
    if args.pause:
        _send_ipc_command("pause")
        return
    if args.resume:
        _send_ipc_command("resume")
        return
    if args.sleep:
        _send_ipc_command("sleep")
        return
    if args.wake:
        _send_ipc_command("wake")
        return
    if args.status:
        _send_ipc_command("status")
        return
    if args.stop:
        _send_ipc_command("stop")
        return

    _setup_logging(args.verbose)

    from voiceflow.config import load_config

    config = load_config()

    if args.list_devices:
        from voiceflow.audio.recorder import AudioRecorder

        devices = AudioRecorder.list_devices()
        print("\nAvailable audio input devices:")
        for d in devices:
            print(f"  [{d['index']}] {d['name']} (default rate: {d['rate']}Hz)")
        return

    if args.test:
        asyncio.run(_run_test_mode(config))
        return

    # Guard against starting multiple daemon instances via kernel advisory file lock
    lock_fd = _acquire_instance_lock()
    if lock_fd is None:
        print("⚠️ VoiceFlow daemon is already running.")
        return

    # Check if a responsive daemon is running on SOCKET_PATH as well
    if SOCKET_PATH.exists():
        import socket

        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(1.0)
                client.connect(str(SOCKET_PATH))
                client.sendall(b"status\n")
                resp = client.recv(1024).decode()
                if "VoiceFlow Daemon: RUNNING" in resp:
                    print("⚠️ VoiceFlow daemon is already running.")
                    return
        except Exception:
            SOCKET_PATH.unlink(missing_ok=True)

    try:
        asyncio.run(_run_daemon(config))
    finally:
        try:
            import fcntl

            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        except Exception:
            pass
        sys.exit(0)


if __name__ == "__main__":
    main()
