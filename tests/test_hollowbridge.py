import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_PATH = REPO_ROOT / "sls-plugin" / "hollowbridge.lua"


def write_mock_hollow_backend(backend_dir: Path, is_decky: bool = False) -> None:
    """Create a realistic mock Hollow Desktop or Hollow Decky backend tree."""
    for pkg in ("hd", "hollow", "sls", "sls/config", "steam"):
        d = backend_dir / pkg
        d.mkdir(parents=True, exist_ok=True)
        (d / "__init__.py").write_text("", encoding="utf-8")

    if is_decky:
        (backend_dir / "hd/decky_env.py").write_text(
            "import decky\n"
            "assert hasattr(decky, 'DECKY_USER_HOME')\n"
            "assert hasattr(decky, 'DECKY_PLUGIN_DIR')\n"
            "assert hasattr(decky, 'DECKY_PLUGIN_SETTINGS_DIR')\n"
            "assert hasattr(decky, 'DECKY_PLUGIN_RUNTIME_DIR')\n"
            "assert hasattr(decky, 'DECKY_PLUGIN_LOG_DIR')\n"
            "assert hasattr(decky, 'DECKY_SETTINGS_DIR')\n",
            encoding="utf-8",
        )

    (backend_dir / "steam/paths.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "def depotcache_dir():\n"
        "    root = Path(os.environ['HOLLOWBRIDGE_STEAM_ROOT'])\n"
        "    d = root / 'steamapps/depotcache'\n"
        "    d.mkdir(parents=True, exist_ok=True)\n"
        "    return d\n",
        encoding="utf-8",
    )

    (backend_dir / "steam/appdetails.py").write_text(
        "import json, os\n"
        "def get_app_details(appid, target=None):\n"
        "    db = json.loads(os.environ.get('MOCK_HOLLOW_DB', '{}'))\n"
        "    return db.get(str(appid), {'error': 'not found'})\n",
        encoding="utf-8",
    )

    (backend_dir / "steam/dlc_labels.py").write_text(
        "def real_unkeyed_depots(details, configured_keys, candidates):\n"
        "    real = details.get('depot_has_manifests', {})\n"
        "    return [str(d) for d in candidates if real.get(str(d)) and str(d) not in configured_keys]\n",
        encoding="utf-8",
    )

    (backend_dir / "sls/config/readers.py").write_text(
        "import json, os\n"
        "def get_configured_depot_keys():\n"
        "    keys = json.loads(os.environ.get('MOCK_CONFIGURED_KEYS', '{}'))\n"
        "    return {'keys': keys}\n",
        encoding="utf-8",
    )

    (backend_dir / "sls/config/adder.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "def add_to_sls(app_id, game_name, depots=None, is_delisted=False):\n"
        "    cfg = Path(os.environ['HOLLOWBRIDGE_SLS_CONFIG'])\n"
        "    text = cfg.read_text(encoding='utf-8') if cfg.is_file() else 'AdditionalPackageApps:\\n'\n"
        "    entry = f'  - {app_id} # {game_name}\\n'\n"
        "    if f'- {app_id}' not in text:\n"
        "        if 'AdditionalPackageApps:' in text:\n"
        "            text = text.replace('AdditionalPackageApps:\\n', 'AdditionalPackageApps:\\n' + entry, 1)\n"
        "        else:\n"
        "            text += '\\nAdditionalPackageApps:\\n' + entry\n"
        "        cfg.write_text(text, encoding='utf-8')\n",
        encoding="utf-8",
    )

    (backend_dir / "hollow/manifests.py").write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        "def seed_manifests_from_cache():\n"
        "    marker = os.environ.get('MOCK_SEED_MARKER')\n"
        "    if marker:\n"
        "        Path(marker).write_text('seeded', encoding='utf-8')\n"
        "    return {'ok': True, 'seeded': 2}\n"
        "def depots_missing_manifests(depot_ids, details=None):\n"
        "    missing = set(json.loads(os.environ.get('MOCK_MISSING_MANIFESTS', '[]')))\n"
        "    return [str(d) for d in depot_ids if str(d) in missing]\n",
        encoding="utf-8",
    )

    (backend_dir / "hollow/inject.py").write_text(
        "import json, os\n"
        + ("from hd import decky_env\n" if is_decky else "")
        + "from sls.config.adder import add_to_sls\n"
        "from steam.appdetails import get_app_details\n"
        "_STATUS = {}\n"
        "def inject_game_sync(app_id, target=None):\n"
        "    details = get_app_details(str(app_id))\n"
        "    if details.get('error'):\n"
        "        return {'injected': False, 'error': details['error']}\n"
        "    add_to_sls(str(app_id), details.get('title', str(app_id)))\n"
        "    outcomes = json.loads(os.environ.get('MOCK_INJECT_OUTCOMES', '{}'))\n"
        "    outcome = outcomes.get(str(app_id), {'done': True})\n"
        "    if outcome.get('grant_keys'):\n"
        "        keys = json.loads(os.environ.get('MOCK_CONFIGURED_KEYS', '{}'))\n"
        "        for k in outcome['grant_keys']:\n"
        "            keys[str(k)] = 'deadbeef' * 8\n"
        "        os.environ['MOCK_CONFIGURED_KEYS'] = json.dumps(keys)\n"
        "    _STATUS[str(app_id)] = outcome\n"
        "    return {'injected': True, 'app_id': str(app_id)}\n"
        "def get_inject_status(app_id):\n"
        "    return _STATUS.get(str(app_id), {'done': True})\n",
        encoding="utf-8",
    )


