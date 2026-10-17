package com.crymetr.cryrec

import android.accessibilityservice.AccessibilityService
import android.annotation.SuppressLint
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Intent
import android.graphics.PixelFormat
import android.graphics.drawable.GradientDrawable
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.HapticFeedbackConstants
import android.view.MotionEvent
import android.view.View
import android.view.ViewConfiguration
import android.view.WindowManager
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import kotlin.math.abs

/**
 * Floating dictation button. Shows next to the screen edge while a text field has focus, in any app.
 * Tap: start; tap again: stop and send. Long press: cancel. Drag: move it.
 * The text goes into the focused field (set text, or paste), or to the clipboard if there is none.
 */
class BubbleService : AccessibilityService(), Dictation.Listener {
    private val ui = Handler(Looper.getMainLooper())
    private lateinit var wm: WindowManager
    private lateinit var dictation: Dictation
    private var root: LinearLayout? = null
    private lateinit var bubble: ImageView
    private lateinit var label: TextView
    private lateinit var params: WindowManager.LayoutParams
    private var target: AccessibilityNodeInfo? = null
    private var shown = false
    private val checkFocus = Runnable { updateVisibility() }

    override fun onServiceConnected() {
        wm = getSystemService(WindowManager::class.java)
        dictation = Dictation(this, this)
        buildView()
        updateVisibility()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        ui.removeCallbacks(checkFocus)
        ui.postDelayed(checkFocus, 150)
    }

    override fun onInterrupt() {}

    override fun onDestroy() {
        if (::dictation.isInitialized) dictation.cancel()
        root?.let { if (shown) wm.removeView(it) }
        super.onDestroy()
    }

    private fun focusedField(): AccessibilityNodeInfo? =
        findFocus(AccessibilityNodeInfo.FOCUS_INPUT)?.takeIf { it.isEditable }

    /** Visible while a text field has focus (and always while recording or sending). */
    private fun updateVisibility() {
        val field = focusedField()
        // Our own keyboard already has a mic.
        val ownKeyboard = windows.any { it.type == android.view.accessibility.AccessibilityWindowInfo.TYPE_INPUT_METHOD &&
            it.root?.packageName == packageName }
        val want = dictation.recording || dictation.sending ||
            (field != null && !ownKeyboard && Prefs(this).configured)
        if (want == shown) return
        val v = root ?: return
        if (want) {
            wm.addView(v, params)
            Dictation.warmUp(this)
        } else wm.removeView(v)
        shown = want
    }

