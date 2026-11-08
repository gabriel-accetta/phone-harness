"""iPhone-over-USB behaviour that needs no phone: what it sends to WebDriverAgent.

A fake WDA answers on a local port, so the real HTTP client, session handling
and JSON shapes run end to end. A fake usbmuxd carries requests to it the way
the real one carries them over the cable. go-ios is never executed; the few
calls that would reach it are patched.

    python -m unittest tests.test_ios_usb
"""
import base64
import json
import os
import plistlib
import re
import socket
import socketserver
import struct
import sys
import tempfile
import threading
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from phone_harness import ios_usb  # noqa: E402
from phone_harness.ios_usb import IPhoneUSB, flatten  # noqa: E402
from phone_harness.transport import Unsupported  # noqa: E402

W, H = 390, 844


def png(w, h):
    """Enough of a PNG for anything that reads its header."""
    return (b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
            + w.to_bytes(4, "big") + h.to_bytes(4, "big") + b"\x08\x06\x00\x00\x00")


def el(kind, label="", x=0, y=0, w=0, h=0, children=(), **extra):
    return {"type": "XCUIElementType" + kind, "label": label or None, "name": label or None,
            "value": None, "rect": {"x": x, "y": y, "width": w, "height": h},
            "isEnabled": "1", "isVisible": "1", "children": list(children), **extra}


# Settings, roughly: a navigation bar whose title is repeated by its static
# text, a cell whose label aggregates its two texts, a switch, a row scrolled
# off screen, and a hidden view.
SOURCE = el("Application", "Settings", 0, 0, W, H, [
    el("Window", "", 0, 0, W, H, [
        el("NavigationBar", "Settings", 0, 47, W, 96, [
            el("StaticText", "Settings", 16, 91, 120, 41)]),
        el("Cell", "Wi-Fi, Home", 0, 200, W, 44, [
            el("StaticText", "Wi-Fi", 60, 211, 50, 22),
            el("StaticText", "Home", 300, 211, 50, 22)]),
        el("Switch", "Airplane Mode", 320, 260, 51, 31, value="0"),
        el("Cell", "Privacy", 0, 1200, W, 44, [
            el("StaticText", "Privacy", 60, 1211, 70, 22)]),
        el("Button", "Hidden", 0, 300, 50, 50, isVisible="0"),
        el("TextField", "", 20, 400, 300, 40, name="search-field", value="hello"),
    ])])


class FakeWDA:
    """A WebDriverAgent that records what it is sent."""

    def __init__(self):
        self.sent = []
        self.locked = False
        self.live_session = "S1"
        self.status_session = "S1"
        self.sessions_made = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self, code, payload):
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                fake.sent.append((method, self.path, body))
                code, payload = fake.route(method, self.path, body)
                self._answer(code, payload)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def route(self, method, path, body):
        m = re.match(r"/session/([^/?]+)(.*)", path)
        if m and m.group(1) != self.live_session:
            return 404, {"value": {"error": "invalid session id",
                                   "message": "Session does not exist"}}
        rest = m.group(2) if m else path
        if path == "/status":
            return 200, {"value": {"ready": True}, "sessionId": self.status_session}
        if method == "POST" and path == "/session":
            self.sessions_made += 1
            self.live_session = f"S{self.sessions_made + 1}"
            return 200, {"value": {"sessionId": self.live_session}, "sessionId": self.live_session}
        if rest == "/window/size":
            return 200, {"value": {"width": W, "height": H}}
        if rest == "/wda/locked":
            return 200, {"value": self.locked}
        if rest == "/screenshot":
            return 200, {"value": base64.b64encode(png(W * 3, H * 3)).decode()}
        if rest.startswith("/source"):
            return 200, {"value": SOURCE}
        if rest == "/wda/activeAppInfo":
            return 200, {"value": {"bundleId": "com.apple.Preferences", "pid": 1}}
        return 200, {"value": None}

    def actions(self):
        return [b["actions"][0]["actions"] for m, p, b in self.sent if p.endswith("/actions")]

    def posts(self, suffix):
        return [b for m, p, b in self.sent if m == "POST" and p.endswith(suffix)]


