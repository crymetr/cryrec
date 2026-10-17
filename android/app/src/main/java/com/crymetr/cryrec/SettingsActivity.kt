package com.crymetr.cryrec

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.provider.Settings
import android.text.InputType
import android.view.View
import android.view.inputmethod.InputMethodManager
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Spinner
import android.widget.TextView

class SettingsActivity : Activity() {
    private lateinit var p: Prefs
    private lateinit var url: EditText
    private lateinit var key: EditText
    private lateinit var language: Spinner
    private lateinit var notes: Spinner
    private lateinit var clean: Spinner
    private lateinit var name: EditText
    private lateinit var polish: CheckBox
    private lateinit var vocab: EditText
    private lateinit var form: LinearLayout
    private lateinit var bubbleState: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        p = Prefs(this)
        form = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(16), dp(20), dp(32))
        }
        form.addView(TextView(this).apply { text = "Settings"; textSize = 22f; setTextColor(getColor(R.color.text)) })

        header("Server")
        hint("Your CryRec server does the transcripts and notes. Use the same address and key as the desktop app.")
        url = field("Address, like https://example.com", p.serverUrl, InputType.TYPE_TEXT_VARIATION_URI)
        key = field("Key", p.serverKey, InputType.TYPE_TEXT_VARIATION_PASSWORD)
        val result = hint("")
        form.addView(button("Test connection") {
            save()
            result.text = "Testing…"
            Thread {
                val msg = try {
                    val h = Api(p).health()
                    "Connected. Server ${h.optString("version")}, model ${h.optString("model")}."
                } catch (e: Exception) {
                    "Failed: ${e.message ?: e.javaClass.simpleName}"
                }
                runOnUiThread { result.text = msg }
            }.start()
        })

        header("Transcript")
        language = spinner("Language", Prefs.LANGUAGES, Prefs.LANGUAGES.indexOf(p.language).coerceAtLeast(0))
        clean = spinner("Clean up the transcript", Prefs.CLEAN_ENGINES.values.toList(),
            Prefs.CLEAN_ENGINES.keys.indexOf(p.cleanEngine).coerceAtLeast(0))
        vocab = field("Vocabulary: one name or term per line, or \"wrong -> right\"", p.vocabulary,
            InputType.TYPE_TEXT_FLAG_MULTI_LINE).apply { minLines = 3; gravity = android.view.Gravity.TOP }
        hint("The server's own vocabulary list is used too.")

        header("Notes")
        notes = spinner("Write notes with", Prefs.NOTES_ENGINES.values.toList(),
            Prefs.NOTES_ENGINES.keys.indexOf(p.notesEngine).coerceAtLeast(0))
        name = field("Your name (used in the notes)", p.myName, InputType.TYPE_TEXT_VARIATION_PERSON_NAME)

        header("Dictation keyboard")
        hint("Talk instead of typing in any app. Turn the keyboard on, then pick it with the keyboard button at the bottom of the screen.")
        polish = CheckBox(this).apply {
            text = "Fix punctuation and filler words (cloud AI on your server)"
            isChecked = p.polish
            setTextColor(getColor(R.color.text))
        }
        form.addView(polish)
        val kb = LinearLayout(this)
        kb.addView(button("Turn on keyboard") { startActivity(Intent(Settings.ACTION_INPUT_METHOD_SETTINGS)) })
        kb.addView(button("Switch keyboard") { getSystemService(InputMethodManager::class.java).showInputMethodPicker() })
        form.addView(kb)

        header("Floating mic")
        hint("A small mic button that shows next to the screen edge whenever you type, in any app, with any keyboard. " +
            "Tap it, talk, tap again: the text goes into the field. Long press cancels, drag moves it.")
        bubbleState = hint("")
        hint("If Android says \"Restricted setting\": open App info, tap ⋮ (top right), \"Allow restricted settings\", then turn it on again.")
        val fb = LinearLayout(this)
        fb.addView(button("Turn on floating mic") { startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)) })
        fb.addView(button("App info") {
            startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, android.net.Uri.fromParts("package", packageName, null)))
        })
        form.addView(fb)

        hint("CryRec ${packageManager.getPackageInfo(packageName, 0).versionName}").setPadding(0, dp(24), 0, 0)
        setContentView(ScrollView(this).apply { fitsSystemWindows = true; addView(form) })
    }

    override fun onResume() {
        super.onResume()
        val on = BubbleService.enabled(this)
        bubbleState.text = if (on) "On." else "Off. Turn it on under Accessibility > CryRec floating mic."
        bubbleState.setTextColor(getColor(if (on) R.color.ok else R.color.muted))
    }

    override fun onPause() {
        save()
        super.onPause()
    }

    private fun save() {
        p.serverUrl = url.text.toString()
        p.serverKey = key.text.toString()
        p.language = Prefs.LANGUAGES[language.selectedItemPosition]
        p.cleanEngine = Prefs.CLEAN_ENGINES.keys.elementAt(clean.selectedItemPosition)
        p.notesEngine = Prefs.NOTES_ENGINES.keys.elementAt(notes.selectedItemPosition)
        p.myName = name.text.toString()
        p.polish = polish.isChecked
        p.vocabulary = vocab.text.toString()
    }

    private fun header(t: String) = form.addView(TextView(this).apply {
        text = t
        textSize = 13f
        setTypeface(typeface, android.graphics.Typeface.BOLD)
        setTextColor(getColor(R.color.accent))
        setPadding(0, dp(24), 0, dp(4))
    })

    private fun hint(t: String) = TextView(this).apply {
        text = t
        textSize = 13f
        setTextColor(getColor(R.color.muted))
        setPadding(0, dp(2), 0, dp(6))
        form.addView(this)
    }

    private fun field(label: String, value: String, variation: Int): EditText {
        form.addView(TextView(this).apply {
            text = label; textSize = 13f; setTextColor(getColor(R.color.muted)); setPadding(0, dp(8), 0, 0)
        })
        return input(value, variation)
    }

    private fun input(value: String, variation: Int) = EditText(this).apply {
        setText(value)
        inputType = InputType.TYPE_CLASS_TEXT or variation
        textSize = 15f
        setTextColor(getColor(R.color.text))
        form.addView(this, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))
    }

    private fun spinner(label: String, items: List<String>, selected: Int): Spinner {
        form.addView(TextView(this).apply {
            text = label; textSize = 13f; setTextColor(getColor(R.color.muted)); setPadding(0, dp(8), 0, 0)
        })
        return Spinner(this).apply {
            adapter = ArrayAdapter(this@SettingsActivity, android.R.layout.simple_spinner_dropdown_item, items)
            setSelection(selected)
            form.addView(this)
        }
    }

    private fun button(t: String, onClick: (View) -> Unit) = Button(this).apply {
        text = t
        isAllCaps = false
        setOnClickListener(onClick)
    }

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()
}
