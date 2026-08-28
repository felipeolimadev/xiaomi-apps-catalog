#!/usr/bin/env python3
"""
Xiaomi System App Catalog Poller
================================
Checks the official Xiaomi market API for app updates and publishes new
versions to GitHub Releases. Runs as a GitHub Actions cron job; it is
idempotent (skips versions already present in catalog.json), so repeated
runs are safe.

Standard library only - no pip install required.

Flow per run:
  1. Load known-apps.json (the curated Xiaomi system app list) + devices.json.
  2. Batch-query the updateinfo API (versionCode=0 -> "latest please").
     Only apps WITH an available update appear in listApp.
  3. For each updated app, query the minicard/download API to get the
     direct CDN URL + MD5 hash + changelog.
  4. If the versionCode is not yet in catalog.json:
       - download the APK from the Xiaomi CDN
       - verify it matches the hash from the API
       - publish it as a GitHub Release (one tag per app-version)
       - append it to catalog.json
  5. Save catalog.json. (The workflow commits it back to the repo.)
"""

import base64
import hashlib
import hmac
import json
import os
import random
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_UPDATEINFO = "https://sg.global.market.xiaomi.com/apm/intl/updateinfo/v2"
API_DOWNLOAD = "https://sg.global.market.xiaomi.com/apm/intl/miniCard/app"
ICON_CDN_HOST = "https://fgb0.market.xiaomi.com/download/"

ROOT = Path(__file__).resolve().parent.parent
KNOWN_APPS_FILE = ROOT / "known-apps.json"
DEVICES_FILE = ROOT / "devices.json"
CATALOG_FILE = ROOT / "catalog.json"

# GitHub owner/repo where Releases live. In CI it is derived from GITHUB_REPOSITORY.
CATALOG_REPO = os.environ.get(
    "CATALOG_REPO", os.environ.get("GITHUB_REPOSITORY", "felipeolimadev/xiaomi-apps-catalog")
)

BATCH_SIZE = 40  # keep URL length under limits


# ---------------------------------------------------------------------------
# Xiaomi API
# ---------------------------------------------------------------------------

