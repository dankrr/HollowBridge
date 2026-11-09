-- HollowBridge SLSsteam plugin.
-- Portable single-file plugin for Linux Desktop (Hollow Desktop) and Steam Deck (Hollow Decky).
-- Observes authenticated RemotePlaySync library snapshots in-process, filters out
-- genuinely owned/family-shared Steam games via CUser:isSubscribed(appId), and
-- prepares newly synchronized Hollow games locally without a network daemon or CDP.

local ENV = (getfenv and getfenv(1)) or _G
local H = ENV.HollowBridge or {}
ENV.HollowBridge = H

local WORKER_TIMEOUT_SEC = 210
local STEP_INTERVAL_SEC = 3

local PYTHON_WORKER_SOURCE = [=[#!/usr/bin/env python3
"""HollowBridge local one-shot worker for Hollow Desktop and Hollow Decky."""
import fcntl
import json
import logging
import os
import re
import stat
import sys
import time
import types
from pathlib import Path

_VDF_TOKEN = re.compile(r'\s*(?:(//[^\n]*(?:\n|$))|"((?:\\.|[^"\\])*)"|([{}]))')


def parse_vdf(text: str) -> dict:
    if len(text) > 4_000_000:
        raise ValueError("vdf too large")
    tokens = []
    pos = 0
    while pos < len(text):
        if not text[pos:].strip():
            break
        m = _VDF_TOKEN.match(text, pos)
        if not m:
            raise ValueError("invalid vdf")
        pos = m.end()
        if m.group(1) is not None:
            continue
        if m.group(2) is not None:
            tokens.append(("value", re.sub(r'\\([\\"])', r"\1", m.group(2))))
        else:
            tokens.append((m.group(3), m.group(3)))

    def block(i: int, nested: bool = False):
        result = {}
        while i < len(tokens):
            kind, key = tokens[i]
            if kind == "}":
                if not nested:
                    raise ValueError("unexpected closing brace")
                return result, i + 1
            if kind != "value" or i + 1 >= len(tokens):
                raise ValueError("invalid key")
            i += 1
            kind, value = tokens[i]
            if kind == "{":
                value, i = block(i + 1, True)
            elif kind == "value":
                i += 1
            else:
                raise ValueError("invalid value")
            if key not in result:
                result[key] = value
            elif isinstance(result[key], dict) and isinstance(value, dict):
                result[key].update(value)
        if nested:
            raise ValueError("missing closing brace")
        return result, i

    parsed, _ = block(0)
    return parsed


def steam_root() -> Path:
    override = os.environ.get("HOLLOWBRIDGE_STEAM_ROOT")
    if override:
        return Path(override)
    return Path.home() / ".local/share/Steam"


def sls_config_path() -> Path:
    override = os.environ.get("HOLLOWBRIDGE_SLS_CONFIG")
    if override:
        return Path(override)
    return Path.home() / ".config/SLSsteam/config.yaml"


def sls_api_path() -> Path:
    override = os.environ.get("HOLLOWBRIDGE_SLS_API")
    if override:
        return Path(override)
    return Path("/tmp/SLSsteam.API")


def library_folders() -> list[tuple[int, Path]]:
    root = steam_root()
    default_steamapps = root / "steamapps"
    folders: list[tuple[int, Path]] = []
    seen: set[Path] = set()
    vdf_path = default_steamapps / "libraryfolders.vdf"
    try:
        data = parse_vdf(vdf_path.read_text(encoding="utf-8", errors="replace"))
        lf = data.get("libraryfolders") or data.get("LibraryFolders") or {}
        if isinstance(lf, dict):
            for idx_str, entry in lf.items():
                if not str(idx_str).isdigit():
                    continue
                idx = int(idx_str)
                folder_path = None
                if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                    folder_path = Path(entry["path"]) / "steamapps"
                elif isinstance(entry, str):
                    folder_path = Path(entry) / "steamapps"
                if folder_path and folder_path.is_dir():
                    resolved = folder_path.resolve()
                    if resolved not in seen:
                        seen.add(resolved)
                        folders.append((idx, folder_path))
    except Exception:
        pass
    if default_steamapps.is_dir():
        resolved = default_steamapps.resolve()
        if resolved not in seen:
            folders.insert(0, (0, default_steamapps))
    return folders


def inspect_app_manifest(appid: int) -> tuple[str, int | None, Path | None]:
    found_empty = None
    for idx, steamapps in library_folders():
        manifest_path = steamapps / f"appmanifest_{appid}.acf"
        if not manifest_path.is_file():
            continue
        try:
            data = parse_vdf(manifest_path.read_text(encoding="utf-8", errors="replace"))
            app = data.get("AppState")
            if not isinstance(app, dict) or int(app.get("appid", 0)) != appid:
                return ("unreadable", idx, manifest_path)
            flags = int(app.get("StateFlags", 0))
            size = int(app.get("SizeOnDisk", 0))
            total = int(app.get("BytesToDownload", 0))
            done = int(app.get("BytesDownloaded", 0))
            depots = app.get("InstalledDepots", {})
            if not isinstance(depots, dict):
                return ("unreadable", idx, manifest_path)
            if (flags == 4 and size > 0 and bool(depots)) or size > 0 or done > 0 or total > 0:
                return ("installed_with_content", idx, manifest_path)
            if flags == 4 and size == 0 and total == 0 and done == 0 and not depots:
                found_empty = ("empty_install", idx, manifest_path)
            else:
                return ("pending_or_unknown", idx, manifest_path)
        except Exception:
            return ("unreadable", idx, manifest_path)
    if found_empty is not None:
        return found_empty
    return ("absent", None, None)


def is_in_additional_package_apps(appid: int) -> bool:
    cfg = sls_config_path()
    if not cfg.is_file():
        return False
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
        m = re.search(
            r"^AdditionalPackageApps:[^\n]*\n(.*?)(?=^[A-Za-z_][A-Za-z_0-9]*:|\Z)",
            text,
            re.M | re.S,
        )
        if not m:
            return False
        return bool(
            re.search(r"^\s*-\s*" + str(appid) + r"(?:\s|#|$)", m.group(1), re.M)
        )
    except Exception:
        return False


def find_ci_key(mapping: dict, target_lower: str):
    if not isinstance(mapping, dict):
        return None
    for k, v in mapping.items():
        if str(k).lower() == target_lower:
            return v
    return None


def needs_proton_compat(appid: int) -> bool:
    vdf_path = steam_root() / "config/config.vdf"
    if not vdf_path.is_file():
        return True
    try:
        data = parse_vdf(vdf_path.read_text(encoding="utf-8", errors="replace"))
        root = find_ci_key(data, "installconfigstore") or {}
        software = find_ci_key(root, "software") or {}
        valve = find_ci_key(software, "valve") or {}
        steam = find_ci_key(valve, "steam") or {}
        mapping = find_ci_key(steam, "compattoolmapping") or {}
        entry = mapping.get(str(appid))
        if not isinstance(entry, dict):
            return True
        name = str(find_ci_key(entry, "name") or "").strip().lower()
        prio_raw = find_ci_key(entry, "priority")
        try:
            prio = int(prio_raw) if prio_raw is not None else 0
        except ValueError:
            prio = 0
        is_native = (not name) or name == "native" or name.startswith("steamlinuxruntime")
        if is_native and prio >= 250:
            # Explicit high-priority native override chosen by the user.
            return False
        return is_native
    except Exception:
        return True


def send_sls_api_commands(commands: list[str], delay: float = 0.4) -> bool:
    if not commands:
        return True
    api = sls_api_path()
    if not api.exists():
        return False
    payload = ("".join(f"{c}\n" for c in commands)).encode("utf-8")
    try:
        guard = os.open(str(api), os.O_RDONLY | os.O_NOFOLLOW)
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            st = os.fstat(guard)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_size != 0:
                return False
            fd = os.open(str(api), os.O_RDWR | os.O_NOFOLLOW)
            try:
                cur = os.fstat(fd)
                if (cur.st_dev, cur.st_ino, cur.st_size) != (st.st_dev, st.st_ino, 0):
                    return False
                if os.write(fd, payload) != len(payload):
                    return False
            finally:
                os.close(fd)
        finally:
            os.close(guard)
        time.sleep(delay)
        guard = os.open(str(api), os.O_RDONLY | os.O_NOFOLLOW)
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            st = os.fstat(guard)
            if stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and st.st_size == len(payload):
                if os.read(guard, len(payload) + 1) == payload:
                    fd = os.open(str(api), os.O_RDWR | os.O_NOFOLLOW)
                    try:
                        cur = os.fstat(fd)
                        if (cur.st_dev, cur.st_ino, cur.st_size) == (st.st_dev, st.st_ino, st.st_size):
                            os.ftruncate(fd, 0)
                    finally:
                        os.close(fd)
        finally:
            os.close(guard)
        return True
    except Exception:
        return False


def setup_hollow_backend() -> str:
    home = Path.home()
    override_dir = os.environ.get("HOLLOWBRIDGE_BACKEND_DIR")
    override_kind = os.environ.get("HOLLOWBRIDGE_BACKEND_KIND")
    if override_dir:
        backend = Path(override_dir)
        kind = override_kind or ("decky" if "decky" in backend.name else "desktop")
    elif (home / ".local/share/hollow-desktop/current/backend/hollow/inject.py").is_file():
        backend = home / ".local/share/hollow-desktop/current/backend"
        kind = "desktop"
    elif (home / "homebrew/plugins/hollow-decky/hollow/inject.py").is_file():
        backend = home / "homebrew/plugins/hollow-decky"
        kind = "decky"
    else:
        raise RuntimeError("hollow_backend_not_found")

    if kind == "decky":
        homebrew = Path(os.environ.get("HOLLOWBRIDGE_HOMEBREW_DIR", str(home / "homebrew")))
        decky_mod = types.ModuleType("decky")
        decky_mod.DECKY_USER_HOME = str(home)
        decky_mod.DECKY_HOME = str(homebrew)
        decky_mod.DECKY_PLUGIN_DIR = str(backend)
        decky_mod.DECKY_PLUGIN_SETTINGS_DIR = str(homebrew / "settings/hollow-decky")
        decky_mod.DECKY_PLUGIN_RUNTIME_DIR = str(homebrew / "data/hollow-decky")
        decky_mod.DECKY_PLUGIN_LOG_DIR = str(homebrew / "logs/hollow-decky")
        decky_mod.DECKY_SETTINGS_DIR = str(homebrew / "settings")
        decky_mod.logger = logging.getLogger("hollow-decky")
        sys.modules["decky"] = decky_mod

    sys.path.insert(0, str(backend))
    return kind


def is_base_ready(appid: int, details: dict) -> bool:
    from hollow.manifests import depots_missing_manifests
    from sls.config.readers import get_configured_depot_keys
    from steam.dlc_labels import real_unkeyed_depots
    from steam.paths import depotcache_dir

    depots = details.get("depots") or {}
    ids = list(depots.keys())
    real = details.get("depot_has_manifests") or {}
    if details.get("error") or not ids or not any(real.get(d) for d in ids):
        return False
    cache = depotcache_dir()
    if not cache or not cache.is_dir():
        return False
    configured = (get_configured_depot_keys() or {}).get("keys", {})
    if not isinstance(configured, dict):
        return False
    if real_unkeyed_depots(details, set(configured), ids):
        return False
    if depots_missing_manifests(ids, details):
        return False
    return True


def run_seed() -> tuple[str, str]:
    setup_hollow_backend()
    from hollow.manifests import seed_manifests_from_cache

    res = seed_manifests_from_cache() or {}
    return ("seeded", f"seeded={res.get('seeded', 0)}")


def run_prepare(appid: int) -> tuple[str, str]:
    manifest_status, lib_idx, _ = inspect_app_manifest(appid)
    if manifest_status == "installed_with_content":
        return ("ignored", "already_installed")
    if manifest_status in ("pending_or_unknown", "unreadable"):
        return ("unknown", f"manifest_{manifest_status}")

    setup_hollow_backend()
    from hollow.inject import get_inject_status, inject_game_sync
    from hollow.manifests import seed_manifests_from_cache
    from sls.config.adder import add_to_sls
    from steam.appdetails import get_app_details

    try:
        seed_manifests_from_cache()
    except Exception:
        pass

    details = get_app_details(str(appid))
    if not isinstance(details, dict) or details.get("error"):
        return ("unknown", "metadata_unavailable")
    depots = details.get("depots")
    real = details.get("depot_has_manifests") or {}
    if not isinstance(depots, dict) or not depots or not any(real.get(d) for d in depots):
        return ("unknown", "no_real_depots")

    platforms = [
        str(d.get("platform") or "").lower()
        for d in depots.values()
        if isinstance(d, dict)
    ]
    if len(platforms) != len(depots):
        return ("unknown", "incomplete_depot_platforms")

    optional_missing: list[str] = []
    if not (is_base_ready(appid, details) and is_in_additional_package_apps(appid)):
        cfg_file = sls_config_path()
        pre_cfg = cfg_file.read_text(encoding="utf-8", errors="replace") if cfg_file.is_file() else None

        def rollback_cfg() -> None:
            if pre_cfg is not None and cfg_file.is_file():
                try:
                    tmp = cfg_file.with_name(cfg_file.name + ".hb.tmp")
                    tmp.write_text(pre_cfg, encoding="utf-8")
                    os.replace(tmp, cfg_file)
                except Exception:
                    pass

        r = inject_game_sync(str(appid)) or {}
        if not r.get("injected"):
            rollback_cfg()
            return ("unknown", "inject_rejected")
        poll_limit = int(os.environ.get("HOLLOWBRIDGE_POLL_SECONDS", "150"))
        done_status = None
        for _ in range(max(1, poll_limit)):
            s = get_inject_status(str(appid)) or {}
            if s.get("done"):
                done_status = s
                break
            time.sleep(float(os.environ.get("HOLLOWBRIDGE_POLL_INTERVAL", "1.0")))
        if done_status is None:
            rollback_cfg()
            return ("unknown", "inject_timeout")

        error = (
            done_status.get("error")
            or done_status.get("manifest_error")
            or done_status.get("depot_keys_error")
        )
        missing = done_status.get("keys_missing") or []
        base_ids = {str(x) for x in depots}
        if error:
            rollback_cfg()
            return ("unknown", "hollow_inject_error")
        if missing:
            missing_set = {str(x) for x in missing}
            if base_ids & missing_set:
                rollback_cfg()
                return ("unknown", "missing_base_depot_keys")
            optional_missing = sorted(missing_set)

        if not is_in_additional_package_apps(appid):
            try:
                add_to_sls(str(appid), str(details.get("title") or appid))
            except Exception:
                pass

        if not is_base_ready(appid, details) or not is_in_additional_package_apps(appid):
            rollback_cfg()
            return ("unknown", "base_verification_failed")

    api_cmds: list[str] = []
    if platforms and all(p == "windows" for p in platforms) and needs_proton_compat(appid):
        api_cmds.append(f"setcompat|{appid}|proton_experimental")

    post_status, post_idx, _ = inspect_app_manifest(appid)
    if post_status == "empty_install":
        target_idx = post_idx if post_idx is not None else (lib_idx if lib_idx is not None else 0)
        api_cmds.append(f"install|{appid}|{target_idx}")

    if api_cmds:
        # Wait briefly so SLSsteam's inotify watcher reloads config.yaml before SLSAPI commands run.
        time.sleep(float(os.environ.get("HOLLOWBRIDGE_API_SETTLE", "0.2")))
        send_sls_api_commands(
            api_cmds, delay=float(os.environ.get("HOLLOWBRIDGE_API_DELAY", "0.4"))
        )

    final_state = "prepared_optional_missing" if optional_missing else "prepared"
    reason = "recovered_empty_install" if post_status == "empty_install" else "ok"
    return (final_state, reason)


def write_status(status_file: Path, state: str, reason: str) -> None:
    clean_reason = re.sub(r"[\r\n\t]+", " ", str(reason or "")).strip()[:120] or "none"
    tmp = status_file.with_name(status_file.name + ".tmp")
    status_file.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(f"STATUS\t{state}\t{clean_reason}\n", encoding="utf-8")
    os.replace(tmp, status_file)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        return 2
    action = argv[1]
    if action == "seed":
        status_path = Path(argv[2])
        try:
            state, reason = run_seed()
        except Exception as exc:
            state, reason = ("unknown", f"seed_error:{type(exc).__name__}")
        write_status(status_path, state, reason)
        return 0
    if action == "prepare" and len(argv) >= 4:
        appid = int(argv[2])
        status_path = Path(argv[3])
        try:
            state, reason = run_prepare(appid)
        except Exception as exc:
            state, reason = ("unknown", f"prepare_error:{type(exc).__name__}")
        write_status(status_path, state, reason)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
]=]

