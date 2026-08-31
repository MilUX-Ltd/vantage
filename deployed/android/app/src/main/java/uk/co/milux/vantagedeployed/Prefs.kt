package uk.co.milux.vantagedeployed

import android.content.Context
import java.io.File

/**
 * What the app SHOWS, kept separate from what the device SYNCS. A device pulls every
 * deployment it is enrolled with (Bindings scope); the operator then picks ONE of those to
 * view, so the Vault Viewer, graph and capture show just that top-level folder. Sync is never
 * affected by the view choice. "*" means show all synced deployments.
 */
object Prefs {
    private const val FILE = "view"
    const val ALL = "*"

    private fun sp(c: Context) = c.getSharedPreferences(FILE, Context.MODE_PRIVATE)

    fun vault(c: Context): String = SyncEngine.vaultRoot(c).path

    /** The deployment labels this device is enrolled with (what it syncs). */
    fun enrolledLabels(c: Context): List<String> =
        Bindings.all(c).firstOrNull { it.status == "enrolled" }
            ?.deploymentScope?.split("|")?.map { it.trim() }?.filter { it.isNotEmpty() }
            ?: emptyList()

    /** The chosen view deployment LABEL, or ALL. Defaults to the first enrolled deployment,
     *  so a freshly-synced device opens focused on one operation rather than the whole vault. */
    fun viewLabel(c: Context): String {
        val stored = sp(c).getString("label", null)
        val enrolled = enrolledLabels(c)
        return when {
            stored == ALL -> ALL
            stored != null && stored in enrolled -> stored
            enrolled.isNotEmpty() -> enrolled.first()
            else -> ALL
        }
    }

    fun setViewLabel(c: Context, label: String) =
        sp(c).edit().putString("label", label).apply()

    /** The FOLDER the viewer and graph scope to: the chosen deployment's folder, or "" for the
     *  whole synced vault when ALL is chosen (or nothing is enrolled). */
    fun deployment(c: Context): String {
        val label = viewLabel(c)
        if (label == ALL) return ""
        return Deployment.folderFor(File(vault(c)), label) ?: label
    }
}
