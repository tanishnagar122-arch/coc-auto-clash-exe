import json
import os
import hashlib
import platform
import sys
import threading
import time
import uuid
import urllib.error
import urllib.request
import webbrowser
import tkinter as tk
from tkinter import messagebox
from datetime import datetime, timezone

import websocket

from bot_core import FancyCoCBotGUI

APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "CoCPremiumSuite")
LICENSE_FILE = os.path.join(APP_DIR, "license.json")
SUPABASE_ANON_KEY = "sb_publishable_0QUOfNfbHNz_0T2I299zvA_Gk3jQgyW"
DEFAULT_FUNCTIONS_URL = "https://uaamvbxklmonvobssdfw.supabase.co/functions/v1"
APP_VERSION = "1.0.1"
SESSION_CHECK_INTERVAL_MS = 60 * 60 * 1000
TRANSIENT_RETRY_INTERVAL_MS = 60 * 1000
REALTIME_RECONNECT_SECONDS = 5


def load_api_base():
    env_value = os.environ.get("COC_SUPABASE_FUNCTIONS")
    if env_value:
        return env_value.rstrip("/")
    exe_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
    config_paths = [
        os.path.join(exe_dir, "server_config.json"),
        os.path.join(os.getcwd(), "server_config.json"),
    ]
    for path in config_paths:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                value = json.load(handle).get("supabase_functions_url", "")
                if value:
                    return value.rstrip("/")
        except Exception:
            pass
    return DEFAULT_FUNCTIONS_URL


API_BASE = load_api_base()


def get_device_id():
    raw = f"{platform.node()}-{uuid.getnode()}-{os.environ.get('USERNAME', '')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_device_name():
    return platform.node() or "Windows PC"


def api_post(path, payload):
    payload = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{API_BASE}{path}",
        data=payload,
        headers={"Content-Type": "application/json", "apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {SUPABASE_ANON_KEY}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8"))
            return data.get("ok", False), data
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read().decode("utf-8"))
            if exc.code in (408, 429) or exc.code >= 500:
                data["transient"] = True
            return False, data
        except Exception:
            transient = exc.code in (408, 429) or exc.code >= 500
            return False, {"message": "License server temporarily unavailable." if transient else "License check failed.", "transient": transient}
    except Exception:
        return False, {"message": "Cannot reach Supabase license server.", "transient": True}


