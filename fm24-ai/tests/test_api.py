import json
import threading
import unittest
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from unittest.mock import Mock
from api.server import create_server, StateService
from bridge.process import MemoryReadError

class StateServiceTests(unittest.TestCase):
    def test_status_rejects_changed_club_even_when_registry_is_reused(self):
        # Observed during the Feb 7 -> Feb 17 reload: stable registry, new team.
        bridge=Mock()
        bridge.fm.alive.return_value=True
        bridge.db.person_pointers.return_value=[0x1000,0x2000]
        bridge.manager.side_effect=MemoryReadError('Current club changed; reconnect')
        service=StateService(); service.bridge=bridge
        status,body=service.get('/status')
        self.assertEqual(status,200)
        self.assertFalse(body['connected'])
        self.assertNotIn('session_id',body)
        self.assertIsNone(service.bridge)
        bridge.close.assert_called_once()

class FakeService:
    def __init__(self): self.calls=[]
    def get(self,path):
        self.calls.append(path)
        return 200,{'read_only':True,'name':'Murić'}

class APITests(unittest.TestCase):
    def setUp(self):
        self.service=FakeService()
        self.server=create_server(0,self.service)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}'
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
    def request(self,path='/status',method='GET',headers=None):
        req=Request(self.url+path,method=method,headers=headers or {})
        try: response=urlopen(req,timeout=3)
        except HTTPError as exc: response=exc
        with response: return response.status,json.loads(response.read()),response.headers
    def test_json_utf8_and_loopback_binding(self):
        status,data,headers=self.request()
        self.assertEqual(status,200); self.assertEqual(data['name'],'Murić')
        self.assertEqual(self.server.server_address[0],'127.0.0.1')
        self.assertEqual(headers['Cache-Control'],'no-store')
    def test_mutation_methods_never_reach_service(self):
        for method in ('POST','PUT','PATCH','DELETE'):
            self.assertEqual(self.request(method=method)[0],405)
        self.assertEqual(self.service.calls,[])
    def test_foreign_host_and_origin_denied(self):
        self.assertEqual(self.request(headers={'Host':'attacker.example'})[0],403)
        self.assertEqual(self.request(headers={'Origin':'https://attacker.example'})[0],403)
        self.assertEqual(self.service.calls,[])
    def test_query_rejected(self):
        self.assertEqual(self.request('/status?address=123')[0],400)
        self.assertEqual(self.service.calls,[])

if __name__=='__main__': unittest.main()
