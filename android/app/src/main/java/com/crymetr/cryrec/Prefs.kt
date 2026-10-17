package com.crymetr.cryrec

import android.content.Context
import android.content.SharedPreferences

/** Settings. Same meaning as the desktop app's cryrec.json, but only the "My server" engines. */
class Prefs(context: Context) {
    private val sp: SharedPreferences = context.getSharedPreferences("cryrec", Context.MODE_PRIVATE)

    var serverUrl: String
        get() = sp.getString("server_url", "")!!.trim().trimEnd('/')
        set(v) = sp.edit().putString("server_url", v.trim()).apply()
    var serverKey: String
        get() = sp.getString("server_key", "")!!.trim()
        set(v) = sp.edit().putString("server_key", v.trim()).apply()
    var language: String
        get() = sp.getString("language", "Auto")!!
        set(v) = sp.edit().putString("language", v).apply()
    /** off | server_local | server_claude */
    var notesEngine: String
        get() = sp.getString("notes_engine", "server_local")!!
        set(v) = sp.edit().putString("notes_engine", v).apply()
    /** off | server_claude */
    var cleanEngine: String
        get() = sp.getString("clean_engine", "off")!!
        set(v) = sp.edit().putString("clean_engine", v).apply()
    var polish: Boolean
        get() = sp.getBoolean("polish", true)
        set(v) = sp.edit().putBoolean("polish", v).apply()
    var myName: String
        get() = sp.getString("my_name", "")!!
        set(v) = sp.edit().putString("my_name", v.trim()).apply()
    var vocabulary: String
        get() = sp.getString("vocabulary", "")!!
        set(v) = sp.edit().putString("vocabulary", v).apply()
    var bubbleRight: Boolean
        get() = sp.getBoolean("bubble_right", true)
        set(v) = sp.edit().putBoolean("bubble_right", v).apply()
    var updateApi: String
        get() = sp.getString("update_api", "")!!
        set(v) = sp.edit().putString("update_api", v).apply()
    var lastUpdateError: String
        get() = sp.getString("last_update_error", "")!!
        set(v) = sp.edit().putString("last_update_error", v).apply()
    var lastUpdateCheck: Long
        get() = sp.getLong("last_update_check", 0)
        set(v) = sp.edit().putLong("last_update_check", v).apply()

    val configured get() = serverUrl.isNotEmpty() && serverKey.isNotEmpty()

    companion object {
        val LANGUAGES = listOf("Auto", "Mixed TR/EN", "Turkish", "English")
        val NOTES_ENGINES = linkedMapOf(
            "off" to "Off", "server_local" to "My server (local AI)", "server_claude" to "My server (cloud AI)")
        val CLEAN_ENGINES = linkedMapOf("off" to "Off", "server_claude" to "My server (cloud AI)")
    }
}
