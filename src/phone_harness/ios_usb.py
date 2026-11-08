"""iPhone over USB: the op vocabulary over WebDriverAgent, from any OS.

No Mac and no jailbreak. The phone runs WebDriverAgent (WDA): Apple's XCTest
UI automation behind a small HTTP server, the same agent Appium uses. go-ios,
a cross-platform stand-in for Xcode's device tooling, starts it over the
cable. Every op is then one HTTP request carried over the cable:

    screen.capture   GET  /screenshot              a real PNG from the phone
    screen.text      GET  /source?format=json      the accessibility tree
    input.*          POST /actions                 W3C touch actions
    input.text       POST /wda/keys                the on-screen keyboard
    nav.home         POST /wda/homescreen
    apps.launch      POST /wda/apps/activate       by bundle id
    apps.current     GET  /wda/activeAppInfo

Unlike iPhone Mirroring this backend has a tree, so screen.text answers
exactly (source: "tree") where the Mac has to recognise glyphs, and ui() and
tap_ui() work. It reaches the phone directly, so nothing on the computer is
covered or focused. Coordinates are iOS points with origin (0, 0) and the
bounds are the screen in points. A screenshot is in pixels, points times the
display scale (2 or 3), so a point read off a screenshot goes through
tap_image_point().

What it cannot do is absent rather than emulated, as on the Mac:

  no `nav.back`      iOS has no system Back button.
  no `nav.recents`   no WDA call opens the app switcher. The gesture that does
                     has not been measured on enough phones to ship.
  no `url.open`      not in this first cut.

What only the user can do, once (setup/ios-usb.md): trust this computer,
turn on Developer Mode, install a WDA build signed with their own Apple ID,
and trust that developer on the phone. A free Apple ID signature lasts seven
days. After that WDA stops launching and the error says so. The harness never
signs, pairs, or types a passcode.

WDA's HTTP API has no authentication, so it never gets a port on this
computer. Each request opens its own connection to WDA's port on the phone
through usbmuxd, Apple's USB multiplexer (what `iproxy` and `ios forward` do
inside), and nothing else on the network can reach it. `ios forward` itself
is not used: it listens on every interface.

The link is up to two long-lived go-ios processes: the iOS 17+ tunnel and the
WDA test run. phone-harness runs every script in a fresh process, so the link
has to outlive it. The first call that needs the phone starts it
(`phone-harness ios-usb start` does the same on purpose) and `phone-harness
ios-usb stop` ends it. The pids live in the run directory. A WDA the user
runs themselves (Appium, iproxy) is reached at `ios_usb.wda = HOST:PORT`
instead, and then the harness starts and stops nothing.
"""
import argparse
import base64
import http.client
import importlib.util
import json
import os
import plistlib
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

from . import config
from . import term
from .transport import Backend, Unsupported

# Apple's Vision OCR is only on a Mac. There it can read a screen the tree
# cannot describe (a game, a canvas); elsewhere the tree is the only reader.
_VISION = importlib.util.find_spec("Vision") is not None

# The test bundle inside every WebDriverAgentRunner build. Re-signing the app
# changes its bundle id, never this name.
XCTEST_CONFIG = "WebDriverAgentRunner.xctest"
WDA_DEVICE_PORT = 8100          # where WDA listens on the phone

_TUNNEL_WAIT = 30               # `tunnel start` to a tunnel go-ios lists
_IMAGE_WAIT = 600               # the first mount downloads the developer image
_START_WAIT = 90                # runwda to a WDA that answers /status
_ROLES = ("tunnel", "wda")

WDA_RELEASES = "https://github.com/appium/WebDriverAgent/releases"
GO_IOS_RELEASES = "https://github.com/danielpaulus/go-ios/releases"
SETUP_DOC = "https://github.com/phone-harness/phone-harness/blob/main/setup/ios-usb.md"

# Names people say -> the bundle ids of Apple's own apps. Third-party apps are
# matched by their display name on the phone instead.
_APPLE_APPS = {
    "settings": "com.apple.Preferences", "safari": "com.apple.mobilesafari",
    "photos": "com.apple.mobileslideshow", "camera": "com.apple.camera",
    "messages": "com.apple.MobileSMS", "mail": "com.apple.mobilemail",
    "notes": "com.apple.mobilenotes", "maps": "com.apple.Maps",
    "weather": "com.apple.weather", "calendar": "com.apple.mobilecal",
    "clock": "com.apple.mobiletimer", "appstore": "com.apple.AppStore",
    "phone": "com.apple.mobilephone", "contacts": "com.apple.MobileAddressBook",
    "music": "com.apple.Music", "reminders": "com.apple.reminders",
    "files": "com.apple.DocumentsApp", "calculator": "com.apple.calculator",
    "health": "com.apple.Health", "wallet": "com.apple.Passbook",
    "facetime": "com.apple.facetime", "shortcuts": "com.apple.shortcuts",
    "findmy": "com.apple.findmy", "podcasts": "com.apple.podcasts",
    "translate": "com.apple.Translate",
}

# input.keys names -> what /wda/keys types. Chords are not something the
# on-screen keyboard can express, so they raise rather than half-work.
_KEYS = {"return": "\n", "enter": "\n", "tab": "\t", "space": " ",
         "delete": "\b", "backspace": "\b"}
_BUTTONS = {"volumeup": "volumeUp", "volumedown": "volumeDown"}

# Element types that only group others. Their label repeats their children's
# ("Wi-Fi, Connected" over "Wi-Fi" and "Connected"), so screen.text takes the
# children's words and keeps the container's only when it has nothing below.
_CONTAINERS = frozenset({
    "Application", "Window", "Other", "Group", "Cell", "Table",
    "CollectionView", "ScrollView", "NavigationBar", "TabBar", "Toolbar",
    "StatusBar", "Sheet", "Alert", "Keyboard", "WebView", "Map",
    "LayoutArea", "LayoutItem",
})
_TAPPABLE = frozenset({
    "Button", "Cell", "Link", "TextField", "SecureTextField", "SearchField",
    "TextView", "Switch", "Toggle", "Slider", "Stepper", "SegmentedControl",
    "Tab", "Icon", "Key", "MenuItem", "PickerWheel",
})


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _major(version):
    try:
        return int(str(version).split(".")[0])
    except ValueError:
        return 0


