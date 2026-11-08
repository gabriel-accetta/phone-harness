# Personal iPhone over USB (no Mac)

Drives the user's own iPhone over a USB cable from Windows, Linux or macOS.
No Mac, no Xcode on this computer, no jailbreak.

The phone runs WebDriverAgent (WDA): Apple's XCTest UI automation behind a
small HTTP server, the same agent Appium uses. [go-ios](https://github.com/danielpaulus/go-ios)
starts it over the cable. The harness reads the screen from the phone's
accessibility tree and touches it through XCTest. WDA's port is never opened
on this computer: every request travels through Apple's USB service to the
phone, so nothing else on the network can reach it.

## Needs

- Python 3.10+ (the normal install).
- go-ios on PATH: `npm install -g go-ios`, or the binary for your OS from
  [its releases](https://github.com/danielpaulus/go-ios/releases).
  Elsewhere: `phone-harness config set ios_usb.go_ios PATH`.
- Apple's USB service, which lets the computer see an iPhone at all:
  - **Windows:** the Apple Devices app (Microsoft Store) or iTunes.
  - **Linux:** `usbmuxd` (`apt install usbmuxd`, or your distro's package).
  - **macOS:** built in.
- An Apple ID to sign WebDriverAgent. A free one works.
- An iPhone on a recent iOS. On iOS 17 and later the harness also opens
  go-ios's tunnel by itself; nothing extra to install.

## Steps only the user can do

1. **Trust this computer.** Plug the phone in, unlock it, tap **Trust**.
2. **Developer Mode** (iOS 16+): Settings > Privacy & Security >
   **Developer Mode**, turn it on, let the phone restart, confirm. If the
   switch is not there yet, `ios devmode reveal` makes it appear; so does
   step 3.
3. **Install a signed WebDriverAgent.** Download
   `WebDriverAgentRunner-Runner.zip` from
   [Appium's WebDriverAgent releases](https://github.com/appium/WebDriverAgent/releases):
   it is a ready-built real-device WDA, unsigned. Sign it with your Apple ID
   and install it with any re-signing tool that runs on your OS (Sideloadly
   is a common one on Windows). Most of them take an `.ipa`, which is the
   same app in a `Payload` folder:

   ```bash
   unzip WebDriverAgentRunner-Runner.zip -d wda
   mkdir Payload && mv wda/WebDriverAgentRunner-Runner.app Payload/
   zip -r WebDriverAgentRunner.ipa Payload
   ```

   Have a Mac anywhere, or a CI runner with one? Building and signing WDA
   there once with Xcode works too.
4. **Trust the developer:** Settings > General > VPN & Device Management >
   your Apple ID > **Trust**.

A free Apple ID signature lasts **7 days**. After that WDA stops launching
and the harness says the signature is not trusted or has expired: repeat
steps 3 and 4. A paid developer account signs for a year.

The harness finds the installed runner by its bundle id, even after a
signing tool renames it. If it cannot tell which one to use:
`phone-harness config set ios_usb.wda_bundle BUNDLE_ID`.

## Check

```bash
phone-harness config set platform ios-usb
phone-harness doctor ios-usb
```

The doctor checks each step in order, names the first one that is missing,
and starts WDA to prove the whole path works (`phone-harness ios-usb stop`
ends it). After that the first helper call of a task starts WDA by itself:
a few seconds normally, longer the very first time while go-ios downloads
the developer disk image.

`phone-harness ios-usb` shows what is attached and whether WDA is up.
Several iPhones plugged in: `phone-harness config set ios_usb.udid UDID`.

## Settings

| Key | Default | |
| --- | --- | --- |
| `ios_usb.go_ios` | `ios` | the go-ios binary |
| `ios_usb.udid` | (empty) | which iPhone, when several are plugged in |
| `ios_usb.wda_bundle` | (empty) | the WDA runner's bundle id; empty finds it |
| `ios_usb.wda` | `usb` | `HOST:PORT` reaches a WDA you run yourself (Appium, iproxy); then the harness starts and stops nothing |
| `ios_usb.autostart` | `true` | the first helper call starts WDA |

Not able to install anything, or no cable? Use a [cloud phone](cloud.md).
