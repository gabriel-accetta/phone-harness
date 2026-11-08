"""Diagnostics: `phone-harness --doctor` walks the ladder for the phone the
helpers would drive — the config default, or `--doctor ios|ios-usb|android`."""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _check(label, ok, hint="", fatal=True):
    """Print a check; a fatal failure is remembered for the verdict."""
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {hint}" if not ok and hint else ""))
    if not ok and fatal:
        _failures.append(label)
    return ok


_failures = []


def run_doctor(platform=None):
    from . import config
    platform = (platform or config.get("platform")).lower()
    print(f"phone-harness doctor — {platform}"
          f"{'' if platform == config.get('platform') else '  (default is ' + config.get('platform') + ')'}\n")
    _failures.clear()
    if platform == "android":
        _doctor_android()
    elif platform in ("ios-usb", "iphone-usb"):
        _doctor_ios_usb()
    else:
        _doctor_ios()
    ok = not _failures
    print("\nall clear" if ok else "\nfix the FAILs above, then re-run")
    if not ok:
        print(CLOUD_HINT)
    return 0 if ok else 1


CLOUD_HINT = ("\nNo working phone here? A cloud phone needs no hardware: "
              "`phone-harness cloud login`, `phone-harness cloud ls`, then "
              "`phone-harness cloud start PHONE` (a cloud iPhone needs no adb; "
              "a cloud Android needs adb). "
              "No install possible? Use the HTTPS API: "
              "https://github.com/phone-harness/phone-harness/blob/main/setup/api.md")


def _host_app():
    """The app macOS asks about: the outermost .app this process runs under
    (a terminal, or an agent's own app). Falls back to a description."""
    found, pid = None, os.getppid()
    for _ in range(40):
        if pid <= 1:
            break
        try:
            out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
            ppid, comm = out.split(None, 1)
        except (OSError, ValueError, subprocess.SubprocessError):
            break
        if ".app/" in comm:
            found = comm.split(".app/")[0].rsplit("/", 1)[-1]
        pid = int(ppid)
    return f'"{found}"' if found else "the app that runs this command (your terminal or agent app)"


# --- iPhone: pyobjc -> permissions -> Mirroring -> capture -> OCR -----------

def _doctor_ios():
    try:
        import Quartz, Vision, AppKit  # noqa: F401
        _check("pyobjc frameworks (Quartz, Vision, AppKit)", True)
    except ImportError as e:
        _check("pyobjc frameworks", False,
               f"pip install pyobjc-framework-Quartz pyobjc-framework-Vision "
               f"pyobjc-framework-Cocoa ({e})")
        return

    from ApplicationServices import AXIsProcessTrusted
    host = _host_app()
    _check("Accessibility permission (taps & keystrokes)", AXIsProcessTrusted(),
           f"System Settings > Privacy & Security > Accessibility: turn on {host}")

    import Quartz as Q
    _check("Screen Recording permission (seeing the phone)",
           bool(Q.CGPreflightScreenCaptureAccess()),
           f"System Settings > Privacy & Security > Screen Recording: turn on {host}, "
           f"then quit and reopen it")

    from . import mirror
    _check(f"{mirror.APP_NAME} installed", Path(mirror.APP_PATH).exists(),
           "requires macOS Sequoia+ with a paired iPhone")

    running = mirror.running_app() is not None
    _check(f"{mirror.APP_NAME} running", running,
           "will auto-launch on first use — not fatal", fatal=False)

    win = mirror.find_window()
    _check("mirroring window found", win is not None,
           "open iPhone Mirroring once manually to pair the phone")
    if not win:
        return

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        path = f.name
    try:
        # Never crash here: a missing Screen Recording grant is exactly what
        # this check exists to report.
        r = subprocess.run(["screencapture", "-x", "-o", "-l", str(win["id"]), path],
                           capture_output=True)
        size = os.path.getsize(path) if os.path.exists(path) else 0
        good = r.returncode == 0 and size > 20_000
        _check(f"window capture works ({size} bytes)", good,
               "capture failed or is blank — Screen Recording permission "
               "needs a terminal restart to take effect")
        if good:
            from . import ocr
            n = len(ocr.recognize(path, win))
            _check(f"Vision OCR works ({n} text boxes)", True)
    finally:
        if os.path.exists(path):
            os.unlink(path)

    from . import ios
    state = ios.IPhone().send("session.state")
    _check(f"session state: {state}", state == "ready",
           "an interstitial is up (iPhone in Use / Connect / Mac Locked) — "
           "clear it on the Mac; lock the iPhone if it says in use", fatal=False)


# --- Android: adb -> a phone -> authorised -> awake -> tree ------------------

_ADB_INSTALL = {"darwin": "brew install android-platform-tools",
                "win32": "winget install Google.PlatformTools"}.get(sys.platform, "apt install adb (or your distro's android-tools)")
_SCRCPY_INSTALL = {"darwin": "brew install scrcpy", "win32": "winget install Genymobile.scrcpy"}.get(sys.platform, "apt install scrcpy")


