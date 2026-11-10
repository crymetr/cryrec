package com.crymetr.cryrec

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.RectF
import android.os.SystemClock
import android.view.View

/**
 * Horizontal dBFS meter, -60 .. 0. Bar = RMS (how loud it sounds), thin line = peak, held 1.5 s.
 * Colours: under -45 dBFS RMS is too quiet for good speech recognition, over -3 dBFS peak is near clipping.
 */
class LevelMeter(context: Context) : View(context) {
    private var rms = MIN
    private var peak = MIN
    private var hold = MIN
    private var holdUntil = 0L
    private val d = resources.displayMetrics.density
    private val paint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val tick = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        textSize = 10 * d
        color = context.getColor(R.color.muted)
        textAlign = Paint.Align.CENTER
    }
    private val r = RectF()

    fun set(rmsDb: Double, peakDb: Double) {
        rms = rmsDb.coerceIn(MIN, 0.0)
        peak = peakDb.coerceIn(MIN, 0.0)
        val now = SystemClock.uptimeMillis()
        if (peak >= hold || now > holdUntil) { hold = peak; holdUntil = now + 1500 }
        invalidate()
    }

    override fun onMeasure(w: Int, h: Int) =
        setMeasuredDimension(getDefaultSize(suggestedMinimumWidth, w), (34 * d).toInt())

    private fun x(db: Double) = ((db - MIN) / -MIN * width).toFloat()

    override fun onDraw(c: Canvas) {
        val barH = 14 * d
        val rad = barH / 2
        paint.color = context.getColor(R.color.line)
        r.set(0f, 0f, width.toFloat(), barH)
        c.drawRoundRect(r, rad, rad, paint)
        paint.color = context.getColor(when {
            peak > -3 -> R.color.accent
            rms < QUIET -> R.color.warn
            else -> R.color.ok
        })
        r.set(0f, 0f, x(rms).coerceAtLeast(barH), barH)
        if (rms > MIN) c.drawRoundRect(r, rad, rad, paint)
        paint.color = context.getColor(if (hold > -3) R.color.accent else R.color.text)
        val hx = x(hold).coerceIn(2 * d, width - 2 * d)
        c.drawRect(hx - d, 0f, hx + d, barH, paint)
        for (db in listOf(-60, -45, -30, -18, -12, -6, 0)) {
            val tx = x(db.toDouble()).coerceIn(8 * d, width - 8 * d)
            c.drawText(db.toString(), tx, barH + 14 * d, tick)
        }
    }

    companion object {
        const val MIN = -60.0
        /** RMS below this for a while means "turn the gain up". */
        const val QUIET = -45.0
    }
}