# --- go-ios -------------------------------------------------------------------

class GoIOSError(RuntimeError):
    """go-ios ran and failed. The message carries its own reason."""


def _go_ios_bin():
    return str(config.get("ios_usb.go_ios"))


def go_ios_path():
    """Where the go-ios binary is, or None."""
    return shutil.which(_go_ios_bin())


def _log_reason(text):
    """The useful part of go-ios's stderr: it logs JSON lines, and the last
    one with an `err` (or at ERROR level) is the reason a command failed."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            return line
        if isinstance(rec, dict) and (rec.get("err") or rec.get("level") in ("ERROR", "FATAL")):
            msg, err = rec.get("msg") or "", rec.get("err") or ""
            return f"{msg}: {err}" if msg and err else (msg or str(err))
    return lines[-1] if lines else ""


def _ios(*args, udid=None, timeout=60):
    """Run go-ios once; -> stdout as JSON when it is JSON, else as text.

    go-ios writes its answer to stdout and JSON log lines to stderr, so a
    failure is reported with the reason from the log, not a traceback."""
    cmd = [_go_ios_bin(), *args] + ([f"--udid={udid}"] if udid else [])
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise GoIOSError(f"go-ios is not installed ({_go_ios_bin()!r} was not found)") from None
    except subprocess.TimeoutExpired:
        raise GoIOSError(f"`ios {' '.join(args)}` did not finish within {timeout}s") from None
    out = r.stdout.decode(errors="replace").strip()
    if r.returncode != 0:
        why = _log_reason(r.stderr.decode(errors="replace")) or out or "no output"
        raise GoIOSError(f"`ios {' '.join(args)}` failed: {why}")
    try:
        return json.loads(out) if out else None
    except ValueError:
        return out


def _info(udid):
    data = _ios("info", udid=udid, timeout=30)
    return data if isinstance(data, dict) else {}


def _apps(udid, include_system=False):
    """[{id, name}] from the phone's installation service."""
    data = _ios("apps", *(["--all"] if include_system else []), udid=udid, timeout=60)
    out = []
    for app in data if isinstance(data, list) else ():
        if isinstance(app, dict) and app.get("CFBundleIdentifier"):
            out.append({"id": app["CFBundleIdentifier"],
                        "name": app.get("CFBundleDisplayName") or app.get("CFBundleName") or ""})
    return out


def _wda_bundles(apps):
    """The installed apps that look like a WebDriverAgent runner. Signing with
    a personal Apple ID renames the bundle, so match its shape, not one id."""
    return sorted(a["id"] for a in apps
                  if "webdriveragent" in a["id"].lower() or a["id"].lower().endswith(".xctrunner"))


def _tunnel_up(udid):
    """True when go-ios already holds an iOS 17+ tunnel to this phone."""
    try:
        rows = _ios("tunnel", "ls", timeout=10)
    except GoIOSError:
        return False
    if not isinstance(rows, list):
        return False
    return any(isinstance(r, dict) and _same_udid(str(r.get("udid") or r.get("Udid") or ""), udid)
               for r in rows)


def developer_mode(udid):
    """True, False, or None when go-ios's answer cannot be read."""
    try:
        out = json.dumps(_ios("devmode", "get", udid=udid, timeout=20)).lower()
    except GoIOSError:
        return None
    if "true" in out:
        return True
    if "false" in out:
        return False
    return None


def usbmux_hint():
    """What to install when Apple's USB service does not answer on this
    computer: port 27015 on Windows, the usbmuxd socket elsewhere."""
    if sys.platform == "win32":
        return ("Windows needs Apple's USB driver to see an iPhone: install the Apple "
                "Devices app (Microsoft Store) or iTunes, open it once, replug the phone.")
    if sys.platform == "darwin":
        return "usbmuxd is part of macOS and did not answer; replug the phone."
    return ("usbmuxd is not running: install it (apt install usbmuxd, or your "
            "distro's package); it starts when a phone is plugged in.")


# Failures go-ios reports while starting WDA, matched by the words in its log,
# and what the user does about each. Order matters: the first match wins.
_KNOWN_FAILURES = (
    (("not been explicitly trusted", "untrusted developer", "invalid code signature",
      "a valid provisioning profile", "0xe8008018", "0xe800801c"),
     "iOS will not run WebDriverAgent: its signature is not trusted or has expired. "
     "On the phone: Settings > General > VPN & Device Management > your Apple ID > "
     "Trust. A free Apple ID signature lasts 7 days; after that, sign and install "
     f"WebDriverAgent again ({SETUP_DOC})."),
    (("developer mode", "developermode"),
     "Developer Mode is off. On the phone: Settings > Privacy & Security > Developer "
     "Mode, turn it on, let the phone restart, and confirm."),
    (("passcode", "device is locked", "devicelocked"),
     "The iPhone is locked. Unlock it, then retry. I never type a passcode."),
    (("27015", "usbmuxd"), usbmux_hint()),
    (("pairing", "not paired", "trust this computer", "invalidhostid"),
     "The iPhone does not trust this computer. Unlock it, replug the cable, and tap "
     "Trust on 'Trust This Computer?'."),
)


def explain(text):
    """What the user does about a go-ios failure, from its own words."""
    low = (text or "").lower()
    for words, advice in _KNOWN_FAILURES:
        if any(w in low for w in words):
            return advice
    reason = _log_reason(text) or "no output"
    return f"go-ios said: {reason[-400:]}"


# --- usbmuxd: a socket to a port on the phone ------------------------------------
#
# Apple's USB multiplexer speaks length-prefixed plists: a 16-byte header
# (total length, version 1, type 8 = plist, tag), then the message. After a
# successful Connect the same socket is a raw byte stream to the phone's port.

