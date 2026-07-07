import os
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

from archive_printer.config import AppConfig
from archive_printer.server import setup_letsencrypt


class LetsEncryptFeaturesTests(unittest.TestCase):
    def test_validation_errors(self):
        # 1. Missing domain
        config_no_domain = AppConfig(
            archive_root=Path("."),
            timezone=ZoneInfo("UTC"),
            use_letsencrypt=True,
            letsencrypt_email="user@example.com"
        )
        with self.assertRaises(ValueError) as ctx:
            setup_letsencrypt(config_no_domain)
        self.assertIn("letsencrypt_domain", str(ctx.exception))

        # 2. Missing email
        config_no_email = AppConfig(
            archive_root=Path("."),
            timezone=ZoneInfo("UTC"),
            use_letsencrypt=True,
            letsencrypt_domain="printer.example.com"
        )
        with self.assertRaises(ValueError) as ctx:
            setup_letsencrypt(config_no_email)
        self.assertIn("letsencrypt_email", str(ctx.exception))

    @patch("pathlib.Path.exists")
    def test_already_exists(self, mock_exists):
        mock_exists.return_value = True

        config = AppConfig(
            archive_root=Path("."),
            timezone=ZoneInfo("UTC"),
            use_letsencrypt=True,
            letsencrypt_email="user@example.com",
            letsencrypt_domain="printer.example.com"
        )
        
        cert, key = setup_letsencrypt(config)
        
        # Verify it returns certbot standard path directly
        if os.name == "nt":
            self.assertTrue(cert.endswith("C:\\Certbot\\live\\printer.example.com\\fullchain.pem") or cert.endswith("C:/Certbot/live/printer.example.com/fullchain.pem"))
        else:
            self.assertEqual(cert, "/etc/letsencrypt/live/printer.example.com/fullchain.pem")

    @patch("subprocess.run")
    @patch("pathlib.Path.exists")
    def test_certbot_execution_manual(self, mock_exists, mock_run):
        # Return False so certbot runs, then True on second call (or just mock run)
        mock_exists.return_value = False

        config = AppConfig(
            archive_root=Path("."),
            timezone=ZoneInfo("UTC"),
            use_letsencrypt=True,
            letsencrypt_email="user@example.com",
            letsencrypt_domain="printer.example.com",
            letsencrypt_dns_provider="manual"
        )

        try:
            setup_letsencrypt(config)
        except Exception:
            pass

        # Verify certbot was called with manual DNS challenges
        expected_cmd = [
            "certbot", "certonly",
            "--non-interactive",
            "--agree-tos",
            "--email", "user@example.com",
            "-d", "printer.example.com",
            "--manual", "--preferred-challenges", "dns", "--manual-public-ip-logging-ok"
        ]
        mock_run.assert_called_once_with(expected_cmd, check=True)

    @patch("subprocess.run")
    @patch("pathlib.Path.exists")
    def test_certbot_execution_cloudflare(self, mock_exists, mock_run):
        mock_exists.return_value = False

        config = AppConfig(
            archive_root=Path("."),
            timezone=ZoneInfo("UTC"),
            use_letsencrypt=True,
            letsencrypt_email="user@example.com",
            letsencrypt_domain="printer.example.com",
            letsencrypt_dns_provider="cloudflare",
            letsencrypt_dns_credentials_file="/creds/cloudflare.ini"
        )

        try:
            setup_letsencrypt(config)
        except Exception:
            pass

        expected_cmd = [
            "certbot", "certonly",
            "--non-interactive",
            "--agree-tos",
            "--email", "user@example.com",
            "-d", "printer.example.com",
            "--dns-cloudflare", "--dns-cloudflare-credentials", "/creds/cloudflare.ini"
        ]
        mock_run.assert_called_once_with(expected_cmd, check=True)

    @patch("subprocess.run")
    @patch("pathlib.Path.exists")
    def test_certbot_execution_route53(self, mock_exists, mock_run):
        mock_exists.return_value = False

        config = AppConfig(
            archive_root=Path("."),
            timezone=ZoneInfo("UTC"),
            use_letsencrypt=True,
            letsencrypt_email="user@example.com",
            letsencrypt_domain="printer.example.com",
            letsencrypt_dns_provider="route53"
        )

        try:
            setup_letsencrypt(config)
        except Exception:
            pass

        expected_cmd = [
            "certbot", "certonly",
            "--non-interactive",
            "--agree-tos",
            "--email", "user@example.com",
            "-d", "printer.example.com",
            "--dns-route53"
        ]
        mock_run.assert_called_once_with(expected_cmd, check=True)

    @patch("subprocess.run")
    @patch("pathlib.Path.exists")
    def test_certbot_execution_custom_script(self, mock_exists, mock_run):
        mock_exists.return_value = False

        config = AppConfig(
            archive_root=Path("."),
            timezone=ZoneInfo("UTC"),
            use_letsencrypt=True,
            letsencrypt_email="user@example.com",
            letsencrypt_domain="printer.example.com",
            letsencrypt_dns_provider="/usr/local/bin/dns-hook.sh"
        )

        try:
            setup_letsencrypt(config)
        except Exception:
            pass

        expected_cmd = [
            "certbot", "certonly",
            "--non-interactive",
            "--agree-tos",
            "--email", "user@example.com",
            "-d", "printer.example.com",
            "--manual", "--preferred-challenges", "dns",
            "--manual-auth-hook", "/usr/local/bin/dns-hook.sh",
            "--manual-public-ip-logging-ok"
        ]
        mock_run.assert_called_once_with(expected_cmd, check=True)


if __name__ == "__main__":
    unittest.main()