class FakeUsbmux:
    """usbmuxd's plist protocol over TCP (USBMUXD_SOCKET_ADDRESS). A Connect
    to `wda_port` on a listed phone is piped to the fake WDA; anything else
    is refused, as the real one refuses a port nothing listens on.
    broken=True hangs up on every client, like a service that is not up."""

    def __init__(self, devices=(), wda_port=None, broken=False):
        self.devices, self.wda_port, self.broken = list(devices), wda_port, broken
        self.connects = []
        fake = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                if fake.broken:
                    return
                sock = self.request
                length = struct.unpack("<IIII", sock.recv(16))[0]
                body = b""
                while len(body) < length - 16:
                    body += sock.recv(length - 16 - len(body))
                msg = plistlib.loads(body)
                if msg["MessageType"] == "ListDevices":
                    return self.reply({"DeviceList": [
                        {"DeviceID": i + 1, "Properties": {"SerialNumber": u, "ConnectionType": "USB"}}
                        for i, u in enumerate(fake.devices)]})
                port = socket.ntohs(msg["PortNumber"])
                fake.connects.append((msg["DeviceID"], port))
                if not (fake.wda_port and port == 8100 and 1 <= msg["DeviceID"] <= len(fake.devices)):
                    return self.reply({"MessageType": "Result", "Number": 3})
                self.reply({"MessageType": "Result", "Number": 0})
                with socket.create_connection(("127.0.0.1", fake.wda_port)) as upstream:
                    pump = threading.Thread(target=self.copy, args=(upstream, sock), daemon=True)
                    pump.start()
                    self.copy(sock, upstream)
                    pump.join(2)

            def reply(self, payload):
                data = plistlib.dumps(payload)
                self.request.sendall(struct.pack("<IIII", 16 + len(data), 1, 8, 1) + data)

            @staticmethod
            def copy(src, dst):
                try:
                    while True:
                        chunk = src.recv(65536)
                        if not chunk:
                            break
                        dst.sendall(chunk)
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.address = "127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class WithFakeWDA(unittest.TestCase):
    """WDA at an address, as `ios_usb.wda = HOST:PORT` reaches one."""

    def setUp(self):
        self.wda = FakeWDA()
        self.addCleanup(self.wda.close)
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        env = {"PHONE_HARNESS_IOS_USB_WDA": f"127.0.0.1:{self.wda.port}",
               "PHONE_HARNESS_HOME": self.home.name}
        patcher = unittest.mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.phone = IPhoneUSB()


class Input(WithFakeWDA):
    def test_a_tap_is_one_finger_down_and_up_at_the_point(self):
        self.phone.send("input.tap", x=195.4, y=400)
        (steps,) = self.wda.actions()
        self.assertEqual(steps[0], {"type": "pointerMove", "duration": 0, "x": 195, "y": 400,
                                    "origin": "viewport"})
        self.assertEqual([s["type"] for s in steps],
                         ["pointerMove", "pointerDown", "pause", "pointerUp"])

    def test_a_long_press_holds_for_the_duration(self):
        self.phone.send("input.press", x=10, y=20, duration=1.5)
        (steps,) = self.wda.actions()
        self.assertEqual(steps[2], {"type": "pause", "duration": 1500})

    def test_a_scroll_holds_still_before_lifting_and_stays_off_the_edges(self):
        self.phone.send("input.scroll", x=W / 2, y=H / 2, dy=-2000, dx=0, steps=10)
        (steps,) = self.wda.actions()
        start, move = steps[0], steps[3]
        self.assertEqual((start["x"], start["y"]), (195, round(H * 0.9)))   # off the home bar
        self.assertEqual((move["x"], move["y"]), (195, round(H * 0.1)))     # off the status bar
        self.assertEqual(move["duration"], 500)
        self.assertEqual(steps[-2:], [{"type": "pause", "duration": 250},
                                      {"type": "pointerUp", "button": 0}])

    def test_a_drag_lifts_while_moving_so_a_short_one_flicks(self):
        self.phone.send("input.drag", x1=300, y1=400, x2=100, y2=400, duration=0.12, steps=6)
        (steps,) = self.wda.actions()
        self.assertEqual([s["type"] for s in steps],
                         ["pointerMove", "pointerDown", "pointerMove", "pointerUp"])
        self.assertEqual(steps[2]["duration"], 120)

    def test_keys_map_to_the_keyboard_and_chords_are_unsupported(self):
        self.phone.send("input.keys", combo="return")
        self.phone.send("input.keys", combo="volumeup")
        self.assertEqual(self.wda.posts("/wda/keys"), [{"value": ["\n"]}])
        self.assertEqual(self.wda.posts("/wda/pressButton"), [{"name": "volumeUp"}])
        with self.assertRaises(Unsupported):
            self.phone.send("input.keys", combo="cmd+1")
        with self.assertRaises(Unsupported):
            self.phone.send("input.keys", combo="f13")

    def test_text_is_typed_as_characters_including_unicode(self):
        self.phone.send("input.text", s="Olá ✓", delay=0.03, keystrokes=False)
        self.assertEqual(self.wda.posts("/wda/keys"), [{"value": list("Olá ✓")}])

    def test_home_needs_no_session(self):
        self.phone.send("nav.home")
        self.assertIn(("POST", "/wda/homescreen", {}), self.wda.sent)

    def test_back_and_recents_are_absent_not_faked(self):
        self.assertFalse(self.phone.supports("nav.back"))
        self.assertFalse(self.phone.supports("nav.recents"))


