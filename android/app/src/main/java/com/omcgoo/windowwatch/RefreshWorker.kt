package com.omcgoo.windowwatch

import android.content.Context
import androidx.glance.appwidget.updateAll
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.util.concurrent.TimeUnit

private const val PERIODIC_WORK = "window-watch-refresh"
private const val ONE_SHOT_WORK = "window-watch-refresh-now"

/**
 * Fetches the Gist and redraws the widget.
 *
 * This exists because the widget used to fetch during composition, on the app-widget
 * broadcast path. WorkManager is the right home for it instead: it can wait for a network
 * to actually exist, it retries with backoff rather than silently missing a slot, and it
 * restores its own schedule after a reboot or a force-stop (work-runtime declares
 * RECEIVE_BOOT_COMPLETED and a force-stop receiver of its own — so this app does not need
 * a boot receiver, and adding one would just duplicate it).
 */
class RefreshWorker(context: Context, params: WorkerParameters) :
    CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        val ok = withContext(Dispatchers.IO) { fetchAndCache(applicationContext) }
        // Redraw either way: on failure the payload is unchanged but the age shown on the
        // widget has moved on, and letting it cross the stale threshold is the point.
        WindowWatchWidget().updateAll(applicationContext)
        return if (ok) Result.success() else Result.retry()
    }
}

/**
 * Fifteen minutes is WorkManager's floor, and half the Fly service's 30-minute write
 * cadence — so the widget lands within about 15 minutes of each new payload without
 * polling for changes that cannot have happened yet.
 *
 * KEEP rather than UPDATE: a schedule already running is already correct, and replacing it
 * on every app launch would restart the interval and delay the next run indefinitely.
 */
fun scheduleRefresh(context: Context) {
    val request = PeriodicWorkRequestBuilder<RefreshWorker>(15, TimeUnit.MINUTES)
        .setConstraints(
            Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()
        )
        .build()
    WorkManager.getInstance(context).enqueueUniquePeriodicWork(
        PERIODIC_WORK, ExistingPeriodicWorkPolicy.KEEP, request
    )
}

/**
 * Start the schedule *and* fetch once immediately.
 *
 * The immediate fetch is not redundant: composition reads cache only, so without it a
 * newly placed widget would sit on "—" until the first periodic run, which WorkManager is
 * free to defer by up to the full interval. Both calls are cheap and non-blocking — the
 * periodic one is a no-op when already scheduled, and the one-shot waits for a network
 * rather than holding anything up when there isn't one.
 */
fun ensureFresh(context: Context) {
    scheduleRefresh(context)
    refreshNow(context)
}

/** A single refresh right now, for the button in MainActivity. */
fun refreshNow(context: Context) {
    WorkManager.getInstance(context).enqueueUniqueWork(
        ONE_SHOT_WORK,
        androidx.work.ExistingWorkPolicy.REPLACE,
        OneTimeWorkRequestBuilder<RefreshWorker>()
            .setConstraints(
                Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()
            )
            .build(),
    )
}
