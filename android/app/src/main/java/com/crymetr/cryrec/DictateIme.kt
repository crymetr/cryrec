package com.crymetr.cryrec

import android.annotation.SuppressLint
import android.content.Intent
import android.inputmethodservice.InputMethodService
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.view.Gravity
import android.view.KeyEvent
import android.view.MotionEvent
import android.view.View
import android.view.inputmethod.EditorInfo
import android.view.inputmethod.InputMethodManager
import android.widget.ImageButton
import android.widget.LinearLayout
import android.widget.TextView

/**
 * Dictation keyboard. Tap the mic to start and tap again to send, or hold it and let go to send.
 * The clip goes to the server's /dictate; the text is typed where the cursor is.
 */
class DictateIme : InputMethodService(), Dictation.Listener {
    private val ui = Handler(Looper.getMainLooper())
    private val dictation by lazy { Dictation(this, this) }
    private var downAt = 0L
    private lateinit var status: TextView
    private lateinit var mic: ImageButton

    @SuppressLint("ClickableViewAccessibility")
    override fun onCreateInputView(): View {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(getColor(R.color.kb_bg))
            setPadding(dp(8), dp(8), dp(8), dp(10))
            // Keep the keys above the gesture bar / nav buttons.
            setOnApplyWindowInsetsListener { v, insets ->
                val nav = insets.getInsets(android.view.WindowInsets.Type.navigationBars()).bottom
                v.setPadding(dp(8), dp(8), dp(8), dp(10) + nav)
                insets
            }
        }
        status = TextView(this).apply {
            gravity = Gravity.CENTER
            textSize = 14f
            setTextColor(getColor(R.color.muted))
            setPadding(dp(8), dp(4), dp(8), dp(8))
            setOnClickListener { if (!dictation.micAllowed() || !Prefs(this@DictateIme).configured) openApp() }
        }
        root.addView(status)

