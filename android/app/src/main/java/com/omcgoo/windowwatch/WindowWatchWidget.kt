package com.omcgoo.windowwatch

import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.DpSize
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.glance.GlanceId
import androidx.glance.GlanceModifier
import androidx.glance.LocalSize
import androidx.glance.action.clickable
import androidx.glance.appwidget.GlanceAppWidget
import androidx.glance.appwidget.GlanceAppWidgetReceiver
import androidx.glance.appwidget.SizeMode
import androidx.glance.appwidget.action.actionStartActivity
import androidx.glance.appwidget.cornerRadius
import androidx.glance.appwidget.provideContent
import androidx.glance.background
import androidx.glance.layout.Alignment
import androidx.glance.layout.Column
import androidx.glance.layout.Row
import androidx.glance.layout.RowScope
import androidx.glance.layout.Spacer
import androidx.glance.layout.fillMaxSize
import androidx.glance.layout.height
import androidx.glance.layout.padding
import androidx.glance.layout.width
import androidx.glance.text.FontWeight
import androidx.glance.text.Text
import androidx.glance.text.TextAlign
import androidx.glance.text.TextStyle
import androidx.glance.unit.ColorProvider
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

private data class Theme(val bg: Color, val accent: Color, val label: String, val hint: String)

private val THEMES = mapOf(
    "open" to Theme(Color(0xFF0F3460), Color(0xFF4DB6FF), "OPEN", "let the air through"),
    "close" to Theme(Color(0xFF7B1E1E), Color(0xFFFF8C8C), "CLOSE", "hold the cool in"),
)
private val UNKNOWN = Theme(Color(0xFF1A1A2E), Color(0xFF888888), "—", "waiting for data")

private val WHITE_STRONG = ColorProvider(Color(0xD9FFFFFF))
private val WHITE_DIM = ColorProvider(Color(0x73FFFFFF))
private val WHITE_FAINT = ColorProvider(Color(0x47FFFFFF))
private val AMBER = ColorProvider(Color(0xE6FFD060))

/** One cell of height is roughly 50dp; anything taller gets the fuller layout. */
private val COMPACT = DpSize(250.dp, 48.dp)
private val TALL = DpSize(250.dp, 110.dp)

class WindowWatchWidget : GlanceAppWidget() {
    override val sizeMode = SizeMode.Responsive(setOf(COMPACT, TALL))

    override suspend fun provideGlance(context: Context, id: GlanceId) {
        val data = withContext(Dispatchers.IO) { fetchStatus(context) }
        provideContent { WidgetBody(data) }
    }
}

class WindowWatchReceiver : GlanceAppWidgetReceiver() {
    override val glanceAppWidget: GlanceAppWidget = WindowWatchWidget()
}

@Composable
private fun WidgetBody(data: Status?) {
    val theme = THEMES[data?.status] ?: UNKNOWN
    val dashboard = Intent(Intent.ACTION_VIEW, Uri.parse(DASHBOARD_URL))
        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    val compact = LocalSize.current.height < 80.dp

    Row(
        modifier = GlanceModifier
            .fillMaxSize()
            .background(theme.bg)
            .cornerRadius(16.dp)
            .clickable(actionStartActivity(dashboard))
            .padding(horizontal = if (compact) 14.dp else 18.dp, vertical = if (compact) 6.dp else 14.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        if (compact) CompactContent(theme, data) else TallContent(theme, data)
    }
}

/**
 * 4x1. Only two lines fit, so it drops the hint and the "updated" age and keeps what
 * actually drives a decision: the verdict, the two temperatures, and what's coming.
 */
@Composable
private fun RowScope.CompactContent(theme: Theme, data: Status?) {
    Text(
        text = theme.label,
        style = TextStyle(
            fontSize = 22.sp,
            fontWeight = FontWeight.Bold,
            color = ColorProvider(theme.accent),
        ),
        maxLines = 1,
    )
    Spacer(GlanceModifier.defaultWeight())
    Column(horizontalAlignment = Alignment.End) {
        Text(
            text = tempLine(data) ?: "no reading",
            style = TextStyle(fontSize = 13.sp, fontWeight = FontWeight.Medium,
                color = WHITE_STRONG, textAlign = TextAlign.End),
            maxLines = 1,
        )
        forecastLine(data)?.let {
            Spacer(GlanceModifier.height(2.dp))
            Text(
                text = it,
                style = TextStyle(fontSize = 11.sp, color = AMBER, textAlign = TextAlign.End),
                maxLines = 1,
            )
        }
    }
}

/** 4x2 and taller — the original Scriptable layout, with room to breathe. */
@Composable
private fun RowScope.TallContent(theme: Theme, data: Status?) {
    Column {
        Text(
            text = theme.label,
            style = TextStyle(fontSize = 34.sp, fontWeight = FontWeight.Bold,
                color = ColorProvider(theme.accent)),
            maxLines = 1,
        )
        Spacer(GlanceModifier.height(4.dp))
        Text(
            text = theme.hint,
            style = TextStyle(fontSize = 12.sp,
                color = ColorProvider(theme.accent.copy(alpha = 0.55f))),
            maxLines = 1,
        )
    }
    Spacer(GlanceModifier.width(8.dp))
    Spacer(GlanceModifier.defaultWeight())
    Column(horizontalAlignment = Alignment.End) {
        data?.outdoorC?.let {
            Text(
                text = "%.1f°C outside".format(it),
                style = TextStyle(fontSize = 14.sp, fontWeight = FontWeight.Medium,
                    color = WHITE_STRONG, textAlign = TextAlign.End),
                maxLines = 1,
            )
            Spacer(GlanceModifier.height(2.dp))
        }
        data?.indoorEstC?.let {
            Text(
                text = "%.1f°C inside".format(it),
                style = TextStyle(fontSize = 12.sp, color = WHITE_DIM, textAlign = TextAlign.End),
                maxLines = 1,
            )
            Spacer(GlanceModifier.height(5.dp))
        }
        forecastLine(data)?.let {
            Text(
                text = it,
                style = TextStyle(fontSize = 12.sp, color = AMBER, textAlign = TextAlign.End),
                maxLines = 2,
            )
            Spacer(GlanceModifier.height(5.dp))
        }
        Text(
            text = "Updated ${timeAgo(data?.updatedUtc)}",
            style = TextStyle(fontSize = 11.sp, color = WHITE_FAINT, textAlign = TextAlign.End),
            maxLines = 1,
        )
    }
}

/** "23.3° out · 25.1° in", skipping either half if that sensor had nothing to say. */
private fun tempLine(data: Status?): String? {
    val out = data?.outdoorC?.let { "%.1f° out".format(it) }
    val inside = data?.indoorEstC?.let { "%.1f° in".format(it) }
    return listOfNotNull(out, inside).takeIf { it.isNotEmpty() }?.joinToString(" · ")
}
