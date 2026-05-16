import http.server
import socketserver
import threading
from src.core.config import MEDIA_DIR, get_env, logger

class MediaProxyServer:
    def __init__(self):
        self.port = int(get_env("MEDIA_PROXY_PORT", "8080"))
        self.server = None
        self.thread = None

    def start(self):
        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(MEDIA_DIR), **kwargs)
            def log_message(self, format, *args):
                pass # Suppress HTTP logs to keep console clean

        class ReusableTCPServer(socketserver.TCPServer):
            allow_reuse_address = True

        try:
            self.server = ReusableTCPServer(("", self.port), Handler)
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            logger.info(f"Media Proxy Server running on port {self.port}")
        except OSError as e:
            if getattr(e, "winerror", None) == 10048:
                logger.warning(f"Media Proxy port {self.port} is already in use. Another app instance may already be running.")
            else:
                logger.error(f"Failed to start proxy server: {e}")
        except Exception as e:
            logger.error(f"Failed to start proxy server: {e}")

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            logger.info("Media Proxy Server stopped.")