class UsbmuxError(OSError):
    """usbmuxd is not reachable, or refused the connection."""


def _usbmux_socket(timeout):
    """A connected socket to usbmuxd: TCP 27015 on Windows (the Apple Mobile
    Device Service), /var/run/usbmuxd elsewhere. USBMUXD_SOCKET_ADDRESS, the
    override libimobiledevice reads, wins: UNIX:/path or HOST:PORT."""
    addr = os.environ.get("USBMUXD_SOCKET_ADDRESS")
    try:
        if addr and not addr.startswith("UNIX:"):
            host, _, port = addr.rpartition(":")
            return socket.create_connection((host or "127.0.0.1", int(port)), timeout=timeout)
        if not addr and sys.platform == "win32":
            return socket.create_connection(("127.0.0.1", 27015), timeout=timeout)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(addr[len("UNIX:"):] if addr else "/var/run/usbmuxd")
        except OSError:
            s.close()
            raise
        return s
    except (OSError, ValueError) as e:
        raise UsbmuxError(f"usbmuxd is not reachable ({e})") from None


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise UsbmuxError("usbmuxd closed the connection")
        buf += chunk
    return buf


def _mux_ask(sock, message):
    body = plistlib.dumps({"ClientVersionString": "phone-harness", "ProgName": "phone-harness",
                           "kLibUSBMuxVersion": 3, **message})
    sock.sendall(struct.pack("<IIII", 16 + len(body), 1, 8, 1) + body)
    length = struct.unpack("<IIII", _recv_exact(sock, 16))[0]
    reply = plistlib.loads(_recv_exact(sock, length - 16))
    return reply if isinstance(reply, dict) else {}


def _same_udid(a, b):
    """UDIDs compare without case or dashes: usbmuxd builds disagree on both."""
    return a.replace("-", "").lower() == b.replace("-", "").lower()


def usbmux_devices(timeout=5):
    """{udid: usbmux device id} for the iPhones on USB. Raises UsbmuxError
    when usbmuxd itself does not answer."""
    s = _usbmux_socket(timeout)
    try:
        reply = _mux_ask(s, {"MessageType": "ListDevices"})
    except (OSError, ValueError) as e:
        raise UsbmuxError(f"usbmuxd did not list devices ({e})") from None
    finally:
        s.close()
    out = {}
    for d in reply.get("DeviceList") or ():
        props = d.get("Properties") or {}
        if props.get("ConnectionType", "USB") == "USB" and props.get("SerialNumber"):
            out[props["SerialNumber"]] = d.get("DeviceID")
    return out


def usbmux_connect(device_id, port, timeout):
    """A socket to `port` on the phone. Raises UsbmuxError when nothing
    listens there, which usbmuxd answers at once."""
    s = _usbmux_socket(timeout)
    try:
        reply = _mux_ask(s, {"MessageType": "Connect", "DeviceID": device_id,
                             "PortNumber": socket.htons(port)})
        if reply.get("Number") != 0:
            raise UsbmuxError(f"nothing answers on port {port} of the iPhone "
                              f"(usbmuxd result {reply.get('Number')})")
        return s
    except BaseException:
        s.close()
        raise


# --- the link: processes that outlive one script --------------------------------

def _state_path():
    return config.run_dir() / "ios-usb.json"


def log_path(role):
    return config.run_dir() / f"ios-usb-{role}.log"


def read_link():
    """The running link's record, or None."""
    try:
        data = json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_link(data):
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=p.name, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, p)


def _clear_link():
    try:
        _state_path().unlink()
    except OSError:
        pass


def _alive(pid):
    """Whether `pid` is a running process. Not os.kill(pid, 0) on Windows:
    there that call terminates the process instead of probing it."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(k.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _process_name(pid):
    """The executable's file name, or None when it cannot be read."""
    if sys.platform == "win32":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)
        if not h:
            return None
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(len(buf))
            if not k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return None
            return os.path.basename(buf.value)
        finally:
            k.CloseHandle(h)
    try:
        out = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return os.path.basename(out) or None


def _ours(pid):
    """A recorded pid that is still go-ios. Pids are reused, and a link that
    died days ago must not take an unrelated process down with it."""
    if not _alive(pid):
        return False
    name = _process_name(pid)
    want = os.path.basename(_go_ios_bin())
    return bool(name) and os.path.splitext(name)[0].lower() == os.path.splitext(want)[0].lower()


def _spawn(role, args):
    """Start one go-ios process detached from this one, its output in a log.
    It has to survive the script that started it."""
    log = log_path(role)
    log.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
    else:
        kw = {"start_new_session": True}
    with open(log, "wb") as f:
        return subprocess.Popen([_go_ios_bin(), *args], stdin=subprocess.DEVNULL,
                                stdout=f, stderr=subprocess.STDOUT, **kw)


def _log_tail(role, limit=4000):
    try:
        data = log_path(role).read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode(errors="replace")


