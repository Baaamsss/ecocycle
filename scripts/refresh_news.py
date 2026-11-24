#!/usr/bin/env python3
"""EcoCycle headline index: direct publisher RSS/Atom with link checks.

- Respects feed availability; never invents article/date metadata.
- Preserves previously collected entries across failed updates.
- Publishes titles and links only; full articles stay at the publishers.
- Google News discovery is NOT published as an unverified article link.
"""
import concurrent.futures
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse, quote, urlencode, urljoin
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import json
import re
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
MAX_BYTES = 1_800_000
HEADERS = {'User-Agent': 'EcoCycle-edu-headline-reader/1.0 (+academic noncommercial); Mozilla/5.0', 'Accept':'application/rss+xml, application/atom+xml, application/xml, text/xml, text/html;q=0.5'}
GOOGLE = 'https://news.google.com/rss/search?'

class FeedLinks(HTMLParser):
    def __init__(self):
        super().__init__(); self.links=[]
    def handle_starttag(self, tag, attrs):
        if tag.lower() != 'link': return
        d=dict(attrs)
        if 'alternate' in d.get('rel','').lower() and any(x in d.get('type','').lower() for x in ('rss','atom','xml')) and d.get('href'):
            self.links.append(d['href'])

def get(url):
    parsed=urlparse(url)
    if parsed.scheme!='https': raise ValueError('HTTPS required')
    req=Request(url,headers=HEADERS)
    with urlopen(req,timeout=13) as resp:
        final=urlparse(resp.geturl())
        if final.scheme!='https': raise ValueError('HTTPS redirect required')
        raw=resp.read(MAX_BYTES+1)
        if len(raw)>MAX_BYTES: raise ValueError('Too large')
        return raw,resp.geturl(),resp.headers.get('Content-Type','')

def host_ok(url,domain):
    try:
        u=urlparse(url)
        return u.scheme=='https' and (u.hostname==domain or u.hostname.endswith('.'+domain))
    except (ValueError,AttributeError):return False

def article_link_alive(url, domain):
    """Best-effort check of a direct publisher article URL, not Google News.

    A 200 from a publisher is a point-in-time check, never a lifetime guarantee.
    HEAD is cheap; retry GET for publishers rejecting HEAD requests.
    """
    if not host_ok(url, domain):
        return False
    for method in ('HEAD', 'GET'):
        try:
            h=dict(HEADERS)
            if method=='GET': h['Range']='bytes=0-1023'
            req=Request(url, headers=h, method=method)
            with urlopen(req,timeout=8) as resp:
                if not host_ok(resp.geturl(),domain):return False
                return 200 <= resp.status < 400
        except HTTPError as e:
            if method=='HEAD' and e.code in (400,403,405,406,501):continue
            return False
        except (URLError, TimeoutError, ValueError, OSError):
            if method=='HEAD':continue
            return False
    return False

def verified_entries(items, src):
    # Bound network work: article checks are parallel but limited per publisher.
    candidates=items[:20]
    results=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        checks=list(pool.map(lambda a: article_link_alive(a['url'], src['domain']), candidates))
    checked=datetime.now(timezone.utc).isoformat(timespec='seconds')
    for item, ok in zip(candidates,checks):
        if ok:
            item={**item, 'verified_at':checked}
            results.append(item)
    return results

def txt(s):
    return re.sub(r'\s+',' ',unescape(re.sub('<[^>]+>',' ',s or ''))).strip()[:300]

def normalized_date(s):
    if not s:return None
    try:
        dt=parsedate_to_datetime(s)
    except (ValueError,TypeError,IndexError):
        try:dt=datetime.fromisoformat(s.strip().replace('Z','+00:00'))
        except(ValueError,TypeError):return None
    if dt.tzinfo is None:dt=dt.replace(tzinfo=timezone.utc)
    now=datetime.now(timezone.utc)
    if dt>now+timedelta(days=2) or dt.year<2010:return None
    return dt.astimezone(timezone.utc).date().isoformat()

def get_text(node,parts):
    for p in parts:
        v=node.findtext(p)
        if v and v.strip():return v.strip()
    return ''

def parse_feed(raw,source,via='RSS penerbit'):
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():raise ValueError('DTD not allowed')
    root=ET.fromstring(raw)
    entries=root.findall('.//channel/item')
    atom=False
    if not entries:
        entries=root.findall('.//{http://www.w3.org/2005/Atom}entry'); atom=True
    if not entries: raise ValueError('RSS/Atom entries not found')
    found=[];seen=set()
    for e in entries[:70]:
        if atom:
            title=get_text(e,['{http://www.w3.org/2005/Atom}title'])
            link=''
            for a in e.findall('{http://www.w3.org/2005/Atom}link'):
                if a.attrib.get('rel','alternate') in ('alternate','') and a.attrib.get('href'):
                    link=a.attrib['href'];break
            date=normalized_date(get_text(e,['{http://www.w3.org/2005/Atom}published','{http://www.w3.org/2005/Atom}updated']))
        else:
            title=get_text(e,['title'])
            link=get_text(e,['link'])
            date=normalized_date(get_text(e,['pubDate','{http://purl.org/dc/elements/1.1/}date']))
        title=txt(title); link=link.strip()
        if not title or not date or link in seen:continue
        if via=='Google News RSS':
            if not host_ok(link,'news.google.com'):continue
        elif not host_ok(link,source['domain']):continue
        seen.add(link)
        found.append({'title':title,'url':link,'date':date,'source':source['name'],'source_id':source['id'],'via':via})
    return found

