"""Conservative recovery after a native Steam remote-install attempt."""
import fcntl
import os
import stat
import time
from pathlib import Path
from .catalog import library_paths
from .manifest import inspect_manifest, parse_vdf


def find_manifest(appid):
    found=[]
    for folder in library_paths():
        path=folder/f'appmanifest_{appid}.acf'
        if path.exists():
            info=inspect_manifest(path,appid)
            if info['status']=='installed_with_content':return path,info
            found.append((path,info))
    if len(found)==1:return found[0]
    if len(found)>1:return None,{'status':'unreadable'}
    return None,None


def library_index(manifest):
    steamapps=Path.home()/'.local/share/Steam/steamapps'
    if manifest.parent.resolve()==steamapps.resolve():return 0
    try:
        folders=parse_vdf((steamapps/'libraryfolders.vdf').read_text())['libraryfolders']
        for index,folder in folders.items():
            if index.isdigit() and isinstance(folder,dict) and isinstance(folder.get('path'),str):
                if (Path(folder['path'])/'steamapps').resolve()==manifest.parent.resolve():return int(index)
    except (OSError,ValueError,KeyError,TypeError):pass
    return None


def retry_via_sls(appid,index,path='/tmp/SLSsteam.API',delay=1):
    """Issue one command only if the existing SLSsteam command file is idle.

    SLSsteam's file protocol has no transactional acknowledgement and other
    writers may not honor advisory locks; callers must treat exceptions as an
    ambiguous outcome and never replay automatically.
    """
    if type(appid) is not int or not 0<appid<=0xffffffff or type(index) is not int or not 0<=index<=1000:
        raise ValueError('invalid SLSsteam request')
    # A writable close itself triggers SLSsteam. Inspect with a read-only
    # descriptor first so a known-busy file is never accidentally dispatched.
    guard=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    command=f'install|{appid}|{index}\n'.encode()
    try:
        fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
        st=os.fstat(guard)
        if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid() or st.st_size!=0:
            raise RuntimeError('SLSsteam command file is busy or unsafe')
        fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW)
        try:
            current=os.fstat(fd)
            if (current.st_dev,current.st_ino,current.st_size)!=(st.st_dev,st.st_ino,0):
                raise RuntimeError('SLSsteam command file changed')
            if os.write(fd,command)!=len(command):raise RuntimeError('partial SLSsteam command')
        finally:os.close(fd) # CLOSE_WRITE triggers SLSsteam.
    finally:os.close(guard)
    time.sleep(delay)
    # Never remove a different writer's command. Closing the empty file causes
    # a harmless empty notification to the existing SLSsteam API.
    guard=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
        st=os.fstat(guard)
        if stat.S_ISREG(st.st_mode) and st.st_uid==os.getuid() and st.st_size==len(command):
            if os.read(guard,len(command)+1)==command:
                fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW)
                try:
                    current=os.fstat(fd)
                    if (current.st_dev,current.st_ino,current.st_size)==(st.st_dev,st.st_ino,st.st_size) and os.read(fd,len(command)+1)==command:
                        os.ftruncate(fd,0)
                finally:os.close(fd)
    finally:os.close(guard)


def recover(appid,source_hollow,ops,registered,ready,hollow_kind,wait=90,sleep=time.sleep,clock=time.monotonic):
    """Never prepare on account-side positive, unknown provenance, or active content."""
    if not ready():return 'failed','Steam is unavailable.'
    if ops.store_signal(appid)=='store_reported':return 'ignored','Steam Store reports an account-side license; native Steam remains in control.'
    if not (source_hollow or registered(appid)):return 'ignored','No existing Hollow registration is known; native Steam remains in control.'
    deadline=clock()+wait
    empty=None
    stable=0
    while clock()<deadline:
        if not ready():return 'unknown','Steam stopped during the native request.'
        path,info=find_manifest(appid)
        if info and info['status']=='installed_with_content':return 'ignored','Steam installed content natively.'
        if info and info['status']=='empty_install':
            if path==empty:stable+=1
            else:empty,stable=path,1
            if stable>=3:break
        else:empty,stable=None,0
        sleep(1)
    if stable<3:return 'unknown','No stable empty native install was observed; no Hollow action was taken.'
    index=library_index(empty)
    if index is None:return 'unknown','The Steam library index could not be determined.'
    prep=ops.prepare(appid,hollow_kind)
    if prep.get('result')!='prepared':return 'unknown','Hollow preparation did not complete; inspect its local logs.'
    # A real native installation may have begun while preparation ran.
    path,info=find_manifest(appid)
    if not info or info['status']=='installed_with_content':return 'ignored','Steam already installed content or the manifest disappeared.'
    if info.get('status')=='unreadable':return 'unknown','Steam manifest is unreadable; no second install was requested.'
    flags=info.get('state_flags',0)
    if flags & (0x200 | 0x800 | 0x2000 | 0x8000):
        return 'unknown','Steam is paused, running, uninstalling, or moving this game; no second request was made.'
    if flags & (0x100 | 0x400) or info.get('size_on_disk',0)>0 or info.get('bytes_downloaded',0)>0 or info.get('installed_depot_count',0)>0:
        return 'observing','Steam is already processing content; no second request was made.'
    retry_via_sls(appid,index)
    return 'observing','Hollow prepared the game and one SLSsteam install command was submitted; delivery is unconfirmed.'