class Locked(WithFakeWDA):
    def test_a_locked_phone_refuses_input_and_still_shows_its_screen(self):
        self.wda.locked = True
        self.assertEqual(self.phone.send("session.state"), "locked")
        with self.assertRaises(RuntimeError) as cm:
            self.phone.send("input.tap", x=1, y=1)
        self.assertIn("locked", str(cm.exception))
        self.assertIn("never type a passcode", str(cm.exception))
        self.assertEqual(self.wda.actions(), [])
        path, bounds = self.phone.send("screen.capture")
        self.addCleanup(os.unlink, path)
        self.assertEqual(Path(path).read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual((bounds["w"], bounds["h"]), (W, H))


class Screen(WithFakeWDA):
    def test_bounds_are_the_screen_in_points(self):
        self.assertEqual(self.phone.send("screen.bounds"),
                         {"x": 0, "y": 0, "w": W, "h": H, "id": None})

    def test_text_comes_from_the_tree_innermost_words_once(self):
        rows = self.phone.send("screen.text")
        texts = [r["text"] for r in rows]
        self.assertEqual(texts, ["Settings", "Wi-Fi", "Home", "Airplane Mode", "hello"])
        self.assertTrue(all(r["source"] == "tree" and r["confidence"] == 1.0 for r in rows))
        wifi = rows[1]
        self.assertEqual((wifi["x"], wifi["y"], wifi["w"], wifi["h"]), (85, 222, 50, 22))

    def test_the_tree_has_the_android_node_shape(self):
        nodes = self.phone.send("tree")
        self.assertTrue(all("_says" not in n for n in nodes))
        switch = next(n for n in nodes if n["text"] == "Airplane Mode")
        self.assertEqual(switch["desc"], "0")
        self.assertTrue(switch["clickable"])
        self.assertEqual(switch["class"], "XCUIElementTypeSwitch")
        field = next(n for n in nodes if n["id"] == "search-field")
        self.assertEqual(field["text"], "hello")
        self.assertNotIn("Privacy", [n["text"] for n in nodes])     # scrolled off
        self.assertNotIn("Hidden", [n["text"] for n in nodes])

    def test_a_session_wda_dropped_is_replaced_once(self):
        self.wda.status_session = "OLD"                            # status still names it
        self.wda.live_session = "S9"
        self.phone.send("input.tap", x=1, y=1)
        self.assertEqual(self.wda.sessions_made, 1)
        tries = [p for m, p, b in self.wda.sent if p.endswith("/actions")]
        self.assertEqual(tries, ["/session/OLD/actions", "/session/S2/actions"])


class Apps(WithFakeWDA):
    def test_apple_apps_launch_by_name_without_asking_the_phone(self):
        with unittest.mock.patch.object(ios_usb, "_apps", side_effect=AssertionError):
            got = self.phone.send("apps.launch", name="App Store")
        self.assertEqual(got, "com.apple.AppStore")
        self.assertEqual(self.wda.posts("/wda/apps/activate"), [{"bundleId": "com.apple.AppStore"}])

    def test_other_apps_are_found_by_their_display_name(self):
        apps = [{"id": "com.tinyspeck.chatlyio", "name": "Slack"},
                {"id": "com.facebook.WebDriverAgentRunner.xctrunner", "name": "WebDriverAgentRunner-Runner"}]
        with unittest.mock.patch.object(ios_usb, "_apps", return_value=apps), \
                unittest.mock.patch.object(ios_usb, "read_link", return_value={"udid": "U1"}):
            self.assertEqual(self.phone.send("apps.launch", name="slack"), "com.tinyspeck.chatlyio")
            with self.assertRaises(RuntimeError) as cm:
                self.phone.send("apps.launch", name="Nope")
        self.assertIn("Slack", str(cm.exception))

    def test_current_app_is_the_bundle_id(self):
        self.assertEqual(self.phone.send("apps.current"), "com.apple.Preferences")


class OverUSB(unittest.TestCase):
    """The default: no port on this computer, every request through usbmuxd."""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.env = {"PHONE_HARNESS_HOME": self.home.name}
        patcher = unittest.mock.patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("PHONE_HARNESS_IOS_USB_WDA", None)
        os.environ.pop("PHONE_HARNESS_IOS_USB_UDID", None)
        self.phone = IPhoneUSB()

    def mux(self, **kw):
        mux = FakeUsbmux(**kw)
        self.addCleanup(mux.close)
        os.environ["USBMUXD_SOCKET_ADDRESS"] = mux.address
        return mux

    def state(self, go_ios="/bin/ios"):
        with unittest.mock.patch.object(ios_usb, "go_ios_path", return_value=go_ios):
            return self.phone.send("session.state")

    def test_requests_reach_wda_on_the_phone_through_usbmuxd(self):
        wda = FakeWDA()
        self.addCleanup(wda.close)
        mux = self.mux(devices=["00008110-000A1C2E3F45001E"], wda_port=wda.port)
        self.phone.send("input.tap", x=10, y=20)
        self.assertEqual(len(wda.actions()), 1)
        self.assertTrue(mux.connects)
        self.assertTrue(all(c == (1, 8100) for c in mux.connects))

    def test_the_configured_phone_is_matched_without_dashes_or_case(self):
        wda = FakeWDA()
        self.addCleanup(wda.close)
        mux = self.mux(devices=["AAAA0001", "00008110000A1C2E3F45001E"], wda_port=wda.port)
        os.environ["PHONE_HARNESS_IOS_USB_UDID"] = "00008110-000a1c2e3f45001e"
        self.assertEqual(self.phone.send("apps.current"), "com.apple.Preferences")
        self.assertTrue(all(dev == 2 for dev, _ in mux.connects))

    def test_each_missing_step_has_its_own_state(self):
        self.mux(devices=["U1"])
        self.assertEqual(self.state(go_ios=None), "no-go-ios")
        self.assertEqual(self.state(), "not-started")             # attached, WDA not running
        self.mux(devices=[])
        self.assertEqual(self.state(), "no-device")
        self.mux(broken=True)
        self.assertEqual(self.state(), "no-usbmux")

    def test_without_autostart_require_names_the_command(self):
        self.mux(devices=["U1"])
        with unittest.mock.patch.dict(os.environ, {"PHONE_HARNESS_IOS_USB_AUTOSTART": "0"}), \
                unittest.mock.patch.object(ios_usb, "go_ios_path", return_value="/bin/ios"):
            with self.assertRaises(RuntimeError) as cm:
                self.phone.send("session.require")
        self.assertIn("phone-harness ios-usb start", str(cm.exception))

    def test_bounds_never_start_anything(self):
        self.mux(devices=["U1"])
        with unittest.mock.patch.object(ios_usb, "start_link", side_effect=AssertionError):
            self.assertIsNone(self.phone.send("screen.bounds"))

    def test_a_wda_at_an_address_that_is_down_is_never_started(self):
        os.environ["PHONE_HARNESS_IOS_USB_WDA"] = "127.0.0.1:9"
        with unittest.mock.patch.object(ios_usb, "_spawn", side_effect=AssertionError):
            with self.assertRaises(RuntimeError) as cm:
                self.phone.send("session.require")
        self.assertIn("Nothing answers at 127.0.0.1:9", str(cm.exception))


class Link(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        patcher = unittest.mock.patch.dict(os.environ, {"PHONE_HARNESS_HOME": self.home.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_stop_never_kills_a_recorded_pid_that_is_not_go_ios(self):
        ios_usb._write_link({"udid": "U1", "pids": {"wda": os.getpid()}})
        self.assertEqual(ios_usb.stop_link(), [])                  # this test is still alive
        self.assertIsNone(ios_usb.read_link())

    def test_a_runner_that_exits_fails_at_once_with_the_reason(self):
        ios_usb.log_path("wda").parent.mkdir(parents=True, exist_ok=True)
        ios_usb.log_path("wda").write_text(json.dumps({
            "level": "ERROR", "msg": "Failed running WDA",
            "err": "The application could not be launched because the developer profile "
                   "has not been explicitly trusted by the user."}) + "\n")
        exited = unittest.mock.Mock(poll=lambda: 1)
        with self.assertRaises(RuntimeError) as cm:
            ios_usb._wait(lambda: False, 60, exited, "wda", "WebDriverAgent")
        self.assertIn("VPN & Device Management", str(cm.exception))


class GoIOS(unittest.TestCase):
    def test_a_resigned_runner_is_still_found(self):
        apps = [{"id": "com.facebook.WebDriverAgentRunner.xctrunner.ABCDE12345", "name": ""},
                {"id": "com.example.app", "name": "App"},
                {"id": "io.me.Runner.xctrunner", "name": ""}]
        self.assertEqual(ios_usb._wda_bundles(apps),
                         ["com.facebook.WebDriverAgentRunner.xctrunner.ABCDE12345",
                          "io.me.Runner.xctrunner"])

    def test_the_reason_is_the_last_error_in_the_log(self):
        log = "\n".join([json.dumps({"level": "INFO", "msg": "starting"}),
                         json.dumps({"level": "ERROR", "msg": "failed getting device list",
                                     "err": "dial tcp 127.0.0.1:27015: refused"})])
        self.assertEqual(ios_usb._log_reason(log),
                         "failed getting device list: dial tcp 127.0.0.1:27015: refused")
        self.assertEqual(ios_usb.explain(log), ios_usb.usbmux_hint())

    def test_known_failures_get_the_users_next_step(self):
        self.assertIn("Developer Mode", ios_usb.explain("DeveloperMode is disabled"))
        self.assertIn("Trust", ios_usb.explain("InvalidHostID"))
        self.assertTrue(ios_usb.explain("something new").startswith("go-ios said: "))


class Flatten(unittest.TestCase):
    def test_a_container_speaks_only_when_nothing_below_it_does(self):
        tree = el("Window", "", 0, 0, W, H, [el("Other", "Custom control", 0, 0, 100, 100)])
        nodes = flatten(tree, W, H)
        self.assertEqual([n["text"] for n in nodes if n["_says"]], ["Custom control"])

    def test_booleans_may_be_real_booleans(self):
        tree = el("Button", "OK", 0, 0, 10, 10, isVisible=False)
        self.assertEqual(flatten(tree, W, H), [])


class Wiring(unittest.TestCase):
    def test_connect_builds_it_by_name_on_any_os(self):
        from phone_harness import transport
        self.assertIsInstance(transport.connect("ios-usb"), IPhoneUSB)

    @unittest.skipIf(sys.platform == "darwin", "iPhone Mirroring exists on a Mac")
    def test_ios_off_a_mac_points_at_ios_usb(self):
        from phone_harness import transport
        with self.assertRaises(RuntimeError) as cm:
            transport.connect("ios")
        self.assertIn("needs macOS", str(cm.exception))
        self.assertIn("ios-usb", str(cm.exception))

    def test_capture_size_reads_the_png_header_without_vision(self):
        from phone_harness import helpers
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(png(1170, 2532))
        self.addCleanup(os.unlink, f.name)
        self.assertEqual(helpers._capture_size(f.name), (1170, 2532))


if __name__ == "__main__":
    unittest.main()
