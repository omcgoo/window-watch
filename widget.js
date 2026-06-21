// Window Watch — Scriptable widget (medium / full-width)
// Install: paste into a new Scriptable script, add to home screen as a Medium widget

const GIST_API_URL = "https://api.github.com/gists/bd24c63bd7e129c86942db6ed67f9008"
const DASHBOARD_URL = "https://voop-ollie.github.io/window-watch/"

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
widget.url = DASHBOARD_URL
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

// Current temp
if (data?.outdoor_c != null) {
  const tempText = right.addText(`${data.outdoor_c.toFixed(1)}°C now`)
  tempText.font = Font.semiboldSystemFont(14)
  tempText.textColor = new Color("#ffffff", 0.85)
  tempText.rightAlignText()
  right.addSpacer(5)
}

// Forecast line — most actionable info
if (data?.forecast_max_c != null) {
  let fcLine
  if (data.forecast_close_hour != null) {
    fcLine = `Close before ${fmtHour(data.forecast_close_hour)}`
  } else {
    fcLine = `Peak ${data.forecast_max_c.toFixed(0)}° at ${fmtHour(data.forecast_peak_hour)}`
  }
  const fcText = right.addText(fcLine)
  fcText.font = Font.systemFont(12)
  fcText.textColor = data.forecast_close_hour != null
    ? new Color("#ffd060", 0.9)
    : new Color("#ffffff", 0.5)
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
