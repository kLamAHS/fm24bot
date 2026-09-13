"""Optional live comparisons against independently captured FM date/age UI."""
import json
from pathlib import Path
from .club import CurrentClub
from .players import decode_player
from .process import MemoryReadError

def validate_dates(db,research_dir,observations='ui-date-observations.json',squad_ages=True):
    research=Path(research_dir)
    expected=json.loads((research/observations).read_text(encoding='utf-8'))
    as_of=db.current_date()
    checks={'game_date':as_of.isoformat()==expected['game_date']}
    persons={db.fm.read_uint32(p+12):p for p in db.person_pointers()}
    rows=[]
    for observed in expected['players']:
        actual=decode_player(db,persons[observed['id']],True,as_of=as_of)
        p=actual['player']
        compared={k:p[k]==observed[k] for k in ['id','name','date_of_birth','age']}
        compared['age_as_of']=p['age_as_of']==expected['game_date']
        rows.append({'id':p['id'],'checks':compared,'passed':all(compared.values()),'memory':actual})
    squad_checks=[]
    if squad_ages:
        expected_squad=json.loads((research/'ui-squad-ages.json').read_text(encoding='utf-8'))
        squad=CurrentClub(db).resolve().squad()
        by_name={p.name:p for p in squad}
        checks['squad_names']=set(by_name)==set(expected_squad['ages_by_name']) and len(squad)==len(by_name)
        checks['squad_age_reference_date']=expected_squad['game_date']==as_of.isoformat()
        squad_checks=[{'name':name,'expected':age,'actual':by_name[name].age,'passed':by_name[name].age==age} for name,age in expected_squad['ages_by_name'].items()]
    if db.current_date()!=as_of: raise MemoryReadError('Game date changed during validation')
    return {'checks':checks,'players':rows,'squad_ages':squad_checks,'resolution':db.dates.evidence(),
            'passed':all(checks.values()) and all(row['passed'] for row in rows+squad_checks)}
