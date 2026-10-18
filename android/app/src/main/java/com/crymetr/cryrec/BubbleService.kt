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
 * Floating dictation button. Shows a little above the keyboard while you type, in any app.
 * Tap: start; tap again: stop and send. Long press: cancel. Drag: move it (side and height are remembered).
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
    private val checkFocusLate = Runnable { updateVisibility() }
    /** While shown, look again every second so a missed "keyboard closed" event can't leave it stuck. */
    private val poll = Runnable { updateVisibility() }
    private var keyboardTop = 0  // px from the bottom of the screen to the top of the keyboard

    override fun onServiceConnected() {
        wm = getSystemService(WindowManager::class.java)
        dictation = Dictation(this, this)
        buildView()
        updateVisibility()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        ui.removeCallbacks(checkFocus)
        ui.removeCallbacks(checkFocusLate)
        ui.postDelayed(checkFocus, 150)
        ui.postDelayed(checkFocusLate, 700)  // the keyboard window can show up after the focus event
    }

    override fun onInterrupt() {}

    override fun onDestroy() {
        ui.removeCallbacksAndMessages(null)
        if (::dictation.isInitialized) dictation.cancel()
        root?.let { if (shown) wm.removeView(it) }
        super.onDestroy()
    }

    /**
     * The text field being typed in. Chrome and other web views report the page container as the
     * input focus, so when that isn't editable, look inside the active window for a focused editable node.
     */
    private fun focusedField(): AccessibilityNodeInfo? {
        val f = findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
        if (f?.isEditable == true) return f
        val root = (f ?: windows.firstOrNull { it.isActive }?.root) ?: return null
        val queue = ArrayDeque<AccessibilityNodeInfo>().apply { add(root) }
        var seen = 0
        while (queue.isNotEmpty() && seen++ < 3000) {
            val n = queue.removeFirst()
            if (n.isEditable && n.isFocused) return n
            for (i in 0 until n.childCount) n.getChild(i)?.let { queue.add(it) }
        }
        return null
    }

    /**
     * Visible only while a keyboard is on screen, floating a little above it
     * (and always while recording or sending, so a hidden keyboard can't strand a dictation).
     * Any keyboard counts: some apps and web pages never report which field has focus.
     */
    private fun updateVisibility() {
        ui.removeCallbacks(poll)
        val ime = windows.firstOrNull { it.type == android.view.accessibility.AccessibilityWindowInfo.TYPE_INPUT_METHOD }
        val ownKeyboard = ime?.root?.packageName == packageName  // our keyboard has its own mic
        val typing = ime != null && !ownKeyboard && Prefs(this).configured
        val want = dictation.recording || dictation.sending || typing
        val v = root ?: return
        if (ime != null && typing) {
            val r = android.graphics.Rect().also { ime.getBoundsInScreen(it) }
            keyboardTop = wm.currentWindowMetrics.bounds.height() - r.top
            val y = keyboardTop + dp(Prefs(this).bubbleLift)
            if (y != params.y) {
                params.y = y
                if (shown) wm.updateViewLayout(v, params)
            }
        }
        if (want != shown) {
            if (want) {
                wm.addView(v, params)
                Dictation.warmUp(this)
            } else wm.removeView(v)
            shown = want
        }
        if (shown) ui.postDelayed(poll, 1000)
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
            addView(label, LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT)
                .apply { bottomMargin = dp(4) })
            addView(bubble, LinearLayout.LayoutParams(dp(52), dp(52)))
        }
        setIdleLook()
        params = WindowManager.LayoutParams(
            WindowManager.LayoutParams.WRAP_CONTENT, WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
            PixelFormat.TRANSLUCENT,
        ).apply {
            gravity = Gravity.BOTTOM or (if (p.bubbleRight) Gravity.END else Gravity.START)
            x = dp(4)
            y = 0
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
                    if (dragging) {  // up/down: anywhere between the keyboard and the top of the screen
                        val top = wm.currentWindowMetrics.bounds.height() - dp(140)
                        params.y = (startY - (e.rawY - downY)).toInt().coerceIn(keyboardTop, maxOf(keyboardTop, top))
                        wm.updateViewLayout(root, params)
                    }
                }
                MotionEvent.ACTION_UP -> {
                    ui.removeCallbacks(longPress)
                    if (dragging) {
                        // Snap to the nearer side; remember the side and the height above the keyboard.
                        val right = e.rawX > resources.displayMetrics.widthPixels / 2
                        params.gravity = Gravity.BOTTOM or (if (right) Gravity.END else Gravity.START)
                        wm.updateViewLayout(root, params)
                        Prefs(this).apply {
                            bubbleRight = right
                            bubbleLift = ((params.y - keyboardTop) / resources.displayMetrics.density).toInt()
                        }
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