local function logInfo(msg)
    if log and type(log.info) == "function" then
        pcall(log.info, "[HollowBridge] " .. tostring(msg))
    end
end

local function logWarn(msg)
    if log and type(log.warn) == "function" then
        pcall(log.warn, "[HollowBridge] " .. tostring(msg))
    end
end

local function nowSec()
    return os.time() or 0
end

local function shellQuote(s)
    return "'" .. tostring(s):gsub("'", "'\\''") .. "'"
end

local function fileExists(path)
    local f = io.open(path, "rb")
    if f then
        f:close()
        return true
    end
    return false
end

local function readFile(path, maxBytes)
    local f = io.open(path, "rb")
    if not f then return nil end
    local data = f:read(maxBytes or 1048576)
    f:close()
    return data
end

local function writeAtomic(path, content)
    local tmp = path .. ".tmp"
    local f = io.open(tmp, "wb")
    if not f then return false end
    local ok = f:write(content)
    f:close()
    if not ok then
        os.remove(tmp)
        return false
    end
    local renamed = os.rename(tmp, path)
    if not renamed then
        os.remove(tmp)
        return false
    end
    return true
end

local function stateDir()
    local override = os.getenv("HOLLOWBRIDGE_STATE_DIR")
    if override and override ~= "" then return override end
    local home = os.getenv("HOME")
    if not home or home == "" then return nil end
    return home .. "/.local/state/hollowbridge"