def _wait(ready, timeout, proc, role, what):
    """Poll `ready` until it is true. A process that exits first fails at
    once with its own reason instead of sitting out the timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ready():
            return
        if proc.poll() is not None:
            raise RuntimeError(f"{what} stopped. {explain(_log_tail(role))}")
        time.sleep(0.5)
    raise RuntimeError(f"{what} did not come up within {timeout}s. {explain(_log_tail(role))}")


def external_wda():
    """HOST:PORT of a WebDriverAgent the user runs themselves, or None for
    the link this module manages over USB (ios_usb.wda = "usb", the default)."""
    target = str(config.get("ios_usb.wda") or "usb").strip()
    return None if target.lower() == "usb" else target


def connect_wda(udid=None):
    """A WDA client: to ios_usb.wda when that is an address, else over
    usbmuxd to the link's phone, ios_usb.udid, or the only iPhone on USB.
    Nothing is reached until a request is made."""
    addr = external_wda()
    if addr:
        host, _, port = addr.rpartition(":")
        return WDA(lambda timeout: socket.create_connection((host or "127.0.0.1", int(port)),
                                                             timeout=timeout), addr)

    def opener(timeout):
        want = udid or (read_link() or {}).get("udid") or config.get("ios_usb.udid") or None
        devices = usbmux_devices(timeout)
        if want:
            found = [dev for u, dev in devices.items() if _same_udid(u, want)]
        else:
            found = list(devices.values()) if len(devices) == 1 else []
        if not found:
            raise UsbmuxError(f"the iPhone {want} is not on USB" if want else
                              f"{len(devices)} iPhones on USB, so none is the obvious one")
        return usbmux_connect(found[0], WDA_DEVICE_PORT, timeout)

    return WDA(opener, "the iPhone over USB")


def pick_udid(udid=None):
    """The one iPhone to use: an explicit one, else ios_usb.udid, else the
    only phone plugged in. A configured phone that is not attached is a
    failure, not a cue to use a different one."""
    udid = udid or config.get("ios_usb.udid") or None
    try:
        attached = list(usbmux_devices())
    except UsbmuxError:
        raise RuntimeError(usbmux_hint()) from None
    if udid:
        match = [u for u in attached if _same_udid(u, udid)]
        if not match:
            raise RuntimeError(f"The iPhone {udid} (ios_usb.udid) is not attached over USB. "
                               "This did not pick another phone.")
        return match[0]
    if not attached:
        raise RuntimeError(_STATE_HELP["no-device"])
    if len(attached) > 1:
        raise RuntimeError("Several iPhones are attached (" + ", ".join(attached) + "). "
                           "Choose one: `phone-harness config set ios_usb.udid UDID`.")
    return attached[0]


def pick_bundle(udid):
    """The WebDriverAgent runner to start: ios_usb.wda_bundle, else the one
    installed runner."""
    want = config.get("ios_usb.wda_bundle")
    if want:
        return want
    found = _wda_bundles(_apps(udid))
    if not found:
        raise RuntimeError(_NO_WDA)
    if len(found) > 1:
        raise RuntimeError("Several WebDriverAgent builds are installed (" + ", ".join(found) +
                           "). Choose one: `phone-harness config set ios_usb.wda_bundle ID`.")
    return found[0]


def start_link(udid=None, say=None):
    """Bring WDA up on the phone; -> the link record.

    Idempotent: a WDA that already answers is reused, whoever started it.
    Leftovers of a link that died are cleared first, since none of them can
    be reused. Each step that fails raises with what the user does next."""
    say = say or (lambda msg: print(term.note(msg, sys.stderr), file=sys.stderr, flush=True))
    addr = external_wda()
    if connect_wda(udid).status() is not None:
        return read_link() or {"wda": addr or "usb"}
    if addr:
        raise RuntimeError(f"Nothing answers at {addr} (ios_usb.wda). Start WebDriverAgent "
                           "there, or `phone-harness config unset ios_usb.wda` to let "
                           "phone-harness start it over USB.")
    stop_link()
    if not go_ios_path():
        raise RuntimeError(_STATE_HELP["no-go-ios"])
    udid = pick_udid(udid)
    try:
        info = _info(udid)
    except GoIOSError as e:
        raise RuntimeError(explain(str(e))) from None
    name, version = info.get("DeviceName") or udid, str(info.get("ProductVersion") or "")
    link = {"udid": udid, "name": name, "ios": version, "pids": {}, "started": _now()}
    procs = {}
    try:
        if _major(version) >= 17 and not _tunnel_up(udid):
            say(f"opening the iOS {version} tunnel to {name}...")
            pairs = config.state_dir() / "go-ios"
            pairs.mkdir(parents=True, exist_ok=True)
            procs["tunnel"] = _spawn("tunnel", ["tunnel", "start", "--userspace",
                                                f"--pair-record-path={pairs}", f"--udid={udid}"])
            link["pids"]["tunnel"] = procs["tunnel"].pid
            _write_link(link)
            _wait(lambda: _tunnel_up(udid), _TUNNEL_WAIT, procs["tunnel"], "tunnel",
                  "The iOS 17 tunnel")
        say("mounting the developer disk image (the first time downloads it)...")
        images = config.cache_dir() / "devimages"
        images.mkdir(parents=True, exist_ok=True)
        try:
            _ios("image", "auto", f"--basedir={images}", udid=udid, timeout=_IMAGE_WAIT)
        except GoIOSError as e:
            raise RuntimeError(f"The developer disk image did not mount. {explain(str(e))}") from None
        bundle = pick_bundle(udid)
        link["bundle"] = bundle
        say(f"starting WebDriverAgent ({bundle}) on {name}...")
        procs["wda"] = _spawn("wda", ["runwda", f"--bundleid={bundle}",
                                      f"--testrunnerbundleid={bundle}",
                                      f"--xctestconfig={XCTEST_CONFIG}", f"--udid={udid}"])
        link["pids"]["wda"] = procs["wda"].pid
        _write_link(link)
        wda = connect_wda(udid)
        _wait(lambda: wda.status() is not None, _START_WAIT, procs["wda"], "wda",
              "WebDriverAgent")
    except BaseException:
        for proc in procs.values():
            if proc.poll() is None:
                proc.terminate()
        _clear_link()
        raise
    return link


def stop_link():
    """Stop what start_link started, newest first; -> the roles stopped.
    A tunnel this computer already had is not ours and stays up."""
    link = read_link()
    if not link:
        return []
    stopped = []
    for role in reversed(_ROLES):
        pid = (link.get("pids") or {}).get(role)
        if not _ours(pid):
            continue
        try:
            os.kill(pid, signal.SIGTERM)             # TerminateProcess on Windows
        except OSError:
            continue
        for _ in range(20):
            if not _alive(pid):
                break
            time.sleep(0.1)
        stopped.append(role)
    _clear_link()
    return stopped


# --- WebDriverAgent ---------------------------------------------------------------

class LinkDown(RuntimeError):
    """Nothing answers where WDA should be: the link is not running."""


class WDAError(RuntimeError):
    """WDA answered with an error. `kind` is its W3C error code."""

    def __init__(self, message, kind=""):
        super().__init__(message)
        self.kind = kind


class _Connection(http.client.HTTPConnection):
    """HTTP over a socket the opener provides (usbmuxd, or plain TCP)."""

    def __init__(self, opener, timeout):
        super().__init__("127.0.0.1", WDA_DEVICE_PORT, timeout=timeout)
        self._opener = opener

    def connect(self):
        self.sock = self._opener(self.timeout)


class WDA:
    """A WebDriverAgent client. `opener(timeout)` returns a connected socket
    to WDA; `where` names it in errors. Standard library only, and no proxy
    from the environment ever sees a request."""

    def __init__(self, opener, where):
        self._opener = opener
        self.where = where
        self.sid = None

    def call(self, method, path, body=None, timeout=30):
        """One request; -> the decoded JSON body. Raises LinkDown or WDAError."""
        if body is None and method == "POST":
            body = {}
        data = json.dumps(body).encode() if body is not None else None
        conn = _Connection(self._opener, timeout)
        try:
            conn.request(method, path, body=data,
                         headers={"Content-Type": "application/json"} if data else {})
            r = conn.getresponse()
            code, raw = r.status, r.read()
        except (http.client.HTTPException, OSError) as e:
            raise LinkDown(f"WebDriverAgent is not answering on {self.where}: {e}") from None
        finally:
            conn.close()
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            raise WDAError(f"WebDriverAgent answered HTTP {code} with something that is not "
                           f"JSON to {method} {path}") from None
        value = payload.get("value") if isinstance(payload, dict) else None
        # Older builds answer 200 carrying an error, newer ones 4xx/5xx.
        if isinstance(value, dict) and (value.get("error") or (code >= 400 and value.get("message"))):
            raise WDAError(value.get("message") or value.get("error"), value.get("error") or "")
        if code >= 400:
            raise WDAError(f"WebDriverAgent answered HTTP {code} to {method} {path}")
        return payload if isinstance(payload, dict) else {"value": payload}

    def status(self, timeout=2):
        """/status, or None when nothing (or no WDA) answers."""
        if not self.listening():
            return None
        try:
            return self.call("GET", "/status", timeout=timeout)
        except (LinkDown, WDAError):
            return None

    def listening(self):
        """Whether anything accepts a connection. usbmuxd refuses at once
        when nothing listens on the phone. The short timeout matters on
        Windows, which takes about 2s to refuse a closed local port."""
        try:
            self._opener(0.5).close()
            return True
        except OSError:
            return False

    def session(self, fresh=False):
        """The session id. WDA keeps one; it is reused across scripts so each
        run does not pay for a new one."""
        if self.sid and not fresh:
            return self.sid
        sid = None if fresh else (self.status(timeout=5) or {}).get("sessionId")
        if not sid:
            payload = self.call("POST", "/session",
                                {"capabilities": {"alwaysMatch": {}, "firstMatch": [{}]}},
                                timeout=60)
            sid = payload.get("sessionId") or (payload.get("value") or {}).get("sessionId")
        if not sid:
            raise WDAError("WebDriverAgent did not open a session")
        self.sid = sid
        return sid

    def s(self, method, path, body=None, timeout=30):
        """A request inside the session; -> its value. A session WDA dropped
        (it does when the runner restarts) is replaced once."""
        for attempt in (0, 1):
            sid = self.session(fresh=bool(attempt))
            try:
                return self.call(method, f"/session/{sid}{path}", body, timeout).get("value")
            except WDAError as e:
                if attempt or "session" not in f"{e.kind} {e}".lower():
                    raise
                self.sid = None


# --- the tree -----------------------------------------------------------------------

def _text(v):
    if v is None or isinstance(v, (dict, list)):
        return ""
    return str(v).strip()


def _flag(v, default):
    """WDA's JSON source writes booleans as "1"/"0"; newer builds as true/false."""
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes")


