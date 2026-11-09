"""Native pywebview host; one data directory owns one server and window."""
import json
import os
from pathlib import Path
import sys
import threading
import time
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen


_runtime_prepared = False


def prepare_runtime():
    """Select the installed desktop ABI before PDF parsing imports CFFI.

    The standalone OCR worker keeps its own normal Python environment. In the
    desktop process, pythonnet and its CFFI backend must come from the same
    vendored installation even when restoring old PDF history at startup.
    """
    global _runtime_prepared
    if _runtime_prepared:
        return
    vendor = str(Path(__file__).parent / 'desktop_runtime')
    if vendor not in sys.path:
        sys.path.insert(0, vendor)
    if sys.platform == 'win32':
        import cffi
        cffi.FFI()  # Load its matching native backend before cryptography/PDFs.
    _runtime_prepared = True


class Instance:
    def __init__(self, root):
        self.root = Path(root)
        self.file = (self.root / 'desktop.lock').open('a+b')
        if os.fstat(self.file.fileno()).st_size == 0:
            self.file.write(b'0'); self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.owned = True
        except OSError:
            self.owned = False

    def focus(self):
        state = json.loads((self.root / 'desktop-session.json').read_text('utf-8'))
        url = state['url']
        parts = urlsplit(url)
        if parts.scheme != 'http' or parts.hostname != '127.0.0.1' or not parts.port:
            raise ValueError('Некорректный адрес окна приложения')
        req = Request(url + '/api/desktop/focus', data=b'{}',
                      headers={'Origin': url, 'X-Local-App': '1', 'Content-Type': 'application/json'})
        with urlopen(req, timeout=5) as response:
            if not json.load(response).get('ok'):
                raise RuntimeError('Работающее окно не отвечает')

    def close(self):
        if self.owned:
            (self.root / 'desktop-session.json').unlink(missing_ok=True)
        self.file.close()


class Bridge:
    def __init__(self, url, webview):
        self._url, self._webview = url, webview

    def open_html(self, value):
        import re
        url = urljoin(self._url + '/', value)
        parts = urlsplit(url)
        if (parts.netloc != urlsplit(self._url).netloc or parts.scheme != 'http'
                or not re.fullmatch(r'/api/(?:view/[0-9a-f]{32}|installed/[0-9a-f]{32}/view)/document\.html', parts.path)):
            raise ValueError('Разрешён только HTML готового документа')
        self._webview.create_window('Предпросмотр в HTML', url, width=1100, height=850)
        return True


def acquire_or_focus(root, timeout=10):
    deadline = time.monotonic() + timeout
    while True:
        instance = Instance(root)
        if instance.owned:
            return instance
        try:
            instance.focus()
            return None
        except (OSError, ValueError):
            if time.monotonic() >= deadline:
                raise
        finally:
            instance.close()
        time.sleep(.2)


def run(server, url, root):
    prepare_runtime()
    if sys.platform == 'win32':
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Region.DocumentConverter')
    import webview
    bridge = Bridge(url, webview)
    window = webview.create_window('Конвертер документов', url, js_api=bridge,
                                  width=1320, height=900, min_size=(900, 650))
    window.events.loaded += window.show
    server.desktop_focus = lambda: (window.restore(), window.show())
    Path(root, 'desktop-session.json').write_text(json.dumps({'url': url, 'pid': os.getpid()}), 'utf-8')
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .25}, daemon=True)
    thread.start()
    def watch_server():
        thread.join()
        for item in list(webview.windows):
            item.destroy()
    threading.Thread(target=watch_server, daemon=True).start()
    def close_children():
        for item in list(webview.windows):
            if item is not window:
                item.destroy()
    window.events.closed += close_children
    try:
        webview.start(gui='edgechromium' if sys.platform == 'win32' else None,
                      icon=str(Path(__file__).parent / 'assets' / 'app-icon.ico'),
                      private_mode=False, storage_path=str(Path(root) / 'webview-profile'))
    finally:
        server.shutdown()
        thread.join(timeout=10)