        val row = LinearLayout(this).apply { gravity = Gravity.CENTER_VERTICAL }
        row.addView(key(R.drawable.ic_keyboard, "Switch keyboard") {
            if (!switchToPreviousInputMethod()) getSystemService(InputMethodManager::class.java).showInputMethodPicker()
        }.apply {
            setOnLongClickListener { getSystemService(InputMethodManager::class.java).showInputMethodPicker(); true }
        })
        mic = ImageButton(this).apply {
            setImageResource(R.drawable.ic_mic)
            scaleType = android.widget.ImageView.ScaleType.FIT_CENTER
            setPadding(dp(22), dp(22), dp(22), dp(22))
            setBackgroundResource(R.drawable.rec_button)
            contentDescription = "Dictate"
            elevation = dp(3).toFloat()
            setOnTouchListener { _, e -> onMicTouch(e); true }
        }
        row.addView(mic, LinearLayout.LayoutParams(dp(76), dp(76)).apply { marginStart = dp(28); marginEnd = dp(28) })
        val back = key(R.drawable.ic_backspace, "Delete") {}
        back.setOnTouchListener { _, e -> onBackspaceTouch(e); true }
        row.addView(back)
        row.gravity = Gravity.CENTER
        root.addView(row, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, dp(92)))

        val bottom = LinearLayout(this).apply { setPadding(0, dp(6), 0, 0) }
        bottom.addView(TextView(this).apply {
            text = "space"
            gravity = Gravity.CENTER
            setTextColor(getColor(R.color.muted))
            setBackgroundResource(R.drawable.key)
            setOnClickListener { currentInputConnection?.commitText(" ", 1) }
        }, LinearLayout.LayoutParams(0, dp(44), 1f).apply { marginEnd = dp(6) })
        bottom.addView(key(R.drawable.ic_enter, "Enter") { enter() }, LinearLayout.LayoutParams(dp(72), dp(44)))
        root.addView(bottom)
        return root
    }

    override fun onStartInputView(info: EditorInfo?, restarting: Boolean) {
        super.onStartInputView(info, restarting)
        if (!dictation.recording && !dictation.sending) idleStatus()
        Dictation.warmUp(this)  // load the server's model as soon as the keyboard shows
    }

    override fun onFinishInputView(finishingInput: Boolean) {
        dictation.cancel()
        super.onFinishInputView(finishingInput)
    }

    private fun idleStatus() {
        status.text = when {
            !dictation.micAllowed() -> "Open CryRec once to allow the microphone (tap here)"
            !Prefs(this).configured -> "Set your server in CryRec first (tap here)"
            else -> "Tap to talk, tap again to send. Or hold."
        }
        mic.setImageResource(R.drawable.ic_mic)
        mic.alpha = 1f
    }

    private fun onMicTouch(e: MotionEvent) {
        when (e.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                downAt = SystemClock.elapsedRealtime()
                if (!dictation.recording && !dictation.sending) {
                    if (!dictation.micAllowed() || !Prefs(this).configured) return openApp()
                    if (dictation.start()) mic.setImageResource(R.drawable.ic_stop)
                } else downAt = 0  // second tap: send on release
            }
            MotionEvent.ACTION_UP -> {
                if (!dictation.recording) return
                val held = downAt > 0 && SystemClock.elapsedRealtime() - downAt > HOLD_MS
                if (downAt == 0L || held) dictation.stopAndSend()
            }
        }
    }

    override fun onTick(seconds: Long) {
        status.text = "Listening  ${formatDuration(seconds)}   ·   tap ⌫ to cancel"
    }

    override fun onSending() {
        mic.setImageResource(R.drawable.ic_mic)
        mic.alpha = 0.5f
        status.text = "Writing…"
    }

    override fun onText(text: String) {
        idleStatus()
        val ic = currentInputConnection ?: return
        val before = ic.getTextBeforeCursor(1, 0)
        val space = if (before.isNullOrEmpty() || before.last().isWhitespace()) "" else " "
        ic.commitText(space + text, 1)
    }

    override fun onError(message: String) {
        idleStatus()
        status.text = message
    }

    override fun onIdle() {
        if (::status.isInitialized) idleStatus()
    }

    private val repeatDelete = object : Runnable {
        override fun run() { deleteOne(); ui.postDelayed(this, 60) }
    }

    private fun onBackspaceTouch(e: MotionEvent) {
        when (e.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                if (dictation.recording || dictation.sending) { dictation.cancel(); return }
                deleteOne()
                ui.postDelayed(repeatDelete, 450)
            }
            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> ui.removeCallbacks(repeatDelete)
        }
    }

    private fun deleteOne() = sendDownUpKeyEvents(KeyEvent.KEYCODE_DEL)

    private fun enter() {
        val info = currentInputEditorInfo
        val action = (info?.imeOptions ?: 0) and EditorInfo.IME_MASK_ACTION
        val multiLine = ((info?.inputType ?: 0) and android.text.InputType.TYPE_TEXT_FLAG_MULTI_LINE) != 0
        if (!multiLine && action != EditorInfo.IME_ACTION_NONE && action != EditorInfo.IME_ACTION_UNSPECIFIED &&
            ((info?.imeOptions ?: 0) and EditorInfo.IME_FLAG_NO_ENTER_ACTION) == 0)
            currentInputConnection?.performEditorAction(action)
        else sendDownUpKeyEvents(KeyEvent.KEYCODE_ENTER)
    }

    private fun openApp() {
        val target = if (dictation.micAllowed()) SettingsActivity::class.java else MainActivity::class.java
        startActivity(Intent(this, target).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK).putExtra(MainActivity.EXTRA_ASK_MIC, true))
    }

    private fun key(icon: Int, label: String, onClick: () -> Unit) = ImageButton(this).apply {
        setImageResource(icon)
        contentDescription = label
        setBackgroundResource(R.drawable.key)
        setOnClickListener { onClick() }
        layoutParams = LinearLayout.LayoutParams(dp(60), dp(48))
    }

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    companion object {
        private const val HOLD_MS = 400
    }
}