def md5hex(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def sign_params(params: dict, timestamp: int) -> tuple[str, str]:
    """HMAC signature. Algorithm chosen by timestamp % 4 (SHA-1/256/384/512)."""
    parts = [f"{k}&{v}" for k, v in params.items() if v is not None]
    string_to_sign = "=".join(parts)
    nonce = f"{timestamp}_{random.randint(0, 999)}"
    key = f"good luck!{nonce}"
    alg_map = {0: hashlib.sha1, 1: hashlib.sha256, 2: hashlib.sha384, 3: hashlib.sha512}
    digest = hmac.new(key.encode(), string_to_sign.encode(), alg_map[timestamp % 4]).digest()
    sig = base64.urlsafe_b64encode(digest).decode()
    return sig, nonce


def device_params(device: dict, market_version_key: str) -> dict:
    """Build the ~40 device params expected by the API (reverse of d.m())."""
    p = {
        "sdk": str(device["sdk"]),
        "os": device["os"],
        "la": device["locale"].split("_")[0],
        "co": device["locale"].split("_")[1] if "_" in device["locale"] else device["locale"],
        "ro": "",
        "marketVersion": device[market_version_key],
        "miuiBigVersionName": device["miuiBigVersionName"],
        "miuiBigVersionCode": device["miuiBigVersionCode"],
        "model": device["model"],
        "device": device["device"],
        "resolution": device["resolution"],
        "densityScaleFactor": device["densityScaleFactor"],
        "lo": device["locale"],
        "romSku": device["romSku"],
        "network": "wifi",
        "cpuArchitecture": device["cpuArchitecture"],
        "deviceType": "1",
        "fromApk": "com.xiaomi.discover",
        "compressAlgo": "1",
        "international": "2",
        "installDay": "1",
        "launchDay": "1",
        "clientFlag": "1",
        "xmsClientId": md5hex("android-xiaomi-terr1-rso2"),
        "xmsVersion": md5hex(""),
        "ARCoreApkVersion": "-1",
        "systemType": "0",
        "carrier": "",
        "instance_id": md5hex(device["androidId"]),
        "isCooperativePhone": "false",
        "supportPatchVer": "3D",
        "autoUpdateSetting": "0",
        "customRegion": "",
        "customCarrier": "",
        "rsa1": "false",
        "rsa3": "",
        "rsa4": "true",
        "mcc": device["mcc"],
        "customCota": device["customCota"],
    }
    return p


def api_post(url: str, params: dict) -> dict:
    timestamp = int(time.time())
    sig, nonce = sign_params(params, timestamp)
    params = dict(params)
    params["timestamp"] = str(timestamp)
    params["nonce"] = nonce
    params["signature"] = sig

    body = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("User-Agent", "4.10.0")
    req.add_header("Host", urllib.parse.urlparse(url).netloc)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def check_updates(device: dict, packages: list[str]) -> list[dict]:
    """Query updateinfo for a batch. Returns listApp entries (only apps with updates)."""
    params = device_params(device, "marketVersionUpdateinfo")
    params["packageName"] = ",".join(packages)
    params["versionCode"] = ",".join(["0"] * len(packages))
    params["invalidSystemPackageHash"] = "null"
    params["filterGA"] = "false"
    data = api_post(API_UPDATEINFO, params)
    return data.get("listApp", [])


def get_download_info(device: dict, app_id: int, package: str) -> dict:
    """Query the minicard/download API for a single app. Returns full payload."""
    params = device_params(device, "marketVersionDownload")
    params["marketVersion"] = device["marketVersionDownload"]
    params["appId"] = str(app_id)
    params["type"] = "app,download"
    params["packageName"] = package
    params["authVersion"] = "1"
    params["ref"] = ""
    params["sourcePackage"] = "com.miui.systemappupdater"
    return api_post(API_DOWNLOAD, params)


def download_file(url: str, dest: str) -> None:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as fh:
        fh.write(resp.read())


def md5_of_file(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# GitHub Releases
# ---------------------------------------------------------------------------

def release_tag(package: str, version_code: int) -> str:
    return f"{package}@{version_code}"


def asset_name(package: str, version_code: int) -> str:
    """Unique asset filename per app-version, e.g. com.mi.app-20250221.apk."""
    pkg = package.replace(":", "_").replace("/", "_")
    return f"{pkg}-v{version_code}.apk"


def gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def release_exists(tag: str) -> bool:
    r = gh("release", "view", tag)
    return r.returncode == 0


def publish_release(tag: str, title: str, notes: str, asset_path: str, asset: str) -> str:
    """Publish a GitHub Release for one app version. Returns the release URL."""
    notes_file = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8")
    notes_file.write(notes)
    notes_file.close()

    if not release_exists(tag):
        r = gh(
            "release", "create", tag,
            "--title", title,
            "--notes-file", notes_file.name,
            "--repo", CATALOG_REPO,
        )
        if r.returncode != 0:
            raise RuntimeError(f"gh release create failed: {r.stderr}")
    else:
        gh("release", "edit", tag, "--notes-file", notes_file.name, "--repo", CATALOG_REPO)

    os.unlink(notes_file.name)

    r = gh("release", "upload", tag, asset_path, "--repo", CATALOG_REPO, "--clobber")
    if r.returncode != 0 and "already exists" not in r.stderr:
        raise RuntimeError(f"gh release upload failed: {r.stderr}")

    return f"https://github.com/{CATALOG_REPO}/releases/tag/{tag}"


# ---------------------------------------------------------------------------
# Catalog handling
# ---------------------------------------------------------------------------

def load_catalog() -> dict:
    if CATALOG_FILE.exists():
        return json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
    return {"updatedAt": None, "apps": {}}


def save_catalog(catalog: dict) -> None:
    catalog["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    CATALOG_FILE.write_text(json.dumps(catalog, indent=2, ensure_ascii=False, sort_keys=True))


def mark_version_as_released(app: dict, version: dict) -> None:
    """Merge a released version into the catalog entry for an app."""
    app["versions"] = app.get("versions", [])
    if not any(v["versionCode"] == version["versionCode"] for v in app["versions"]):
        app["versions"].append(version)
        app["versions"].sort(key=lambda v: v["versionCode"], reverse=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    known_apps = json.loads(KNOWN_APPS_FILE.read_text(encoding="utf-8"))
    devices = json.loads(DEVICES_FILE.read_text(encoding="utf-8"))
    device = devices[0]  # default device config

    # Track only the Xiaomi first-party system apps (like the web UI).
    targets = [a for a in known_apps if a.get("category") == "xiaomi"]
    print(f"[poller] {len(targets)} xiaomi apps tracked, device={device['name']}")

    catalog = load_catalog()
    newly_published = 0

    for i in range(0, len(targets), BATCH_SIZE):
        batch = targets[i : i + BATCH_SIZE]
        packages = [a["packageName"] for a in batch]
        try:
            updates = check_updates(device, packages)
        except Exception as e:  # noqa: BLE001 - keep going on partial failures
            print(f"[poller] batch {i // BATCH_SIZE + 1} FAILED: {e}")
            continue

        for upd in updates:
            pkg = upd.get("packageName")
            vc = upd.get("versionCode")
            if not pkg or not vc:
                continue

            entry = catalog["apps"].setdefault(
                pkg,
                {
                    "packageName": pkg,
                    "displayName": upd.get("displayName", pkg),
                    "category": "xiaomi",
                    "icon": f"{ICON_CDN_HOST}{upd['icon']}" if upd.get("icon") else None,
                    "versions": [],
                },
            )

            if any(v["versionCode"] == vc for v in entry["versions"]):
                print(f"[poller] SKIP {pkg} v{vc} (already published)")
                continue

            try:
                detail = get_download_info(device, upd["appId"], pkg)
            except Exception as e:  # noqa: BLE001
                print(f"[poller] download-info FAILED {pkg}: {e}")
                continue

            apk = detail["download"]["apks"][0]
            apk_url = f"{detail['download']['host']}{apk['url']}"
            expected_md5 = apk.get("hash", "").lower()
            tag = release_tag(pkg, vc)
            asset = asset_name(pkg, vc)

            with tempfile.TemporaryDirectory() as tmp:
                apk_path = os.path.join(tmp, asset)
                print(f"[poller] downloading {pkg} v{vc} ({apk.get('size', 0)} bytes)...")
                try:
                    download_file(apk_url, apk_path)
                except Exception as e:  # noqa: BLE001
                    print(f"[poller] download FAILED {pkg}: {e}")
                    continue

                actual_md5 = md5_of_file(apk_path)
                if expected_md5 and actual_md5 != expected_md5:
                    print(f"[poller] HASH MISMATCH {pkg}: expected={expected_md5} got={actual_md5}")
                    continue

                notes = f"## {upd.get('displayName', pkg)}\n\n- Package: `{pkg}`\n- Version: {upd.get('versionName')} (vc {vc})\n- Size: {apk.get('size', 0):,} bytes\n- MD5: `{actual_md5}`\n\n{upd.get('changeLog') or ''}\n\n---\n\n*Published automatically by the Xiaomi App Catalog poller.*"

                release_url = publish_release(
                    tag,
                    f"{upd.get('displayName', pkg)} v{upd.get('versionName', vc)}",
                    notes,
                    apk_path,
                    asset,
                )

                mark_version_as_released(
                    entry,
                    {
                        "versionCode": vc,
                        "versionName": upd.get("versionName"),
                        "apkSize": apk.get("size", 0),
                        "md5": actual_md5,
                        "releaseUrl": f"https://github.com/{CATALOG_REPO}/releases/download/{tag}/{asset}",
                        "publishedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    },
                )
            newly_published += 1
            print(f"[poller] PUBLISHED {pkg} v{vc} -> {release_url}")

    save_catalog(catalog)
    print(f"[poller] done. {newly_published} new version(s) published, "
          f"{sum(len(a['versions']) for a in catalog['apps'].values())} total listed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())