import http.server
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest import mock

from run_alone import run_alone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ci'))
import verify_flows


class DeployedProbe(unittest.TestCase):
    @run_alone
    def test_a_target_that_answers_too_slowly_gets_no_answer_within_the_deadline(self):
        class Drip(http.server.BaseHTTPRequestHandler):
            def do_HEAD(self):
                for byte in b'HTTP/1.1 200 OK\r\n':
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(0.2)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(('localhost', 0), Drip)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f'http://localhost:{server.server_port}'
        with mock.patch.object(verify_flows, 'PROBE_TIMEOUT', 1):
            started = time.monotonic()
            self.assertEqual(verify_flows.probe(url), (None, f'HEAD {url} got no response within 1s'))
        self.assertLess(time.monotonic() - started, 2)
