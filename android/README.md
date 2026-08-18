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

- **4×1** (the default) — verdict, `23.3° out · 25.1° in`, and the forecast line.
- **4×2 or taller** — adds the hint line, split indoor/outdoor rows, and the
  "Updated Nm ago" age.

Refreshes every 30 minutes (`updatePeriodMillis`), same cadence as the iOS widget.
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
