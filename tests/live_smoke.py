"""Opt-in integration check: run `python -m tests.live_smoke` on the test save."""
import json
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from api.server import create_server

def main():
    server = create_server(0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f'http://127.0.0.1:{server.server_port}'
    responses = {}
    checks = {}
    def request(path, expected_status=200, method='GET'):
        try:
            response = urlopen(Request(base + path, method=method), timeout=60)
        except HTTPError as exc:
            response = exc
        with response:
            data = json.loads(response.read())
            responses[method+' '+path] = {'status':response.status, 'body':data}
            checks[method+' '+path] = response.status == expected_status
            return data
    try:
        status = request('/status')
        checks['connected'] = status.get('connected') is True
        expected = json.loads(Path('research/ui-observations.json').read_text(encoding='utf-8'))
        readiness = {p['id']:p for p in json.loads(Path('research/ui-readiness-observations.json').read_text())['players']}
        for observed in expected['players']:
            player = request('/players/'+str(observed['id']))['data']
            checks[str(observed['id'])+' identity'] = player['id']==observed['id'] and player['name']==observed['name']
            checks[str(observed['id'])+' attributes'] = all(player['attributes'][k]==v for k,v in observed['attributes'].items())
            ready=readiness[observed['id']]
            current=player['readiness']['status']=='current'
            checks[str(observed['id'])+' condition']=player['condition']==(ready['condition_raw']/100 if current else None)
            checks[str(observed['id'])+' sharpness']=player['match_sharpness']==(ready['match_sharpness_raw']/100 if current else None)
            checks[str(observed['id'])+' positions']=player['position_ratings']==ready['position_ratings']
        manager = request('/manager')['data']
        club = request('/club')['data']
        squad = request('/squad')['data']
        names = json.loads(Path('research/ui-squad-names.json').read_text(encoding='utf-8'))
        checks['manager'] = manager == {'id':2002077023, 'name':'Liam Boyd'}
        checks['club'] = club['id']==742 and club['name']=='Wycombe Wanderers'
        checks['squad'] = len(squad)==31 and {p['name'] for p in squad}==set(names)
        checks['club squad'] = club['squad']==squad
        game=request('/game')['data']
        dates=json.loads(Path('research/ui-date-observations.json').read_text(encoding='utf-8'))
        checks['game date']=game['date']==dates['game_date']==status['game_date']
        for observed in dates['players']:
            p=request('/players/'+str(observed['id']))['data']
            for field in ('date_of_birth','age'):
                checks[str(observed['id'])+' '+field]=p[field]==observed[field]
            checks[str(observed['id'])+' age reference']=p['age_as_of']==game['date']
        ages=json.loads(Path('research/ui-squad-ages.json').read_text(encoding='utf-8'))['ages_by_name']
        checks['all squad ages']={p['name']:p['age'] for p in squad}==ages and all(p['age_as_of']==game['date'] for p in squad)
        morale=json.loads(Path('research/ui-morale-observations.json').read_text(encoding='utf-8'))['morale_by_name']
        for p in squad:
            checks[str(p['id'])+' morale']=p['morale']==morale[p['name']]
        checks['morale capability']='morale' in status['capabilities'] and 'morale' not in status['unresolved']
        # These Gretna values matched across the two saved snapshots. English
        # labels were independently visible in the February 7 UI.
        gretna=json.loads(Path('research/ui-morale-gretna-observations.json').read_text(encoding='utf-8'))['morale_by_name']
        captured=json.loads(Path('research/morale-gretna-capture-earlier.json').read_text(encoding='utf-8'))['players']
        for observed in captured:
            p=request('/players/'+str(observed['id']))['data']
            checks[str(observed['id'])+' Gretna morale']=p['id']==observed['id'] and p['name']==observed['name'] and p['morale']==gretna[p['name']] and p['morale_rating']==bytes.fromhex(observed['hex'])[0x25F]
        request('/players/0',404)
        request('/players/bad',400)
        request('/match',501)
        request('/squad',405,'POST')
        request('/missing',404)
        # Use the same live session to exercise the address-free Python lookup.
        bridge = server.state_service.bridge
        checks['python lookup'] = asdict(bridge.player(29232937)) == responses['GET /players/29232937']['body']['data']
        checks['loopback'] = server.server_address[0]=='127.0.0.1'
        checks['consistent connection identity'] = all(item['body'].get('session_id')==status['session_id'] for item in responses.values() if item['status']==200 and 'data' in item['body'])
        report = {'captured_at':datetime.now(timezone.utc).isoformat(), 'pid':status.get('pid'),
                  'checks':checks, 'responses':responses, 'passed':all(checks.values())}
        Path('research/api-observation-validation.json').write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
        print(json.dumps({'passed':report['passed'], 'checks':len(checks), 'pid':report['pid']}))
        if not report['passed']: raise SystemExit(1)
    finally:
        server.shutdown(); server.server_close(); worker.join()
        server.state_service.close()

if __name__=='__main__': main()
