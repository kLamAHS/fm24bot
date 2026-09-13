"""Compare live memory reads with independently transcribed UI observations."""
from datetime import datetime,timezone
import json
from pathlib import Path
from .players import decode_player
from .club import CurrentClub
from .date_validation import validate_dates

def validate_live(db,research_dir):
    research=Path(research_dir)
    expected=json.loads((research/'ui-observations.json').read_text(encoding='utf-8'))
    ids={db.fm.read_uint32(p+12):p for p in db.person_pointers()}
    results=[]
    for observed in expected['players']:
        actual=decode_player(db,ids[observed['id']],True)
        p=actual['player']; checks={'id':p['id']==observed['id'],'name':p['name']==observed['name']}
        checks.update({key:p['attributes'][key]==val for key,val in observed['attributes'].items()})
        results.append({'id':p['id'],'name':p['name'],'checks':checks,'passed':all(checks.values()),'memory':actual})
    context=CurrentClub(db).resolve(); squad=context.squad()
    expected_names=set(json.loads((research/'ui-squad-names.json').read_text(encoding='utf-8')))
    actual_names={p.name for p in squad}
    squad_check={'count':len(squad),'expected_count':len(expected_names),'missing':sorted(expected_names-actual_names),'extra':sorted(actual_names-expected_names),'passed':len(squad)==len(expected_names) and actual_names==expected_names}
    readiness=[]
    for observed in json.loads((research/'ui-readiness-observations.json').read_text(encoding='utf-8'))['players']:
        actual=decode_player(db,ids[observed['id']],True)
        checks={k:actual['evidence'][k]==observed[k] for k in ('condition_raw','match_sharpness_raw','fatigue_raw')}
        checks.update({key:actual['player']['position_ratings'][key]==value for key,value in observed['position_ratings'].items()})
        readiness.append({'id':observed['id'],'checks':checks,'passed':all(checks.values()),'memory':actual})
    expected_positions=json.loads((research/'ui-squad-positions.json').read_text(encoding='utf-8'))['positions_by_id']
    by_id={p.id:p for p in squad}
    positions=[{'id':int(uid),'expected':value,'actual':by_id[int(uid)].positions,'passed':set(value)==set(by_id[int(uid)].positions)} for uid,value in expected_positions.items()]
    dates=validate_dates(db,research)
    passed=all(r['passed'] for r in results+readiness+positions) and squad_check['passed'] and dates['passed']
    return {'captured_at':datetime.now(timezone.utc).isoformat(),'pid':db.fm.pid,'resolution':db.info(),'players':results,'squad':squad_check,'readiness':readiness,'squad_positions':positions,'dates':dates,'context':context.evidence(),'passed':passed}
