import time
import json
import threading
import requests
from datetime import datetime
from pynput import mouse, keyboard
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import psutil
import pygetwindow as gw
from shared.events import Event


class ActivityCapture:
    def __init__(self, backend_url: str, interval_ms: int = 2000):
        self.backend_url = backend_url
        self.interval_ms = interval_ms
        self.session_id = f"session_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
        self.event_buffer = []
        self.buffer_lock = threading.Lock()
        self.running = False
        self.last_active_window = ""
        self.mouse_listener = None
        self.file_observer = None

    def start(self):
        self.running = True
        self._start_mouse_listener()
        self._start_file_watcher()
        self._start_flush_thread()
        print(f"Activity capture started (session: {self.session_id})")

    def stop(self):
        self.running = False
        if self.mouse_listener:
            self.mouse_listener.stop()
        if self.file_observer:
            self.file_observer.stop()
            self.file_observer.join()
        self._flush_buffer()
        print("Activity capture stopped")

    def _start_mouse_listener(self):
        def on_click(x, y, button, pressed):
            if pressed:
                # Coordinates are both noisy and personally identifying. Capture
                # the meaningful context (the active application) instead.
                self._record_event("desktop", "click", self.last_active_window or "desktop_app", "")

        def on_scroll(x, y, dx, dy):
            self._record_event("desktop", "scroll", self.last_active_window or "desktop_app", "")

        self.mouse_listener = mouse.Listener(on_click=on_click, on_scroll=on_scroll)
        self.mouse_listener.start()

    def _start_file_watcher(self):
        class DownloadHandler(FileSystemEventHandler):
            def __init__(self, capture):
                self.capture = capture

            def on_created(self, event):
                if not event.is_directory and event.src_path.endswith(('.pdf', '.doc', '.docx', '.xls', '.xlsx', '.csv', '.txt', '.png', '.jpg', '.jpeg')):
                    extension = event.src_path.rsplit('.', 1)[-1].lower()
                    self.capture._record_event("filesystem", "download", "downloaded_file", f"file_type={extension}")

        downloads_path = self._get_downloads_path()
        if downloads_path:
            self.file_observer = Observer()
            self.file_observer.schedule(DownloadHandler(self), downloads_path, recursive=False)
            self.file_observer.start()

    def _get_downloads_path(self):
        import os
        return os.path.expanduser("~/Downloads")

    def _record_event(self, source: str, action: str, target: str, payload: str):
        active_window = self._get_active_window()
        if active_window and active_window != self.last_active_window:
            self._record_event("window", "focus", active_window, "")
            self.last_active_window = active_window

        event = Event(
            source=source,
            action=action,
            target=target,
            payload=payload,
            session_id=self.session_id
        )
        with self.buffer_lock:
            self.event_buffer.append(event)

    def _get_active_window(self) -> str:
        try:
            window = gw.getActiveWindow()
            if window:
                # Do not store window titles: browser tabs and document names can
                # contain customer data. Map them to broad application categories.
                title = window.title.lower()
                if "gmail" in title or "mail" in title:
                    return "email"
                if "slack" in title or "teams" in title:
                    return "messaging"
                if "excel" in title or "spreadsheet" in title:
                    return "spreadsheet"
                if any(browser in title for browser in ("chrome", "edge", "firefox", "safari")):
                    return "browser"
                return "desktop_app"
        except:
            pass
        return ""

    def _start_flush_thread(self):
        def flush_loop():
            while self.running:
                time.sleep(self.interval_ms / 1000.0)
                self._flush_buffer()

        thread = threading.Thread(target=flush_loop, daemon=True)
        thread.start()

    def _flush_buffer(self):
        with self.buffer_lock:
            if not self.event_buffer:
                return
            events = self.event_buffer[:]
            self.event_buffer.clear()

        for event in events:
            try:
                requests.post(self.backend_url, json=json.loads(event.to_json()), timeout=5)
            except Exception as e:
                print(f"Failed to send event: {e}")
                with self.buffer_lock:
                    self.event_buffer.insert(0, event)


def main():
    import sys
    sys.path.insert(0, '.')
    backend_url = "http://localhost:8080/api/v1/events/ingest"
    capture = ActivityCapture(backend_url, interval_ms=2000)
    try:
        capture.start()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        capture.stop()


if __name__ == "__main__":
    main()
