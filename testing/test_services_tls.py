"""Exercise certificate reconciliation without root access or ACME requests."""
import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, mock_open, patch


spec = importlib.util.spec_from_file_location('services_tls', Path(__file__).resolve().parents[1] / 'deploy/custom_services_tls.py')
tls = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'fcntl': SimpleNamespace(flock=MagicMock(), LOCK_EX=1, LOCK_NB=2)}):
    spec.loader.exec_module(tls)


class CertificateReconciliationTest(unittest.TestCase):
    def check_sync(self, slugs, existing='', resolved=True, expired=False):
        conn = MagicMock()
        conn.__enter__.return_value.execute.return_value = [(s,) for s in slugs]
        cert = MagicMock()
        cert.exists.return_value = bool(existing)
        command_results = [SimpleNamespace(returncode=1 if expired else 0), SimpleNamespace(returncode=0)]
        with patch.object(tls, 'CERT', cert), patch.object(tls.sqlite3, 'connect', return_value=conn), \
                patch.object(tls.subprocess, 'check_output', return_value=existing), \
                patch.object(tls.subprocess, 'run', side_effect=command_results) as run, \
                patch.object(tls.socket, 'gethostbyname_ex', return_value=('domain', [], ['206.237.20.173' if resolved else '192.0.2.1'])), \
                patch('builtins.open', mock_open()), patch.object(sys, 'argv', ['tls-sync']):
            tls.main()
        return [call.args[0] for call in run.call_args_list]

    def test_new_valid_service_is_issued_without_manual_command(self):
        commands = self.check_sync(['traven', '--unsafe', '../escape', 'Uppercase'])
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][0], '/usr/local/bin/certbot')
        self.assertEqual(commands[0][commands[0].index('--domains') + 1], 'traven.services.vsakuya.xyz')

    def test_unchanged_certificate_does_not_issue_again(self):
        commands = self.check_sync(['traven'], 'DNS:traven.services.vsakuya.xyz')
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][0], 'openssl')

    def test_new_service_extends_existing_certificate(self):
        commands = self.check_sync(['traven', 'media'], 'DNS:traven.services.vsakuya.xyz')
        self.assertIn('--expand', commands[0])
        self.assertEqual(commands[0][commands[0].index('--domains') + 1], 'media.services.vsakuya.xyz,traven.services.vsakuya.xyz')

    def test_unresolved_new_service_waits_without_acme_request(self):
        commands = self.check_sync(['media'], 'DNS:traven.services.vsakuya.xyz', resolved=False)
        self.assertTrue(all(command[0] != '/usr/local/bin/certbot' for command in commands))

    def test_expiring_certificate_is_renewed(self):
        commands = self.check_sync(['traven'], 'DNS:traven.services.vsakuya.xyz', expired=True)
        self.assertEqual(commands[1][:4], ['/usr/local/bin/certbot', 'renew', '--cert-name', 'custom-services'])


if __name__ == '__main__':
    unittest.main()
