"""Alerts: Windows toast notification (winotify) + our own sound (winsound) + alerts.log.

The sound is played by the app itself instead of the toast, because Windows often mutes
toast sounds (Focus assist, per-app notification settings). Toasts are shown in a
background thread, so a slow PowerShell call never delays market checks.

On non-Windows systems (e.g. when testing) everything falls back to console output.
"""

from __future__ import annotations

import threading
import time
from decimal import Decimal

from . import vataga
from .settings import APP_NAME, alerts_log_path, load_settings

try:
    from winotify import Notification, audio
except ImportError:  # not on Windows / package missing
    Notification = None
    audio = None

try:
    import winsound
except ImportError:  # not on Windows
    winsound = None

_sound_lock = threading.Lock()

# Set by the GUI: called as listener(symbol, title, message, direction) from any thread; shows the popup window.
alert_listener = None


def _format_price(price: float) -> str:
    return format(Decimal(str(price)).normalize(), "f")


def _append_log(line: str) -> None:
    """Record an alert / problem in alerts.log (never raises)."""
    try:
        path = alerts_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {line}\n")
    except OSError:
        pass


def play_sound() -> None:
    """Play a short two-tone beep without blocking the caller."""
    if winsound is None:
        return

    def _play() -> None:
        if not _sound_lock.acquire(blocking=False):  # a sound is already playing
            return
        try:
            try:
                winsound.Beep(988, 160)
                winsound.Beep(1319, 240)
            except RuntimeError:
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        finally:
            _sound_lock.release()

    threading.Thread(target=_play, name="alert-sound", daemon=True).start()


def _show_toast(title: str, message: str, symbol: str | None) -> None:
    try:
        if Notification is None:
            print(f"[notification] {title}: {message}")
            return
        toast = Notification(app_id=APP_NAME, title=title, msg=message, duration="long")
        toast.set_audio(audio.Silent, loop=False)  # sound is played by play_sound()
        if symbol:
            toast.add_actions(label="Open in Vataga", launch=vataga.link_for(symbol))
            toast.add_actions(label="Open on Binance", launch=f"https://www.binance.com/en/futures/{symbol}")
        toast.show()
    except Exception as exc:
        _append_log(f"TOAST FAILED for '{title}': {exc}")


def _notify(symbol: str, title: str, message: str, sound: bool, direction: int = 0) -> None:
    settings = load_settings()
    if sound:
        play_sound()
    if settings.toast_enabled:
        threading.Thread(target=_show_toast, args=(title, message, symbol), daemon=True).start()
    if settings.popup_enabled and alert_listener is not None:
        try:
            alert_listener(symbol, title, message, direction)
        except Exception as exc:
            _append_log(f"POPUP FAILED for '{title}': {exc}")


def show_surge(
    symbol: str,
    volume_ratio: float,
    natr_ratio: float,
    natr: float,
    volume_window: float,
    window_minutes: int,
    sound: bool = True,
) -> None:
    """Alert for a coin that entered the surge list (volume and NATR jumped)."""
    message = (
        f"Volume x{volume_ratio:.1f} and NATR x{natr_ratio:.1f} vs the usual level (last {window_minutes} min).\n"
        f"NATR {natr:.2f}%, volume in the window {volume_window / 1e6:.2f}M USDT"
    )
    _append_log(
        f"ALERT {symbol} volume x{volume_ratio:.1f}, NATR x{natr_ratio:.1f} ({natr:.2f}%), "
        f"window volume {volume_window / 1e6:.2f}M USDT"
    )
    _notify(symbol=symbol, title=f"{symbol}  vol x{volume_ratio:.1f} / NATR x{natr_ratio:.1f}", message=message, sound=sound)


def show_warning(title: str, message: str, sound: bool = True) -> None:
    """Problem notice (e.g. connection lost). Shown without coin buttons."""
    _append_log(f"WARNING {title}: {message}")
    _notify(symbol="", title=title, message=message, sound=sound)


def show_test(sound: bool = True) -> None:
    _notify("BTCUSDT", "Test notification", "Notifications are working.", sound)
