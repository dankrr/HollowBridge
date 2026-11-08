-- HollowBridge agent launcher. Independent of RemotePlaySync.
-- This file is a candidate, not installed automatically. It adds no Steam hook.
-- Configuration: ~/.config/hollowbridge/native.conf, one KEY=value per line.
local home=os.getenv('HOME')
if not home or home=='' then return end
local file=io.open(home..'/.config/hollowbridge/native.conf','r')
if not file then if log and log.warn then log.warn('[HollowBridge] native config missing') end;return end
local cfg={}
for line in file:lines() do
    local key,value=line:match('^([A-Z_]+)=([^\r\n]+)$')
    if key and value then cfg[key]=value end
end
file:close()
local function tailscale_ip(value)
    if not value then return false end
    local a,b,c,d=value:match('^(%d+)%.(%d+)%.(%d+)%.(%d+)$')
    a,b,c,d=tonumber(a),tonumber(b),tonumber(c),tonumber(d)
    return a==100 and b and b>=64 and b<=127 and c and c<=255 and d and d<=255
end
local valid_device=cfg.DEVICE and cfg.DEVICE:match('^[%w_-]+$') and #cfg.DEVICE<=64
local valid_peer=cfg.PEER_CLIENT_ID and cfg.PEER_CLIENT_ID:match('^[1-9]%d*$') and #cfg.PEER_CLIENT_ID<=20
local observe_native=cfg.OBSERVE_NATIVE=='1'
local port=tonumber(cfg.PORT or '39482')
if not tailscale_ip(cfg.BIND) or not tailscale_ip(cfg.PEER) or not valid_device or (observe_native and not valid_peer)
    or (cfg.OBSERVE_NATIVE and cfg.OBSERVE_NATIVE~='0' and cfg.OBSERVE_NATIVE~='1')
    or (cfg.HOLLOW~='desktop' and cfg.HOLLOW~='decky') or not port or port%1~=0 or port<1 or port>65535 then
    if log and log.warn then log.warn('[HollowBridge] invalid native config; agent not started') end
    return
end
local f=io.open('/proc/self/stat','r');local raw=f and f:read('*l');if f then f:close() end
local pid=raw and raw:match('^(%d+)');local rest=raw and raw:match('^%d+ %b() (.*)$')
local start;local i=0
if rest then for value in rest:gmatch('%S+') do i=i+1;if i==20 then start=value;break end end end
if not pid or not start or not start:match('^%d+$') then return end
local function q(s)return "'"..tostring(s):gsub("'","'\\''").."'" end
local function exists(path)local h=io.open(path,'r');if h then h:close();return true end return false end
local project=home..'/.local/share/hollowbridge'
if not exists(project..'/hollowbridge/__main__.py') then
    if log and log.warn then log.warn('[HollowBridge] agent package missing') end;return
end
local clean='/usr/bin/env -u LD_AUDIT -u LD_PRELOAD -u LD_LIBRARY_PATH '
local args='/usr/bin/python3 -m hollowbridge serve --bind '..q(cfg.BIND)..' --peer '..q(cfg.PEER)
    ..' --device '..q(cfg.DEVICE)..' --hollow '..q(cfg.HOLLOW)..' --port '..q(port)
    ..' --observe-sync'
    ..' --steam-pid '..q(pid)..' --steam-start '..q(start)
if observe_native then args=args..' --observe-native --peer-client-id '..q(cfg.PEER_CLIENT_ID) end
-- Stock SLSsteam has no onTick supervisor. Use the already-running user
-- service manager for a transient unit; it adds no always-on supervisor.
-- The agent exits successfully when this Steam identity disappears, so the
-- unit is collected rather than restarted after Steam shuts down.
local unit='hollowbridge-steam.service'
local target=clean..'/usr/bin/systemd-run --user --collect --quiet --service-type=exec'
    ..' --unit='..q(unit)..' --property=Restart=on-failure --property=RestartSec=5s'
    ..' --working-directory='..q(project)..' -- '..clean..args
local host='/usr/bin/steam-runtime-launch-client'
if exists(host) then target=clean..q(host)..' --host -- '..target end
local cache=home..'/.cache/hollowbridge'
local cmd='(umask 077; mkdir -p '..q(cache)..'; cd '..q(project)..' && '..target..' </dev/null >>'..q(cache..'/agent.log')..' 2>&1) &'
local pidfile=home..'/.local/state/hollowbridge/native-agent.pid'
local function agent_alive()
    local h=io.open(pidfile,'r');local line=h and h:read('*l');if h then h:close() end
    local agentpid,expected
    if line then agentpid,expected=line:match('^(%d+) (%d+)$') end
    if not agentpid or not expected then return false end
    local p=io.open('/proc/'..agentpid..'/stat','r');local text=p and p:read('*l');if p then p:close() end
    local fields=text and text:match('^%d+ %b() (.*)$');local n=0
    if fields then for value in fields:gmatch('%S+') do n=n+1;if n==20 then return value==expected end end end
    return false
end
local function ensure_agent()
    if not agent_alive() then
        os.execute(cmd)
        if log and log.info then log.info('[HollowBridge] requested native agent startup') end
    end
end
ensure_agent()
