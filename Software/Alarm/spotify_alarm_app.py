import base64
import json
import os
import secrets
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tkinter import Tk, StringVar, BooleanVar, END
from tkinter import ttk, messagebox
from tkinter.scrolledtext import ScrolledText
from urllib.parse import urlparse, parse_qs, urlencode

import requests


APP_NAME = "Spotify Alarm"
SETTINGS_PATH = Path(__file__).with_name("spotify_alarm_settings.json")
TOKENS_PATH = Path(__file__).with_name("spotify_alarm_tokens.json")

SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"


def now_ts() -> int:
    return int(time.time())


def basic_auth_header(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("utf-8")


def parse_spotify_target(text: str):
    text = text.strip()
    if not text:
        raise ValueError("Enter a Spotify track, album, or playlist URL/URI.")

    if text.startswith("spotify:"):
        parts = text.split(":")
        if len(parts) < 3:
            raise ValueError("That Spotify URI does not look valid.")
        content_type = parts[1]
        if content_type == "track":
            return {"kind": "track", "uri": text}
        if content_type in {"album", "playlist"}:
            return {"kind": "context", "uri": text}
        raise ValueError("Only track, album, and playlist URIs are supported.")

    parsed = urlparse(text)
    if "spotify.com" not in parsed.netloc:
        raise ValueError("Paste a Spotify URL or URI.")
    path_parts = [p for p in parsed.path.split("/") if p]
    if len(path_parts) < 2:
        raise ValueError("That Spotify URL does not look valid.")
    content_type = path_parts[0]
    content_id = path_parts[1]
    if "?" in content_id:
        content_id = content_id.split("?", 1)[0]

    if content_type == "track":
        return {"kind": "track", "uri": f"spotify:track:{content_id}"}
    if content_type in {"album", "playlist"}:
        return {"kind": "context", "uri": f"spotify:{content_type}:{content_id}"}
    raise ValueError("Only track, album, and playlist URLs are supported.")


class TokenStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self):
        if not self.path.exists():
            return None
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, data):
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path != self.server.expected_path:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Wrong callback path.")
            return

        self.server.auth_code = query.get("code", [None])[0]
        self.server.state = query.get("state", [None])[0]
        self.server.error = query.get("error", [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body style='font-family: sans-serif;'>"
            b"<h2>Spotify authorization received.</h2>"
            b"<p>You can close this tab and return to the app.</p>"
            b"</body></html>"
        )

    def log_message(self, format, *args):
        return


class SpotifyClient:
    def __init__(self, settings_getter, logger):
        self.settings_getter = settings_getter
        self.logger = logger
        self.token_store = TokenStore(TOKENS_PATH)

    def _settings(self):
        return self.settings_getter()

    def _token_data(self):
        token_data = self.token_store.load()
        if not token_data:
            raise RuntimeError("Not authorized yet. Click 'Authorize Spotify' first.")
        return token_data

    def _ensure_token(self):
        token_data = self._token_data()
        if token_data.get("expires_at", 0) > now_ts() + 60:
            return token_data["access_token"]

        refresh_token = token_data.get("refresh_token")
        if not refresh_token:
            raise RuntimeError("Missing refresh token. Re-authorize Spotify.")

        settings = self._settings()
        client_id = settings["client_id"].strip()
        client_secret = settings["client_secret"].strip()
        redirect_uri = settings["redirect_uri"].strip()

        headers = {
            "Authorization": basic_auth_header(client_id, client_secret),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "redirect_uri": redirect_uri,
        }
        response = requests.post(SPOTIFY_TOKEN_URL, headers=headers, data=data, timeout=20)
        response.raise_for_status()
        refreshed = response.json()

        token_data["access_token"] = refreshed["access_token"]
        token_data["expires_at"] = now_ts() + int(refreshed.get("expires_in", 3600))
        if refreshed.get("refresh_token"):
            token_data["refresh_token"] = refreshed["refresh_token"]
        self.token_store.save(token_data)
        self.logger("Refreshed Spotify access token.")
        return token_data["access_token"]

    def authorize(self):
        settings = self._settings()
        client_id = settings["client_id"].strip()
        client_secret = settings["client_secret"].strip()
        redirect_uri = settings["redirect_uri"].strip()

        if not client_id or not client_secret:
            raise RuntimeError("Enter your Spotify Client ID and Client Secret first.")
        if not redirect_uri:
            raise RuntimeError("Enter a Redirect URI first.")

        parsed = urlparse(redirect_uri)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or not parsed.port:
            raise RuntimeError(
                "For simplicity, use a local Redirect URI like http://127.0.0.1:8888/callback"
            )

        state = secrets.token_urlsafe(16)
        scope = "user-modify-playback-state user-read-playback-state"
        params = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
        }
        auth_url = f"{SPOTIFY_AUTH_URL}?{urlencode(params)}"

        self.logger("Opening Spotify authorization in your browser...")
        webbrowser.open(auth_url)

        server = HTTPServer((parsed.hostname, parsed.port), OAuthCallbackHandler)
        server.expected_path = parsed.path or "/"
        server.auth_code = None
        server.state = None
        server.error = None
        server.timeout = 1

        start = time.time()
        self.logger("Waiting for Spotify callback on the local redirect URI...")
        while time.time() - start < 180:
            server.handle_request()
            if server.error:
                raise RuntimeError(f"Spotify authorization failed: {server.error}")
            if server.auth_code:
                if server.state != state:
                    raise RuntimeError("State check failed. Try authorizing again.")
                break
        else:
            raise RuntimeError("Timed out waiting for Spotify authorization.")

        headers = {
            "Authorization": basic_auth_header(client_id, client_secret),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {
            "grant_type": "authorization_code",
            "code": server.auth_code,
            "redirect_uri": redirect_uri,
        }
        response = requests.post(SPOTIFY_TOKEN_URL, headers=headers, data=data, timeout=20)
        response.raise_for_status()
        token_payload = response.json()

        saved = {
            "access_token": token_payload["access_token"],
            "refresh_token": token_payload.get("refresh_token"),
            "expires_at": now_ts() + int(token_payload.get("expires_in", 3600)),
        }
        self.token_store.save(saved)
        self.logger("Spotify authorization finished successfully.")

    def _api(self, method, path, *, params=None, json_data=None):
        access_token = self._ensure_token()
        headers = {"Authorization": f"Bearer {access_token}"}
        if json_data is not None:
            headers["Content-Type"] = "application/json"

        response = requests.request(
            method,
            f"{SPOTIFY_API_BASE}{path}",
            headers=headers,
            params=params,
            json=json_data,
            timeout=20,
        )

        if response.status_code == 204:
            return None
        if not response.ok:
            try:
                details = response.json()
            except Exception:
                details = response.text
            raise RuntimeError(f"Spotify API error {response.status_code}: {details}")
        return response.json()

    def get_devices(self):
        data = self._api("GET", "/me/player/devices")
        return data.get("devices", [])

    def transfer_playback(self, device_id: str):
        self._api("PUT", "/me/player", json_data={"device_ids": [device_id], "play": False})

    def start_playback(self, target_text: str, device_name_contains: str = ""):
        target = parse_spotify_target(target_text)
        device_id = None
        device_name_contains = device_name_contains.strip()

        if device_name_contains:
            devices = self.get_devices()
            for device in devices:
                if device_name_contains.lower() in device.get("name", "").lower():
                    device_id = device["id"]
                    break
            if not device_id:
                available = ", ".join(d.get("name", "Unknown") for d in devices) or "(none)"
                raise RuntimeError(
                    "Could not find a Spotify device matching that name. "
                    f"Available devices: {available}"
                )
            self.transfer_playback(device_id)

        payload = {"position_ms": 0}
        if target["kind"] == "track":
            payload["uris"] = [target["uri"]]
        else:
            payload["context_uri"] = target["uri"]

        params = {"device_id": device_id} if device_id else None
        self._api("PUT", "/me/player/play", params=params, json_data=payload)

        if device_name_contains:
            self.logger(f"Started playback on device matching '{device_name_contains}'.")
        else:
            self.logger("Started playback on the current active Spotify device.")


class AlarmScheduler:
    def __init__(self, app):
        self.app = app
        self.thread = None
        self.stop_event = threading.Event()
        self.last_trigger_date = None

    def is_running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self):
        if self.is_running():
            raise RuntimeError("Alarm is already armed.")
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def _loop(self):
        self.app.log("Alarm armed.")
        while not self.stop_event.is_set():
            settings = self.app.get_settings()
            alarm_text = settings["alarm_time"].strip()
            repeat_daily = settings["repeat_daily"]

            try:
                alarm_hour, alarm_minute = self.app.parse_alarm_time(alarm_text)
            except Exception:
                self.app.log("Alarm stopped: invalid time format.")
                return

            now = datetime.now()
            today_key = now.strftime("%Y-%m-%d")

            if (
                now.hour == alarm_hour
                and now.minute == alarm_minute
                and self.last_trigger_date != today_key
            ):
                self.last_trigger_date = today_key
                self.app.log("Alarm time reached. Starting Spotify playback...")
                try:
                    self.app.spotify.start_playback(
                        settings["spotify_target"],
                        settings["device_name_contains"],
                    )
                except Exception as exc:
                    self.app.log(f"Playback failed: {exc}")

                if not repeat_daily:
                    self.app.log("One-time alarm finished. Disarming.")
                    return

            time.sleep(1)

        self.app.log("Alarm disarmed.")


class SpotifyAlarmApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME)
        self.spotify = SpotifyClient(self.get_settings, self.log)
        self.scheduler = AlarmScheduler(self)

        defaults = {
            "client_id": "",
            "client_secret": "",
            "redirect_uri": "http://127.0.0.1:8888/callback",
            "spotify_target": "",
            "device_name_contains": "",
            "alarm_time": "07:00",
            "repeat_daily": True,
        }
        loaded = defaults.copy()
        if SETTINGS_PATH.exists():
            try:
                loaded.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
            except Exception:
                pass

        self.client_id_var = StringVar(value=loaded["client_id"])
        self.client_secret_var = StringVar(value=loaded["client_secret"])
        self.redirect_uri_var = StringVar(value=loaded["redirect_uri"])
        self.spotify_target_var = StringVar(value=loaded["spotify_target"])
        self.device_name_var = StringVar(value=loaded["device_name_contains"])
        self.alarm_time_var = StringVar(value=loaded["alarm_time"])
        self.repeat_daily_var = BooleanVar(value=loaded["repeat_daily"])

        self._build_ui()
        self.log("Pair your MakerHawk module to the computer first, then set it as the computer's audio output.")
        self.log("After that, this app just tells Spotify on the computer to start playing at the alarm time.")

    def _build_ui(self):
        frame = ttk.Frame(self.root, padding=12)
        frame.grid(sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)

        row = 0
        for label, variable, show in [
            ("Spotify Client ID", self.client_id_var, None),
            ("Spotify Client Secret", self.client_secret_var, "*"),
            ("Redirect URI", self.redirect_uri_var, None),
            ("Song / Album / Playlist URL or URI", self.spotify_target_var, None),
            ("Spotify device name contains", self.device_name_var, None),
            ("Alarm time (24h HH:MM)", self.alarm_time_var, None),
        ]:
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=4)
            entry = ttk.Entry(frame, textvariable=variable, show=show)
            entry.grid(row=row, column=1, sticky="ew", pady=4)
            row += 1

        ttk.Checkbutton(frame, text="Repeat daily", variable=self.repeat_daily_var).grid(
            row=row, column=1, sticky="w", pady=4
        )
        row += 1

        button_frame = ttk.Frame(frame)
        button_frame.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(8, 8))
        for i in range(6):
            button_frame.columnconfigure(i, weight=1)

        ttk.Button(button_frame, text="Save settings", command=self.save_settings).grid(row=0, column=0, padx=3)
        ttk.Button(button_frame, text="Authorize Spotify", command=self.authorize_spotify).grid(row=0, column=1, padx=3)
        ttk.Button(button_frame, text="List devices", command=self.list_devices).grid(row=0, column=2, padx=3)
        ttk.Button(button_frame, text="Play now", command=self.play_now).grid(row=0, column=3, padx=3)
        ttk.Button(button_frame, text="Arm alarm", command=self.arm_alarm).grid(row=0, column=4, padx=3)
        ttk.Button(button_frame, text="Disarm", command=self.disarm_alarm).grid(row=0, column=5, padx=3)
        row += 1

        self.log_box = ScrolledText(frame, height=14, wrap="word")
        self.log_box.grid(row=row, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
        frame.rowconfigure(row, weight=1)

    def parse_alarm_time(self, text: str):
        text = text.strip()
        parts = text.split(":")
        if len(parts) != 2:
            raise ValueError("Use HH:MM.")
        hour = int(parts[0])
        minute = int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("Invalid 24-hour time.")
        return hour, minute

    def get_settings(self):
        return {
            "client_id": self.client_id_var.get(),
            "client_secret": self.client_secret_var.get(),
            "redirect_uri": self.redirect_uri_var.get(),
            "spotify_target": self.spotify_target_var.get(),
            "device_name_contains": self.device_name_var.get(),
            "alarm_time": self.alarm_time_var.get(),
            "repeat_daily": bool(self.repeat_daily_var.get()),
        }

    def save_settings(self):
        SETTINGS_PATH.write_text(json.dumps(self.get_settings(), indent=2), encoding="utf-8")
        self.log("Settings saved.")

    def log(self, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_box.insert(END, f"[{timestamp}] {message}\n")
        self.log_box.see(END)
        self.root.update_idletasks()

    def _run_in_thread(self, target):
        def wrapped():
            try:
                target()
            except Exception as exc:
                self.log(str(exc))
                messagebox.showerror(APP_NAME, str(exc))
        threading.Thread(target=wrapped, daemon=True).start()

    def authorize_spotify(self):
        self.save_settings()
        self._run_in_thread(self.spotify.authorize)

    def list_devices(self):
        self.save_settings()

        def action():
            devices = self.spotify.get_devices()
            if not devices:
                self.log("No available Spotify devices found. Open Spotify on the target computer/device first.")
                return
            self.log("Available Spotify devices:")
            for d in devices:
                active = "ACTIVE" if d.get("is_active") else "idle"
                self.log(f"  - {d.get('name')} | type={d.get('type')} | {active}")

        self._run_in_thread(action)

    def play_now(self):
        self.save_settings()

        def action():
            self.spotify.start_playback(
                self.spotify_target_var.get(),
                self.device_name_var.get(),
            )

        self._run_in_thread(action)

    def arm_alarm(self):
        self.save_settings()
        try:
            self.parse_alarm_time(self.alarm_time_var.get())
            parse_spotify_target(self.spotify_target_var.get())
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        try:
            self.scheduler.start()
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))

    def disarm_alarm(self):
        self.scheduler.stop()

    def on_close(self):
        self.scheduler.stop()
        self.root.destroy()


def main():
    root = Tk()
    root.geometry("860x560")
    app = SpotifyAlarmApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
