package com.omcgoo.windowwatch

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.time.Instant
import java.time.LocalTime
import kotlin.math.roundToInt

const val GIST_API_URL = "https://api.github.com/gists/45ba603447bbc3ec258e479f6dee6a20"
const val DASHBOARD_URL = "https://omcgoo.github.io/window-watch/"
private const val STATUS_FILE = "window-watch-status.json"
private const val PREFS = "window_watch"
private const val CACHE_KEY = "status_json"

/** The slice of the dashboard status payload the widget actually renders. */
data class Status(
    val status: String,
    val outdoorC: Double?,
    val indoorEstC: Double?,
    val peakHour: Int?,
    val closeHour: Int?,
    val openHour: Int?,
    val hourly: List<Pair<Int, Double>>,
    val updatedUtc: String?,
)

private fun JSONObject.optDoubleOrNull(key: String): Double? =
    if (!has(key) || isNull(key)) null else optDouble(key).takeIf { !it.isNaN() }

private fun JSONObject.optIntOrNull(key: String): Int? =
    if (!has(key) || isNull(key)) null else optInt(key)

private fun JSONObject.optStringOrNull(key: String): String? =
    if (!has(key) || isNull(key)) null else optString(key).takeIf { it.isNotEmpty() }

private fun parseHourly(arr: JSONArray?): List<Pair<Int, Double>> {
    if (arr == null) return emptyList()
    val out = mutableListOf<Pair<Int, Double>>()
    for (i in 0 until arr.length()) {
        val pair = arr.optJSONArray(i) ?: continue
        if (pair.length() < 2 || pair.isNull(0) || pair.isNull(1)) continue
        out += pair.optInt(0) to pair.optDouble(1)
    }
    return out
}

fun parseStatus(raw: String): Status {
    val o = JSONObject(raw)
    return Status(
        status = o.optStringOrNull("status") ?: "unknown",
        outdoorC = o.optDoubleOrNull("outdoor_c"),
        indoorEstC = o.optDoubleOrNull("indoor_est_c"),
        peakHour = o.optIntOrNull("forecast_peak_hour"),
        closeHour = o.optIntOrNull("forecast_close_hour"),
        openHour = o.optIntOrNull("forecast_open_hour"),
        hourly = parseHourly(o.optJSONArray("forecast_hourly")),
        updatedUtc = o.optStringOrNull("updated_utc"),
    )
}

/**
 * Pulls the status JSON out of the public Gist the Fly service PATCHes every run.
 *
 * The last good payload is cached, so a flaky network shows stale-but-real numbers
 * (with the "Updated" line giving the age away) rather than dropping to "—".
 */
fun fetchStatus(context: Context): Status? {
    val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
    try {
        val conn = (URL(GIST_API_URL).openConnection() as HttpURLConnection).apply {
            setRequestProperty("User-Agent", "WindowWatch-Android")
            setRequestProperty("Accept", "application/vnd.github+json")
            connectTimeout = 15_000
            readTimeout = 15_000
        }
        val body = conn.use { it.inputStream.bufferedReader().readText() }
        val content = JSONObject(body)
            .getJSONObject("files")
            .getJSONObject(STATUS_FILE)
            .getString("content")
        val parsed = parseStatus(content)
        prefs.edit().putString(CACHE_KEY, content).apply()
        return parsed
    } catch (e: Exception) {
        val cached = prefs.getString(CACHE_KEY, null) ?: return null
        return runCatching { parseStatus(cached) }.getOrNull()
    }
}

private inline fun <T> HttpURLConnection.use(block: (HttpURLConnection) -> T): T =
    try { block(this) } finally { disconnect() }

fun fmtHour(h: Int?): String = when {
    h == null -> "?"
    h == 0 -> "midnight"
    h < 12 -> "${h}am"
    h == 12 -> "noon"
    else -> "${h - 12}pm"
}

fun timeAgo(utc: String?): String {
    if (utc == null) return "never"
    val then = runCatching { Instant.parse(utc) }.getOrNull() ?: return "never"
    val mins = (Instant.now().toEpochMilli() - then.toEpochMilli()) / 60_000
    return when {
        mins < 1 -> "just now"
        mins < 60 -> "${mins}m ago"
        else -> "${mins / 60}h ago"
    }
}

/**
 * Mirrors the dashboard's forecast state machine exactly: closed (since/before, plus
 * reopen), close ahead, past the peak on the evening downcurve, or nothing to do today.
 */
fun forecastLine(d: Status?, nowHour: Int = LocalTime.now().hour): String? {
    if (d == null) return null
    val cH = d.closeHour
    val oH = d.openHour
    val closeUpcoming = cH != null && cH >= nowHour

    if (d.status == "close") {
        var line = when {
            cH == null -> "Keep shut"
            closeUpcoming -> "Close before ${fmtHour(cH)}"
            else -> "Closed since ${fmtHour(cH)}"
        }
        if (oH != null) line += " · reopen around ${fmtHour(oH)}"
        return line
    }

    if (closeUpcoming) {
        var line = "Close before ${fmtHour(cH)}"
        if (oH != null) line += " · reopen around ${fmtHour(oH)}"
        return line
    }

    val peakPast = d.peakHour != null && nowHour > d.peakHour
    if (peakPast || cH != null) {
        val rest = d.hourly.filter { it.first >= nowHour }
        return if (rest.isNotEmpty()) {
            "Sliding to ~${rest.minOf { it.second }.roundToInt()}° tonight"
        } else {
            "Past the peak, cooling from here"
        }
    }
    return "No close expected today"
}