def flatten(root, width, height):
    """WDA's JSON source -> [{text, desc, id, class, clickable, x, y, w, h}]
    in document order, the Android node shape, so find_nodes() and tap_ui()
    work unchanged. Only elements with a size and some part on screen.

    text is the accessibility label, else the value (a field's contents);
    desc is the value when both exist; id is the accessibility identifier
    when it is not just the label again. `_says` marks the boxes screen.text
    reads: the innermost words, so a cell does not repeat its own rows."""
    nodes = []

    def walk(el):
        if not isinstance(el, dict):
            return False
        kind = str(el.get("type") or "")
        short = kind[len("XCUIElementType"):] if kind.startswith("XCUIElementType") else kind
        r = el.get("rect") if isinstance(el.get("rect"), dict) else {}
        try:
            x, y, w, h = (float(r.get(k) or 0) for k in ("x", "y", "width", "height"))
        except (TypeError, ValueError):
            x = y = w = h = 0.0
        label, value, name = _text(el.get("label")), _text(el.get("value")), _text(el.get("name"))
        node = None
        if (w > 0 and h > 0 and _flag(el.get("isVisible"), True)
                and x < width and y < height and x + w > 0 and y + h > 0):
            node = {"text": label or value,
                    "desc": value if label and value and value != label else "",
                    "id": name if name not in (label, value) else "",
                    "class": kind,
                    "clickable": short in _TAPPABLE and _flag(el.get("isEnabled"), True),
                    "x": round(x + w / 2), "y": round(y + h / 2),
                    "w": round(w), "h": round(h), "_says": False}
            nodes.append(node)
        below = False
        for child in el.get("children") or ():
            below = walk(child) or below
        if node is not None and node["text"] and not (short in _CONTAINERS and below):
            node["_says"] = True
        return below or bool(node and node["_says"])

    walk(root)
    return nodes


# --- the backend ----------------------------------------------------------------------

