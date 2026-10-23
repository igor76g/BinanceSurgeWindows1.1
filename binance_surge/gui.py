"""Tkinter UI: one window with two lists (1 = volume + NATR surge, 2 = top coins by number of trades) and a settings dialog.

Double-click a ticker in either list to open the coin in Vataga.terminal.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from . import __version__, notifier, popups, vataga
from .binance_api import BinanceApi
from .monitor import Monitor
from .settings import APP_NAME, Settings, alert_times_path, alerts_log_path, load_settings, save_settings

MAX_LOG_LINES = 300
BOLD = ("Segoe UI", 10, "bold")

# (settings key, label, python type, minimum allowed value); label None = section header
SETTINGS_FIELDS = [
    ("Window 1: volume + NATR surge", None, None, None),
    ("surge_min_volume_million", "Minimum 24h volume (million USDT)", float, 0.0),
    ("surge_min_natr", "Minimum NATR of the last window (%)", float, 0.0),
    ("surge_volume_ratio", "Volume must grow more than (x usual)", float, 0.0),
    ("surge_natr_ratio", "NATR must grow more than (x usual)", float, 0.0),
    ("surge_cooldown_minutes", "Notification cooldown per coin (minutes)", int, 0),
    ("Window 2: top coins by number of trades", None, None, None),
    ("top_min_volume_million", "Minimum 24h volume (million USDT)", float, 0.0),
    ("top_min_trades", "Minimum trades in the last window", int, 0),
    ("top_min_natr", "Minimum NATR of the last window (%)", float, 0.0),
    ("top_count", "Number of coins in the list", int, 1),
    ("General", None, None, None),
    ("window_minutes", "Window length: 'last N minutes'", int, 1),
    ("baseline_minutes", "History that defines the 'usual' level (minutes)", int, 15),
    ("check_interval_minutes", "Check interval (minutes)", int, 1),
    ("popup_auto_close_sec", "Close alert popups after (seconds, 0 = never)", int, 0),
]


def fmt_million(value: float) -> str:
    return f"{value / 1e6:,.1f}"


class SymbolTree(ttk.Frame):
    """Treeview with a vertical scrollbar; double-click on a row calls `on_open(symbol)`."""

    def __init__(self, master, columns, on_open, height=12) -> None:
        super().__init__(master)
        self.on_open = on_open
        self.tree = ttk.Treeview(self, columns=[c[0] for c in columns], show="headings", height=height)
        for key, title, width in columns:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="w" if key == "ticker" else "e")
        bar = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self._double_click)

    def _double_click(self, event) -> None:
        item = self.tree.identify_row(event.y)
        if item:
            self.on_open(str(self.tree.set(item, "ticker")))

    def set_rows(self, rows: list[tuple]) -> None:
        self.tree.delete(*self.tree.get_children())
        for row in rows:
            self.tree.insert("", "end", values=row)


class TopPanel(ttk.LabelFrame):
    """Panel 2 (below the surge list): coins with the most trades in the last window."""

    COLUMNS = [
        ("rank", "#", 36),
        ("ticker", "Ticker", 130),
        ("trades", "Trades", 100),
        ("natr", "NATR %", 80),
        ("vol24", "24h vol, M", 100),
    ]

    def __init__(self, master, on_open) -> None:
        super().__init__(master, text=" 2. Top coins by number of trades ", padding=6)
        self.heading_var = tk.StringVar(value="Waiting for the first check...")
        ttk.Label(self, textvariable=self.heading_var, font=BOLD, wraplength=760).pack(anchor="w")
        self.updated_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.updated_var).pack(anchor="w")
        self.list = SymbolTree(self, self.COLUMNS, on_open, height=7)
        self.list.pack(fill="both", expand=True, pady=(4, 0))

    def update_rows(self, rows, settings: Settings, now: float) -> None:
        self.heading_var.set(
            f"Top {settings.top_count} by trades in the last {settings.window_minutes} min "
            f"(24h volume > {settings.top_min_volume_million:g}M, trades >= {settings.top_min_trades}, "
            f"NATR >= {settings.top_min_natr:g}%)"
        )
        self.updated_var.set(f"Updated {time.strftime('%H:%M:%S', time.localtime(now))}; {len(rows)} coin(s) match")
        self.list.set_rows(
            [
                (i + 1, r.symbol, f"{r.trades:,}", f"{r.natr:.2f}", fmt_million(r.volume_24h))
                for i, r in enumerate(rows)
            ]
        )


class SettingsDialog(tk.Toplevel):
    def __init__(self, app: "App") -> None:
        super().__init__(app)
        self.app = app
        self.title("Settings")
        self.resizable(False, False)
        self.transient(app)

        settings = load_settings()
        self.vars: dict[str, tk.StringVar] = {}
        self.types: dict[str, tuple] = {}
        self.labels: dict[str, str] = {}

        root = ttk.Frame(self, padding=14)
        root.pack(fill="both", expand=True)
        form = ttk.Frame(root)
        form.pack(fill="x")
        form.columnconfigure(0, weight=1)
        row = 0
        for key, label, py_type, minimum in SETTINGS_FIELDS:
            if label is None:
                ttk.Label(form, text=key, font=BOLD).grid(row=row, column=0, columnspan=2, sticky="w", pady=(10, 2))
            else:
                var = tk.StringVar()
                value = getattr(settings, key)
                var.set(f"{value:g}" if isinstance(value, float) else str(value))
                self.vars[key] = var
                self.types[key] = (py_type, minimum)
                self.labels[key] = label
                ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=2, padx=(0, 16))
                ttk.Entry(form, textvariable=var, width=12).grid(row=row, column=1, sticky="e", pady=2)
            row += 1

        self.sound_var = tk.BooleanVar(value=settings.sound_enabled)
        self.popup_var = tk.BooleanVar(value=settings.popup_enabled)
        self.toast_var = tk.BooleanVar(value=settings.toast_enabled)
        self.autostart_var = tk.BooleanVar(value=settings.autostart_monitoring)
        ttk.Label(root, text="Notifications (window 1 only)", font=BOLD).pack(anchor="w", pady=(12, 2))
        ttk.Checkbutton(root, text="Play a sound", variable=self.sound_var).pack(anchor="w")
        ttk.Checkbutton(root, text="Popup window with buttons (always on top)", variable=self.popup_var).pack(anchor="w")
        ttk.Checkbutton(root, text="Windows toast notification", variable=self.toast_var).pack(anchor="w")
        ttk.Checkbutton(root, text="Start monitoring automatically when the app opens", variable=self.autostart_var).pack(anchor="w", pady=(6, 0))

        ttk.Label(root, text="Vataga.terminal", font=BOLD).pack(anchor="w", pady=(12, 2))
        vrow = ttk.Frame(root)
        vrow.pack(fill="x")
        self.vtype_var = tk.StringVar(value=settings.vataga_type)
        self.vport_var = tk.StringVar(value=str(settings.vataga_port))
        ttk.Label(vrow, text="Market").pack(side="left")
        ttk.Combobox(vrow, textvariable=self.vtype_var, values=["F", "S"], width=3, state="readonly").pack(side="left", padx=4)
        ttk.Label(vrow, text="(F = futures, S = spot)   Port").pack(side="left")
        ttk.Entry(vrow, textvariable=self.vport_var, width=7).pack(side="left", padx=4)
        ttk.Button(vrow, text="Test Vataga", command=self.test_vataga).pack(side="right")

        buttons = ttk.Frame(root)
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text="Save", command=self.save).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=6)

    def read(self) -> Settings | None:
        values: dict = {}
        for key, var in self.vars.items():
            py_type, minimum = self.types[key]
            raw = var.get().strip().replace(",", ".")
            try:
                value = py_type(raw)
            except ValueError:
                messagebox.showerror(APP_NAME, f"'{self.labels[key]}' must be a valid number.", parent=self)
                return None
            if value < minimum:
                messagebox.showerror(APP_NAME, f"'{self.labels[key]}' must be at least {minimum:g}.", parent=self)
                return None
            values[key] = value
        try:
            port = int(self.vport_var.get().strip())
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            messagebox.showerror(APP_NAME, "Port must be a number from 1 to 65535.", parent=self)
            return None
        return Settings(
            sound_enabled=self.sound_var.get(),
            popup_enabled=self.popup_var.get(),
            toast_enabled=self.toast_var.get(),
            autostart_monitoring=self.autostart_var.get(),
            vataga_type=self.vtype_var.get() or "F",
            vataga_port=port,
            vataga_partner_id=load_settings().vataga_partner_id,
            **values,
        )

    def save(self) -> None:
        settings = self.read()
        if settings is None:
            return
        try:
            save_settings(settings)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not save settings: {exc}", parent=self)
            return
        self.app.add_log("Settings saved")
        self.app.settings_saved()
        self.destroy()

    def test_vataga(self) -> None:
        settings = self.read()
        if settings is None:
            return
        ok, message = vataga.open_symbol("BTCUSDT", settings)
        if ok:
            messagebox.showinfo(APP_NAME, "Request sent. BTCUSDT should open in the window linked to 'Screener'.", parent=self)
        else:
            messagebox.showerror(APP_NAME, message, parent=self)


class App(tk.Tk):
    SURGE_COLUMNS = [
        ("ticker", "Ticker", 120),
        ("vol_x", "Volume x", 80),
        ("natr_x", "NATR x", 70),
        ("natr", "NATR %", 70),
        ("vol24", "24h vol, M", 100),
        ("change", "Change %", 90),
        ("since", "Since", 80),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} {__version__}: volume + NATR surge")
        self.geometry("820x800+60+30")
        self.minsize(700, 640)

        self.api = BinanceApi()
        self.monitor: Monitor | None = None
        self.messages: queue.Queue = queue.Queue()
        self.last_settings = load_settings()

        self._build_widgets()
        notifier.alert_listener = lambda symbol, title, message, direction=0: self.messages.put(
            ("popup", (symbol, title, message, direction))
        )

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(200, self._drain_messages)
        if self.last_settings.autostart_monitoring:
            self.after(500, self.start_monitoring)

    # ------------------------------------------------------------------ UI

    def _build_widgets(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)

        ttk.Label(root, text="Binance Futures: volume + NATR surge", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        self.status_var = tk.StringVar(value="Stopped")
        ttk.Label(root, textvariable=self.status_var, wraplength=780).pack(anchor="w", pady=(2, 8))

        buttons = ttk.Frame(root)
        buttons.pack(fill="x")
        self.start_button = ttk.Button(buttons, text="Start monitoring", command=self.start_monitoring)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(buttons, text="Stop", command=self.stop_monitoring, state="disabled")
        self.stop_button.pack(side="left", padx=6)
        self.check_button = ttk.Button(buttons, text="Check now", command=self.check_now, state="disabled")
        self.check_button.pack(side="left")
        ttk.Button(buttons, text="Settings", command=self.open_settings).pack(side="right")

        paned = ttk.PanedWindow(root, orient="vertical")
        paned.pack(fill="both", expand=True, pady=(10, 4))

        surge_frame = ttk.LabelFrame(paned, text=" 1. Volume + NATR surge ", padding=6)
        self.heading_var = tk.StringVar(value="")
        ttk.Label(surge_frame, textvariable=self.heading_var, font=BOLD, wraplength=760).pack(anchor="w")
        self.updated_var = tk.StringVar(value="")
        ttk.Label(surge_frame, textvariable=self.updated_var).pack(anchor="w")
        self.surge_list = SymbolTree(surge_frame, self.SURGE_COLUMNS, self.open_symbol, height=9)
        self.surge_list.pack(fill="both", expand=True, pady=(4, 0))
        paned.add(surge_frame, weight=1)

        self.top_panel = TopPanel(paned, self.open_symbol)
        paned.add(self.top_panel, weight=1)

        self.hint_var = tk.StringVar(value="Double-click a ticker to open it in Vataga.terminal")
        ttk.Label(root, textvariable=self.hint_var, wraplength=780).pack(anchor="w")

        tools = ttk.Frame(root)
        tools.pack(fill="x", pady=(8, 2))
        ttk.Label(tools, text="Log", font=BOLD).pack(side="left")
        ttk.Button(tools, text="Test notification", command=self.test_notification).pack(side="right")
        ttk.Button(tools, text="Alerts log", command=self.open_alerts_log).pack(side="right", padx=6)
        ttk.Button(tools, text="Close all popups", command=popups.close_all).pack(side="right")
        self.log_widget = scrolledtext.ScrolledText(root, height=6, state="disabled", wrap="word")
        self.log_widget.pack(fill="both", expand=False)

    # ------------------------------------------------------------- actions

    def open_settings(self) -> None:
        SettingsDialog(self)

    def settings_saved(self) -> None:
        self.last_settings = load_settings()
        if self.monitor is not None and self.monitor.is_alive():
            self.monitor.check_now()  # apply the new conditions right away

    def start_monitoring(self) -> None:
        if self.monitor is not None and self.monitor.is_alive():
            return
        self.monitor = Monitor(
            api=self.api,
            load_settings=load_settings,
            on_results=lambda surge, top, since, now: self.messages.put(("results", (surge, top, since, now))),
            on_alert=lambda row, settings: notifier.show_surge(
                row.symbol, row.volume_ratio, row.natr_ratio, row.natr, row.volume_window,
                settings.window_minutes, sound=settings.sound_enabled,
            ),
            log=lambda text: self.messages.put(("log", text)),
            set_status=lambda text: self.messages.put(("status", text)),
            warn=lambda title, text: notifier.show_warning(title, text, sound=load_settings().sound_enabled),
            state_path=alert_times_path(),
        )
        self.monitor.start()
        self.start_button.config(state="disabled")
        self.stop_button.config(state="normal")
        self.check_button.config(state="normal")
        self.status_var.set("Starting: the first check downloads candles of all coins and takes a little longer...")

    def stop_monitoring(self) -> None:
        if self.monitor is not None:
            self.monitor.stop()
        self.stop_button.config(state="disabled")
        self.check_button.config(state="disabled")

    def check_now(self) -> None:
        if self.monitor is not None and self.monitor.is_alive():
            self.monitor.check_now()
            self.status_var.set("Checking now...")

    def open_symbol(self, symbol: str) -> None:
        """Open a coin in Vataga.terminal (called on double-click; the request runs in a thread)."""
        settings = load_settings()
        self.hint_var.set(f"Sending {symbol} to Vataga.terminal...")

        def work() -> None:
            ok, message = vataga.open_symbol(symbol, settings)
            self.messages.put(("hint", f"{symbol} opened in Vataga.terminal" if ok else message))

        threading.Thread(target=work, daemon=True).start()

    def test_notification(self) -> None:
        try:
            notifier.show_test(sound=load_settings().sound_enabled)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not show notification: {exc}")

    def open_alerts_log(self) -> None:
        path = alerts_log_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)
            os.startfile(str(path))  # type: ignore[attr-defined]  (Windows only)
        except (OSError, AttributeError) as exc:
            messagebox.showerror(APP_NAME, f"Could not open {path}: {exc}")

    # ---------------------------------------------------------------- misc

    def add_log(self, text: str) -> None:
        self.log_widget.config(state="normal")
        self.log_widget.insert("end", f"{time.strftime('%H:%M:%S')}  {text}\n")
        lines = int(self.log_widget.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.log_widget.delete("1.0", f"{lines - MAX_LOG_LINES + 1}.0")
        self.log_widget.see("end")
        self.log_widget.config(state="disabled")

    def _show_results(self, surge, top, since, now) -> None:
        s = load_settings()
        self.heading_var.set(
            f"Volume and NATR of the last {s.window_minutes} min vs the previous {s.baseline_minutes} min "
            f"(24h volume > {s.surge_min_volume_million:g}M, NATR >= {s.surge_min_natr:g}%, "
            f"volume > x{s.surge_volume_ratio:g}, NATR > x{s.surge_natr_ratio:g})"
        )
        self.updated_var.set(f"Updated {time.strftime('%H:%M:%S', time.localtime(now))}; {len(surge)} coin(s) match")
        self.surge_list.set_rows(
            [
                (r.symbol, f"{r.volume_ratio:.1f}", f"{r.natr_ratio:.1f}", f"{r.natr:.2f}",
                 fmt_million(r.volume_24h), f"{r.price_change:+.2f}",
                 time.strftime("%H:%M", time.localtime(since.get(r.symbol, now))))
                for r in surge
            ]
        )
        self.top_panel.update_rows(top, s, now)

    def _drain_messages(self) -> None:
        """Move messages from the monitor thread into the UI. One bad message never stops the loop."""
        while True:
            try:
                kind, payload = self.messages.get_nowait()
            except queue.Empty:
                break
            try:
                if kind == "log":
                    self.add_log(payload)
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "hint":
                    self.hint_var.set(payload)
                elif kind == "results":
                    self._show_results(*payload)
                elif kind == "popup":
                    popups.show_popup(self, *payload)
            except Exception as exc:
                try:
                    self.add_log(f"UI error ({kind}): {exc}")
                except Exception:
                    pass

        if self.monitor is not None and not self.monitor.is_alive():
            self.monitor = None
            self.start_button.config(state="normal")
            self.stop_button.config(state="disabled")
            self.check_button.config(state="disabled")
        self.after(200, self._drain_messages)

    def _on_close(self) -> None:
        if self.monitor is not None:
            self.monitor.stop()
        self.destroy()


def main() -> None:
    # Toast button click: Windows starts the app with binancesurge://open?symbol=XXXUSDT
    if len(sys.argv) > 1 and vataga.handle_url(sys.argv[1]):
        return
    vataga.register_protocol()
    App().mainloop()
