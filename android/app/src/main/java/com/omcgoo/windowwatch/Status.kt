package com.omcgoo.windowwatch

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.time.Duration
import java.time.Instant
import java.time.LocalTime
import kotlin.math.roundToInt

const val GIST_API_URL = "https://api.github.com/gists/45ba603447bbc3ec258e479f6dee6a20"
const val DASHBOARD_URL = "https://omcgoo.github.io/window-watch/"
private const val STATUS_FILE = "window-watch-status.json"
private const val PREFS = "window_watch"
private const val CACHE_KEY = "status_json"
private const val LAST_OK_KEY = "last_success_utc"
private const val LAST_ERR_KEY = "last_error"

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
 * The last payload we successfully fetched, or null before the first ever fetch.
 *
 * Deliberately does no I/O: this is what the widget renders from. Composition happens on
 * the app-widget broadcast path, which the system gives roughly ten seconds before it
 * treats the process as hung — and at boot, when the launcher is restoring widgets, Wi-Fi
 * has usually not associated yet, so a fetch there would burn the whole budget every time
 * and get the process killed at the worst possible moment. Fetching belongs in
 * RefreshWorker; drawing reads what that last left behind.
 */
fun readCached(context: Context): Status? {
    val cached = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        .getString(CACHE_KEY, null) ?: return null
    return runCatching { parseStatus(cached) }.getOrNull()
}

/**
 * Pulls the status JSON out of the public Gist the Fly service PATCHes every run and caches
 * it for [readCached]. Returns true if the fetch succeeded.
 *
 * Timeouts are deliberately short. The Fly service only rewrites the Gist every 30 minutes
 * (runner.py), so there is nothing to gain from waiting a long time on a bad network — the
 * next run is along shortly, and a caller that gives up quickly costs the user nothing
 * because the cached payload is still on screen.
 *
 * Records the outcome either way: with no developer mode on the phone there is no adb and
 * no logcat, so what MainActivity can show is the only window into why a widget went stale.
 */
fun fetchAndCache(context: Context): Boolean {
    val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
    return try {
        val conn = (URL(GIST_API_URL).openConnection() as HttpURLConnection).apply {
            setRequestProperty("User-Agent", "WindowWatch-Android")
            setRequestProperty("Accept", "application/vnd.github+json")
            connectTimeout = 5_000
            readTimeout = 5_000
        }
        val body = conn.use { it.inputStream.bufferedReader().readText() }
        val content = JSONObject(body)
            .getJSONObject("files")
            .getJSONObject(STATUS_FILE)
            .getString("content")
        parseStatus(content)  // fail before caching rather than storing something unreadable
        prefs.edit()
            .putString(CACHE_KEY, content)
            .putString(LAST_OK_KEY, Instant.now().toString())
            .remove(LAST_ERR_KEY)
            .apply()
        true
    } catch (e: Exception) {
        prefs.edit()
            .putString(LAST_ERR_KEY, "${e.javaClass.simpleName}: ${e.message ?: "no detail"}")
            .apply()
        false
    }
}

/** When the last fetch succeeded, as an ISO instant — null if one never has. */
fun lastSuccess(context: Context): String? =
    context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(LAST_OK_KEY, null)

/** Why the last fetch failed, cleared by the next success. */
fun lastError(context: Context): String? =
    context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(LAST_ERR_KEY, null)

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
 * Whether a payload is old enough that it should say so rather than be presented as current.
 *
 * The Fly service rewrites the Gist every 30 minutes, so anything past that plus slack has
 * missed at least one run. Worth surfacing because the cache means stale data looks exactly
 * like fresh data otherwise — the numbers are real, they are just from a while ago.
 */
fun isStale(utc: String?, threshold: Duration = Duration.ofMinutes(45)): Boolean {
    val then = runCatching { Instant.parse(utc) }.getOrNull() ?: return true
    return Duration.between(then, Instant.now()) > threshold
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