    @SuppressLint("ClickableViewAccessibility")
    private fun buildView() {
        val p = Prefs(this)
        bubble = ImageView(this).apply {
            setImageResource(R.drawable.ic_mic)
            setPadding(dp(14), dp(14), dp(14), dp(14))
            elevation = dp(6).toFloat()
            contentDescription = "Dictate"
        }
        label = TextView(this).apply {
            textSize = 12f
            setTextColor(0xFFFFFFFF.toInt())
            setPadding(dp(8), dp(3), dp(8), dp(3))
            background = GradientDrawable().apply { setColor(0xCC1E2230.toInt()); cornerRadius = dp(10).toFloat() }
            visibility = View.GONE
        }
        root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            setPadding(dp(4), dp(4), dp(4), dp(4))
            addView(bubble, LinearLayout.LayoutParams(dp(52), dp(52)))
            addView(label, LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT)
                .apply { topMargin = dp(4) })
        }
        setIdleLook()
        params = WindowManager.LayoutParams(
            WindowManager.LayoutParams.WRAP_CONTENT, WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
            PixelFormat.TRANSLUCENT,
        ).apply {
            gravity = Gravity.TOP or (if (p.bubbleRight) Gravity.END else Gravity.START)
            x = 0
            y = p.bubbleY.takeIf { it >= 0 } ?: (resources.displayMetrics.heightPixels / 3)
        }

        val slop = ViewConfiguration.get(this).scaledTouchSlop
        var downX = 0f; var downY = 0f; var startY = 0; var dragging = false; var longPressed = false
        val longPress = Runnable {
            longPressed = true
            if (dictation.recording || dictation.sending) {
                bubble.performHapticFeedback(HapticFeedbackConstants.LONG_PRESS)
                dictation.cancel()
            }
        }
        bubble.setOnTouchListener { _, e ->
            when (e.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    downX = e.rawX; downY = e.rawY; startY = params.y; dragging = false; longPressed = false
                    ui.postDelayed(longPress, ViewConfiguration.getLongPressTimeout().toLong())
                }
                MotionEvent.ACTION_MOVE -> {
                    if (!dragging && (abs(e.rawX - downX) > slop || abs(e.rawY - downY) > slop)) {
                        dragging = true
                        ui.removeCallbacks(longPress)
                    }
                    if (dragging) {
                        params.y = (startY + (e.rawY - downY)).toInt().coerceAtLeast(0)
                        wm.updateViewLayout(root, params)
                    }
                }
                MotionEvent.ACTION_UP -> {
                    ui.removeCallbacks(longPress)
                    if (dragging) {
                        // Snap to the nearer edge.
                        val right = e.rawX > resources.displayMetrics.widthPixels / 2
                        params.gravity = Gravity.TOP or (if (right) Gravity.END else Gravity.START)
                        wm.updateViewLayout(root, params)
                        Prefs(this).apply { bubbleRight = right; bubbleY = params.y }
                    } else if (!longPressed) onTap()
                }
                MotionEvent.ACTION_CANCEL -> ui.removeCallbacks(longPress)
            }
            true
        }
    }

    private fun onTap() {
        when {
            dictation.sending -> {}
            dictation.recording -> dictation.stopAndSend()
            !dictation.micAllowed() -> startActivity(Intent(this, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK).putExtra(MainActivity.EXTRA_ASK_MIC, true))
            else -> {
                target = focusedField()
                if (dictation.start()) {
                    bubble.performHapticFeedback(HapticFeedbackConstants.VIRTUAL_KEY)
                    bubble.background = circle(getColor(R.color.accent))
                    bubble.setImageResource(R.drawable.ic_stop)
                    showLabel("0:00")
                }
            }
        }
    }

    override fun onTick(seconds: Long) = showLabel(formatDuration(seconds))

    override fun onSending() {
        bubble.performHapticFeedback(HapticFeedbackConstants.VIRTUAL_KEY)
        bubble.setImageResource(R.drawable.ic_mic)
        bubble.alpha = 0.6f
        showLabel("Writing…")
    }

    override fun onText(text: String) {
        setIdleLook()
        insert(text)
        updateVisibility()
    }

    override fun onError(message: String) {
        setIdleLook()
        showLabel(message)
        ui.postDelayed({ if (!dictation.recording && !dictation.sending) label.visibility = View.GONE }, 4000)
        updateVisibility()
    }

    override fun onIdle() {
        setIdleLook()
        updateVisibility()
    }

    private fun setIdleLook() {
        bubble.background = circle(0xFF1E2230.toInt())
        bubble.setImageResource(R.drawable.ic_mic)
        bubble.alpha = 0.92f
        label.visibility = View.GONE
    }

    private fun showLabel(text: String) {
        label.text = text
        label.visibility = View.VISIBLE
    }

    /** Type into the field that had focus: replace the selection (or insert at the cursor). */
    private fun insert(text: String) {
        val node = focusedField() ?: target?.takeIf { it.refresh() && it.isEditable }
        target = null
        if (node == null) return copyOnly(text)
        if (!node.isPassword) {
            val current = if (node.isShowingHintText) "" else node.text?.toString() ?: ""
            var s = node.textSelectionStart
            var e = node.textSelectionEnd
            if (s < 0 || s > current.length) s = current.length
            if (e < s || e > current.length) e = s
            val before = current.substring(0, s)
            val ins = (if (before.isEmpty() || before.last().isWhitespace()) "" else " ") + text
            val args = Bundle().apply {
                putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, before + ins + current.substring(e))
            }
            if (node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)) {
                val cursor = Bundle().apply {
                    putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT, s + ins.length)
                    putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT, s + ins.length)
                }
                node.performAction(AccessibilityNodeInfo.ACTION_SET_SELECTION, cursor)
                return
            }
        }
        // Fields that refuse SET_TEXT (some web views, password boxes): paste instead.
        clip(text)
        if (!node.performAction(AccessibilityNodeInfo.ACTION_PASTE))
            Toast.makeText(this, "Copied. Long-press the field and paste.", Toast.LENGTH_SHORT).show()
    }

    private fun copyOnly(text: String) {
        clip(text)
        Toast.makeText(this, "No text field. Copied to the clipboard.", Toast.LENGTH_SHORT).show()
    }

    private fun clip(text: String) =
        getSystemService(ClipboardManager::class.java).setPrimaryClip(ClipData.newPlainText("Dictation", text))

    private fun circle(color: Int) = GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(color) }

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    companion object {
        /** Is the floating button turned on in Android's accessibility settings? */
        fun enabled(context: android.content.Context): Boolean {
            val list = android.provider.Settings.Secure.getString(context.contentResolver,
                android.provider.Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES) ?: return false
            return list.split(':').any { it.startsWith(context.packageName + "/") && it.endsWith("BubbleService") }
        }
    }
}
