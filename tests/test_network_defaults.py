import argparse
import contextlib
import io
import json
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import network_defaults as net
import provision
import wizard

class NetworkDefaultTests(unittest.TestCase):
    def args(self,**changes):
        values=dict(domain='host.example.org',tls_mode='auto',port=None,test=False)
        values.update(changes); return argparse.Namespace(**values)

    def test_matching_public_hostname_is_default(self):
        with patch.object(net,'public_addresses',return_value=['8.8.8.8']), patch.object(net.subprocess,'check_output',return_value='vps.example.org\n'), patch.object(net.socket,'getaddrinfo',return_value=[(None,None,None,None,('8.8.8.8',0))]):
            self.assertEqual(net.discover_host()['host'],'vps.example.org')

    def test_mismatched_hostname_falls_back_to_public_ip(self):
        with patch.object(net,'public_addresses',return_value=['8.8.8.8']), patch.object(net.subprocess,'check_output',return_value='vps.example.org\n'), patch.object(net.socket,'getaddrinfo',return_value=[(None,None,None,None,('1.1.1.1',0))]):
            self.assertEqual(net.discover_host()['host'],'8.8.8.8')

    def test_private_hostname_never_becomes_public_default(self):
        with patch.object(net,'public_addresses',return_value=[]), patch.object(net.subprocess,'check_output',return_value='localhost\n'):
            self.assertEqual(net.discover_host()['host'],'')

    def test_validation_supports_public_ip_and_rejects_urls(self):
        for value in ('8.8.8.8','2606:4700:4700::1111','vps.example.org'): self.assertTrue(net.valid_host(value))
        for value in ('127.0.0.1','localhost','https://vps.example.org','vps.example.org:8443','a..org','-a.example.org'): self.assertFalse(net.valid_host(value))

    def test_occupied_ipv4_port_not_proposed(self):
        with socket.socket() as listener:
            listener.bind(('0.0.0.0',0)); listener.listen()
            self.assertFalse(net.port_available(listener.getsockname()[1]))

    def test_fallback_port_when_preferred_busy(self):
        with patch.object(net,'port_available',side_effect=lambda port:port==9443):
            self.assertEqual(net.suggest_port(8443),9443)

    def test_port_443_and_auto_acme_when_standard_ports_free(self):
        with patch.object(provision,'port_available',return_value=True):
            mode=provision.configuration(self.args())
        self.assertEqual((mode['port'],mode['tls_mode']),(443,'acme'))
        self.assertEqual(net.edge_profile(mode),'acme')

    def test_busy_standard_ports_use_custom_internal_tls(self):
        with patch.object(provision,'port_available',return_value=False), patch.object(provision,'suggest_port',return_value=8443):
            mode=provision.configuration(self.args())
        self.assertEqual((mode['port'],mode['tls_mode']),(8443,'internal'))
        self.assertEqual(net.edge_profile(mode),'public')

    def test_public_ip_never_claims_acme_support(self):
        with patch.object(provision,'port_available',return_value=True), patch.object(provision,'suggest_port',return_value=8443):
            mode=provision.configuration(self.args(domain='8.8.8.8'))
        self.assertEqual(mode['tls_mode'],'internal')
        with self.assertRaises(SystemExit): provision.configuration(self.args(domain='8.8.8.8',tls_mode='acme',port=443))

    def test_custom_port_preserved_in_url(self):
        self.assertEqual(net.url_for({'mode':'public','host':'vps.example.org','port':8443,'tls_mode':'internal'}),'https://vps.example.org:8443/')
        self.assertEqual(net.url_for({'mode':'public','host':'2606:4700:4700::1111','port':8443,'tls_mode':'internal'}),'https://[2606:4700:4700::1111]:8443/')

    def test_custom_port_caddy_uses_internal_ca(self):
        text=provision.render_caddy({'host':'8.8.8.8','tls_mode':'internal'},'synthetic-test-value')
        self.assertIn('tls internal',text); self.assertIn('https://8.8.8.8',text)
        self.assertNotIn('8443',text) # host port mapping belongs to Compose

    def test_wizard_reprompts_instead_of_stopping_service(self):
        with patch.object(wizard,'answer',side_effect=['443','9443']), patch.object(wizard,'port_available',side_effect=[False,True]), patch.object(wizard,'suggest_port',return_value=9443), patch.object(wizard,'call') as call, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(wizard.select_free_port('Port',443),9443)
        call.assert_not_called()

    def test_wizard_uses_detected_domain_and_port_defaults(self):
        seen=[]
        def response(prompt,default=''):
            seen.append((prompt,default)); return default
        with patch.object(wizard,'discover_host',return_value={'host':'vps.example.org','addresses':['8.8.8.8'],'kind':'domain'}), patch.object(wizard,'answer',side_effect=response), patch.object(wizard.socket,'getaddrinfo',return_value=[]), patch.object(wizard,'port_available',side_effect=lambda port:port==8443), patch.object(wizard,'suggest_port',return_value=8443), patch.object(wizard,'yes',return_value=True), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(wizard.select_public_access(),('vps.example.org',8443,'internal'))
        self.assertEqual(seen[0][1],'vps.example.org'); self.assertEqual(seen[1][1],'8443')

    def test_declining_self_signed_does_not_deploy(self):
        with patch.object(wizard,'discover_host',return_value={'host':'8.8.8.8'}), patch.object(wizard,'answer',side_effect=['8.8.8.8','8443']), patch.object(wizard,'port_available',return_value=True), patch.object(wizard,'suggest_port',return_value=8443), patch.object(wizard,'yes',return_value=False), patch.object(wizard,'call') as call, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit): wizard.select_public_access()
        call.assert_not_called()

if __name__=='__main__': unittest.main()
