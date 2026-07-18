// Window Watch — Scriptable widget (medium / full-width)
// Install: paste into a new Scriptable script named "Window Watch",
// then add to home screen as a Medium widget.

const GIST_API_URL = "https://api.github.com/gists/45ba603447bbc3ec258e479f6dee6a20"
const DASHBOARD_URL = "https://omcgoo.github.io/window-watch/"
const SCRIPT_NAME   = "Window Watch"  // must match the name in Scriptable

// When tapped, the widget deep-links back into Scriptable which runs this
// script again — but outside widget context. We detect that and open the
// dashboard in an in-app sheet (no new Safari tab) then exit.
if (!config.runsInWidget) {
  await Safari.openInApp(DASHBOARD_URL, false)
  Script.complete()
  return
}

const THEME = {
  open: {
    bg:     new Color("#0f3460"),
    accent: new Color("#4db6ff"),
    label:  "OPEN",
    hint:   "let the air through",
  },
  close: {
    bg:     new Color("#7b1e1e"),
    accent: new Color("#ff8c8c"),
    label:  "CLOSE",
    hint:   "hold the cool in",
  },
  unknown: {
    bg:     new Color("#1a1a2e"),
    accent: new Color("#888888"),
    label:  "—",
    hint:   "waiting for data",
  },
}

function fmtHour(h) {
  if (h == null) return "?"
  if (h === 0)   return "midnight"
  if (h < 12)    return `${h}am`
  if (h === 12)  return "noon"
  return `${h - 12}pm`
}

function timeAgo(utcString) {
  if (!utcString) return "never"
  const diff = Math.floor((Date.now() - new Date(utcString).getTime()) / 60000)
  if (diff < 1) return "just now"
  if (diff < 60) return `${diff}m ago`
  return `${Math.floor(diff / 60)}h ago`
}

async function fetchStatus() {
  try {
    const req = new Request(GIST_API_URL)
    req.headers = {
      "User-Agent": "Scriptable-Window-Watch"
    }
    const res = await req.loadJSON()
    return JSON.parse(res.files["window-watch-status.json"].content)
  } catch (e) {
    return null
  }
}

const data = await fetchStatus()
const status = data?.status ?? "unknown"
const theme = THEME[status] ?? THEME.unknown

const widget = new ListWidget()
widget.backgroundColor = theme.bg
widget.setPadding(14, 18, 14, 18)
widget.url = `scriptable:///run?scriptName=${encodeURIComponent(SCRIPT_NAME)}`
widget.refreshAfterDate = new Date(Date.now() + 30 * 60 * 1000)

// ── Row: left status + right details ────────
const row = widget.addStack()
row.layoutHorizontally()
row.centerAlignContent()

// Left column
const left = row.addStack()
left.layoutVertically()
left.size = new Size(150, 0)

const labelText = left.addText(theme.label)
labelText.font = Font.boldSystemFont(38)
labelText.textColor = theme.accent
labelText.minimumScaleFactor = 0.7

left.addSpacer(4)

const hintText = left.addText(theme.hint)
hintText.font = Font.systemFont(12)
hintText.textColor = new Color(theme.accent.hex, 0.55)

row.addSpacer()

// Right column
const right = row.addStack()
right.layoutVertically()

// Outdoor vs indoor
if (data?.outdoor_c != null) {
  const tempText = right.addText(`${data.outdoor_c.toFixed(1)}°C outside`)
  tempText.font = Font.semiboldSystemFont(14)
  tempText.textColor = new Color("#ffffff", 0.85)
  tempText.rightAlignText()
  right.addSpacer(2)
}
if (data?.indoor_est_c != null) {
  const indoorText = right.addText(`${data.indoor_est_c.toFixed(1)}°C inside`)
  indoorText.font = Font.systemFont(12)
  indoorText.textColor = new Color("#ffffff", 0.45)
  indoorText.rightAlignText()
  right.addSpacer(5)
}

// Forecast line — mirrors the dashboard's state machine: closed (since/before +
// reopen), close ahead, reopened for the evening, or no close needed today.
const nowHour = new Date().getHours()
const cH = data?.forecast_close_hour, oH = data?.forecast_open_hour
const closeUpcoming = cH != null && cH >= nowHour
let fcLine = null
if (status === "close") {
  fcLine = cH == null ? "Keep shut"
    : closeUpcoming ? `Close before ${fmtHour(cH)}`
    : `Closed since ${fmtHour(cH)}`
  if (oH != null) fcLine += ` · reopen around ${fmtHour(oH)}`
} else if (closeUpcoming) {
  fcLine = `Close before ${fmtHour(cH)}`
  if (oH != null) fcLine += ` · reopen around ${fmtHour(oH)}`
} else {
  const peakPast = data?.forecast_peak_hour != null && nowHour > data.forecast_peak_hour
  if (peakPast || cH != null) {
    // Post-peak downcurve: show where tonight's slide is heading
    const rest = (data?.forecast_hourly || []).filter(p => p[0] >= nowHour && p[1] != null)
    fcLine = rest.length
      ? `Sliding to ~${Math.round(Math.min(...rest.map(p => p[1])))}° tonight`
      : "Past the peak, cooling from here"
  } else {
    fcLine = "No close expected today"
  }
}
if (fcLine) {
  const fcText = right.addText(fcLine)
  fcText.font = Font.systemFont(12)
  fcText.textColor = new Color("#ffd060", 0.9)
  fcText.rightAlignText()
  right.addSpacer(5)
}

// Updated time
const updatedText = right.addText(`Updated ${timeAgo(data?.updated_utc)}`)
updatedText.font = Font.systemFont(11)
updatedText.textColor = new Color("#ffffff", 0.28)
updatedText.rightAlignText()

Script.setWidget(widget)
widget.presentMedium()
Script.complete()