def api_get(path):
    request = urllib.request.Request(
        f"{API_BASE}{path}",
        headers={"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {SUPABASE_ANON_KEY}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return True, json.loads(response.read().decode("utf-8"))
    except Exception:
        return False, {"message": "Cannot reach update server."}


def check_update_required():
    ok, data = api_get("/app-release")
    if not ok:
        return False, data
    latest = str(data.get("latest_version") or APP_VERSION)
    if latest != APP_VERSION:
        data["update_required"] = True
        return True, data
    return False, data


def show_update_prompt(root, data):
    url = data.get("download_url") or ""
    latest = data.get("latest_version") or "latest"
    message = f"Update required. Latest version: {latest}."
    if messagebox.askyesno("Update required", message + "\n\nOpen download page now?"):
        if url:
            webbrowser.open(url)
        else:
            webbrowser.open("https://uaamvbxklmonvobssdfw.supabase.co/functions/v1/app-release")
    root.destroy()


def verify_key(license_key):
    return api_post("/auth-license", {
        "license_key": license_key.strip(),
        "device_id": get_device_id(),
        "device_name": get_device_name(),
        "app_version": APP_VERSION,
    })


def session_check(license_key):
    return api_post("/session-check", {
        "license_key": license_key.strip(),
        "device_id": get_device_id(),
        "device_name": get_device_name(),
        "app_version": APP_VERSION,
    })


def save_license(session_info):
    os.makedirs(APP_DIR, exist_ok=True)
    with open(LICENSE_FILE, "w", encoding="utf-8") as handle:
        json.dump(session_info, handle)


def clear_license():
    try:
        os.remove(LICENSE_FILE)
    except FileNotFoundError:
        pass


def load_saved_license():
    try:
        with open(LICENSE_FILE, "r", encoding="utf-8") as handle:
            return json.load(handle).get("license_key", "")
    except Exception:
        return ""


class LicenseLogin:
    def __init__(self, root):
        self.root = root
        self.root.title("CoC Auto Clash")
        self.root.geometry("620x460")
        self.root.resizable(False, False)
        self.root.configure(bg="#07111F")
        self.session_info = None
        self.session_connection_failures = 0
        self.realtime_stop = None
        self.realtime_socket = None
        self.expiry_timer = None
        needs_update, update_data = check_update_required()
        if needs_update:
            show_update_prompt(self.root, update_data)
            return

        shell = tk.Frame(root, bg="#07111F")
        shell.pack(fill="both", expand=True, padx=28, pady=24)

        hero = tk.Frame(shell, bg="#0F1B2D", highlightbackground="#263A56", highlightthickness=1)
        hero.pack(fill="x", pady=(0, 18))

        tk.Label(hero, text="CoC Auto Clash", bg="#0F1B2D", fg="#FFD166", font=("Segoe UI", 25, "bold")).pack(anchor="w", padx=22, pady=(20, 0))
        tk.Label(hero, text="BY TANISH NAGAR", bg="#0F1B2D", fg="#8FA3BC", font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=22, pady=(0, 6))
        tk.Label(
            hero,
            text="License-protected farming dashboard for your desktop setup.",
            bg="#0F1B2D",
            fg="#B8C6DA",
            font=("Segoe UI", 10)
        ).pack(anchor="w", padx=22)

        stats = tk.Frame(hero, bg="#0F1B2D")
        stats.pack(fill="x", padx=22, pady=18)
        for title, caption, color in (
            ("OCR", "Loot reading", "#5DB7FF"),
            ("Keys", "Website unlock", "#FFD166"),
            ("Control", "One clean panel", "#35D07F"),
        ):
            card = tk.Frame(stats, bg="#08111F", highlightbackground="#263A56", highlightthickness=1)
            card.pack(side="left", expand=True, fill="x", padx=(0, 10))
            tk.Label(card, text=title, bg="#08111F", fg=color, font=("Segoe UI", 13, "bold")).pack(anchor="w", padx=12, pady=(10, 0))
            tk.Label(card, text=caption, bg="#08111F", fg="#8FA3BC", font=("Segoe UI", 8)).pack(anchor="w", padx=12, pady=(0, 10))

        form = tk.Frame(shell, bg="#07111F")
        form.pack(fill="x")

        tk.Label(form, text="License key", bg="#07111F", fg="#FFFFFF", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.key_entry = tk.Entry(form, bg="#0F1B2D", fg="#FFFFFF", insertbackground="#FFFFFF", relief="flat", font=("Consolas", 13), width=34)
        self.key_entry.pack(fill="x", ipady=10, pady=(8, 14))
        self.key_entry.insert(0, load_saved_license())

        self.status = tk.Label(form, text="License server: Supabase Edge Functions", bg="#07111F", fg="#9FB6C3", font=("Segoe UI", 9))
        self.status.pack(anchor="w", pady=(0, 18))

        tk.Button(form, text="Unlock Dashboard", bg="#FFD166", fg="#08111F", activebackground="#FFE08A", relief="flat", font=("Segoe UI", 12, "bold"), command=self.unlock).pack(fill="x", ipady=11)
        tk.Button(form, text="Use saved license", bg="#0F1B2D", fg="#DDE7EA", activebackground="#182843", relief="flat", font=("Segoe UI", 9), command=self.try_saved).pack(fill="x", ipady=8, pady=(10, 0))

    def try_saved(self):
        saved = load_saved_license()
        if saved:
            self.key_entry.delete(0, tk.END)
            self.key_entry.insert(0, saved)
            self.unlock()
        else:
            messagebox.showinfo("No saved key", "Paste a license key from the website first.")

    def unlock(self):
        license_key = self.key_entry.get().strip()
        if not license_key:
            messagebox.showwarning("Missing key", "Please enter your license key.")
            return
        self.status.config(text="Checking license...")
        self.root.update_idletasks()
        ok, data = verify_key(license_key)
        if not ok:
            if data.get("update_required"):
                show_update_prompt(self.root, data)
                return
            message = data.get("message", "License rejected.")
            self.status.config(text=message)
            messagebox.showerror("License rejected", message)
            return
        data["license_key"] = license_key
        data["device_id"] = get_device_id()
        data["device_name"] = get_device_name()
        save_license(data)
        self.session_info = data
        self.open_dashboard()

    def open_dashboard(self):
        for widget in self.root.winfo_children():
            widget.destroy()
        self.app = FancyCoCBotGUI(self.root, self.session_info, self.logout_to_login, self.check_license_now)
        self.start_realtime_listener()
        self.schedule_local_expiry()
        self.schedule_session_check()

    def schedule_local_expiry(self):
        if self.expiry_timer:
            self.root.after_cancel(self.expiry_timer)
            self.expiry_timer = None
        raw = str((self.session_info or {}).get("expires_at") or "").strip()
        if not raw:
            return
        try:
            expires = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            delay_ms = max(0, int((expires - datetime.now(timezone.utc)).total_seconds() * 1000))
            self.expiry_timer = self.root.after(delay_ms, lambda: self.expire_session("License expired."))
        except (TypeError, ValueError, OverflowError, tk.TclError):
            pass

    def expire_session(self, reason):
        if self.session_info and hasattr(self, "app"):
            self.app.force_license_logout(reason)

    def start_realtime_listener(self):
        self.stop_realtime_listener()
        license_key = str((self.session_info or {}).get("license_key") or "").strip().upper()
        if not license_key:
            return
        topic = "license:" + hashlib.sha256(license_key.encode("utf-8")).hexdigest()
        stop_event = threading.Event()
        self.realtime_stop = stop_event

        def worker():
            ws_url = (
                "wss://uaamvbxklmonvobssdfw.supabase.co/realtime/v1/websocket"
                f"?apikey={SUPABASE_ANON_KEY}&vsn=1.0.0"
            )
            while not stop_event.is_set():
                def on_open(ws):
                    ws.send(json.dumps({
                        "topic": f"realtime:{topic}",
                        "event": "phx_join",
                        "payload": {
                            "config": {
                                "broadcast": {"ack": False, "self": False},
                                "presence": {"enabled": False},
                                "private": False,
                            }
                        },
                        "ref": "1",
                        "join_ref": "1",
                    }))

                def on_message(_ws, raw):
                    try:
                        message = json.loads(raw)
                        if message.get("event") != "broadcast":
                            return
                        envelope = message.get("payload") or {}
                        if envelope.get("event") != "license_access_changed":
                            return
                        payload = envelope.get("payload") or {}
                        status = str(payload.get("status") or "").lower()
                        if payload.get("device_reset") or status in ("paused", "banned", "revoked", "expired", "deleted"):
                            reason = (
                                "License activation was reset."
                                if payload.get("device_reset")
                                else f"License is {status}."
                            )
                            self.root.after(0, lambda r=reason: self.expire_session(r))
                        elif status == "active" and payload.get("expires_at"):
                            self.session_info["expires_at"] = payload["expires_at"]
                            self.root.after(0, self.schedule_local_expiry)
                    except Exception:
                        pass

                ws = websocket.WebSocketApp(ws_url, on_open=on_open, on_message=on_message)
                self.realtime_socket = ws
                ws.run_forever(ping_interval=25, ping_timeout=10)
                self.realtime_socket = None
                if stop_event.wait(REALTIME_RECONNECT_SECONDS):
                    break

        threading.Thread(target=worker, daemon=True, name="license-realtime").start()

    def stop_realtime_listener(self):
        if self.realtime_stop:
            self.realtime_stop.set()
            self.realtime_stop = None
        if self.realtime_socket:
            try:
                self.realtime_socket.close()
            except Exception:
                pass
            self.realtime_socket = None

    def check_license_now(self):
        ok, data = session_check(self.session_info.get("license_key", ""))
        if ok:
            self.session_connection_failures = 0
            self.session_info.update(data)
            save_license(self.session_info)
            return True, "License accepted."
        if data.get("transient"):
            self.session_connection_failures += 1
            return True, "License server temporarily unavailable; using the last verified session."
        return False, data.get("message", "License session ended.")

    def logout_to_login(self, reason="Logged out."):
        self.stop_realtime_listener()
        if self.expiry_timer:
            self.root.after_cancel(self.expiry_timer)
            self.expiry_timer = None
        clear_license()
        for widget in self.root.winfo_children():
            widget.destroy()
        self.session_info = None
        LicenseLogin(self.root)

    def schedule_session_check(self):
        if not self.session_info:
            return
        self.root.after(SESSION_CHECK_INTERVAL_MS, self.check_session)

    def check_session(self):
        if not self.session_info:
            return
        ok, data = session_check(self.session_info.get("license_key", ""))
        if not ok:
            if data.get("transient"):
                self.session_connection_failures += 1
                if self.session_connection_failures == 1 and hasattr(self, "app"):
                    self.app.write_terminal("License server connection interrupted. Keeping this session active and retrying automatically.")
                self.root.after(TRANSIENT_RETRY_INTERVAL_MS, self.check_session)
                return
            if data.get("update_required"):
                show_update_prompt(self.root, data)
                return
            reason = data.get("message", "License session ended.")
            clear_license()
            if hasattr(self, "app"):
                self.app.force_license_logout(reason)
            return
        if self.session_connection_failures and hasattr(self, "app"):
            self.app.write_terminal("License server connection restored.")
        self.session_connection_failures = 0
        self.session_info.update(data)
        self.schedule_local_expiry()
        self.schedule_session_check()


if __name__ == "__main__":
    root = tk.Tk()
    LicenseLogin(root)
    root.mainloop()

