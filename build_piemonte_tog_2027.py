#!/usr/bin/env python3
"""Build an independent 2027 TEST rail ICS from monthly train-search JSON reports.
Inputs: tog_2027_*.json, each with results in the v4 schema. No network access.
Does not generate train routes: upstream reports must contain actual route options.
"""
import json, os, re, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROME=ZoneInfo('Europe/Rome')
INPUT_DIR=Path(os.getenv('RAIL_REPORT_DIR', '.'))
CANDIDATE=Path('Piemonte_tog_2027_candidate.ics')
PUBLISHED=Path('Piemonte_tog_2027.ics')
REPORT=Path('tog_2027_validation.json')
DESTS=('NIZZA MONFERRATO','ASTI','ALESSANDRIA')

def clock(s):
    m=re.fullmatch(r'(\d{1,2}):(\d{2})',str(s))
    if not m or int(m[1])>47 or int(m[2])>59: raise ValueError('invalid time '+str(s))
    return int(m[1])*60+int(m[2])

def escape(s):
    return str(s).replace('\\','\\\\').replace(';','\\;').replace(',','\\,').replace('\n','\\n')

def fold(s):
    chunks=[]; buf=''; limit=75
    for ch in s:
        if len((buf+ch).encode('utf-8'))>limit:
            chunks.append(buf);buf=ch;limit=74
        else: buf+=ch
    return '\r\n '.join(chunks+[buf])

def validate(option, ready):
    legs=option.get('legs',[])
    if not 1<=len(legs)<=5:return None,'leg count'
    parsed=[]
    try:
        for leg in legs:
            a,b=clock(leg['departure']),clock(leg['arrival'])
            if b<=a or b-a>480:return None,'leg duration / overnight leg unsupported'
            parsed.append((a,b,leg))
        first=ready.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(minutes=parsed[0][0])
        if first<ready or first-ready>timedelta(hours=6):return None,'first departure not reachable'
        for (_,end,prev),(start,_,nxt) in zip(parsed,parsed[1:]):
            if prev['to'].strip().upper()!=nxt['from'].strip().upper():return None,'station change'
            if not 10<=start-end<=45:return None,'transfer outside 10-45 min'
        if parsed[-1][1]-parsed[0][0]>720:return None,'journey too long'
        return parsed,None
    except (KeyError,ValueError,TypeError) as exc:return None,str(exc)

def event(row,arrival,dest,legs,label,stamp):
    midnight=arrival.replace(hour=0,minute=0,second=0,microsecond=0)
    dep=midnight+timedelta(minutes=legs[0][0]);end=midnight+timedelta(minutes=legs[-1][1])
    # UID is stable across reruns; separate recommendation and alternative identities.
    uid=f"rail-2027-{row['flight']}-{arrival:%Y%m%dT%H%M}-{label.lower()}@hfedder-piemonte"
    detail=' | '.join(f"{l['from']} {l['departure']} -> {l['to']} {l['arrival']} (tog {l.get('train','?')})" for _,_,l in legs)
    return ['BEGIN:VEVENT','UID:'+uid,'DTSTAMP:'+stamp,
      'DTSTART:'+dep.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
      'DTEND:'+end.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
      'SUMMARY:'+escape(f'TOG {label} (TEST) {legs[0][2]["from"]} -> {dest}'),
      'LOCATION:'+escape(legs[0][2]['from']),
      'DESCRIPTION:'+escape(f"TEST, IKKE VERIFICERET. Efter {row['flight']} til {row['airport']} kl. {arrival:%H:%M}. {detail}. Lufthavnstransport er estimeret; kontroller alle afgange."),
      'STATUS:TENTATIVE','TRANSP:TRANSPARENT','END:VEVENT']

