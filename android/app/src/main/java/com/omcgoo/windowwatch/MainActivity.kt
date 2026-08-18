package com.omcgoo.windowwatch

import android.app.Activity
import android.appwidget.AppWidgetManager
import android.content.ComponentName
import android.content.Intent
import android.graphics.Color
import android.net.Uri
import android.os.Bundle
import android.view.Gravity
import android.view.ViewGroup
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView

/**
 * A widget-only app is confusing without somewhere to land, so this screen does the two
 * things you'd want after installing: put the widget on the home screen, and open the
 * dashboard. Everything else happens on the home screen itself.
 */
class MainActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setBackgroundColor(Color.parseColor("#0F3460"))
            setPadding(64, 64, 64, 64)
        }

        root.addView(TextView(this).apply {
            text = "Window Watch"
            textSize = 28f
            setTextColor(Color.parseColor("#4DB6FF"))
            gravity = Gravity.CENTER
        })

        root.addView(TextView(this).apply {
            text = "Add the widget to your home screen to see whether the windows " +
                "should be open or closed. It refreshes every 30 minutes."
            textSize = 15f
            setTextColor(Color.parseColor("#B0FFFFFF"))
            gravity = Gravity.CENTER
            setPadding(0, 32, 0, 48)
        })

        root.addView(Button(this).apply {
            text = "Add widget to home screen"
            setOnClickListener { pinWidget() }
        })

        root.addView(Button(this).apply {
            text = "Open dashboard"
            setOnClickListener {
                startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(DASHBOARD_URL)))
            }
        })

        setContentView(root, ViewGroup.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
    }

    /** Launchers that support pinning show a placement dialog; older ones don't respond. */
    private fun pinWidget() {
        val manager = getSystemService(AppWidgetManager::class.java)
        val provider = ComponentName(this, WindowWatchReceiver::class.java)
        val supported = manager?.isRequestPinAppWidgetSupported == true
        if (supported && manager!!.requestPinAppWidget(provider, null, null)) return
        android.widget.Toast.makeText(
            this,
            "Your launcher doesn't support one-tap pinning — long-press the home " +
                "screen, choose Widgets, then Window Watch.",
            android.widget.Toast.LENGTH_LONG,
        ).show()
    }
}
