"""Owned human inbox records; no UI interaction and no process-wide scans."""
import struct
from .process import MemoryReadError
from .rtti import require_base_type,type_info
from .dates import decode_game_date,decode_game_time
from .players import identity,name_entry
from structures.inbox import Inbox,InboxMessage

def message_time(date_raw,minute_adjustment):
    if not 0<=minute_adjustment<=14: raise MemoryReadError('Invalid inbox minute adjustment')
    clock=decode_game_time(date_raw)
    if clock is None:return None
    hour,minute=map(int,clock.split(':'));minutes=hour*60+minute-minute_adjustment
    # Both Feb 1 midnight messages display 00:00 despite adjustments of 11/14.
    minutes=max(0,minutes)
    return f'{minutes//60:02}:{minutes%60:02}'

def read_inbox(context):
    context.check();db=context.db;fm=db.fm
    known=set(db.person_pointers());today=db.current_date();clock=db.dates.read_raw();guards=[]
    def observed(address,size):
        raw=fm.read_bytes(address,size);guards.append((address,raw));return raw
    holder=struct.unpack('<Q',observed(context.staff+0x338,8))[0]
    begin,end=struct.unpack('<QQ',observed(holder,16))
    if begin==end==0:data=b''
    else:
        if not 0x10000<=begin<=end<0x7fffffff0000 or begin%8 or (end-begin)%8 or end-begin>8*10000:
            raise MemoryReadError('Invalid human inbox vector')
        data=observed(begin,end-begin) if end!=begin else b''
    pointers=[p for (p,) in struct.iter_unpack('<Q',data)]
    if len(pointers)!=len(set(pointers)):raise MemoryReadError('Duplicate inbox item pointer')
    messages=[];ids=set()
    for ptr in pointers:
        ti=require_base_type(db,ptr,'.?AVNEWS_ITEM@db@@')
        raw=observed(ptr,0xb8);uid=struct.unpack_from('<I',raw,0xa8)[0]
        if uid==0 or uid in ids:raise MemoryReadError('Invalid or duplicate inbox message ID')
        ids.add(uid);sent=decode_game_date(raw[0xa0:0xa4])
        if sent>today:raise MemoryReadError('Inbox item is dated after the game date')
        sender=struct.unpack_from('<Q',raw,0x90)[0];sender_id=sender_name=None
        if sender:
            if sender not in known:raise MemoryReadError('Inbox sender outside the Person registry')
            sti=type_info(db,sender)
            if (sti['name'],sti['offset'])==('.?AVSUPPORT_STAFF@db@@',0x88):
                # Support staff have their UI names in their complete object.
                # Adjacent generic names must not be substituted for these.
                sender_id=struct.unpack('<I',observed(sender+0xc,4))[0]
                first,last=struct.unpack('<QQ',observed(sender-0x88+0x30,16))
                sender_name=(name_entry(fm,first)+' '+name_entry(fm,last)).strip()
                if not sender_name or not 0<sender_id<0x80000000:raise MemoryReadError('Invalid support staff sender')
            elif (sti['name'],sti['offset']) in {('.?AVACTUAL_NON_PLAYER@db@@',0xf8),('.?AVHUMAN_NON_PLAYER@db@@',0x450),('.?AVACTUAL_PLAYER@db@@',0x278)}:
                observed(sender,0x78);sender_id,sender_name,*_=identity(fm,sender)
            else:raise MemoryReadError('Unsupported inbox sender class')
        name=ti['name']
        if not name.startswith('.?AV') or not name.endswith('@@'):raise MemoryReadError('Unsupported news type name')
        kind=name[4:-2].removesuffix('@db').lower()
        messages.append(InboxMessage(uid,sent.isoformat(),message_time(raw[0xa0:0xa4],raw[0xb3]),not bool(raw[0xb0]&1),kind,sender_id,sender_name))
    for address,raw in guards:
        if fm.read_bytes(address,len(raw))!=raw:raise MemoryReadError('Inbox changed during observation')
    if db.dates.read_raw()!=clock or set(db.person_pointers())!=known:raise MemoryReadError('Inbox registry or game time changed')
    context.check()
    return Inbox(messages,sum(m.unread for m in messages))
