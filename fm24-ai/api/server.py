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
                # FM can reuse the entire Person registry across a save reload.
                # Validate the manager/club chain as well before reporting ready.
                b.manager()
                game=b.game()
                return 200,{'connected':True,'pid':b.fm.pid,'session_id':b.session_id,'read_only':True,'build':'24.4.2+2081827','game_date':game.date,'capabilities':['player_identity','player_attributes_47','primary_nationality','employment_contracts','loan_contracts','positions','condition','match_sharpness','readiness_freshness','morale','date_of_birth','age','game_date','current_manager','current_club','team_roster','club_finances','transfer_budget','wage_budget','fixtures','results','time_of_day','match_viewer','match_score','match_clock','match_team_statistics','match_player_identity','match_player_ratings','match_player_goals','match_player_yellow_cards','match_retained_condition','match_player_positions','opposition_starting_formation','club_staff','selected_tactic','selected_lineup','inbox_metadata','training_schedule','training_program_settings','player_shortlists','scout_report_metadata','scout_report_knowledge','transfer_targets'],'unresolved':['match_replay_classification','match_current_simulation_condition','match_red_cards','match_injuries','opposition_current_formation','virtual_player_names','contract_clauses','secondary_nationalities','staff_attributes','team_instructions','all_tactic_role_combinations','inbox_text','inbox_attachments','training_current_ratings','training_positions','all_training_focus_labels','effective_training_intensity','scouting_recommendations','scouting_report_text','world_scouting_knowledge','shortlist_expiry','transfer_target_terms','all_transfer_target_labels','finance_breakdowns','scouting_budget','debts'],'validation_scope':'validated on two snapshots of one Wycombe career and full FM restarts, including a different active match after restart; see research reports for per-field limits'}
            except Exception as exc:
                self.close(); self.last_error=str(exc)
                return 200,{'connected':False,'read_only':True,'reason':str(exc)}
        if path not in ('/game','/manager','/club','/squad','/finances','/fixtures','/match','/staff','/tactics','/inbox','/training','/scouting','/shortlists','/transfer-targets') and not path.startswith('/players/'):
            return 404,{'error':'not_found'}
        try:
            b=self.ready()
            if path=='/game': data=asdict(b.game())
            elif path=='/manager': data=asdict(b.manager())
            elif path=='/club': data=asdict(b.current_club())
            elif path=='/squad': data=[asdict(p) for p in b.squad()]
            elif path=='/finances': data=asdict(b.finances())
            elif path=='/fixtures': data=asdict(b.fixtures())
            elif path=='/match': data=asdict(b.match())
            elif path=='/staff': data=[asdict(s) for s in b.staff()]
            elif path=='/tactics': data=asdict(b.tactics())
            elif path=='/inbox': data=asdict(b.inbox())
            elif path=='/training': data=asdict(b.training())
            elif path=='/scouting': data=asdict(b.scouting())
            elif path=='/shortlists': data=asdict(b.shortlists())
            elif path=='/transfer-targets': data=asdict(b.transfer_targets())
            else:
                token=path.removeprefix('/players/')
                if not token.isascii() or not token.isdecimal() or len(token)>10:
                    return 400,{'error':'invalid_player_id'}
                data=asdict(b.player(int(token)))
            return 200,{'observed_at':datetime.now(timezone.utc).isoformat(),'session_id':b.session_id,'data':data}
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