_STATE_HELP = {
    "no-go-ios": ("go-ios is not installed, so this computer cannot reach an iPhone over "
                  f"USB. Install it (`npm install -g go-ios`, or a release from "
                  f"{GO_IOS_RELEASES}) or set ios_usb.go_ios to the binary, then retry."),
    "no-device": ("No iPhone is attached over USB. Plug it in, unlock it, and tap Trust "
                  "on 'Trust This Computer?', then retry."),
    "not-started": ("WebDriverAgent is not running on the iPhone. Start it with "
                    "`phone-harness ios-usb start`, then retry."),
}

_NO_WDA = ("WebDriverAgent is not installed on this iPhone. Download "
           f"WebDriverAgentRunner-Runner.zip from {WDA_RELEASES}, sign it with your Apple "
           f"ID and install it; no Mac is needed ({SETUP_DOC}). If it is installed under a "
           "bundle id without 'WebDriverAgent' in it: "
           "`phone-harness config set ios_usb.wda_bundle ID`.")

_LOCKED = ("The iPhone is locked. Unlock it, then retry. I never type a passcode. "
           "It locks again after its Auto-Lock time (Settings > Display & Brightness "
           "> Auto-Lock).")

_LINK_LOST = ("The connection to WebDriverAgent dropped and did not come back. Is the "
              "cable still in? `phone-harness ios-usb` shows what is attached; "
              "`phone-harness doctor ios-usb` walks the setup.")