def main():
    paths=sorted(INPUT_DIR.glob('tog_2027_*.json'))
    paths=[p for p in paths if p.name!='tog_2027_validation.json']
    report={'year':2027,'files':[],'input_flights':0,'excluded_outside_06_18':0,'without_valid_option':0,'recommended_events':0,'alternative_events':0,'destinations_recommended':{},'rejections':[],'published':False,'status':'test_only_not_verified'}
    rows=[]; periods=set()
    for p in paths:
        data=json.loads(p.read_text(encoding='utf-8'))
        period=data.get('period')
        if not isinstance(period,str) or not re.fullmatch(r'2027-(0[1-9]|1[0-2])',period):continue
        if period in periods:raise ValueError('Duplicate month '+period)
        periods.add(period)
        if data.get('status')!='test_only_not_verified':raise ValueError('Unexpected status in '+p.name)
        report['files'].append(p.name)
        rows.extend(data.get('results',[]))
    report['months_present']=sorted(periods)
    report['months_missing']=[f'2027-{m:02d}' for m in range(1,13) if f'2027-{m:02d}' not in periods]
    report['input_flights']=len(rows)
    if not rows:raise ValueError('No 2027 monthly train-search reports found; no calendar created')
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    lines=['BEGIN:VCALENDAR','VERSION:2.0','PRODID:-//hfedder//Piemonte Rail 2027 TEST//DA','CALSCALE:GREGORIAN','METHOD:PUBLISH','X-WR-CALNAME:Piemonte Tog - TEST 2027']
    seen=set()
    for row in rows:
        arrival=datetime.fromisoformat(row['flight_arrival_local']).astimezone(ROME)
        key=(row['flight'],arrival.isoformat(),row['airport'])
        if key in seen:continue
        seen.add(key)
        if arrival.year!=2027 or not 360<=arrival.hour*60+arrival.minute<=1080:
            report['excluded_outside_06_18']+=1;continue
        ready=datetime.fromisoformat(row['assumed_ready_at_station']).astimezone(ROME)
        best={}
        for dest in DESTS:
            for option in row.get('options',{}).get(dest,[]):
                legs,error=validate(option,ready)
                if error:
                    report['rejections'].append({'flight':row['flight'],'date':arrival.date().isoformat(),'destination':dest,'reason':error})
                elif dest not in best or (legs[-1][1],len(legs))<(best[dest][-1][1],len(best[dest])):best[dest]=legs
        if not best:report['without_valid_option']+=1;continue
        fastest=min(best,key=lambda d:(best[d][-1][1],len(best[d])))
        recommended='NIZZA MONFERRATO' if 'NIZZA MONFERRATO' in best and best['NIZZA MONFERRATO'][-1][1]<=best[fastest][-1][1]+30 else fastest
        other=[d for d in best if d!=recommended]
        alternative=min(other,key=lambda d:(best[d][-1][1],len(best[d]))) if other else None
        for dest,label in ((recommended,'ANBEFALET'),(alternative,'ALTERNATIV')):
            if dest is None:continue
            lines.extend(event(row,arrival,dest,best[dest],label,stamp))
            field='recommended_events' if label=='ANBEFALET' else 'alternative_events'
            report[field]+=1
            if label=='ANBEFALET':report['destinations_recommended'][dest]=report['destinations_recommended'].get(dest,0)+1
    report['status']='incomplete_month_coverage' if report['months_missing'] else 'test_only_not_verified'
    REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    if not report['recommended_events']:
        CANDIDATE.unlink(missing_ok=True);raise ValueError('No usable journeys')
    lines.append('END:VCALENDAR')
    CANDIDATE.write_bytes(('\r\n'.join(fold(line) for line in lines)+'\r\n').encode('utf-8'))
    # Publishing is deliberately blocked until 12 monthly datasets exist.
    if os.getenv('PUBLISH_RAIL','false').lower()=='true':
        if report['months_missing']:raise ValueError('Refusing to publish incomplete year')
        PUBLISHED.write_bytes(CANDIDATE.read_bytes());report['published']=True
        REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='rejections'},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