class HollowBridgeLuaStateMachineTest(unittest.TestCase):
    def run_lua(self, lua_script: str, env_overrides: dict | None = None) -> str:
        env = os.environ.copy()
        if env_overrides:
            env.update(env_overrides)
        proc = subprocess.run(
            ["luajit", "-e", lua_script],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        return proc.stdout

    def test_baseline_new_sync_ownership_and_installed_filtering(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            steam_root = tmp_path / "steam"
            (steam_root / "steamapps").mkdir(parents=True)

            # App 300 is already installed with content on disk.
            (steam_root / "steamapps/appmanifest_300.acf").write_text(
                '"AppState"\n{\n\t"appid"\t\t"300"\n\t"StateFlags"\t\t"4"\n'
                '\t"SizeOnDisk"\t\t"1048576"\n\t"BytesToDownload"\t\t"1048576"\n'
                '\t"BytesDownloaded"\t\t"1048576"\n'
                '\t"InstalledDepots"\n\t{\n\t\t"301"\n\t\t{\n\t\t\t"manifest"\t\t"111"\n\t\t}\n\t}\n}\n',
                encoding="utf-8",
            )

            script = f"""
HOLLOWBRIDGE_TEST_MODE = true
local spawned = {{}}
HOLLOWBRIDGE_TEST_USER = {{
    loggedOn = true,
    isSubscribed = function(_, appId)
        -- App 200 is genuinely owned on Steam
        return appId == 200
    end,
}}
HOLLOWBRIDGE_TEST_SPAWN = function(args, statusFile)
    spawned[#spawned + 1] = args
    local f = io.open(statusFile, "wb")
    if args:match("^seed ") then
        f:write("STATUS\\tseeded\\tseeded=2\\n")
    elseif args:match("^prepare 400 ") then
        f:write("STATUS\\tprepared\\tok\\n")
    elseif args:match("^prepare 500 ") then
        f:write("STATUS\\tprepared_optional_missing\\tok\\n")
    end
    f:close()
    return true
end

RemotePlaySync = {{
    snapshotsApplied = 1,
    remoteLibraries = {{
        deck = {{
            session = 10,
            generation = 1,
            additionalAppIds = {{ 100, 101 }},
        }},
    }},
}}

dofile({ str(PLUGIN_PATH)!r })

-- 1. First observation records 100 and 101 as baseline and only runs startup seed.
HollowBridge.test.step(true)
assert(HollowBridge.apps.deck[100].state == "baseline")
assert(HollowBridge.apps.deck[101].state == "baseline")
assert(#spawned == 1 and spawned[1]:match("^seed "))

-- 2. Second snapshot adds 200 (genuine owned), 300 (already installed), 400 (Hollow), 500 (Hollow w/ optional DLC missing).
RemotePlaySync.snapshotsApplied = 2
RemotePlaySync.remoteLibraries.deck.generation = 2
RemotePlaySync.remoteLibraries.deck.additionalAppIds = {{ 100, 101, 200, 300, 400, 500 }}

HollowBridge.test.step(true)
assert(HollowBridge.apps.deck[200].state == "ignored")
assert(HollowBridge.apps.deck[200].reason == "genuine_owned")
assert(HollowBridge.apps.deck[300].state == "ignored")
assert(HollowBridge.apps.deck[300].reason == "already_installed")
assert(HollowBridge.apps.deck[400].state == "prepared")

HollowBridge.test.step(true)
assert(HollowBridge.apps.deck[500].state == "prepared_optional_missing")

-- 3. Duplicate snapshot does not re-run preparation.
HollowBridge.test.step(true)
assert(#spawned == 3)
print("STATE_MACHINE_OK")
"""
            out = self.run_lua(
                script,
                {
                    "HOLLOWBRIDGE_STATE_DIR": str(state_dir),
                    "HOLLOWBRIDGE_STEAM_ROOT": str(steam_root),
                },
            )
            self.assertIn("STATE_MACHINE_OK", out)

    def test_interrupted_preparing_and_empty_install_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            steam_root = tmp_path / "steam"
            (steam_root / "steamapps").mkdir(parents=True)
            state_dir.mkdir(parents=True)

            # Simulate an interrupted 'preparing' row (app 700) and a baseline row (app 800) from prior session.
            (state_dir / "state.tsv").write_text(
                "ORIGIN\tdeck\t10\t1\n"
                "APP\tdeck\t700\t1\tpreparing\t0\t1000\tsynced_new\n"
                "APP\tdeck\t800\t1\tbaseline\t0\t1000\tinitial_baseline\n",
                encoding="utf-8",
            )

            # User clicked "Install on Device" for baseline app 800, creating an empty 0-depot manifest.
            (steam_root / "steamapps/appmanifest_800.acf").write_text(
                '"AppState"\n{\n\t"appid"\t\t"800"\n\t"StateFlags"\t\t"4"\n'
                '\t"SizeOnDisk"\t\t"0"\n\t"BytesToDownload"\t\t"0"\n'
                '\t"BytesDownloaded"\t\t"0"\n\t"InstalledDepots"\n\t{\n\t}\n}\n',
                encoding="utf-8",
            )

            script = f"""
HOLLOWBRIDGE_TEST_MODE = true
local spawned = {{}}
HOLLOWBRIDGE_TEST_USER = {{
    loggedOn = true,
    isSubscribed = function(_, _) return false end,
}}
HOLLOWBRIDGE_TEST_SPAWN = function(args, statusFile)
    spawned[#spawned + 1] = args
    local f = io.open(statusFile, "wb")
    if args:match("^seed ") then
        f:write("STATUS\\tseeded\\tseeded=1\\n")
    elseif args:match("^prepare 800 ") then
        f:write("STATUS\\tprepared\\trecovered_empty_install\\n")
    end
    f:close()
    return true
end

dofile({ str(PLUGIN_PATH)!r })

-- Interrupted 'preparing' app 700 must transition to 'unknown' on startup and never re-run automatically.
assert(HollowBridge.apps.deck[700].state == "unknown")
assert(HollowBridge.apps.deck[700].reason == "interrupted_on_restart")

-- Step 1: sees empty install #1 for app 800 (needs 2 stable observations by default)
HollowBridge.test.step(true)
assert(HollowBridge.apps.deck[800].state == "baseline")

-- Step 2: sees empty install #2 -> promotes app 800 to pending and recovers it once
HollowBridge.test.step(true)
assert(HollowBridge.apps.deck[800].state == "prepared")
assert(HollowBridge.apps.deck[800].recovered == 1)
assert(HollowBridge.apps.deck[800].reason == "recovered_empty_install")

-- Step 3: even if empty manifest remains briefly, recovered=1 prevents infinite recovery loops
HollowBridge.test.step(true)
assert(#spawned == 2)
print("RECOVERY_AND_RESTART_OK")
"""
            out = self.run_lua(
                script,
                {
                    "HOLLOWBRIDGE_STATE_DIR": str(state_dir),
                    "HOLLOWBRIDGE_STEAM_ROOT": str(steam_root),
                },
            )
            self.assertIn("RECOVERY_AND_RESTART_OK", out)


class HollowWorkerBackendTest(unittest.TestCase):
    def materialize_worker(self, state_dir: Path) -> Path:
        env = os.environ.copy()
        env["HOLLOWBRIDGE_STATE_DIR"] = str(state_dir)
        subprocess.run(
            [
                "luajit",
                "-e",
                f"HOLLOWBRIDGE_TEST_MODE=true; dofile({str(PLUGIN_PATH)!r}); assert(HollowBridge.test.materializeWorker())",
            ],
            cwd=str(REPO_ROOT),
            env=env,
            check=True,
            timeout=10,
        )
        worker = state_dir / "hollow_worker.py"
        self.assertTrue(worker.is_file())
        return worker

    def test_desktop_and_decky_worker_preparation_compat_and_sls_api(self):
        for kind, is_decky in (("desktop", False), ("decky", True)):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                state_dir = tmp_path / "state"
                steam_root = tmp_path / "steam"
                (steam_root / "steamapps/depotcache").mkdir(parents=True)
                (steam_root / "config").mkdir(parents=True)
                (steam_root / "config/config.vdf").write_text(
                    '"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n\t\t\t\t"CompatToolMapping"\n\t\t\t\t{\n\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n',
                    encoding="utf-8",
                )
                # Empty manifest exists for app 254700 so worker should issue both setcompat and install
                (steam_root / "steamapps/appmanifest_254700.acf").write_text(
                    '"AppState"\n{\n\t"appid"\t\t"254700"\n\t"StateFlags"\t\t"4"\n'
                    '\t"SizeOnDisk"\t\t"0"\n\t"BytesToDownload"\t\t"0"\n'
                    '\t"BytesDownloaded"\t\t"0"\n\t"InstalledDepots"\n\t{\n\t}\n}\n',
                    encoding="utf-8",
                )

                sls_cfg = tmp_path / "config.yaml"
                sls_cfg.write_text("PlayNotOwnedGames: yes\nAdditionalPackageApps:\n", encoding="utf-8")
                sls_api = tmp_path / "SLSsteam.API"
                sls_api.write_text("", encoding="utf-8")

                backend_dir = tmp_path / ("hollow-decky" if is_decky else "hollow-desktop")
                write_mock_hollow_backend(backend_dir, is_decky=is_decky)

                worker = self.materialize_worker(state_dir)
                status_file = state_dir / "worker.status"
                seed_marker = tmp_path / "seeded.marker"

                mock_db = {
                    "254700": {
                        "title": "Resident Evil 0",
                        "depots": {"254701": {"platform": "windows"}},
                        "depot_has_manifests": {"254701": True},
                    }
                }
                mock_outcomes = {
                    "254700": {
                        "done": True,
                        "grant_keys": ["254701"],
                        "keys_missing": ["999999"],  # Optional DLC depot missing
                    }
                }

                env = os.environ.copy()
                env.update(
                    {
                        "HOLLOWBRIDGE_STATE_DIR": str(state_dir),
                        "HOLLOWBRIDGE_STEAM_ROOT": str(steam_root),
                        "HOLLOWBRIDGE_SLS_CONFIG": str(sls_cfg),
                        "HOLLOWBRIDGE_SLS_API": str(sls_api),
                        "HOLLOWBRIDGE_BACKEND_DIR": str(backend_dir),
                        "HOLLOWBRIDGE_BACKEND_KIND": kind,
                        "HOLLOWBRIDGE_HOMEBREW_DIR": str(tmp_path / "homebrew"),
                        "HOLLOWBRIDGE_API_SETTLE": "0.02",
                        "HOLLOWBRIDGE_API_DELAY": "0.15",
                        "MOCK_HOLLOW_DB": subprocess.list2cmdline([""])[1:-1]
                        if False
                        else __import__("json").dumps(mock_db),
                        "MOCK_INJECT_OUTCOMES": __import__("json").dumps(mock_outcomes),
                        "MOCK_CONFIGURED_KEYS": "{}",
                        "MOCK_MISSING_MANIFESTS": "[]",
                        "MOCK_SEED_MARKER": str(seed_marker),
                    }
                )

                captured_api_writes: list[str] = []
                stop_sniffer = threading.Event()

                def sniff_api():
                    while not stop_sniffer.is_set():
                        try:
                            txt = sls_api.read_text(encoding="utf-8")
                            if txt and (not captured_api_writes or captured_api_writes[-1] != txt):
                                captured_api_writes.append(txt)
                        except Exception:
                            pass
                        time.sleep(0.01)

                sniffer = threading.Thread(target=sniff_api, daemon=True)
                sniffer.start()
                try:
                    subprocess.run(
                        [sys.executable, str(worker), "prepare", "254700", str(status_file)],
                        env=env,
                        check=True,
                        timeout=10,
                    )
                finally:
                    stop_sniffer.set()
                    sniffer.join(timeout=1)

                self.assertTrue(seed_marker.is_file())
                status_text = status_file.read_text(encoding="utf-8").strip()
                self.assertEqual(
                    status_text,
                    "STATUS\tprepared_optional_missing\trecovered_empty_install",
                )
                self.assertIn("- 254700 # Resident Evil 0", sls_cfg.read_text(encoding="utf-8"))
                self.assertTrue(
                    any(
                        "setcompat|254700|proton_experimental\ninstall|254700|0\n" in w
                        for w in captured_api_writes
                    ),
                    f"Expected setcompat+install in SLSsteam.API writes, got {captured_api_writes!r}",
                )
                # Verify SLSsteam.API was truncated back to 0 bytes after delivery
                self.assertEqual(sls_api.stat().st_size, 0)

    def test_failed_inject_rolls_back_sls_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            steam_root = tmp_path / "steam"
            (steam_root / "steamapps/depotcache").mkdir(parents=True)

            sls_cfg = tmp_path / "config.yaml"
            original_cfg = "PlayNotOwnedGames: yes\nAdditionalPackageApps:\n  - 100 # Existing\n"
            sls_cfg.write_text(original_cfg, encoding="utf-8")

            backend_dir = tmp_path / "hollow-desktop"
            write_mock_hollow_backend(backend_dir, is_decky=False)
            worker = self.materialize_worker(state_dir)
            status_file = state_dir / "worker.status"

            mock_db = {
                "999": {
                    "title": "Unkeyed Game",
                    "depots": {"9991": {"platform": "linux"}},
                    "depot_has_manifests": {"9991": True},
                }
            }
            mock_outcomes = {
                "999": {
                    "done": True,
                    "keys_missing": ["9991"],  # Base depot key missing!
                }
            }

            env = os.environ.copy()
            env.update(
                {
                    "HOLLOWBRIDGE_STATE_DIR": str(state_dir),
                    "HOLLOWBRIDGE_STEAM_ROOT": str(steam_root),
                    "HOLLOWBRIDGE_SLS_CONFIG": str(sls_cfg),
                    "HOLLOWBRIDGE_BACKEND_DIR": str(backend_dir),
                    "HOLLOWBRIDGE_BACKEND_KIND": "desktop",
                    "MOCK_HOLLOW_DB": __import__("json").dumps(mock_db),
                    "MOCK_INJECT_OUTCOMES": __import__("json").dumps(mock_outcomes),
                    "MOCK_CONFIGURED_KEYS": "{}",
                    "MOCK_MISSING_MANIFESTS": "[]",
                }
            )

            subprocess.run(
                [sys.executable, str(worker), "prepare", "999", str(status_file)],
                env=env,
                check=True,
                timeout=10,
            )

            self.assertEqual(
                status_file.read_text(encoding="utf-8").strip(),
                "STATUS\tunknown\tmissing_base_depot_keys",
            )
            # config.yaml must be rolled back so 999 is not left in AdditionalPackageApps
            self.assertEqual(sls_cfg.read_text(encoding="utf-8"), original_cfg)

    def test_end_to_end_lua_spawns_real_python_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            steam_root = tmp_path / "steam"
            (steam_root / "steamapps/depotcache").mkdir(parents=True)
            (steam_root / "config").mkdir(parents=True)
            (steam_root / "config/config.vdf").write_text(
                '"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n\t\t\t\t"CompatToolMapping"\n\t\t\t\t{\n\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n',
                encoding="utf-8",
            )
            sls_cfg = tmp_path / "config.yaml"
            sls_cfg.write_text("PlayNotOwnedGames: yes\nAdditionalPackageApps:\n", encoding="utf-8")
            sls_api = tmp_path / "SLSsteam.API"
            sls_api.write_text("", encoding="utf-8")

            backend_dir = tmp_path / "hollow-decky"
            write_mock_hollow_backend(backend_dir, is_decky=True)

            mock_db = {
                "1818450": {
                    "title": "Ad Infinitum",
                    "depots": {"1818451": {"platform": "windows"}},
                    "depot_has_manifests": {"1818451": True},
                }
            }
            mock_outcomes = {
                "1818450": {
                    "done": True,
                    "grant_keys": ["1818451"],
                }
            }

            script = f"""
HOLLOWBRIDGE_TEST_MODE = true
HOLLOWBRIDGE_TEST_USER = {{
    loggedOn = true,
    isSubscribed = function(_, appId) return false end,
}}
RemotePlaySync = {{
    snapshotsApplied = 1,
    remoteLibraries = {{
        desktop = {{
            session = 100,
            generation = 1,
            additionalAppIds = {{ 394360 }},
        }},
    }},
}}
dofile({ str(PLUGIN_PATH)!r })

-- Step 1: records 394360 as baseline and spawns real Python seed worker
HollowBridge.test.step(true)
local deadline = os.time() + 5
while HollowBridge.activeWorker and os.time() < deadline do
    os.execute("sleep 0.05")
    HollowBridge.test.pollWorker()
end
assert(HollowBridge.seededManifests == true)
assert(HollowBridge.apps.desktop[394360].state == "baseline")

-- Step 2: peer adds Ad Infinitum (1818450)
RemotePlaySync.snapshotsApplied = 2
RemotePlaySync.remoteLibraries.desktop.generation = 2
RemotePlaySync.remoteLibraries.desktop.additionalAppIds = {{ 394360, 1818450 }}

HollowBridge.test.step(true)
deadline = os.time() + 5
while HollowBridge.activeWorker and os.time() < deadline do
    os.execute("sleep 0.05")
    HollowBridge.test.pollWorker()
end
assert(HollowBridge.apps.desktop[1818450].state == "prepared")
print("E2E_LUA_PYTHON_OK")
"""
            env = os.environ.copy()
            env.update(
                {
                    "HOLLOWBRIDGE_STATE_DIR": str(state_dir),
                    "HOLLOWBRIDGE_STEAM_ROOT": str(steam_root),
                    "HOLLOWBRIDGE_SLS_CONFIG": str(sls_cfg),
                    "HOLLOWBRIDGE_SLS_API": str(sls_api),
                    "HOLLOWBRIDGE_BACKEND_DIR": str(backend_dir),
                    "HOLLOWBRIDGE_BACKEND_KIND": "decky",
                    "HOLLOWBRIDGE_HOMEBREW_DIR": str(tmp_path / "homebrew"),
                    "HOLLOWBRIDGE_PYTHON_CMD": sys.executable,
                    "HOLLOWBRIDGE_API_SETTLE": "0.01",
                    "HOLLOWBRIDGE_API_DELAY": "0.05",
                    "MOCK_HOLLOW_DB": __import__("json").dumps(mock_db),
                    "MOCK_INJECT_OUTCOMES": __import__("json").dumps(mock_outcomes),
                    "MOCK_CONFIGURED_KEYS": "{}",
                    "MOCK_MISSING_MANIFESTS": "[]",
                }
            )
            proc = subprocess.run(
                ["luajit", "-e", script],
                cwd=str(REPO_ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            self.assertIn("E2E_LUA_PYTHON_OK", proc.stdout)
            self.assertIn("- 1818450 # Ad Infinitum", sls_cfg.read_text(encoding="utf-8"))


class InstallScriptTest(unittest.TestCase):
    def test_install_update_from_legacy_and_uninstall(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            fake_bin = tmp_path / "bin"
            fake_bin.mkdir(parents=True)

            # Stub pgrep and systemctl so host Steam state does not affect offline test
            (fake_bin / "pgrep").write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
            (fake_bin / "pgrep").chmod(0o755)
            (fake_bin / "systemctl").write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
            (fake_bin / "systemctl").chmod(0o755)

            (home / ".config/SLSsteam/plugins").mkdir(parents=True)
            (home / "homebrew/plugins/hollow-decky/hollow").mkdir(parents=True)
            (home / "homebrew/plugins/hollow-decky/hollow/inject.py").write_text("# stub\n", encoding="utf-8")

            # Simulate legacy HollowBridge installation first
            (home / ".local/share/hollowbridge/hollowbridge").mkdir(parents=True)
            (home / ".local/share/hollowbridge/hollowbridge/__main__.py").write_text("# legacy\n", encoding="utf-8")
            (home / ".config/hollowbridge").mkdir(parents=True)
            (home / ".config/hollowbridge/native.conf").write_text("DEVICE=deck\n", encoding="utf-8")
            plugin_dest = home / ".config/SLSsteam/plugins/hollowbridge.lua"
            plugin_dest.write_text("-- HollowBridge agent launcher.\n", encoding="utf-8")

            env = os.environ.copy()
            env["HOME"] = str(home)
            env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"

            # 1. Update from legacy installation
            res = subprocess.run(
                [str(REPO_ROOT / "install.sh"), "update"],
                cwd=str(REPO_ROOT),
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("Updated", res.stdout)
            self.assertIn("-- HollowBridge SLSsteam plugin.", plugin_dest.read_text(encoding="utf-8"))
            self.assertFalse((home / ".local/share/hollowbridge").exists())
            self.assertFalse((home / ".config/hollowbridge").exists())

            # 2. Uninstall
            res = subprocess.run(
                [str(REPO_ROOT / "install.sh"), "uninstall"],
                cwd=str(REPO_ROOT),
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("HollowBridge removed", res.stdout)
            self.assertFalse(plugin_dest.exists())

            # 3. Fresh install
            res = subprocess.run(
                [str(REPO_ROOT / "install.sh"), "install"],
                cwd=str(REPO_ROOT),
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("Installed", res.stdout)
            self.assertTrue(plugin_dest.is_file())


if __name__ == "__main__":
    unittest.main()