class IPhoneUSB(Backend):
    name = "iphone-usb"

    def __init__(self, udid=None):
        self._udid = udid
        self._wda = None
        self._gate_at = 0.0

    # --- reaching WDA -------------------------------------------------------

    def _up(self):
        """A client for a WDA that answers right now, or None. Never starts
        anything: the read-only ops ask this."""
        wda = self._wda or connect_wda(self._udid)
        if wda.status(timeout=2) is None:
            return None
        self._wda = wda
        return wda

    def _client(self):
        if self._wda is None and self._up() is None:
            self._session_require()               # starts the link, or raises the physical step
        return self._wda

    def _w(self, method, path, body=None, timeout=30, session=True):
        """One WDA request. If the link dropped (cable out, runner killed)
        it is brought up again once before giving up."""
        for attempt in (0, 1):
            wda = self._client()
            try:
                if session:
                    return wda.s(method, path, body, timeout)
                return wda.call(method, path, body, timeout).get("value")
            except LinkDown:
                self._wda = None
                if attempt:
                    raise RuntimeError(_LINK_LOST) from None

    def _udid_now(self):
        return (read_link() or {}).get("udid") or self._udid or pick_udid()

    # --- unlocked? ----------------------------------------------------------

    def _gate(self):
        """Refuse to drive a locked phone. Unlocking is the user's; a
        passcode is never typed for them. Cached for 2s so a burst of taps
        checks once."""
        if time.time() - self._gate_at < 2.0:
            return
        if self._w("GET", "/wda/locked", session=False):
            raise RuntimeError(_LOCKED)
        self._gate_at = time.time()

    # --- screen -------------------------------------------------------------

    def _screen_bounds(self):
        """The screen in points, or None when WDA is not up. Read each time:
        the phone may have rotated since the last call."""
        wda = self._up()
        if wda is None:
            return None
        size = wda.s("GET", "/window/size") or {}
        if not size.get("width") or not size.get("height"):
            return None
        return {"x": 0, "y": 0, "w": round(size["width"]), "h": round(size["height"]),
                "id": (read_link() or {}).get("udid") or self._udid}

    def _screen_require(self):
        b = self._screen_bounds()
        return b if b is not None else self._session_require()

    def _screen_capture(self, path=None):
        """Works while the phone is locked, so the user can be shown why."""
        bounds = self._screen_require()
        png = base64.b64decode(self._w("GET", "/screenshot", session=False, timeout=60))
        if path is None:
            fd, path = tempfile.mkstemp(prefix="phone-iphone-usb-", suffix=".png")
            os.close(fd)
        with open(path, "wb") as f:
            f.write(png)
        return path, bounds

    def _walk(self):
        b = self._screen_require()
        raw = self._w("GET", "/source?format=json", timeout=60)
        if isinstance(raw, str):
            raw = json.loads(raw)
        return flatten(raw, b["w"], b["h"])

    def _screen_text(self, min_confidence=0.3):
        """From the accessibility tree: exact strings, exact boxes, already
        in tap coordinates. A screen WDA cannot snapshot (it times out on
        some busy views) is read from pixels on a Mac, else it raises."""
        self._gate()
        try:
            nodes = self._walk()
        except WDAError as e:
            if _VISION:
                return self._pixel_text(min_confidence)
            raise RuntimeError(f"the accessibility tree is not available on this screen "
                               f"({e}). Read it from screenshot() instead.") from None
        out, seen = [], set()
        for n in nodes:
            key = (n["text"], n["x"], n["y"])
            if n["_says"] and key not in seen:
                seen.add(key)
                out.append({"text": n["text"], "confidence": 1.0, "source": "tree",
                            "x": n["x"], "y": n["y"], "w": n["w"], "h": n["h"]})
        return out

    def _pixel_text(self, min_confidence=0.3):
        """Vision OCR over a screenshot, scaled to points."""
        from . import ocr as _vision
        path, bounds = self._screen_capture()
        try:
            rows = _vision.recognize(path, {"x": 0, "y": 0, "w": bounds["w"], "h": bounds["h"]})
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        return [{**r, "source": "pixels"} for r in rows if r["confidence"] >= min_confidence]

    if _VISION:
        def _screen_text_pixels(self, min_confidence=0.3):
            """Force pixel OCR: games, canvases, views with no accessibility."""
            self._gate()
            return self._pixel_text(min_confidence)

    # --- input --------------------------------------------------------------

    def _touch(self, *steps):
        """One finger, as W3C actions."""
        self._w("POST", "/actions", {"actions": [{
            "type": "pointer", "id": "finger1", "parameters": {"pointerType": "touch"},
            "actions": list(steps)}]}, timeout=60)

    def _input_tap(self, x, y):
        self._gate()
        self._touch(_move(x, y), _DOWN, _pause(50), _UP)

    def _input_press(self, x, y, duration=0.8):
        self._gate()
        self._touch(_move(x, y), _DOWN, _pause(duration * 1000), _UP)

    def _input_drag(self, x1, y1, x2, y2, duration=0.35, steps=14):
        """The finger lifts while it is still moving, so a short fast drag is
        a flick with momentum, as on glass. `steps` is accepted and unused:
        WDA interpolates the move itself."""
        self._gate()
        self._touch(_move(x1, y1), _DOWN, _move(x2, y2, max(10, duration * 1000)), _UP)

    def _input_scroll(self, x, y, dy, dx=0, steps=6):
        """A finger drag standing in for a wheel, in content space: +dy
        reveals what is above, so the finger travels +dy points down.

        It must not fling, or a walk through a list skips rows without any
        error: move slowly, hold still, then lift. The gesture is centred on
        (x, y) and kept off the edges, where iOS reads a swipe from the top
        as Notification Center and from the bottom as Home."""
        self._gate()
        b = self._screen_bounds() or {}
        w, h = b.get("w", 0), b.get("h", 0)
        x1, y1, x2, y2 = x - dx / 2, y - dy / 2, x + dx / 2, y + dy / 2
        if w and h:
            keep = lambda v, size, lo: min(max(v, size * lo), size * (1 - lo))
            x1, x2 = keep(x1, w, 0.06), keep(x2, w, 0.06)
            y1, y2 = keep(y1, h, 0.1), keep(y2, h, 0.1)
        self._touch(_move(x1, y1), _DOWN, _pause(50),
                    _move(x2, y2, max(300, int(steps) * 50)), _pause(250), _UP)

    def _input_keys(self, combo):
        self._gate()
        parts = [p.strip().lower() for p in combo.split("+")]
        if len(parts) > 1:
            raise Unsupported(f"iphone-usb cannot press chords ({combo!r}); "
                              "the on-screen keyboard takes single keys")
        k = parts[0]
        if k == "home":
            return self._nav_home()
        if k in _BUTTONS:
            return self._w("POST", "/wda/pressButton", {"name": _BUTTONS[k]})
        ch = _KEYS.get(k) or (combo.strip() if len(combo.strip()) == 1 else None)
        if ch is None:
            raise Unsupported(f"iphone-usb has no key named {combo!r}")
        self._w("POST", "/wda/keys", {"value": [ch]})

    def _input_text(self, s, delay=0.03, keystrokes=False):
        """Typed on the on-screen keyboard into the focused field. There is
        no clipboard shared with this computer, so this is always the
        keystroke path, whatever `keystrokes` says: iOS autocorrect can still
        rewrite a word as it lands. Any Unicode works. `delay` is unused."""
        self._gate()
        if s:
            self._w("POST", "/wda/keys", {"value": list(s)},
                    timeout=max(60, 0.2 * len(s) + 30))

    # --- navigation ---------------------------------------------------------

    def _nav_home(self):
        self._w("POST", "/wda/homescreen", session=False)
        time.sleep(0.5)

    # No _nav_back: iOS has no system Back button. No _nav_recents: see the
    # module docstring.

    # --- apps ---------------------------------------------------------------

    def _resolve_app(self, name):
        """A bundle id, or the name on the Home Screen. Apple's own apps are
        known by name; anything else is looked up on the phone: an exact
        bundle id, then the display name, then a fragment of an id."""
        want = _norm(name)
        if want in _APPLE_APPS:
            return _APPLE_APPS[want]
        apps = _apps(self._udid_now(), include_system=True)
        ids = [a["id"] for a in apps]
        if name in ids:
            return name
        named = [a["id"] for a in apps if want and _norm(a["name"]) == want]
        if named:
            return named[0]
        hits = sorted((i for i in ids if want and want in _norm(i)), key=len)
        if hits:
            return hits[0]
        theirs = sorted({a["name"] or a["id"] for a in apps if not a["id"].startswith("com.apple.")})
        raise RuntimeError(f"no installed app matches {name!r}. Installed: "
                           f"{', '.join(theirs) or 'no third-party apps'}. "
                           "Pass one of these names or a bundle id.")

    def _apps_launch(self, name):
        """Brings the app to the front, launching it if it is not running.
        Returns the bundle id."""
        self._gate()
        bundle = self._resolve_app(name)
        self._w("POST", "/wda/apps/activate", {"bundleId": bundle}, timeout=60)
        return bundle

    def _apps_current(self):
        info = self._w("GET", "/wda/activeAppInfo", session=False) or {}
        return info.get("bundleId") or None

    def _apps_list(self, include_system=False):
        return sorted(a["id"] for a in _apps(self._udid_now(), include_system=include_system))

    # --- session ------------------------------------------------------------

    def _session_state(self):
        """'ready' | 'locked' | 'not-started' | 'no-device' | 'no-usbmux' | 'no-go-ios'.

        'not-started' means a phone is attached and WDA is not running on it
        yet; the first session.require starts it."""
        wda = self._up()
        if wda is not None:
            try:
                return "locked" if wda.call("GET", "/wda/locked").get("value") else "ready"
            except (LinkDown, WDAError):
                return "ready"
        if external_wda():
            return "not-started"                  # require says nothing answers there
        if not go_ios_path():
            return "no-go-ios"
        try:
            return "not-started" if usbmux_devices() else "no-device"
        except UsbmuxError:
            return "no-usbmux"

    def _session_detail(self):
        parts = []
        try:
            parts.append("on USB: " + (", ".join(usbmux_devices()) or "nothing"))
        except UsbmuxError as e:
            parts.append(str(e))
        link, addr = read_link(), external_wda()
        up = self._up() is not None
        if addr:
            parts.append(f"WebDriverAgent at {addr} (ios_usb.wda): {'up' if up else 'down'}")
        elif link:
            parts.append(f"WebDriverAgent {link.get('bundle') or '?'} on {link.get('name')}: "
                         f"{'up' if up else 'down'}")
        return "; ".join(parts)

    def _session_require(self):
        """Bounds once WDA answers, else raise telling the USER what to do.
        Starts the link when a phone is attached and nothing runs yet."""
        state = self._session_state()
        if state == "not-started":
            if not config.get("ios_usb.autostart"):
                raise RuntimeError(_STATE_HELP["not-started"])
            start_link(udid=self._udid)
            self._wda = None
            state = self._session_state()
        if state == "ready":
            b = self._screen_bounds()
            if b is not None:
                return b
            raise RuntimeError("WebDriverAgent answered but gave no screen size; retry in a moment.")
        if state == "locked":
            self._gate_at = 0.0
            raise RuntimeError(_LOCKED)
        if state == "no-usbmux":
            raise RuntimeError(usbmux_hint())
        raise RuntimeError(_STATE_HELP.get(state, _STATE_HELP["no-device"]))

    def _session_refocus(self):
        return None                    # nothing on this computer needs focus

    # --- interruption -------------------------------------------------------

    def _focus_probe(self):
        return (True,)                 # the phone is reached directly; no window

    def _focus_diff(self, before, after):
        return {"raised": False, "stole_focus": False}

    # --- tree / raw ---------------------------------------------------------

    def _tree(self):
        """[{text, desc, id, class, clickable, x, y, w, h}] in points, from
        WDA's snapshot of the foreground app. class is the XCUIElementType."""
        self._gate()
        return [{k: v for k, v in n.items() if k != "_says"} for n in self._walk()]

    def _raw(self, cmd, binary=False, timeout=60):
        """One WebDriverAgent request: "GET /status", or
        'POST /session/{session}/wda/pressButton {"name": "volumeUp"}'.
        {session} becomes the current session id. -> the response's value."""
        method, _, rest = cmd.strip().partition(" ")
        path, _, body = rest.strip().partition(" ")
        if not path.startswith("/"):
            raise ValueError('shell() here takes one WebDriverAgent request: "GET /status"')
        wda = self._client()
        if "{session}" in path:
            path = path.replace("{session}", wda.session())
        return wda.call(method.upper(), path, json.loads(body) if body.strip() else None,
                        timeout=timeout).get("value")


