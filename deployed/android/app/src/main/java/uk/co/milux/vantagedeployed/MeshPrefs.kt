package uk.co.milux.vantagedeployed

import android.content.Context

/** The mesh bearer's settings. Off by default: the bearer is a capability the operator turns
 *  on for a kit that has radios (ADR-001 decision 5). Port 256 is Meshtastic PRIVATE_APP. */
object MeshPrefs {
    private const val FILE = "mesh"
    const val DEFAULT_PORT = 256

    private fun sp(c: Context) = c.getSharedPreferences(FILE, Context.MODE_PRIVATE)

    fun enabled(c: Context): Boolean = sp(c).getBoolean("enabled", false)
    fun port(c: Context): Int = sp(c).getInt("port", DEFAULT_PORT)
    // Off by default: send local edits back over the mesh only when explicitly enabled, and
    // even then only while the phone is off WiFi, so it never competes with the IP sync.
    fun reverse(c: Context): Boolean = sp(c).getBoolean("reverse", false)

    fun set(c: Context, enabled: Boolean, port: Int, reverse: Boolean) =
        sp(c).edit().putBoolean("enabled", enabled).putInt("port", port)
            .putBoolean("reverse", reverse).apply()

    /** Broadcast actions for the configured port. 256 is PRIVATE_APP and broadcasts twice. */
    fun receiveActions(port: Int): List<String> =
        if (port == 256) listOf("com.geeksville.mesh.RECEIVED.PRIVATE_APP",
                                "com.geeksville.mesh.RECEIVED.256")
        else listOf("com.geeksville.mesh.RECEIVED.$port")
}
