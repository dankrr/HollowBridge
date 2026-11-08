"""Read-only catalog assembled from local Steam manifests and Hollow registrations."""
import re
from pathlib import Path
from .manifest import parse_vdf


def library_paths(home=None):
    home=Path.home() if home is None else Path(home)
    steamapps=home/'.local/share/Steam/steamapps'
    paths=[steamapps]
    try:
        folders=parse_vdf((steamapps/'libraryfolders.vdf').read_text())['libraryfolders']
        for value in folders.values():
            if isinstance(value,dict) and isinstance(value.get('path'),str):
                p=Path(value['path'])/'steamapps'
                if p not in paths: paths.append(p)
    except (OSError,ValueError,KeyError,TypeError): pass
    return paths


def games(home=None):
    home=Path.home() if home is None else Path(home)
    result={}
    try: config=(home/'.config/SLSsteam/config.yaml').read_text()
    except OSError: config=''
    for section in ('AdditionalApps','AdditionalPackageApps'):
        m=re.search(r'^'+section+r':[^\n]*\n(.*?)(?=^[A-Za-z_][A-Za-z_0-9]*:|\Z)',config,re.M|re.S)
        for line in (m.group(1).splitlines() if m else []):
            entry=re.match(r'^\s*-\s*([1-9]\d{0,9})\s*(?:#\s*(.*))?$',line)
            if not entry: continue
            appid=int(entry.group(1))
            if appid>4294967295: continue
            title=(entry.group(2) or '').strip()[:200]
            item=result.setdefault(appid,{'appid':appid,'name':title or str(appid),'hollow_registered':False,'installed':False})
            if title and item['name']==str(appid):item['name']=title
            item['hollow_registered']=True
    for folder in library_paths(home):
        try: paths=list(folder.glob('appmanifest_*.acf'))
        except OSError: continue
        for p in paths:
            try:
                if p.stat().st_size>4_000_000:continue
                data=parse_vdf(p.read_text())['AppState']; appid=int(data['appid'])
                if not 1<=appid<=4294967295:continue
                name=str(data.get('name') or appid)[:200]
                installed=int(data.get('StateFlags','0'))==4 and int(data.get('SizeOnDisk','0'))>0 and bool(data.get('InstalledDepots'))
                item=result.setdefault(appid,{'appid':appid,'name':name,'hollow_registered':False,'installed':False})
                if item['name']==str(appid):item['name']=name
                item['installed']=item['installed'] or installed
            except (OSError,ValueError,KeyError,TypeError):continue
    return sorted(result.values(),key=lambda x:(x['name'].casefold(),x['appid']))