def discover(src):
    try:
        html,url,_=get(src['news'])
        parser=FeedLinks();parser.feed(html[:300_000].decode('utf-8','replace'))
        return [urljoin(url,x) for x in parser.links if host_ok(urljoin(url,x),src['domain'])][:3]
    except Exception:return []

def google_feed(src):
    # A site-limited Google News search RSS is only a discovery fallback.
    q='site:'+src['domain']+' when:90d'
    return GOOGLE+urlencode({'q':q,'hl':'id','gl':'ID','ceid':'ID:id'})

def fetch_source(src):
    # Strict publishing mode: RSS directly from publishers only.
    # Google News search results are deliberately NOT published as article links.
    tried=set(); failures=[]
    feeds=list(src.get('feeds',[])) + discover(src)
    for url in feeds[:5]:
        if url in tried or not host_ok(url,src['domain']):continue
        tried.add(url)
        try:
            data,final,_=get(url)
            if not host_ok(final,src['domain']):raise ValueError('redirected off publisher')
            items=parse_feed(data,src)
            confirmed=verified_entries(items,src)
            if confirmed:
                return {'id':src['id'],'mode':'verified_publisher_rss','items':confirmed,
                        'label':'RSS penerbit; URL artikel dicek','count':len(confirmed)}
            failures.append('RSS tersedia tetapi tidak ada tautan yang lolos pemeriksaan')
        except Exception as e:failures.append(str(e)[:90])
    return {'id':src['id'],'mode':'unavailable','items':[],
            'label':'Buka situs resmi penerbit','count':0,
            'reason':' | '.join(failures[-2:])[:180]}

def refresh():
    srcs=json.loads((ROOT/'sources.json').read_text(encoding='utf-8'))
    out=ROOT/'news.json'
    try:old=json.loads(out.read_text(encoding='utf-8'))
    except Exception:old={'items':[]}
    results=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures={pool.submit(fetch_source,s):s for s in srcs}
        for fut in concurrent.futures.as_completed(futures):
            try:r=fut.result()
            except Exception as e:r={'id':futures[fut]['id'],'mode':'unavailable','items':[],'label':'Gagal mengambil','reason':str(e)[:120]}
            results.append(r)
            print(r['id'],r['mode'],len(r.get('items',[])))
    # If absolutely every source is unavailable, retain the EXACT published dataset.
    # Errors remain visible in workflow logs, never in public EcoNews UI.
    successes=[r for r in results if r.get('items')]
    if not successes:
        print('WARNING: No source passed verification. Retaining last known good data; no invented articles.')
        return
    # Remove all Google News links from exported news.json, even if present in old data.
    # Retain last verified publisher links for up to 30 days when a source is temporarily down.
    src_domains={s['id']:s['domain'] for s in srcs}
    cutoff=datetime.now(timezone.utc)-timedelta(days=30)
    def safe_old(a):
        if not isinstance(a,dict) or not a.get('url') or not a.get('date'):return False
        domain=src_domains.get(a.get('source_id'))
        if not domain or not host_ok(a['url'],domain):return False
        verified=a.get('verified_at')
        if not verified:return False  # old RSS links were never checked
        try:
            dt=datetime.fromisoformat(verified.replace('Z','+00:00'))
            return dt.tzinfo is not None and dt>=cutoff
        except (ValueError,TypeError):return False
    allitems={}
    for a in ([a for a in old.get('items',[]) if safe_old(a)]+[a for r in results for a in r.get('items',[]) ]):
        if not isinstance(a,dict) or not a.get('date') or not a.get('source_id'):continue
        allitems[a['source_id']+'|'+a['url']]=a
    items=sorted(allitems.values(),key=lambda x:(x['date'],x['source']),reverse=True)[:300]
    statuses=[]
    for s in srcs:
        r=next((x for x in results if x['id']==s['id']),None) or {'mode':'unavailable','label':'Unknown'}
        statuses.append({'id':s['id'],'name':s['name'],'mode':r['mode'],'label':r.get('label',''),'count':len([x for x in items if x['source_id']==s['id']])})
    now=datetime.now(timezone.utc).isoformat(timespec='seconds')
    # checked_at means aggregator ran; it does NOT mean every publisher posted today.
    new={'checked_at':now,'updated':now if items!=old.get('items',[]) else old.get('updated'), 'items':items,'status':statuses,
         'note':'Artikel dari RSS penerbit, dengan pemeriksaan status tautan saat pengambilan. Pemeriksaan tidak menjamin tautan selamanya aktif; buka sumber resmi bila halaman berubah.'}
    out.write_text(json.dumps(new,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('Saved',len(items),'article links,',len(statuses),'sources')

if __name__=='__main__':refresh()
