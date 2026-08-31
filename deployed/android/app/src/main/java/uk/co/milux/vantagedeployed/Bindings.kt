package uk.co.milux.vantagedeployed

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject

/**
 * The boxes this phone is bound to (Spec 002). Each binding stands alone: its own URL, its own
 * channel pin, its own monotonic request counter. One or two bindings is the expected shape
 * (rear and kit); Forget removes the local half, the operator's revoke removes the box's half.
 * No addresses are hardcoded anywhere: every value arrives in a scanned or typed payload.
 */
data class Binding(
    val box: String,
    val url: String,
    val pin: String,
    var status: String,          // pending | enrolled | revoked
    var deploymentScope: String,
    var counter: Long,
)

object Bindings {
    private const val FILE = "bindings"
    private const val KEY = "list"

    private fun sp(c: Context) = c.getSharedPreferences(FILE, Context.MODE_PRIVATE)

    fun all(c: Context): List<Binding> {
        val arr = JSONArray(sp(c).getString(KEY, "[]") ?: "[]")
        return (0 until arr.length()).map { i ->
            val o = arr.getJSONObject(i)
            Binding(o.getString("box"), o.getString("url"), o.getString("pin"),
                    o.getString("status"), o.optString("deployment"), o.optLong("counter"))
        }
    }

    private fun save(c: Context, list: List<Binding>) {
        val arr = JSONArray()
        list.forEach { b ->
            arr.put(JSONObject().put("box", b.box).put("url", b.url).put("pin", b.pin)
                .put("status", b.status).put("deployment", b.deploymentScope)
                .put("counter", b.counter))
        }
        sp(c).edit().putString(KEY, arr.toString()).apply()
    }

    /** Add or replace the binding for a URL. Re-joining a box replaces the binding but the
     *  request counter carries forward: the box keeps its watermark across re-enrolments of
     *  the same key, so resetting here would replay-refuse every request after a re-join. */
    fun put(c: Context, b: Binding) {
        val old = all(c).firstOrNull { it.url == b.url }
        if (old != null && old.counter > b.counter) b.counter = old.counter
        save(c, all(c).filter { it.url != b.url } + b)
    }

    fun update(c: Context, b: Binding) = put(c, b)

    fun forget(c: Context, url: String) = save(c, all(c).filter { it.url != url })

    /** The next counter for a binding, persisted before use so a crash cannot reuse a value. */
    fun nextCounter(c: Context, url: String): Long {
        val list = all(c)
        val b = list.first { it.url == url }
        b.counter += 1
        save(c, list)
        return b.counter
    }
}
