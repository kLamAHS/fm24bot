"""Local, GET-only JSON API, with no third-party dependencies."""
from dataclasses import asdict
from datetime import datetime,timezone
from http.server import HTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlsplit
import json
import logging
import time
from bridge.session import FMBridge

log=logging.getLogger(__name__)

class StateService:
    def __init__(self):
        self.bridge=None; self.last_error=None; self.last_attempt=0

    def ready(self):
        if self.bridge and not self.bridge.fm.alive(): self.close()
        if self.bridge is None:
            if time.monotonic()-self.last_attempt<2:
                raise RuntimeError(self.last_error or 'Waiting to reconnect')
            self.last_attempt=time.monotonic()
            try:
                self.bridge=FMBridge().attach()
                self.last_error=None
            except Exception as exc:
                self.last_error=str(exc); raise
        return self.bridge

    def close(self):
        if self.bridge: self.bridge.close()
        self.bridge=None

    def get(self,path):
        if path=='/status':
            try:
                b=self.ready()
                # A loaded-save registry must still be present.
                b.db.person_pointers()
                return 200,{'connected':True,'pid':b.fm.pid,'read_only':True,'build':'24.4.2+2081827','capabilities':['player_identity','nine_attributes','current_manager','current_club','team_roster'],'unresolved':['age','positions','condition','morale','fixtures','finances','match'],'validation_scope':'one save, three profiles; see research reports'}
            except Exception as exc:
                self.close(); self.last_error=str(exc)
                return 200,{'connected':False,'read_only':True,'reason':str(exc)}
        if path in ('/fixtures','/finances','/match','/game'):
            return 501,{'error':'not_implemented','message':'Memory fields for this subsystem have not been validated.'}
        if path not in ('/manager','/club','/squad') and not path.startswith('/players/'):
            return 404,{'error':'not_found'}
        try:
            b=self.ready()
            if path=='/manager': data=asdict(b.manager())
            elif path=='/club': data=asdict(b.current_club())
            elif path=='/squad': data=[asdict(p) for p in b.squad()]
            else:
                token=path.removeprefix('/players/')
                if not token.isascii() or not token.isdecimal() or len(token)>10:
                    return 400,{'error':'invalid_player_id'}
                data=asdict(b.player(int(token)))
            return 200,{'observed_at':datetime.now(timezone.utc).isoformat(),'data':data}
        except KeyError:
            return 404,{'error':'player_not_found'}
        except Exception as exc:
            self.last_error=str(exc); self.close()
            log.warning('Observation unavailable: %s',exc)
            return 503,{'error':'observation_unavailable','message':str(exc)}

def create_server(port=8765,service=None):
    state=service or StateService()
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup(); self.connection.settimeout(5)
        def reply(self,status,payload):
            raw=json.dumps(payload,ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(raw)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.end_headers(); self.wfile.write(raw)
        def do_GET(self):
            authority=self.headers.get('Host','')
            allowed={f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}
            origin=self.headers.get('Origin')
            if authority not in allowed or (origin and origin not in {'http://'+x for x in allowed}):
                self.reply(403,{'error':'local_access_only'}); return
            path=urlsplit(self.path)
            if path.scheme or path.netloc or path.query:
                self.reply(400,{'error':'invalid_request'}); return
            status,data=state.get(path.path)
            self.reply(status,data)
        def reject_write(self):
            self.close_connection=True
            self.reply(405,{'error':'read_only','allowed':['GET']})
        do_POST=do_PUT=do_PATCH=do_DELETE=do_OPTIONS=do_HEAD=reject_write
        def log_message(self,fmt,*args): log.info(fmt,*args)
    server=HTTPServer(('127.0.0.1',port),Handler)
    server.state_service=state
    return server

def serve(port=8765):
    server=create_server(port)
    log.info('Read-only bridge listening on http://127.0.0.1:%s',port)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        server.state_service.close(); server.server_close()

if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    serve()