def _move(x, y, ms=0):
    return {"type": "pointerMove", "duration": int(ms), "x": round(x), "y": round(y),
            "origin": "viewport"}


def _pause(ms):
    return {"type": "pause", "duration": int(ms)}


_DOWN = {"type": "pointerDown", "button": 0}
_UP = {"type": "pointerUp", "button": 0}


# --- CLI (phone-harness ios-usb ...) -------------------------------------------------

CLI_USAGE = """Usage:
  phone-harness ios-usb          the iPhones on USB, and whether WebDriverAgent is up
  phone-harness ios-usb start    start WebDriverAgent on the iPhone
                                 (the first helper call does this by itself)
  phone-harness ios-usb stop     stop WebDriverAgent (and the iOS 17+ tunnel it opened)
"""


def _parsers():
    def make(name, description):
        return term.Parser(prog=f"phone-harness ios-usb {name}",
                           usage=f"phone-harness ios-usb {name}",
                           description=description,
                           formatter_class=argparse.RawDescriptionHelpFormatter)
    return {"start": make("start", "Start WebDriverAgent on the attached iPhone. Idempotent: "
                                   "one that already answers is reused."),
            "stop": make("stop", "Stop WebDriverAgent, and the iOS 17+ tunnel if "
                                 "phone-harness opened it.")}


def cli(args):
    try:
        return _cli(list(args))
    except SystemExit as e:
        if isinstance(e.code, str):
            print(term.err(e.code), file=sys.stderr)
            return 1
        raise


def _status():
    path = go_ios_path()
    print(f"go-ios: {path or 'not found (' + _go_ios_bin() + ')'}")
    try:
        attached = list(usbmux_devices())
    except UsbmuxError:
        print(term.err(usbmux_hint(), sys.stdout))
        return 1
    rows = []
    for udid in attached:
        try:
            info = _info(udid) if path else {}
            rows.append(f"{udid} ({info.get('DeviceName') or '?'}, "
                        f"iOS {info.get('ProductVersion') or '?'})")
        except GoIOSError:
            rows.append(f"{udid} (not trusted yet?)")
    print("on USB: " + (", ".join(rows) or "nothing"))
    link, addr = read_link(), external_wda()
    up = connect_wda().status() is not None
    if addr:
        print(f"WebDriverAgent: {'up' if up else 'not answering'} at {addr} (ios_usb.wda; "
              "phone-harness starts and stops nothing there)")
    elif up and link:
        print(f"WebDriverAgent: up - {link.get('bundle')} on {link.get('name')}, "
              f"since {link.get('started', '?')[:16]}")
    elif up:
        print("WebDriverAgent: up (started outside phone-harness)")
    else:
        print("WebDriverAgent: off - the first helper call starts it, or `phone-harness ios-usb start`")
    if not path:
        print(_STATE_HELP["no-go-ios"])
        return 1
    return 0


def _cli(args):
    if args[:1] in (["-h"], ["--help"]):
        print(CLI_USAGE)
        return 0
    cmd = args[0] if args else None
    if cmd is not None:
        parser = _parsers().get(cmd)
        if parser is None:
            print(CLI_USAGE)
            return 2
        term.reject_unknown_flags(parser, args[1:])
    if cmd is None:
        return _status()
    if cmd == "start":
        try:
            link = start_link()
        except RuntimeError as e:
            print(term.err(str(e), sys.stdout))
            return 1
        print("ready: WebDriverAgent is up"
              + (f" on {link['name']}" if link.get("name") else ""))
        if config.get("platform") != "ios-usb":
            print(term.note("tip: `phone-harness config set platform ios-usb` makes this iPhone "
                            "the default, so nothing needs PHONE_HARNESS_PLATFORM=ios-usb"))
        return 0
    stopped = stop_link()
    print("stopped: " + ", ".join(stopped) if stopped else "nothing was running")
    return 0