end

local function steamRoot()
    local override = os.getenv("HOLLOWBRIDGE_STEAM_ROOT")
    if override and override ~= "" then return override end
    local home = os.getenv("HOME")
    if not home or home == "" then return nil end
    return home .. "/.local/share/Steam"
end

local function ensureStateDir()
    local dir = stateDir()
    if not dir then return nil end
    if not H._dirCreated or H._dirCreated ~= dir then
        os.execute("umask 077; mkdir -p " .. shellQuote(dir))
        H._dirCreated = dir
    end
    return dir
end

local function materializeWorker()
    local dir = ensureStateDir()
    if not dir then return nil end
    local workerPath = dir .. "/hollow_worker.py"
    local existing = readFile(workerPath, #PYTHON_WORKER_SOURCE + 64)
    if existing ~= PYTHON_WORKER_SOURCE then
        if not writeAtomic(workerPath, PYTHON_WORKER_SOURCE) then
            return nil
        end
    end
    return workerPath
end

H.origins = H.origins or {}
H.apps = H.apps or {}
H.emptySeen = H.emptySeen or {}
H.activeWorker = H.activeWorker or nil
H.seededManifests = H.seededManifests or false
H.stateLoadedDir = H.stateLoadedDir or nil
H.lastStepAt = H.lastStepAt or 0
H.lastSnapshotsApplied = H.lastSnapshotsApplied or -1

local function saveState()
    local dir = ensureStateDir()
    if not dir then return false end
    local lines = {}
    local originKeys = {}
    for origin in pairs(H.origins) do
        originKeys[#originKeys + 1] = origin
    end
    table.sort(originKeys)
    for _, origin in ipairs(originKeys) do
        local o = H.origins[origin]
        lines[#lines + 1] = string.format(
            "ORIGIN\t%s\t%d\t%d",
            origin,
            tonumber(o.session) or 0,
            tonumber(o.generation) or 0
        )
    end
    local appOriginKeys = {}
    for origin in pairs(H.apps) do
        appOriginKeys[#appOriginKeys + 1] = origin
    end
    table.sort(appOriginKeys)
    for _, origin in ipairs(appOriginKeys) do
        local byApp = H.apps[origin] or {}
        local appIds = {}
        for appId in pairs(byApp) do
            appIds[#appIds + 1] = appId
        end
        table.sort(appIds)
        for _, appId in ipairs(appIds) do
            local r = byApp[appId]
            lines[#lines + 1] = string.format(
                "APP\t%s\t%d\t%d\t%s\t%d\t%d\t%s",
                origin,
                appId,
                tonumber(r.present) or 0,
                tostring(r.state or "unknown"),
                tonumber(r.recovered) or 0,
                tonumber(r.updated) or 0,
                tostring(r.reason or "none"):gsub("[\r\n\t]", " ")
            )
        end
    end
    return writeAtomic(dir .. "/state.tsv", table.concat(lines, "\n") .. (#lines > 0 and "\n" or ""))
end

local function loadState()
    local dir = ensureStateDir()
    if not dir then return end
    if H.stateLoadedDir == dir then return end
    H.origins = {}
    H.apps = {}
    local content = readFile(dir .. "/state.tsv", 4 * 1024 * 1024)
    local dirty = false
    if content then
        for line in content:gmatch("[^\r\n]+") do
            local kind = line:match("^([^\t]+)\t")
            if kind == "ORIGIN" then
                local origin, sess, gen = line:match("^ORIGIN\t([^\t]+)\t(%d+)\t(%d+)$")
                if origin and sess and gen then
                    H.origins[origin] = {
                        session = tonumber(sess) or 0,
                        generation = tonumber(gen) or 0,
                    }
                end
            elseif kind == "APP" then
                local origin, appStr, presStr, state, recStr, updStr, reason =
                    line:match("^APP\t([^\t]+)\t(%d+)\t([01])\t([^\t]+)\t([01])\t(%d+)\t(.*)$")
                local appId = tonumber(appStr)
                if origin and appId and appId > 0 then
                    if state == "preparing" then
                        state = "unknown"
                        reason = "interrupted_on_restart"
                        dirty = true
                    end
                    H.apps[origin] = H.apps[origin] or {}
                    H.apps[origin][appId] = {
                        present = tonumber(presStr) or 0,
                        state = state,
                        recovered = tonumber(recStr) or 0,
                        updated = tonumber(updStr) or 0,
                        reason = (reason and reason ~= "") and reason or "none",
                    }
                end
            end
        end
    end
    H.stateLoadedDir = dir
    if dirty then saveState() end
end

local function getLoggedOnUser()
    if HOLLOWBRIDGE_TEST_MODE and type(HOLLOWBRIDGE_TEST_USER) == "table" then
        if HOLLOWBRIDGE_TEST_USER.loggedOn then
            return HOLLOWBRIDGE_TEST_USER
        end
        return nil
    end
    if not SLS or not SLS.steamEngine then return nil end
    local engine = SLS.steamEngine
    if type(engine.getUser) ~= "function" then return nil end
    local okUser, user = pcall(function() return engine:getUser(0) end)
    if not okUser or not user or type(user.getClientUser) ~= "function" then return nil end
    local okClient, clientUser = pcall(function() return user:getClientUser() end)
    if not okClient or not clientUser or type(clientUser.loggedOn) ~= "function" then return nil end
    local okLogged, loggedOn = pcall(function() return clientUser:loggedOn() end)
    if not okLogged or loggedOn ~= true then return nil end
    return user
end

local function isGenuineSubscribed(user, appId)
    if not user or type(user.isSubscribed) ~= "function" then return false end
    local ok, sub = pcall(function() return user:isSubscribed(appId) end)
    return ok and sub == true
end

local function librarySteamappsDirs()
    local root = steamRoot()
    if not root then return {} end
    local defaultDir = root .. "/steamapps"
    local dirs = { defaultDir }
    local seen = { [defaultDir] = true }
    local vdf = readFile(defaultDir .. "/libraryfolders.vdf", 1048576)
    if vdf then
        for path in vdf:gmatch('"path"%s+"([^"]+)"') do
            local candidate = path:gsub('\\\\', '\\') .. "/steamapps"
            if not seen[candidate] then
                seen[candidate] = true
                dirs[#dirs + 1] = candidate
            end
        end
    end
    return dirs
end

local function inspectAppManifest(appId)
    local foundEmpty = false
    for _, dir in ipairs(librarySteamappsDirs()) do
        local text = readFile(dir .. "/appmanifest_" .. tostring(appId) .. ".acf", 262144)
        if text then
            local manifestAppId = tonumber(text:match('"appid"%s+"(%d+)"'))
            local flags = tonumber(text:match('"StateFlags"%s+"(%d+)"'))
            local size = tonumber(text:match('"SizeOnDisk"%s+"(%d+)"')) or 0
            local total = tonumber(text:match('"BytesToDownload"%s+"(%d+)"')) or 0
            local done = tonumber(text:match('"BytesDownloaded"%s+"(%d+)"')) or 0
            local installedBlock = text:match('"InstalledDepots"%s*(%b{})')
            local hasDepots = installedBlock and installedBlock:match('"%d+"%s*{') ~= nil
            if manifestAppId ~= appId or not flags then
                return "unreadable"
            end
            if (flags == 4 and size > 0 and hasDepots) or size > 0 or total > 0 or done > 0 then
                return "installed_with_content"
            elseif flags == 4 and size == 0 and total == 0 and done == 0 and not hasDepots then
                foundEmpty = true
            else
                return "pending_or_unknown"
            end
        end
    end
    if foundEmpty then return "empty_install" end
    return "absent"
end

local function observeRemoteLibraries()
    loadState()
    local rps = ENV.RemotePlaySync
    if type(rps) ~= "table" or type(rps.remoteLibraries) ~= "table" then
        return false
    end
    local changed = false
    local now = nowSec()
    for origin, lib in pairs(rps.remoteLibraries) do
        if type(origin) == "string" and origin ~= "" and #origin <= 128
            and type(lib) == "table" and type(lib.additionalAppIds) == "table" then
            local session = tonumber(lib.session) or 0
            local generation = tonumber(lib.generation) or 0
            local prev = H.origins[origin]
            if not prev or prev.session ~= session or generation > prev.generation then
                local isBaseline = (prev == nil)
                H.origins[origin] = { session = session, generation = generation }
                H.apps[origin] = H.apps[origin] or {}
                local known = H.apps[origin]
                local currentSet = {}
                for _, rawId in ipairs(lib.additionalAppIds) do
                    local appId = tonumber(rawId)
                    if appId and appId > 0 and appId <= 4294967295 and math.floor(appId) == appId then
                        currentSet[appId] = true
                        local row = known[appId]
                        if not row then
                            known[appId] = {
                                present = 1,
                                state = isBaseline and "baseline" or "pending",
                                recovered = 0,
                                updated = now,
                                reason = isBaseline and "initial_baseline" or "synced_new",
                            }
                        elseif row.present == 0 then
                            row.present = 1
                            row.updated = now
                        end
                    end
                end
                for appId, row in pairs(known) do
                    if row.present == 1 and not currentSet[appId] then
                        row.present = 0
                        row.updated = now
                    end
                end
                changed = true
            end
        end
    end
    if changed then saveState() end
    return changed
end

local function checkEmptyInstalls(user)
    if not user then return false end
    local stableNeeded = tonumber(os.getenv("HOLLOWBRIDGE_EMPTY_STABLE_COUNT")) or 2
    local changed = false
    local now = nowSec()
    for _, byApp in pairs(H.apps) do
        for appId, row in pairs(byApp) do
            if row.present == 1 and row.recovered == 0
                and (row.state == "baseline" or row.state == "prepared" or row.state == "prepared_optional_missing") then
                local mStatus = inspectAppManifest(appId)
                if mStatus == "empty_install" and not isGenuineSubscribed(user, appId) then
                    local count = (H.emptySeen[appId] or 0) + 1
                    H.emptySeen[appId] = count
                    if count >= stableNeeded then
                        H.emptySeen[appId] = nil
                        row.state = "pending"
                        row.recovered = 1
                        row.updated = now
                        row.reason = "empty_install_recovery"
                        changed = true
                        logInfo("Detected empty install for synced app " .. tostring(appId) .. "; queuing Hollow recovery")
                    end
                else
                    H.emptySeen[appId] = nil
                end
            end
        end
    end
    if changed then saveState() end
    return changed
end

local function spawnBackgroundWorker(args, statusFile)
    os.remove(statusFile)
    os.remove(statusFile .. ".tmp")
    if HOLLOWBRIDGE_TEST_MODE and type(HOLLOWBRIDGE_TEST_SPAWN) == "function" then
        return HOLLOWBRIDGE_TEST_SPAWN(args, statusFile)
    end
    local dir = ensureStateDir()
    local workerPath = materializeWorker()
    if not dir or not workerPath then return false end
    local customCmd = os.getenv("HOLLOWBRIDGE_PYTHON_CMD")
    local pyCmd = customCmd and customCmd ~= "" and customCmd or "/usr/bin/python3"
    local cleanEnv = "/usr/bin/env -u LD_AUDIT -u LD_PRELOAD -u LD_LIBRARY_PATH "
    local inner = cleanEnv .. shellQuote(pyCmd) .. " " .. shellQuote(workerPath) .. " " .. args
    local hostLauncher = "/usr/bin/steam-runtime-launch-client"
    if not customCmd and fileExists(hostLauncher) and fileExists("/run/host") then
        inner = cleanEnv .. shellQuote(hostLauncher) .. " --host -- " .. inner
    end
    local logPath = dir .. "/worker.log"
    local fullCmd = "(umask 077; " .. inner .. " </dev/null >>" .. shellQuote(logPath) .. " 2>&1) &"
    os.execute(fullCmd)
    return true
end

local function pollWorker()
    local w = H.activeWorker
    if not w then return false end
    local content = readFile(w.statusFile, 4096)
    if content then
        local state, reason = content:match("^STATUS\t([^\t\r\n]+)\t([^\r\n]*)")
        if state then
            os.remove(w.statusFile)
            H.activeWorker = nil
            if w.kind == "seed" then
                H.seededManifests = true
                logInfo("Manifest cache seed completed (" .. tostring(reason) .. ")")
                return true
            elseif w.kind == "prepare" and w.origin and w.appId then
                local row = H.apps[w.origin] and H.apps[w.origin][w.appId]
                if row and row.state == "preparing" then
                    row.state = state
                    row.reason = (reason and reason ~= "") and reason or "none"
                    row.updated = nowSec()
                    saveState()
                end
                logInfo(string.format(
                    "App %d preparation finished: state=%s reason=%s",
                    w.appId, tostring(state), tostring(reason)
                ))
                return true
            end
        end
    end
    if nowSec() - (w.startedAt or 0) > WORKER_TIMEOUT_SEC then
        os.remove(w.statusFile)
        H.activeWorker = nil
        if w.kind == "seed" then
            H.seededManifests = true
        elseif w.kind == "prepare" and w.origin and w.appId then
            local row = H.apps[w.origin] and H.apps[w.origin][w.appId]
            if row and row.state == "preparing" then
                row.state = "unknown"
                row.reason = "worker_timeout"
                row.updated = nowSec()
                saveState()
            end
        end
        logWarn("Background worker timed out")
        return true
    end
    return false
end

local function startNextWorker(user)
    if H.activeWorker or not user then return false end
    local dir = ensureStateDir()
    if not dir then return false end
    local statusFile = dir .. "/worker.status"

    if not H.seededManifests then
        local args = "seed " .. shellQuote(statusFile)
        if spawnBackgroundWorker(args, statusFile) then
            H.activeWorker = {
                kind = "seed",
                statusFile = statusFile,
                startedAt = nowSec(),
            }
            return true
        end
        H.seededManifests = true
    end

    local candidates = {}
    for origin, byApp in pairs(H.apps) do
        for appId, row in pairs(byApp) do
            if row.present == 1 and row.state == "pending" then
                candidates[#candidates + 1] = {
                    origin = origin,
                    appId = appId,
                    row = row,
                }
            end
        end
    end
    table.sort(candidates, function(a, b)
        if a.row.updated ~= b.row.updated then
            return a.row.updated < b.row.updated
        end
        if a.appId ~= b.appId then
            return a.appId < b.appId
        end
        return a.origin < b.origin
    end)

    local dirty = false
    local now = nowSec()
    for _, item in ipairs(candidates) do
        local row = item.row
        local appId = item.appId
        if isGenuineSubscribed(user, appId) then
            row.state = "ignored"
            row.reason = "genuine_owned"
            row.updated = now
            dirty = true
        else
            local mStatus = inspectAppManifest(appId)
            if mStatus == "installed_with_content" then
                row.state = "ignored"
                row.reason = "already_installed"
                row.updated = now
                dirty = true
            else
                row.state = "preparing"
                row.updated = now
                saveState()
                local args = "prepare " .. tostring(appId) .. " " .. shellQuote(statusFile)
                if spawnBackgroundWorker(args, statusFile) then
                    H.activeWorker = {
                        kind = "prepare",
                        origin = item.origin,
                        appId = appId,
                        statusFile = statusFile,
                        startedAt = now,
                    }
                    logInfo("Started local Hollow preparation for app " .. tostring(appId))
                    return true
                else
                    row.state = "unknown"
                    row.reason = "spawn_failed"
                    saveState()
                    return false
                end
            end
        end
    end
    if dirty then saveState() end
    return false
end

local function step(force)
    local now = nowSec()
    local rps = ENV.RemotePlaySync
    local rpsApplied = (type(rps) == "table" and tonumber(rps.snapshotsApplied)) or -1
    local snapshotChanged = (rpsApplied ~= H.lastSnapshotsApplied)
    if not force and not snapshotChanged then
        local minWait = H.activeWorker and 1 or STEP_INTERVAL_SEC
        if now - H.lastStepAt < minWait then
            return
        end
    end
    H.lastStepAt = now
    H.lastSnapshotsApplied = rpsApplied

    loadState()
    pollWorker()
    observeRemoteLibraries()
    local user = getLoggedOnUser()
    if user then
        checkEmptyInstalls(user)
        startNextWorker(user)
        if H.activeWorker and pollWorker() then
            startNextWorker(user)
            if H.activeWorker then
                pollWorker()
            end
        end
    end
end

local function runProtected(label, fn)
    local ok, err = xpcall(fn, function(e)
        if debug and type(debug.traceback) == "function" then
            return debug.traceback(tostring(e), 2)
        end
        return tostring(e)
    end)
    if not ok then
        logWarn(label .. " error: " .. tostring(err))
    end
end

H._handlers = {
    onConfigLoaded = function() runProtected("configLoaded", function() step(true) end) end,
    onInitialized = function() runProtected("initialized", function() step(true) end) end,
    onReload = function()
        runProtected("luaReload", function()
            H.activeWorker = nil
            H.stateLoadedDir = nil
        end)
    end,
    onPacket = function() runProtected("packet", function() step(false) end) end,
}

if SLS and type(SLS.registerCallback) == "function" and not H.callbacksRegistered then
    SLS.registerCallback("SLSsteam::configLoaded", function()
        if H._handlers then H._handlers.onConfigLoaded() end
    end)
    SLS.registerCallback("SLSsteam::initialized", function()
        if H._handlers then H._handlers.onInitialized() end
    end)
    SLS.registerCallback("SLSsteam::luaReload", function()
        if H._handlers then H._handlers.onReload() end
    end)
    SLS.registerCallback("Network::recvPkt", function(_pkt)
        if H._handlers then H._handlers.onPacket() end
    end)
    SLS.registerCallback("Network::sendPkt", function(_pkt)
        if H._handlers then H._handlers.onPacket() end
    end)
    H.callbacksRegistered = true
end

if SLS and type(SLS.onTick) == "function" and not H.tickRegistered then
    local okTick, handle = pcall(function()
        return SLS.onTick(function()
            if H._handlers then H._handlers.onPacket() end
        end, 1000)
    end)
    if okTick and handle and handle ~= 0 then
        H.tickRegistered = true
    end
end

runProtected("startup", function()
    loadState()
    materializeWorker()
    logInfo("HollowBridge plugin loaded")
end)

if HOLLOWBRIDGE_TEST_MODE then
    H.test = {
        step = step,
        loadState = loadState,
        saveState = saveState,
        observeRemoteLibraries = observeRemoteLibraries,
        checkEmptyInstalls = checkEmptyInstalls,
        pollWorker = pollWorker,
        startNextWorker = startNextWorker,
        inspectAppManifest = inspectAppManifest,
        materializeWorker = materializeWorker,
    }
end
