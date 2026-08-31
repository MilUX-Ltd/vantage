// Ported from milux-vault-sync android/brain Deployment.kt at 42a89de (ADR-001); package renamed,
// vault rebased on the product's synced app-private store.
package uk.co.milux.vantagedeployed

import java.io.File

/**
 * One definition of a deployment, mirrored from `src/vaultsync/deployment.py` so the EUD and
 * the box always agree. A deployment IS its `operation:` label; a top-level folder is its HOME,
 * bound by the folder's index note (`type: deployment-index`). No index note: the folder name
 * stands in as the label. A label that differs from the folder name is legal and is reported,
 * never hidden.
 *
 * The app stores the FOLDER (stable on disk, used for file scoping and sync paths) and shows and
 * transmits the LABEL (what the graph, the boards and the box match on).
 */
object Deployment {
    data class Dep(val folder: String, val label: String, val hasIndex: Boolean, val mismatch: Boolean)

    private val SKIP = setOf(".obsidian", ".git", ".stversions", ".stfolder", ".trash")
    private val FM = Regex("^---\\s*\\n(.*?)\\n---\\s*\\n", RegexOption.DOT_MATCHES_ALL)
    private val WIKI = Regex("\\[\\[([^\\]|#]+)")

    private fun opLabel(raw: String): String {
        var v = raw.trim().trim('"', '\'')
        WIKI.find(v)?.let { v = it.groupValues[1] }
        return v.trim()
    }

    private fun frontmatter(text: String): Map<String, String> {
        val m = FM.find(text) ?: return emptyMap()
        val fm = HashMap<String, String>()
        for (line in m.groupValues[1].lines()) {
            val i = line.indexOf(':')
            if (i > 0) fm[line.substring(0, i).trim().lowercase()] = line.substring(i + 1).trim()
        }
        return fm
    }

    private fun indexLabel(folder: File): String? {
        val names = folder.listFiles()?.filter { it.isFile && it.name.endsWith(".md") }
            ?.sortedBy { it.name } ?: return null
        for (f in names) {
            val fm = runCatching { frontmatter(f.readText()) }.getOrNull() ?: continue
            if (fm["type"]?.trim()?.lowercase() == "deployment-index") {
                val label = opLabel(fm["operation"] ?: "")
                if (label.isNotEmpty()) return label
            }
        }
        return null
    }

    /** Every deployment in the vault, one entry per top-level folder, folder-name order. */
    fun resolve(vault: File): List<Dep> {
        val entries = vault.listFiles()?.filter {
            it.isDirectory && !it.name.startsWith(".") && it.name !in SKIP
        }?.sortedBy { it.name } ?: return emptyList()
        return entries.map { dir ->
            val label = indexLabel(dir)
            Dep(folder = dir.name, label = label ?: dir.name,
                hasIndex = label != null,
                mismatch = label != null && label != dir.name)
        }
    }

    /** The home folder for a label; a folder name is accepted directly. Null when unknown. */
    fun folderFor(vault: File, label: String): String? {
        if (label.isEmpty()) return null
        val deps = resolve(vault)
        deps.firstOrNull { it.label == label }?.let { return it.folder }
        deps.firstOrNull { it.folder == label }?.let { return it.folder }
        return null
    }

    /** The label of the deployment homed in `folder` (the folder name when nothing better). */
    fun labelFor(vault: File, folder: String): String =
        resolve(vault).firstOrNull { it.folder == folder }?.label ?: folder
}
