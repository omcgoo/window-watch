// Window Watch — Scriptable widget
// Install: paste this into a new Scriptable script, add to home screen as small/medium widget

const GIST_URL = "https://gist.githubusercontent.com/voop-ollie/bd24c63bd7e129c86942db6ed67f9008/raw/window-watch-status.json"

const COLORS = {
  open:    { bg: new Color("#0f3460"), text: new Color("#e0f0ff"), emoji: "🪟" },
  close:   { bg: new Color("#7b1e1e"), text: new Color("#ffe8e8"), emoji: "🔒" },
  unknown: { bg: new Color("#2a2a2a"), text: new Color("#cccccc"), emoji: "❓" },
}

async function fetchStatus() {
  try {
    const req = new Request(GIST_URL + "?t=" + Date.now())
    return await req.loadJSON()
  } catch (e) {
    return null
  }
}

function timeAgo(utcString) {
  if (!utcString) return "never"
  const diff = Math.floor((Date.now() - new Date(utcString).getTime()) / 60000)
  if (diff < 1) return "just now"
  if (diff < 60) return `${diff}m ago`
  return `${Math.floor(diff / 60)}h ago`
}

const data = await fetchStatus()
const status = data?.status ?? "unknown"
const theme = COLORS[status] ?? COLORS.unknown

const widget = new ListWidget()
widget.backgroundColor = theme.bg
widget.setPadding(14, 16, 14, 16)

// Top row: emoji + status
const top = widget.addStack()
top.layoutHorizontally()
top.centerAlignContent()

const emojiText = top.addText(theme.emoji)
emojiText.font = Font.systemFont(28)
top.addSpacer(8)

const stateText = top.addText(status === "unknown" ? "—" : status.toUpperCase())
stateText.font = Font.boldSystemFont(26)
stateText.textColor = theme.text

widget.addSpacer(8)

// Outdoor temp
if (data?.outdoor_c != null) {
  const tempText = widget.addText(`Outside: ${data.outdoor_c.toFixed(1)}°C`)
  tempText.font = Font.systemFont(14)
  tempText.textColor = new Color(theme.text.hex, 0.8)
}

// Thresholds
if (data?.close_above_c != null) {
  const threshText = widget.addText(`Close >${data.close_above_c}° / Open <${data.open_below_c}°`)
  threshText.font = Font.systemFont(11)
  threshText.textColor = new Color(theme.text.hex, 0.5)
}

widget.addSpacer()

// Last updated
const updatedText = widget.addText(`Updated ${timeAgo(data?.updated_utc)}`)
updatedText.font = Font.systemFont(10)
updatedText.textColor = new Color(theme.text.hex, 0.4)

Script.setWidget(widget)
widget.presentSmall()
Script.complete()
