# Window Watch — Android widget

A home-screen widget that mirrors the iOS Scriptable one in [`../widget.js`](../widget.js).
Both read the same public Gist that `window_watch.py` PATCHes every run, so the phone
never talks to Fly directly.

## Install on the phone

Developer mode is **not** needed — that's only for `adb`. Copy
`dist/window-watch-1.0.apk` to the phone (Proton Drive, email, USB file transfer),
tap it, and allow "install unknown apps" for whichever app you opened it from.

Then either open **Window Watch** and tap *Add widget to home screen*, or long-press
the home screen → Widgets → Window Watch.

## Layout

It's responsive on height, so one widget covers both shapes:

- **4×1** (the default) — verdict, `23.3° out · 25.1° in`, and the forecast line. Only two
  lines fit, so once the payload is older than 45 minutes that second line switches to
  `Updated 2h ago` instead: a stale forecast describes a day that has moved on, and the
  cache means old numbers otherwise look exactly like fresh ones.
- **4×2 or taller** — adds the hint line, split indoor/outdoor rows, and the
  "Updated Nm ago" age.

Opening the app shows when it last fetched and the last error if there is one. That exists
because this phone has no developer mode, so there's no adb and no logcat — it's the only
way to find out why a widget went stale.

## Refreshing

`RefreshWorker` (WorkManager, every 15 minutes, needs a network) fetches the Gist and
redraws. **Drawing never touches the network** — `provideGlance` reads only the cache.

That split is the whole point. Composition runs on the app-widget broadcast path, which
gets roughly ten seconds; the first version fetched there with 15-second timeouts, so at
boot — Wi-Fi not yet associated, launcher restoring widgets — it burned the whole budget
every time and got killed, which is why the widget kept vanishing on restart. It also
explains the staleness: `updatePeriodMillis` is clamped to 30 minutes *and* suppressed
during Doze, so an idle phone could drift for hours. `updatePeriodMillis` is still set as
a free backstop, but WorkManager is what actually keeps it current.

Fifteen minutes is WorkManager's floor and half the Fly service's 30-minute write cadence
(`runner.py`), so the widget can't lag a new payload by much more than 15 minutes. Nothing
faster would help — the Gist doesn't change more often than that.

WorkManager restores its own schedule after a reboot or force-stop (`work-runtime` declares
`RECEIVE_BOOT_COMPLETED` itself), so this app deliberately has **no boot receiver**.

The last good payload is cached, so a dropped network shows stale-but-real numbers
rather than falling back to "—". Tapping opens the dashboard.

## Building

Needs a JDK and the Android SDK (`platforms;android-35`, `build-tools;35.0.0`).
`local.properties` is gitignored — point it at your SDK:

```
echo "sdk.dir=$ANDROID_HOME" > local.properties
JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" ./gradlew assembleDebug
```

The APK is debug-signed, which installs fine by hand. It would need a release
keystore only to go on the Play Store.
