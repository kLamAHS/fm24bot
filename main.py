import argparse
import json
import logging
from dataclasses import asdict
from pathlib import Path
from bridge import FMProcess
from bridge.database import Database
from bridge.players import find_players

def main():
    parser=argparse.ArgumentParser(description='Read-only FM24 observation bridge')
    parser.add_argument('command',choices=['status','resolve','find'])
    parser.add_argument('query',nargs='?',default='')
    parser.add_argument('--pid',type=int)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(levelname)s %(message)s')
    with FMProcess(args.pid) as fm:
        if args.command=='status':
            result={'pid':fm.pid,'access':'read_only','architecture':'x64','alive':fm.alive(),'modules':[asdict(m) for m in fm.modules()]}
        else:
            db=Database(fm).resolve()
            result={'pid':fm.pid,'resolution':db.info()}
            if args.command=='find':
                if not args.query: parser.error('find requires an ID or name')
                result.update(find_players(db,args.query))
        data=json.dumps(result,indent=2,ensure_ascii=False)
        if args.output: args.output.write_text(data,encoding='utf-8')
        print(data)

if __name__=='__main__': main()
