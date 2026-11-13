package com.crymetr.cryrec

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.text.SpannableStringBuilder
import android.text.method.LinkMovementMethod
import android.text.style.ClickableSpan
import android.view.View
import android.view.inputmethod.EditorInfo
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView

/** Ask a question about the recordings on this phone; the answer cites passages, tap one to open its recording. */
class AskActivity : Activity() {
    private lateinit var question: EditText
    private lateinit var go: Button
    private lateinit var out: TextView
    @Volatile private var closed = false
    private var busy = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val pad = dp(16)
        val root = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(pad, pad, pad, pad) }
        root.addView(TextView(this).apply { text = "Ask your recordings"; textSize = 20f; setTextColor(getColor(R.color.text)) })
        root.addView(TextView(this).apply {
            text = "A question in your own words. Your server's AI answers from the recordings on this phone and shows where each point comes from."
            textSize = 13f; setTextColor(getColor(R.color.muted)); setPadding(0, dp(4), 0, dp(10))
        })
        question = EditText(this).apply {
            hint = "Why did we decide to…?"
            setTextColor(getColor(R.color.text)); setHintTextColor(getColor(R.color.muted))
            textSize = 16f
            maxLines = 3
            imeOptions = EditorInfo.IME_ACTION_SEARCH
            setOnEditorActionListener { _, id, _ -> if (id == EditorInfo.IME_ACTION_SEARCH) { ask(); true } else false }
        }
        root.addView(question)
        go = Button(this, null, 0, android.R.style.Widget_DeviceDefault_Button_Borderless).apply {
            text = "Ask"; isAllCaps = false; textSize = 15f
            setTextColor(getColor(R.color.text)); setBackgroundResource(R.drawable.pill)
            layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT, dp(44)).apply { topMargin = dp(8) }
            setPadding(dp(24), 0, dp(24), 0)
            setOnClickListener { ask() }
        }
        root.addView(go)
        out = TextView(this).apply {
            textSize = 15f; setTextColor(getColor(R.color.text)); setLineSpacing(0f, 1.15f)
            setPadding(dp(14), dp(14), dp(14), dp(14)); setBackgroundResource(R.drawable.card)
            movementMethod = LinkMovementMethod.getInstance()
            visibility = View.GONE
        }
        root.addView(ScrollView(this).apply { addView(out); setPadding(0, dp(12), 0, 0) },
            LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f))
        setContentView(android.widget.FrameLayout(this).apply { fitsSystemWindows = true; addView(root) })
    }

    override fun onDestroy() { closed = true; super.onDestroy() }

    private fun ask() {
        val q = question.text.toString().trim()
        if (q.isEmpty() || busy) return
        if (!Prefs(this).configured) { show("Set your server in Settings first."); return }
        busy = true
        go.isEnabled = false
        show("Thinking…")
        Thread {
            val r = runCatching { Recall.answer(applicationContext, q, { s -> runOnUiThread { if (busy) show(s) } }, { closed }) }
            runOnUiThread {
                busy = false
                go.isEnabled = true
                r.onFailure { show("Could not answer: ${it.message}") }.onSuccess { render(it) }
            }
        }.start()
    }

    private fun show(s: CharSequence) { out.visibility = View.VISIBLE; out.text = s }

    private fun render(a: Recall.Answer) {
        if (a.passages.isEmpty()) {
            show("Nothing in the recordings on this phone matches this question.\nSearched for: " + a.words.joinToString(", "))
            return
        }
        val byN = a.passages.associateBy { it.n }
        val b = SpannableStringBuilder()
        fun link(text: String, p: Recall.Passage) {
            val start = b.length
            b.append(text)
            b.setSpan(object : ClickableSpan() {
                override fun onClick(v: View) {
                    startActivity(Intent(this@AskActivity, DetailActivity::class.java).putExtra("name", p.session)
                        .putExtra("at", p.seconds))
                }
            }, start, b.length, 0)
        }
        var pos = 0
        for (m in Regex("\\[(\\d+(?:\\s*,\\s*\\d+)*)\\]").findAll(a.text)) {  // [3] and [1, 2, 7]
            b.append(a.text, pos, m.range.first)
            for (num in Regex("\\d+").findAll(m.groupValues[1])) {
                val p = num.value.toIntOrNull()?.let { byN[it] }
                if (p != null) link("[${num.value}]", p) else b.append("[${num.value}]")
            }
            pos = m.range.last + 1
        }
        b.append(a.text, pos, a.text.length)
        b.append("\n\nSources (tap to open)\n")
        for (p in a.passages) {
            link("[${p.n}] ${p.session}" + (if (p.seconds >= 0) " · " + formatDuration(p.seconds.toLong()) else " · notes"), p)
            b.append("\n" + p.text.replace("\n", " ").take(160) + "\n\n")
        }
        b.append("An AI wrote this answer from the passages above. Open a source to check it.")
        show(b)
    }

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()
}
