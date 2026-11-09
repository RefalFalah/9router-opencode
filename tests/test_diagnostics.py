import contextlib
import errno
import io
import os
from pathlib import Path
import socket
import ssl
import sys
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import setup_9router as setup


class NetworkDiagnosticsTests(unittest.TestCase):
    def test_known_network_errors_are_classified_without_raw_details(self):
        certificate = ssl.SSLCertVerificationError(1, "fake-secret-url-and-password")
        certificate.verify_code = 20
        cases = (
            (URLError(certificate), "TLS_CERTIFICATE_VERIFY_FAILED"),
            (URLError(socket.gaierror(-2, "fake-secret-url-and-password")), "DNS_ERROR"),
            (URLError(TimeoutError("fake-secret-url-and-password")), "TIMEOUT"),
            (URLError(ssl.SSLError(1, "fake-secret-url-and-password")), "TLS_ERROR"),
            (URLError(ConnectionRefusedError(errno.ECONNREFUSED, "fake-secret-url-and-password")), "ECONNREFUSED"),
            (URLError(OSError(errno.ENETUNREACH, "fake-secret-url-and-password")), "ENETUNREACH"),
            (URLError("Tunnel connection failed: 407 fake-secret-url-and-password"), "PROXY_ERROR"),
            (URLError("fake-secret-url-and-password"), "NETWORK_ERROR"),
        )
        for error, expected in cases:
            with self.subTest(expected=expected):
                result = setup.network_error_message(error)
                self.assertIn(expected, result)
                self.assertNotIn("fake-secret-url-and-password", result)

    def test_fetch_models_exposes_safe_network_category(self):
        opener = MagicMock()
        opener.open.side_effect = URLError(socket.gaierror(-2, "fake-secret"))
        with patch.object(setup, "build_opener", return_value=opener):
            with self.assertRaises(setup.SetupError) as captured:
                setup.fetch_models("https://router.test/v1", "fake-secret")
        self.assertIn("DNS_ERROR", str(captured.exception))
        self.assertNotIn("fake-secret", str(captured.exception))

    def test_configurable_timeout_before_response_headers(self):
        opener = MagicMock()
        opener.open.side_effect = URLError(TimeoutError("fake-secret"))
        with patch.object(setup, "build_opener", return_value=opener):
            with self.assertRaises(setup.SetupError) as captured:
                setup.fetch_models("https://router.test/v1", "fake-secret", timeout=120)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 120)
        self.assertIn("120 detik", str(captured.exception))
        self.assertIn("sebelum header HTTP", str(captured.exception))
        self.assertNotIn("fake-secret", str(captured.exception))

    def test_timeout_while_reading_models_is_distinguished(self):
        opener = MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.read.side_effect = TimeoutError("fake-secret")
        with patch.object(setup, "build_opener", return_value=opener):
            with self.assertRaises(setup.SetupError) as captured:
                setup.fetch_models("https://router.test/v1", "fake-secret", timeout=75)
        self.assertIn("75 detik", str(captured.exception))
        self.assertIn("saat membaca body daftar model", str(captured.exception))
        self.assertNotIn("fake-secret", str(captured.exception))

    def test_cli_passes_timeout_to_keyless_diagnosis(self):
        with patch.object(setup, "diagnose_connection", return_value=0) as diagnosis:
            result = setup.main(["--url", "https://router.test/v1", "--diagnose", "--timeout", "95"])
        self.assertEqual(result, 0)
        diagnosis.assert_called_once_with("https://router.test/v1", timeout=95)

    def test_timeout_must_be_positive(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as captured:
            setup.main(["--timeout", "0"])
        self.assertEqual(captured.exception.code, 2)

    def test_diagnosis_401_sends_no_key_and_skips_config_and_prompts(self):
        opener = MagicMock()
        opener.open.side_effect = HTTPError(
            "https://router.test/v1/models", 401, "Unauthorized", {}, io.BytesIO(b'{"error":"key required"}')
        )
        output = io.StringIO()
        with patch.object(setup, "build_opener", return_value=opener), patch.object(setup, "config_location") as location, patch.object(setup.getpass, "getpass") as getpass, patch.dict(os.environ, {"NINEROUTER_API_KEY": "fake-secret", "HTTPS_PROXY": "http://user:fake-proxy-password@proxy.test"}), contextlib.redirect_stdout(output):
            result = setup.main(["--url", "https://router.test/v1", "--diagnose"])
        self.assertEqual(result, 0)
        location.assert_not_called()
        getpass.assert_not_called()
        request = opener.open.call_args.args[0]
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(request.get_header("User-agent"), setup.USER_AGENT)
        self.assertIn("HTTP 401", output.getvalue())
        self.assertIn("HTTPS_PROXY", output.getvalue())
        self.assertNotIn("fake-secret", output.getvalue())
        self.assertNotIn("fake-proxy-password", output.getvalue())

    def test_diagnosis_tls_failure_is_not_treated_as_success(self):
        opener = MagicMock()
        opener.open.side_effect = URLError(ssl.SSLCertVerificationError(1, "fake-secret"))
        output = io.StringIO()
        with patch.object(setup, "build_opener", return_value=opener), contextlib.redirect_stdout(output):
            result = setup.diagnose_connection("https://router.test/v1")
        self.assertEqual(result, 1)
        self.assertIn("TLS_CERTIFICATE_VERIFY_FAILED", output.getvalue())
        self.assertNotIn("fake-secret", output.getvalue())


if __name__ == "__main__":
    unittest.main()