def _doctor_android():
    from . import config
    adb = str(config.get("android.adb"))
    if not shutil.which(adb):
        _check(f"adb found ({adb})", False,
               _ADB_INSTALL + ", or set android.adb to the binary")
        return
    _check(f"adb found ({shutil.which(adb)})", True)
    _check("scrcpy found (optional: live mirror during `android awake`)",
           bool(shutil.which("scrcpy")), _SCRCPY_INSTALL + " — not required", fatal=False)

    from . import android
    phone = android.Android()
    state = phone.send("session.state")
    hints = {
        "no-device": "plug in with USB debugging on and tap Allow, or "
                     "Wireless debugging + `phone-harness android pair CODE`",
        "unauthorized": "tap Allow on the phone's 'Allow USB debugging?' prompt",
        "offline": "unplug/replug, or toggle Wireless debugging off and on",
        "locked": "unlock the phone (`phone-harness android awake` keeps it so)",
        "no-adb": "adb did not answer",
    }
    _check(f"a phone is reachable and ready (state: {state})",
           state in ("ready", "locked"), hints.get(state, ""))
    if state == "locked":
        _check("phone unlocked", False, hints["locked"], fatal=False)
    if state not in ("ready",):
        return

    b = phone.send("screen.bounds")
    label = android._phone_label(b["id"]) if b else "?"
    _check(f"talking to {label} ({b['id']}), screen {b['w']}x{b['h']}", bool(b))
    try:
        n = len(phone.send("tree"))
        _check(f"accessibility tree readable ({n} nodes)", n > 0)
    except RuntimeError as e:
        _check("accessibility tree readable", False, str(e)[:120], fatal=False)
    reg = config.devices_of("android")
    _check(f"remembered phones: {', '.join(reg['phones']) or 'none'}; primary: "
           f"{reg['primary'] or 'none'}", True)


# --- iPhone over USB: go-ios -> usbmuxd -> phone -> trust -> Developer Mode ->
#     WebDriverAgent installed -> WebDriverAgent running -> unlocked -> tree -----

_GO_IOS_INSTALL = ("npm install -g go-ios, or a release from "
                   "https://github.com/danielpaulus/go-ios/releases")


def _doctor_ios_usb():
    from . import config, ios_usb
    path = ios_usb.go_ios_path()
    if not _check(f"go-ios found ({path or config.get('ios_usb.go_ios')})", bool(path),
                  _GO_IOS_INSTALL + ", or set ios_usb.go_ios to the binary"):
        return
    addr = ios_usb.external_wda()
    if addr:
        print(f"  (ios_usb.wda is {addr}: WebDriverAgent there is yours to run)")
    try:
        attached = list(ios_usb.usbmux_devices())
    except ios_usb.UsbmuxError:
        _check("Apple's USB service answers (usbmuxd)", False, ios_usb.usbmux_hint())
        return
    _check("Apple's USB service answers (usbmuxd)", True)
    if not _check(f"an iPhone is attached over USB ({', '.join(attached) or 'none'})",
                  bool(attached),
                  "plug it in, unlock it, and tap Trust on 'Trust This Computer?'"):
        return
    try:
        udid = ios_usb.pick_udid()
    except RuntimeError as e:
        _check("one iPhone to drive", False, str(e))
        return
    try:
        info = ios_usb._info(udid)
    except ios_usb.GoIOSError as e:
        _check("the iPhone trusts this computer", False, ios_usb.explain(str(e)))
        return
    version = str(info.get("ProductVersion") or "?")
    _check(f"talking to {info.get('DeviceName') or udid}, iOS {version}", True)
    if ios_usb._major(version) >= 16:
        on = ios_usb.developer_mode(udid)
        _check("Developer Mode on" + (" (could not read it)" if on is None else ""),
               on is not False,
               "Settings > Privacy & Security > Developer Mode: turn it on, let the "
               "phone restart, confirm")
    if not addr:
        try:
            bundle = ios_usb.pick_bundle(udid)
        except (RuntimeError, ios_usb.GoIOSError) as e:
            _check("WebDriverAgent installed on the phone", False, str(e))
            return
        _check(f"WebDriverAgent installed ({bundle})", True)

    where = addr or "over USB"
    if ios_usb.connect_wda(udid).status() is None:
        if not addr:
            print("  ...starting WebDriverAgent, as the first helper call would "
                  "(`phone-harness ios-usb stop` ends it)")
        try:
            ios_usb.start_link(udid, say=lambda msg: print(f"  ...{msg}"))
        except RuntimeError as e:
            _check(f"WebDriverAgent answers {where}", False, str(e))
            return
    _check(f"WebDriverAgent answers {where}", True)

    phone = ios_usb.IPhoneUSB(udid)
    state = phone.send("session.state")
    _check(f"session state: {state}", state in ("ready", "locked"))
    if state == "locked":
        _check("phone unlocked", False, "unlock the phone; the harness never types a passcode",
               fatal=False)
        return
    b = phone.send("screen.bounds")
    _check(f"screen {b['w']}x{b['h']} points" if b else "screen size", bool(b))
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        shot = f.name
    try:
        phone.send("screen.capture", path=shot)
        size = os.path.getsize(shot)
        with open(shot, "rb") as f:
            png = f.read(8) == b"\x89PNG\r\n\x1a\n"
        _check(f"screenshot works ({size} bytes)", png and size > 1000)
    except RuntimeError as e:
        _check("screenshot works", False, str(e)[:160])
    finally:
        os.unlink(shot)
    try:
        n = len(phone.send("tree"))
        _check(f"accessibility tree readable ({n} nodes)", n > 0)
    except RuntimeError as e:
        _check("accessibility tree readable", False, str(e)[:160], fatal=False)
